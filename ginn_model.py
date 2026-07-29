"""
GINN: GARCH-Informed Neural Network (ICAIF 2024)
GJR-GARCHの予測ボラティリティをLSTMの物理制約として使用し、
Cornish-Fisher VaR/CVaRの入力ボラティリティ精度を改善する。

アーキテクチャ:
    入力: [returns_t, σ_GARCH_t, VIX_t, regime_state_t] × 60日シーケンス
    モデル: 2層LSTM (hidden=64) + Linear出力
    損失:   MSE(σ_pred, |ε_t|) + λ・MSE(σ_pred, σ_GARCH)
    λ=0.3  (GARCHへの正則化強度)

使い方:
    python ginn_model.py --train         # 全保有銘柄で学習
    python ginn_model.py --train --ticker NVDA  # 単一銘柄
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import warnings
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

BASE_DIR = Path(__file__).parent
MODEL_DIR = Path(os.environ.get("ALMANAC_MODEL_DIR", BASE_DIR / "models"))
STATE_DIR = Path(os.environ.get("ALMANAC_STATE_DIR", BASE_DIR))
MODEL_PATH = MODEL_DIR / 'ginn_model.pt'
META_PATH = MODEL_DIR / 'ginn_meta.json'
BUNDLE_ROOT = MODEL_DIR / 'ginn'
CURRENT_POINTER_PATH = BUNDLE_ROOT / 'current.json'
SAFETY_LOG_PATH = STATE_DIR / 'logs' / 'ginn_safety_gate.jsonl'
sys.path.insert(0, str(BASE_DIR))

# モデルパスディレクトリ作成
MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)

# Stage 0A/4A: 中央安全ゲート (2026-07-27 インシデント対応)。
#
# 2026-07-26 18:30 に学習したモデルが n_test=0 (訓練外評価なし) のまま
# 06:24 の分析へ配線され、ボラティリティを GARCH 比 1.79〜3.00倍に
# 膨らませて tier プロンプトへ混入した (AVGO/XLF が誤って ready 判定)。
#
# forecast_ginn() は従来ファイルの存在しかチェックせず、ginn_meta.json の
# 中身 (n_test など) を一切見ていなかった。この関数がその穴を塞ぐ。
#
# Stage 4A: n_test=0 の根本原因 (test 区間だけで seq_len 日の入力コンテキストを
# 要求していたため、test 区間が短いと 0 件になっていた) を train_ginn() 側で
# 修正した上で、昇格基準を「訓練外評価が最低1件」から5つの数値基準へ拡張する。
# 閾値は実装前に固定した — 実際の manifest を見てから閾値を選ぶと、
# たまたま手元のモデルが通る値を選んでしまう危険があるため。
MIN_VALIDATION_SAMPLES = 30       # 統計的に意味を持たせる最低サンプル数
MIN_VALIDATION_TICKERS = 3        # 1銘柄だけの validation を「汎化した」と誤認しない
MAX_GARCH_RATIO_DEGRADATION = 1.5  # GINN の validation MSE が GARCH 基準の何倍まで許容か
MAX_DATA_AGE_DAYS = 10             # 週次 cron を1回逃しても許容する程度の余裕
MIN_FEATURE_COVERAGE = 0.90        # 学習対象銘柄群の平均データ充足率
WALK_FORWARD_TRAIN_FRACTIONS = (0.60, 0.70, 0.80)
WALK_FORWARD_VALIDATION_FRACTION = 0.10
VALIDATION_SCHEME = "rolling_origin_expanding_3fold_v1"
FEATURE_SCHEMA_VERSION = "ginn_v1_constant_aux"
FEATURE_TRANSFORMATION_VERSION = "ginn_normalization_v2_persisted_scaler"
SCALER_VERSION = "per_ticker_train_stats_v2"
SCALER_FILENAME = "scalers.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _git_revision() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=BASE_DIR,
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=5,
        ).strip() or None
    except Exception:
        return None


def _data_snapshot_hash(returns_df: pd.DataFrame) -> str:
    """Hash the actual chronological training frame, including index/NaNs."""
    try:
        values = pd.util.hash_pandas_object(returns_df, index=True).values.tobytes()
        columns = json.dumps(list(map(str, returns_df.columns))).encode("utf-8")
        return hashlib.sha256(columns + b"\x00" + values).hexdigest()
    except Exception:
        return "unavailable"


def _resolve_active_bundle() -> tuple[Path, Path, str | None, str | None]:
    """Return active model/manifest paths and pointer diagnostics.

    ``current.json`` is authoritative once present.  Legacy flat files remain
    readable only for backwards compatibility and are still subject to the
    same manifest promotion checks.
    """
    if not CURRENT_POINTER_PATH.exists():
        return MODEL_PATH, META_PATH, None, None
    try:
        pointer = json.loads(CURRENT_POINTER_PATH.read_text(encoding="utf-8"))
        version = str(pointer["version"])
        bundle_dir = BUNDLE_ROOT / version
        model_path = bundle_dir / "model.pt"
        manifest_path = bundle_dir / "manifest.json"
        expected_model_hash = pointer.get("model_sha256")
        expected_manifest_hash = pointer.get("manifest_sha256")
        if not model_path.is_file() or not manifest_path.is_file():
            return model_path, manifest_path, version, "active_bundle_incomplete"
        if expected_model_hash and _sha256_file(model_path) != expected_model_hash:
            return model_path, manifest_path, version, "active_bundle_checksum_mismatch"
        if not expected_manifest_hash:
            return model_path, manifest_path, version, "active_manifest_checksum_missing"
        if _sha256_file(manifest_path) != expected_manifest_hash:
            return model_path, manifest_path, version, "active_manifest_checksum_mismatch"
        return model_path, manifest_path, version, None
    except Exception:
        return MODEL_PATH, META_PATH, None, "current_pointer_invalid"


def _log_safety_gate_event(event: dict) -> None:
    """rejected legacy model のロード試行などを追記のみで記録する。"""
    try:
        SAFETY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with open(SAFETY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 監査ログの失敗でフォールバック経路自体は止めない


def _load_ginn_meta(path: Path | None = None) -> dict | None:
    path = path or META_PATH
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _meets_promotion_criteria(meta: dict) -> tuple[bool, str | None]:
    """Stage 4A: chronological holdout validation の暫定昇格基準。

    5つの数値基準をすべて満たさない限り昇格を拒否する (fail-closed)。
    「訓練外 test で最終性能を確認した」とは書かない — validation は
    昇格判定に使った時点で真の意味での test ではない (held-out ではあるが、
    このモデル選択プロセス自体に影響するため)。真に未観測のデータに対する
    真の rolling-origin walk-forward と、昇格後の未観測データに対する
    forward 評価は未実装。forward_observations/forward_metrics は予約済み
    だが Stage 4A 時点では常に空であり、最終性能の証明には使えない。
    """
    # 現行の train_ginn() は train 時の銘柄別 scaler 実値を bundle に保存せず、
    # forecast_ginn_result() は直近 inference window で統計を再 fit している。
    # version 文字列だけでは「学習と推論が同じ変換」を証明できないため、
    # scaler artifact と ticker routing が実装されるまで候補は昇格不能にする。
    if meta.get("inference_contract_complete") is not True:
        return False, "inference_contract_incomplete"
    if meta.get("validation_scheme") != VALIDATION_SCHEME:
        return False, "walk_forward_validation_missing"
    scaler = meta.get("scaler_artifact")
    if not isinstance(scaler, dict):
        return False, "scaler_artifact_missing"
    if (
        scaler.get("file") != SCALER_FILENAME
        or not isinstance(scaler.get("sha256"), str)
        or len(scaler.get("sha256") or "") != 64
    ):
        return False, "scaler_artifact_invalid"

    n_validation = meta.get("n_validation")
    if not isinstance(n_validation, int) or n_validation < MIN_VALIDATION_SAMPLES:
        return False, f"insufficient_validation_samples:{n_validation!r}<{MIN_VALIDATION_SAMPLES}"

    n_validation_tickers = meta.get("n_validation_tickers")
    if not isinstance(n_validation_tickers, int) or n_validation_tickers < MIN_VALIDATION_TICKERS:
        return False, f"insufficient_validation_tickers:{n_validation_tickers!r}<{MIN_VALIDATION_TICKERS}"

    vm = meta.get("validation_metrics")
    if not isinstance(vm, dict):
        return False, "missing_validation_metrics"
    validation_mse = vm.get("mse")
    garch_baseline_mse = vm.get("garch_baseline_mse")
    if (
        not isinstance(validation_mse, (int, float))
        or not isinstance(garch_baseline_mse, (int, float))
        or garch_baseline_mse <= 0
    ):
        return False, "incomplete_validation_metrics"
    ratio = validation_mse / garch_baseline_mse
    if ratio > MAX_GARCH_RATIO_DEGRADATION:
        return False, f"garch_ratio_degraded:{ratio:.2f}x>{MAX_GARCH_RATIO_DEGRADATION}x"

    feature_coverage = meta.get("feature_coverage")
    if not isinstance(feature_coverage, (int, float)) or feature_coverage < MIN_FEATURE_COVERAGE:
        return False, f"insufficient_feature_coverage:{feature_coverage!r}<{MIN_FEATURE_COVERAGE}"

    data_end = meta.get("data_end")
    if not data_end:
        return False, "missing_data_end"
    try:
        data_end_dt = datetime.strptime(str(data_end)[:10], "%Y-%m-%d")
    except ValueError:
        return False, "unparseable_data_end"
    age_days = (datetime.now() - data_end_dt).days
    if age_days > MAX_DATA_AGE_DAYS:
        return False, f"stale_data_end:{age_days}d>{MAX_DATA_AGE_DAYS}d"

    return True, None

# ============================================================
# PyTorch モデル定義
# ============================================================

def _get_torch():
    try:
        import torch
        import torch.nn as nn
        return torch, nn
    except ImportError:
        return None, None


class GINNModel:
    """
    GARCHインフォームドLSTMボラティリティ予測モデル。
    PyTorchが利用不可の場合はNoneを返す。
    """

    def __init__(self, input_size: int = 4, hidden_size: int = 64, num_layers: int = 2):
        torch, nn = _get_torch()
        if torch is None:
            self._model = None
            return

        class _LSTM(nn.Module):
            def __init__(self):
                super().__init__()
                self.lstm = nn.LSTM(input_size, hidden_size, num_layers,
                                    batch_first=True, dropout=0.1)
                self.fc = nn.Linear(hidden_size, 1)
                self.softplus = nn.Softplus()  # ボラは常に正

            def forward(self, x):
                out, _ = self.lstm(x)
                return self.softplus(self.fc(out[:, -1, :])).squeeze(-1)

        self._model = _LSTM()
        self._device = torch.device('mps' if torch.backends.mps.is_available() else 'cpu')
        self._model = self._model.to(self._device)

    @property
    def model(self):
        return self._model

    def is_available(self) -> bool:
        return self._model is not None


def _build_sequences(
    returns: pd.Series,
    garch_sigma: pd.Series,
    vix_series: pd.Series | None,
    regime_series: pd.Series | None,
    seq_len: int = 60,
    fit_stats: dict | None = None,
    min_target_pos: int | None = None,
) -> tuple:
    """
    LSTM入力シーケンスとターゲット（翌日絶対リターン）を構築。

    P2-8:
    - ターゲットを r.iloc[i+1] の絶対値に変更（docstring と一致）
    - ループ範囲を range(seq_len, len(feat) - 2) に調整（i+1 が有効）
    - 正規化統計（std, mean）は fit_stats に外部化 → train のみで fit して
      test に同じ統計を適用することで data leak を防ぐ
    - fit_stats=None の場合は入力 series で fit（後方互換、forecast 時用）

    Stage 4A: min_target_pos を指定すると、ターゲット (r.iloc[i+1]) が
    returns 内でその位置以降の点のみを採用する。入力ウィンドウ
    (feat.iloc[i-seq_len:i]) はそれより前 — 例えば train の末尾 — を
    跨いでよい。これにより「validation 区間が seq_len 日以下だと
    サンプルが 0 件になる」問題 (2026-07-27 の n_test=0 インシデントの
    構造的原因) を避けられる: validation のターゲットは常に
    min_target_pos 以降だけなので train 側のターゲットと重複しない
    (リーク無し)。呼び出し側が returns に train+validation の結合系列を
    渡し、min_target_pos に validation 開始位置を渡すことを想定する。

    入力特徴: [returns, σ_GARCH, VIX, regime_state]
    Returns:
        (X, y, stats) — stats は正規化統計（後段で test に再利用可）
    """
    r = returns.dropna()
    σ = garch_sigma.reindex(r.index).ffill().fillna(0.01)
    vix = vix_series.reindex(r.index).fillna(0.2) if vix_series is not None else pd.Series(0.2, index=r.index)
    reg = regime_series.reindex(r.index).fillna(1.0) if regime_series is not None else pd.Series(1.0, index=r.index)

    # 正規化統計: fit_stats があれば再利用、無ければ入力から fit
    if fit_stats is None:
        fit_stats = {
            'r_std':    float(r.std() + 1e-9),
            'sigma_mu': float(σ.mean() + 1e-9),
        }
    r_norm   = r / fit_stats['r_std']
    σ_norm   = σ / fit_stats['sigma_mu']
    vix_norm = vix / 30.0   # VIX 0-1スケール概算（定数スケール）
    reg_norm = reg / 3.0    # レジーム 0-1スケール（定数スケール）

    feat = pd.DataFrame({'r': r_norm, 'sigma': σ_norm, 'vix': vix_norm, 'regime': reg_norm})

    X, y = [], []
    # P2-8: i+1 が有効な範囲でループ（翌日のリアライズを target とする）
    # Stage 4A: min_target_pos があれば開始位置を後ろへずらす（ターゲットの
    # 最小位置は min_target_pos-1、つまり最初のターゲットが r.iloc[min_target_pos]）。
    start = seq_len if min_target_pos is None else max(seq_len, min_target_pos - 1)
    for i in range(start, len(feat) - 2):
        X.append(feat.iloc[i - seq_len:i].values)
        y.append(abs(float(r.iloc[i + 1])))   # 翌日の絶対リターン = 実現ボラ代理変数

    return np.array(X, dtype=np.float32), np.array(y, dtype=np.float32), fit_stats


def _daily_garch_sigma(
    returns: pd.Series,
    *,
    estimator,
) -> float:
    """Fit GARCH on the supplied history only and return daily sigma."""
    try:
        result = estimator(returns, use_ginn=False)
        annual = result.get(
            "forecast_vol", float(returns.std()) * np.sqrt(252)
        )
        value = float(annual) / np.sqrt(252)
    except Exception:
        value = float(returns.std())
    if not np.isfinite(value) or value <= 0:
        value = float(returns.std())
    return max(value, 1e-9)


def _dataset_for_boundary(
    *,
    returns_df: pd.DataFrame,
    tickers: list[str],
    seq_len: int,
    train_fraction: float,
    validation_fraction: float,
    estimator,
) -> dict:
    """Build one expanding-window train/validation fold.

    Every ticker fits its scaler and GARCH baseline only on data available at
    the fold's train boundary. Validation targets lie strictly after that
    boundary and use the train tail only as input context.
    """
    train_X: list[np.ndarray] = []
    train_y: list[np.ndarray] = []
    train_sigma: list[np.ndarray] = []
    validation_X: list[np.ndarray] = []
    validation_y: list[np.ndarray] = []
    validation_sigma: list[np.ndarray] = []
    validation_tickers: set[str] = set()
    scaler_by_ticker: dict[str, dict] = {}

    for ticker in tickers:
        if ticker not in returns_df.columns:
            continue
        r = returns_df[ticker].dropna()
        if len(r) < seq_len + 30:
            continue
        train_end = int(len(r) * train_fraction)
        validation_end = min(
            len(r),
            train_end + max(1, int(len(r) * validation_fraction)),
        )
        if train_end <= seq_len + 2 or validation_end <= train_end:
            continue
        r_train = r.iloc[:train_end]
        r_until_validation_end = r.iloc[:validation_end]
        sigma_value = _daily_garch_sigma(r_train, estimator=estimator)
        sigma_train = pd.Series(sigma_value, index=r_train.index)
        sigma_validation = pd.Series(
            sigma_value, index=r_until_validation_end.index
        )
        X_train, y_train, fit_stats = _build_sequences(
            r_train,
            sigma_train,
            None,
            None,
            seq_len=seq_len,
            fit_stats=None,
        )
        X_validation, y_validation, _ = _build_sequences(
            r_until_validation_end,
            sigma_validation,
            None,
            None,
            seq_len=seq_len,
            fit_stats=fit_stats,
            min_target_pos=train_end,
        )
        if len(X_train) == 0 or len(X_validation) == 0:
            continue
        train_X.append(X_train)
        train_y.append(y_train)
        train_sigma.append(
            np.full(len(y_train), sigma_value, dtype=np.float32)
        )
        validation_X.append(X_validation)
        validation_y.append(y_validation)
        validation_sigma.append(
            np.full(len(y_validation), sigma_value, dtype=np.float32)
        )
        validation_tickers.add(str(ticker))
        scaler_by_ticker[str(ticker).upper()] = {
            "r_std": float(fit_stats["r_std"]),
            "sigma_mu": float(fit_stats["sigma_mu"]),
        }

    return {
        "X_train": np.vstack(train_X) if train_X else np.empty((0, seq_len, 4), dtype=np.float32),
        "y_train": np.concatenate(train_y) if train_y else np.empty((0,), dtype=np.float32),
        "sigma_train": np.concatenate(train_sigma) if train_sigma else np.empty((0,), dtype=np.float32),
        "X_validation": (
            np.vstack(validation_X)
            if validation_X
            else np.empty((0, seq_len, 4), dtype=np.float32)
        ),
        "y_validation": (
            np.concatenate(validation_y)
            if validation_y
            else np.empty((0,), dtype=np.float32)
        ),
        "sigma_validation": (
            np.concatenate(validation_sigma)
            if validation_sigma
            else np.empty((0,), dtype=np.float32)
        ),
        "validation_tickers": validation_tickers,
        "scalers": scaler_by_ticker,
    }


def _train_model(
    *,
    X: np.ndarray,
    y: np.ndarray,
    sigma: np.ndarray,
    epochs: int,
    lr: float,
    garch_lambda: float,
    random_seed: int,
):
    """Train a fresh deterministic model for one fold or final bundle."""
    torch, _ = _get_torch()
    if torch is None:
        raise RuntimeError("torch_unavailable")
    np.random.seed(random_seed)
    torch.manual_seed(random_seed)
    obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
    if not obj.is_available():
        raise RuntimeError("model_unavailable")
    model = obj.model
    device = obj._device
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    X_tensor = torch.FloatTensor(X).to(device)
    y_tensor = torch.FloatTensor(y).to(device)
    sigma_tensor = torch.FloatTensor(sigma).to(device)
    final_loss = 0.0
    for _ in range(epochs):
        model.train()
        optimizer.zero_grad()
        prediction = model(X_tensor)
        realized_mse = torch.mean((prediction - y_tensor) ** 2)
        garch_mse = torch.mean((prediction - sigma_tensor) ** 2)
        loss = realized_mse + garch_lambda * garch_mse
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        final_loss = float(loss.item())
    return model, device, final_loss


def train_ginn(
    tickers: list[str] | None = None,
    lookback_days: int = 1260,
    seq_len: int = 60,
    epochs: int = 50,
    lr: float = 1e-3,
    garch_lambda: float = 0.3,
) -> dict:
    """
    保有銘柄のParquetデータでGINNを学習し、models/ginn_model.pt に保存。

    Args:
        tickers: 学習対象ティッカー（None=全保有銘柄）
        lookback_days: 学習期間（日数）
        seq_len: LSTMシーケンス長
        epochs: 学習エポック数
        lr: 学習率
        garch_lambda: GARCH制約の正則化強度λ

    Returns:
        {'success': bool, 'loss': 最終損失, 'n_samples': サンプル数}
    """
    torch, nn = _get_torch()
    if torch is None:
        return {'success': False, 'error': 'torch未インストール'}

    from portfolio_optimizer import _load_holdings_tickers, load_returns
    from risk_engine import estimate_gjr_garch

    random_seed = 42
    np.random.seed(random_seed)
    torch.manual_seed(random_seed)

    if tickers is None:
        tickers = _load_holdings_tickers()

    returns_df = load_returns(tickers, lookback_days=lookback_days)
    if returns_df.empty:
        return {'success': False, 'error': 'リターンデータ取得失敗'}

    # Rolling-origin walk-forward validation.  Each fold trains a fresh model
    # on an expanding prefix, then evaluates only the next chronological block.
    # The promotion metrics are fixed before looking at the candidate.
    fold_metrics: list[dict] = []
    validation_predictions: list[np.ndarray] = []
    validation_targets: list[np.ndarray] = []
    validation_garch: list[np.ndarray] = []
    validation_ticker_union: set[str] = set()
    for fold_index, train_fraction in enumerate(
        WALK_FORWARD_TRAIN_FRACTIONS, start=1
    ):
        fold = _dataset_for_boundary(
            returns_df=returns_df,
            tickers=list(map(str, tickers)),
            seq_len=seq_len,
            train_fraction=train_fraction,
            validation_fraction=WALK_FORWARD_VALIDATION_FRACTION,
            estimator=estimate_gjr_garch,
        )
        if len(fold["X_train"]) == 0 or len(fold["X_validation"]) == 0:
            continue
        fold_model, fold_device, fold_loss = _train_model(
            X=fold["X_train"],
            y=fold["y_train"],
            sigma=fold["sigma_train"],
            epochs=epochs,
            lr=lr,
            garch_lambda=garch_lambda,
            random_seed=random_seed + fold_index,
        )
        fold_model.eval()
        with torch.no_grad():
            prediction = (
                fold_model(
                    torch.FloatTensor(fold["X_validation"]).to(fold_device)
                )
                .cpu()
                .numpy()
            )
        validation_predictions.append(prediction)
        validation_targets.append(fold["y_validation"])
        validation_garch.append(fold["sigma_validation"])
        validation_ticker_union.update(fold["validation_tickers"])
        fold_mse = float(np.mean((prediction - fold["y_validation"]) ** 2))
        fold_garch_mse = float(
            np.mean((fold["sigma_validation"] - fold["y_validation"]) ** 2)
        )
        fold_metrics.append({
            "fold": fold_index,
            "train_fraction": train_fraction,
            "validation_fraction": WALK_FORWARD_VALIDATION_FRACTION,
            "n_train": int(len(fold["X_train"])),
            "n_validation": int(len(fold["X_validation"])),
            "validation_tickers": int(len(fold["validation_tickers"])),
            "train_loss": round(fold_loss, 8),
            "mse": round(fold_mse, 8),
            "garch_baseline_mse": round(fold_garch_mse, 8),
        })

    n_validation = sum(len(values) for values in validation_targets)
    n_validation_tickers = len(validation_ticker_union)
    validation_mse = (
        float(
            np.mean(
                (
                    np.concatenate(validation_predictions)
                    - np.concatenate(validation_targets)
                )
                ** 2
            )
        )
        if validation_targets
        else None
    )
    garch_baseline_mse = (
        float(
            np.mean(
                (
                    np.concatenate(validation_garch)
                    - np.concatenate(validation_targets)
                )
                ** 2
            )
        )
        if validation_targets
        else None
    )

    # Fit the deployable candidate on all currently available observations.
    # Its per-ticker scalers are persisted and become part of the inference
    # contract; forecast never re-fits them on the live window.
    final_X: list[np.ndarray] = []
    final_y: list[np.ndarray] = []
    final_sigma: list[np.ndarray] = []
    final_scalers: dict[str, dict] = {}
    coverages: list[float] = []
    data_end: pd.Timestamp | None = None
    for ticker in tickers:
        if ticker not in returns_df.columns:
            continue
        raw_col = returns_df[ticker]
        r = raw_col.dropna()
        if len(r) < seq_len + 30:
            continue

        coverages.append(len(r) / max(1, len(raw_col)))
        if data_end is None or r.index[-1] > data_end:
            data_end = r.index[-1]

        garch_sigma_val = _daily_garch_sigma(
            r, estimator=estimate_gjr_garch
        )
        sigma_series = pd.Series(garch_sigma_val, index=r.index)
        X_train, y_train, fit_stats = _build_sequences(
            r,
            sigma_series,
            None,
            None,
            seq_len=seq_len,
            fit_stats=None,
        )
        if len(X_train) == 0:
            continue
        final_X.append(X_train)
        final_y.append(y_train)
        final_sigma.append(
            np.full(len(y_train), garch_sigma_val, dtype=np.float32)
        )
        final_scalers[str(ticker).upper()] = {
            "r_std": float(fit_stats["r_std"]),
            "sigma_mu": float(fit_stats["sigma_mu"]),
        }

    if not final_X:
        return {'success': False, 'error': 'シーケンス構築失敗（データ不足）'}
    final_X_array = np.vstack(final_X)
    final_y_array = np.concatenate(final_y)
    final_sigma_array = np.concatenate(final_sigma)
    model, device, final_loss = _train_model(
        X=final_X_array,
        y=final_y_array,
        sigma=final_sigma_array,
        epochs=epochs,
        lr=lr,
        garch_lambda=garch_lambda,
        random_seed=random_seed,
    )
    n_train = len(final_X_array)
    print(
        f"  GINN walk-forward: train={n_train} validation={n_validation} "
        f"folds={len(fold_metrics)} epochs={epochs} device={device}"
    )
    if validation_mse is not None and garch_baseline_mse is not None:
        print(
            f"  Walk-forward MSE: {validation_mse:.6f} "
            f"(GARCH基準 {garch_baseline_mse:.6f}, "
            f"比={validation_mse / garch_baseline_mse:.2f}x)"
        )

    feature_coverage = round(sum(coverages) / len(coverages), 4) if coverages else 0.0

    trained_at = datetime.now(timezone.utc)
    version = trained_at.strftime("%Y%m%dT%H%M%S%fZ")
    candidate_dir = BUNDLE_ROOT / version
    candidate_dir.mkdir(parents=True, exist_ok=False)
    candidate_model_path = candidate_dir / "model.pt"
    candidate_scaler_path = candidate_dir / SCALER_FILENAME
    candidate_manifest_path = candidate_dir / "manifest.json"
    torch.save(model.state_dict(), candidate_model_path)
    model_sha256 = _sha256_file(candidate_model_path)
    _atomic_write_json(candidate_scaler_path, {
        "schema_version": 1,
        "scaler_version": SCALER_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_transformation_version": FEATURE_TRANSFORMATION_VERSION,
        "tickers": final_scalers,
        "auxiliary_features": {
            "vix": {"mode": "constant", "value": 0.2, "scale": 30.0},
            "regime": {"mode": "constant", "value": 1.0, "scale": 3.0},
        },
    })
    scaler_sha256 = _sha256_file(candidate_scaler_path)
    meta = {
        'schema_version':           2,
        'model_version':            version,
        'trained_at':               trained_at.isoformat(),
        'data_end':                data_end.strftime('%Y-%m-%d') if data_end is not None else None,
        'n_train':                  n_train,
        'n_samples':                n_train,
        'n_validation':             n_validation,
        'n_validation_tickers':     n_validation_tickers,
        'final_loss':               round(final_loss, 6),
        'validation_metrics': {
            'mse':                round(validation_mse, 6) if validation_mse is not None else None,
            'garch_baseline_mse': round(garch_baseline_mse, 6) if garch_baseline_mse is not None else None,
            'folds':              fold_metrics,
        } if validation_mse is not None else None,
        'feature_coverage':         feature_coverage,
        # Stage 4A時点では forward 評価パイプライン (真に未観測のデータでの継続測定) が
        # 未実装のため常に空。将来 recommendation_verifier.py 相当の仕組みで埋める。
        'forward_observations':     0,
        'forward_metrics':          None,
        'inference_contract_complete': True,
        'promotion_policy_version': '4A_walk_forward_persisted_scaler_v2',
        'validation_scheme':         VALIDATION_SCHEME,
        'feature_schema_version':    FEATURE_SCHEMA_VERSION,
        'feature_transformation_version': FEATURE_TRANSFORMATION_VERSION,
        'scaler_version':            SCALER_VERSION,
        'scaler_artifact': {
            'file': SCALER_FILENAME,
            'sha256': scaler_sha256,
            'ticker_count': len(final_scalers),
        },
        'model_sha256':              model_sha256,
        'random_seed':               random_seed,
        'code_revision':             _git_revision(),
        'data_snapshot_hash':        _data_snapshot_hash(returns_df),
        'ticker_universe':           list(map(str, tickers)),
        'dependency_versions': {
            'numpy': np.__version__,
            'pandas': pd.__version__,
            'torch': getattr(torch, '__version__', None),
        },
        'tickers':      tickers[:10],
        'epochs':       epochs,
        'seq_len':      seq_len,
        'garch_lambda': garch_lambda,
        'split':        'rolling_origin_expanding_3fold_then_full_refit',
    }
    promoted, promotion_reason = _meets_promotion_criteria(meta)
    meta['promotion'] = {
        'eligible': promoted,
        'reason': promotion_reason,
        'evaluated_at': datetime.now(timezone.utc).isoformat(),
    }
    _atomic_write_json(candidate_manifest_path, meta)

    if promoted:
        _atomic_write_json(CURRENT_POINTER_PATH, {
            'schema_version': 1,
            'version': version,
            'model_sha256': model_sha256,
            'manifest_sha256': _sha256_file(candidate_manifest_path),
            'scaler_sha256': scaler_sha256,
            'promoted_at': datetime.now(timezone.utc).isoformat(),
            'promotion_policy_version': meta['promotion_policy_version'],
        })
        print(f"  GINN候補を昇格: loss={final_loss:.6f} -> {candidate_dir}")
    else:
        print(f"  GINN候補は未昇格: {promotion_reason} -> {candidate_dir}")
    return {
        'success': True,
        'promoted': promoted,
        'promotion_reason': promotion_reason,
        'model_version': version,
        'candidate_dir': str(candidate_dir),
        'loss': final_loss,
        'n_samples': n_train,
        'n_validation': n_validation,
        'validation_mse': validation_mse,
    }


def _load_inference_scaler(
    *,
    manifest: dict,
    manifest_path: Path,
    ticker: str,
) -> tuple[dict | None, str | None]:
    artifact = manifest.get("scaler_artifact")
    if not isinstance(artifact, dict):
        return None, "scaler_artifact_missing"
    filename = str(artifact.get("file") or "")
    if filename != SCALER_FILENAME:
        return None, "scaler_artifact_invalid"
    path = manifest_path.parent / filename
    if not path.is_file():
        return None, "scaler_file_missing"
    expected = str(artifact.get("sha256") or "")
    if not expected or _sha256_file(path) != expected:
        return None, "scaler_checksum_mismatch"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, "scaler_payload_invalid"
    if (
        not isinstance(payload, dict)
        or payload.get("scaler_version") != SCALER_VERSION
        or payload.get("feature_schema_version") != FEATURE_SCHEMA_VERSION
        or payload.get("feature_transformation_version")
        != FEATURE_TRANSFORMATION_VERSION
    ):
        return None, "scaler_schema_mismatch"
    rows = payload.get("tickers")
    row = rows.get(ticker.upper()) if isinstance(rows, dict) else None
    if not isinstance(row, dict):
        return None, f"ticker_scaler_missing:{ticker.upper()}"
    try:
        r_std = float(row["r_std"])
        sigma_mu = float(row["sigma_mu"])
    except (KeyError, TypeError, ValueError):
        return None, f"ticker_scaler_invalid:{ticker.upper()}"
    if not np.isfinite(r_std) or not np.isfinite(sigma_mu) or r_std <= 0 or sigma_mu <= 0:
        return None, f"ticker_scaler_invalid:{ticker.upper()}"
    return {"r_std": r_std, "sigma_mu": sigma_mu}, None


def forecast_ginn_result(
    returns: pd.Series,
    garch_sigma: float,
    seq_len: int = 60,
    ticker: str | None = None,
) -> dict:
    """
    GINNで翌日の予測ボラティリティを返す（年率換算）。モデル境界の中央安全ゲート。

    manifest (ginn_meta.json) の無いモデル、または事前固定した validation
    昇格基準を満たさないモデルは default-deny で GARCH へフォールバック
    する。ALMANAC_DISABLE_GINN=1 は基準を満たしたモデルがあっても常に
    GARCH へ落とす一方向の kill switch。

    Args:
        returns: 直近リターン系列（60日以上）
        garch_sigma: GJR-GARCH予測σ（年率）。フォールバック値兼物理制約
        seq_len: シーケンス長

    Returns:
        {
          "forecast_vol":     float — 年率換算ボラティリティ予測値
          "used_model":       "ginn" | "gjr_garch"
          "fallback_reason":  str | None — used_model=="gjr_garch" の理由
          "model_version":    str | None — 使用した GINN モデルの trained_at
        }
    """
    def _fallback(reason: str) -> dict:
        return {
            "forecast_vol": garch_sigma,
            "used_model": "gjr_garch",
            "fallback_reason": reason,
            "model_version": None,
        }

    if os.environ.get("ALMANAC_DISABLE_GINN", "").strip().lower() in ("1", "true", "yes"):
        return _fallback("disabled_by_env")

    active_model_path, active_manifest_path, active_version, pointer_error = _resolve_active_bundle()
    if pointer_error:
        _log_safety_gate_event({
            "event": "rejected_active_bundle",
            "reason": pointer_error,
            "current_pointer_path": str(CURRENT_POINTER_PATH),
            "model_path": str(active_model_path),
        })
        return _fallback(pointer_error)

    if not active_model_path.exists():
        return _fallback("model_file_missing")

    meta = _load_ginn_meta(active_manifest_path)
    if meta is None:
        # manifest 無しの legacy model は default-deny。ファイルは削除しない
        # (隔離であって破棄ではない) が、ロードせず監査ログへ記録する。
        _log_safety_gate_event({
            "event": "rejected_legacy_model_load",
            "reason": "manifest_missing",
            "model_path": str(active_model_path),
        })
        return _fallback("manifest_missing")

    ok, reason = _meets_promotion_criteria(meta)
    if not ok:
        _log_safety_gate_event({
            "event": "rejected_legacy_model_load",
            "reason": reason,
            "model_path": str(active_model_path),
            "meta": meta,
        })
        return _fallback(reason)

    routed_ticker = str(
        ticker
        or returns.attrs.get("canonical_instrument_id")
        or returns.attrs.get("ticker")
        or ""
    ).strip().upper()
    if not routed_ticker:
        return _fallback("ticker_routing_missing")
    fit_stats, scaler_error = _load_inference_scaler(
        manifest=meta,
        manifest_path=active_manifest_path,
        ticker=routed_ticker,
    )
    if scaler_error:
        return _fallback(scaler_error)

    torch, nn = _get_torch()
    if torch is None:
        return _fallback("torch_unavailable")

    try:
        ginn_obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
        if not ginn_obj.is_available():
            return _fallback("model_unavailable")

        model = ginn_obj.model
        device = ginn_obj._device
        expected_hash = meta.get("model_sha256")
        if expected_hash and _sha256_file(active_model_path) != expected_hash:
            return _fallback("manifest_model_checksum_mismatch")
        model.load_state_dict(torch.load(active_model_path, map_location=device, weights_only=True))
        model.eval()

        r = returns.dropna().tail(seq_len + 10)
        if len(r) < seq_len:
            return _fallback("insufficient_returns_history")

        # 日次σ（年率→日次変換）
        garch_sigma_daily = garch_sigma / np.sqrt(252)
        garch_s = pd.Series(garch_sigma_daily, index=r.index)

        X, _, _ = _build_sequences(
            r,
            garch_s,
            None,
            None,
            seq_len=seq_len,
            fit_stats=fit_stats,
        )
        if len(X) == 0:
            return _fallback("empty_input_sequence")

        x_tensor = torch.FloatTensor(X[-1:]).to(device)

        with torch.no_grad():
            pred_daily = float(model(x_tensor).item())

        # 日次→年率換算
        pred_annual = pred_daily * np.sqrt(252)

        # 外れ値チェック: GARCHの0.3倍〜3倍の範囲に制限
        pred_annual = max(garch_sigma * 0.3, min(garch_sigma * 3.0, pred_annual))

        return {
            "forecast_vol": round(pred_annual, 4),
            "used_model": "ginn",
            "fallback_reason": None,
            "model_version": active_version or meta.get("model_version") or meta.get("trained_at"),
            "ticker": routed_ticker,
        }

    except Exception as e:
        print(f"  GINN予測失敗（フォールバック）: {e}")
        return _fallback(f"prediction_error:{type(e).__name__}")


def forecast_ginn(
    returns: pd.Series,
    garch_sigma: float,
    seq_len: int = 60,
    ticker: str | None = None,
) -> float:
    """互換 wrapper。既存の呼び出し元・CLI が float を期待するため残す。

    新規コードは forecast_ginn_result() を使い、used_model で GARCH
    フォールバックか GINN かを区別すること (この wrapper では区別できない)。
    """
    return forecast_ginn_result(
        returns, garch_sigma, seq_len, ticker=ticker
    )["forecast_vol"]


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='GINN: GARCH-Informed Neural Network')
    parser.add_argument('--train', action='store_true', help='モデルを学習する')
    parser.add_argument('--ticker', default=None, help='単一銘柄で学習（例: NVDA）')
    parser.add_argument('--epochs', type=int, default=50)
    parser.add_argument('--lambda-garch', type=float, default=0.3, dest='garch_lambda')
    args = parser.parse_args()

    if args.train:
        tickers = [args.ticker] if args.ticker else None
        result = train_ginn(tickers=tickers, epochs=args.epochs, garch_lambda=args.garch_lambda)
        if result.get('success'):
            print(f'\n学習完了: samples={result["n_samples"]}, loss={result["loss"]:.6f}')
        else:
            print(f'\n学習失敗: {result.get("error")}')
            sys.exit(1)
    else:
        # フォールバックテスト
        print('GINNモデルのフォールバックテスト...')
        test_returns = pd.Series(np.random.randn(100) * 0.01)
        σ = forecast_ginn(test_returns, garch_sigma=0.25)
        print(f'予測σ(年率): {σ:.4f}  (モデル{"あり" if MODEL_PATH.exists() else "なし -> GARCH値"})')
