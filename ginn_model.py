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
import json
import os
import sys
import warnings
from pathlib import Path
from datetime import datetime, timezone

import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')

BASE_DIR = Path(__file__).parent
MODEL_PATH = BASE_DIR / 'models' / 'ginn_model.pt'
META_PATH = BASE_DIR / 'models' / 'ginn_meta.json'
SAFETY_LOG_PATH = BASE_DIR / 'logs' / 'ginn_safety_gate.jsonl'
sys.path.insert(0, str(BASE_DIR))

# モデルパスディレクトリ作成
MODEL_PATH.parent.mkdir(exist_ok=True)

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


def _log_safety_gate_event(event: dict) -> None:
    """rejected legacy model のロード試行などを追記のみで記録する。"""
    try:
        SAFETY_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        event = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with open(SAFETY_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception:
        pass  # 監査ログの失敗でフォールバック経路自体は止めない


def _load_ginn_meta() -> dict | None:
    if not META_PATH.exists():
        return None
    try:
        data = json.loads(META_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def _meets_promotion_criteria(meta: dict) -> tuple[bool, str | None]:
    """Stage 4A: walk-forward validation の正式な昇格基準。

    5つの数値基準をすべて満たさない限り昇格を拒否する (fail-closed)。
    「訓練外 test で最終性能を確認した」とは書かない — validation は
    昇格判定に使った時点で真の意味での test ではない (held-out ではあるが、
    このモデル選択プロセス自体に影響するため)。真に未観測のデータに対する
    forward 評価は別途 forward_observations/forward_metrics で追跡する
    (Stage 4A時点では計測パイプライン未実装のため常に空)。
    """
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

    if tickers is None:
        tickers = _load_holdings_tickers()

    returns_df = load_returns(tickers, lookback_days=lookback_days)
    if returns_df.empty:
        return {'success': False, 'error': 'リターンデータ取得失敗'}

    ginn_obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
    if not ginn_obj.is_available():
        return {'success': False, 'error': 'PyTorchモデル初期化失敗'}

    model = ginn_obj.model
    device = ginn_obj._device
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)

    # P2-8: 時系列 train/validation split（先頭 80% train / 末尾 20% validation, shuffle=False 強制）
    # 銘柄ごとに chronological に分け、結合する。
    #
    # Stage 4A: validation のシーケンス構築は r_test 単独ではなく「train+validation の
    # 結合系列 (=r 全体)」に対して行い、min_target_pos=split_idx で
    # 「ターゲットは validation 区間以降のみ」を強制する。入力ウィンドウは
    # train の末尾へ跨ってよい (2026-07-27 インシデントの根本原因である
    # 「validation 区間が seq_len 日以下だとサンプル 0 件になる」問題を解消)。
    all_X_tr, all_y_tr, all_σ_tr = [], [], []
    all_X_va, all_y_va, all_σ_va = [], [], []
    n_validation_tickers = 0
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

        # P2-8: train/validation を r の chronological 境界で分ける
        split_idx = int(len(r) * 0.8)
        r_train = r.iloc[:split_idx]

        # GJR-GARCH σ は train のみで推定 (validation 区間の ground truth を汚染しない)
        try:
            garch_res = estimate_gjr_garch(r_train, use_ginn=False)
            garch_sigma_val = garch_res.get('forecast_vol', r_train.std() * np.sqrt(252)) / np.sqrt(252)
        except Exception:
            garch_sigma_val = float(r_train.std())
        garch_sigma_train = pd.Series(garch_sigma_val, index=r_train.index)
        garch_sigma_full  = pd.Series(garch_sigma_val, index=r.index)

        # train で fit_stats を確定、validation は同じ stats を再利用（data leak 防止）
        X_tr, y_tr, fit_stats = _build_sequences(
            r_train, garch_sigma_train, None, None, seq_len=seq_len, fit_stats=None,
        )
        X_va, y_va, _ = _build_sequences(
            r, garch_sigma_full, None, None, seq_len=seq_len, fit_stats=fit_stats,
            min_target_pos=split_idx,
        )
        if len(X_tr) == 0:
            continue

        all_X_tr.append(X_tr)
        all_y_tr.append(y_tr)
        all_σ_tr.append(np.full(len(y_tr), float(garch_sigma_val), dtype=np.float32))

        if len(X_va) > 0:
            all_X_va.append(X_va)
            all_y_va.append(y_va)
            all_σ_va.append(np.full(len(y_va), float(garch_sigma_val), dtype=np.float32))
            n_validation_tickers += 1

    if not all_X_tr:
        return {'success': False, 'error': 'シーケンス構築失敗（データ不足）'}

    X_train = torch.FloatTensor(np.vstack(all_X_tr)).to(device)
    y_train = torch.FloatTensor(np.concatenate(all_y_tr)).to(device)
    σ_train = torch.FloatTensor(np.concatenate(all_σ_tr)).to(device)

    has_validation = bool(all_X_va)
    if has_validation:
        X_validation = torch.FloatTensor(np.vstack(all_X_va)).to(device)
        y_validation = torch.FloatTensor(np.concatenate(all_y_va)).to(device)
        σ_validation_np = np.concatenate(all_σ_va)

    n_train = len(X_train)
    n_validation = len(X_validation) if has_validation else 0
    print(f"  GINN学習開始: train={n_train} validation={n_validation} サンプル, {epochs}エポック, device={device}")

    model.train()
    final_loss = 0.0
    validation_mse = None
    garch_baseline_mse = None

    for epoch in range(epochs):
        model.train()
        optimizer.zero_grad()
        pred = model(X_train)

        # GINN損失: MSE(pred, |ε_t|) + λ・MSE(pred, σ_GARCH)
        mse_realized = torch.mean((pred - y_train) ** 2)
        mse_garch    = torch.mean((pred - σ_train) ** 2)
        loss         = mse_realized + garch_lambda * mse_garch

        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()

        final_loss = float(loss.item())
        if (epoch + 1) % 10 == 0:
            print(f"    Epoch {epoch+1}/{epochs}: train_loss={final_loss:.6f}")

    # P2-8/4A: train 終了後に validation MSE を測定（過学習検知）。
    # 比較対象として GARCH 定数予測の MSE も同じターゲットで測る
    # (_meets_promotion_criteria の GARCH 比許容悪化率チェックに使う)。
    if has_validation:
        model.eval()
        with torch.no_grad():
            validation_mse = float(torch.mean((model(X_validation) - y_validation) ** 2).item())
        y_validation_np = y_validation.cpu().numpy() if hasattr(y_validation, 'cpu') else np.asarray(y_validation)
        garch_baseline_mse = float(np.mean((σ_validation_np - y_validation_np) ** 2))
        print(
            f"  Validation MSE: {validation_mse:.6f} (GARCH基準 {garch_baseline_mse:.6f}, "
            f"比={validation_mse / garch_baseline_mse:.2f}x)"
        )

    feature_coverage = round(sum(coverages) / len(coverages), 4) if coverages else 0.0

    # 保存
    torch.save(model.state_dict(), MODEL_PATH)
    meta = {
        'trained_at':              datetime.now().isoformat(),
        'data_end':                data_end.strftime('%Y-%m-%d') if data_end is not None else None,
        'n_samples':                n_train,
        'n_validation':             n_validation,
        'n_validation_tickers':     n_validation_tickers,
        'final_loss':               round(final_loss, 6),
        'validation_metrics': {
            'mse':                round(validation_mse, 6) if validation_mse is not None else None,
            'garch_baseline_mse': round(garch_baseline_mse, 6) if garch_baseline_mse is not None else None,
        } if has_validation else None,
        'feature_coverage':         feature_coverage,
        # Stage 4A時点では forward 評価パイプライン (真に未観測のデータでの継続測定) が
        # 未実装のため常に空。将来 recommendation_verifier.py 相当の仕組みで埋める。
        'forward_observations':     0,
        'forward_metrics':          None,
        'promotion_policy_version': '4A_v1',
        'tickers':      tickers[:10],
        'epochs':       epochs,
        'seq_len':      seq_len,
        'garch_lambda': garch_lambda,
        'split':        'chronological_80_20_shuffle_False_with_train_tail_context',
    }
    with open(MODEL_PATH.parent / 'ginn_meta.json', 'w') as f:
        json.dump(meta, f, indent=2)

    print(f"  GINN学習完了: loss={final_loss:.6f} -> {MODEL_PATH}")
    return {'success': True, 'loss': final_loss, 'n_samples': n_train, 'validation_mse': validation_mse}


def forecast_ginn_result(
    returns: pd.Series,
    garch_sigma: float,
    seq_len: int = 60,
) -> dict:
    """
    GINNで翌日の予測ボラティリティを返す（年率換算）。モデル境界の中央安全ゲート。

    manifest (ginn_meta.json) の無いモデル、または昇格基準 (現状は
    n_test > 0) を満たさないモデルは default-deny で GARCH へフォールバック
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

    if not MODEL_PATH.exists():
        return _fallback("model_file_missing")

    meta = _load_ginn_meta()
    if meta is None:
        # manifest 無しの legacy model は default-deny。ファイルは削除しない
        # (隔離であって破棄ではない) が、ロードせず監査ログへ記録する。
        _log_safety_gate_event({
            "event": "rejected_legacy_model_load",
            "reason": "manifest_missing",
            "model_path": str(MODEL_PATH),
        })
        return _fallback("manifest_missing")

    ok, reason = _meets_promotion_criteria(meta)
    if not ok:
        _log_safety_gate_event({
            "event": "rejected_legacy_model_load",
            "reason": reason,
            "model_path": str(MODEL_PATH),
            "meta": meta,
        })
        return _fallback(reason)

    torch, nn = _get_torch()
    if torch is None:
        return _fallback("torch_unavailable")

    try:
        ginn_obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
        if not ginn_obj.is_available():
            return _fallback("model_unavailable")

        model = ginn_obj.model
        device = ginn_obj._device
        model.load_state_dict(torch.load(MODEL_PATH, map_location=device, weights_only=True))
        model.eval()

        r = returns.dropna().tail(seq_len + 10)
        if len(r) < seq_len:
            return _fallback("insufficient_returns_history")

        # 日次σ（年率→日次変換）
        garch_sigma_daily = garch_sigma / np.sqrt(252)
        garch_s = pd.Series(garch_sigma_daily, index=r.index)

        X, _, _ = _build_sequences(r, garch_s, None, None, seq_len=seq_len)
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
            "model_version": meta.get("trained_at"),
        }

    except Exception as e:
        print(f"  GINN予測失敗（フォールバック）: {e}")
        return _fallback(f"prediction_error:{type(e).__name__}")


def forecast_ginn(
    returns: pd.Series,
    garch_sigma: float,
    seq_len: int = 60,
) -> float:
    """互換 wrapper。既存の呼び出し元・CLI が float を期待するため残す。

    新規コードは forecast_ginn_result() を使い、used_model で GARCH
    フォールバックか GINN かを区別すること (この wrapper では区別できない)。
    """
    return forecast_ginn_result(returns, garch_sigma, seq_len)["forecast_vol"]


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
