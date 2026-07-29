"""Stage 0A/4A: GINN 中央安全ゲート + chronological holdout validation。

インシデント: 2026-07-26 18:30 に学習したモデルが n_test=0 (訓練外評価なし)
のまま 2026-07-27 06:24 の分析へ配線され、ボラティリティを GARCH 比
1.79〜3.00倍に膨らませて tier プロンプトへ混入した (AVGO/XLF が誤って
execution_readiness="ready" と判定された一因)。

forecast_ginn() は従来ファイルの存在しかチェックせず、ginn_meta.json の
中身 (n_test など) を一切見ていなかった。forecast_ginn_result() がこの
穴を、消費側ではなくモデル境界そのもので塞ぐ (Stage 0A)。

Stage 4A はこの n_test=0 の構造的原因 (validation 区間だけで seq_len 日の
入力コンテキストを要求していたため、区間が短いと 0 件になっていた) を
train_ginn() 側で修正し、昇格基準を「訓練外評価が最低1件」から
5つの数値基準 (validation サンプル数・銘柄数・GARCH比・特徴量カバレッジ・
データ鮮度) へ拡張する。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import ginn_model as gm  # noqa: E402


def _synthetic_returns(n=200, seed=0) -> pd.Series:
    np.random.seed(seed)
    return pd.Series(np.random.normal(0, 0.01, n), index=pd.date_range("2024-01-01", periods=n))


def test_subprocess_honors_model_and_state_directories(tmp_path):
    model_dir = tmp_path / "models"
    state_dir = tmp_path / "state"
    env = {
        **os.environ,
        "ALMANAC_MODEL_DIR": str(model_dir),
        "ALMANAC_STATE_DIR": str(state_dir),
    }
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, ginn_model as g;"
                "print(json.dumps({'model':str(g.MODEL_PATH),"
                "'safety':str(g.SAFETY_LOG_PATH)}))"
            ),
        ],
        cwd=_REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    paths = json.loads(proc.stdout.strip())
    assert paths["model"].startswith(str(model_dir))
    assert paths["safety"].startswith(str(state_dir))


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PATH", tmp_path / "ginn_model.pt")
    monkeypatch.setattr(gm, "META_PATH", tmp_path / "ginn_meta.json")
    monkeypatch.setattr(gm, "BUNDLE_ROOT", tmp_path / "ginn")
    monkeypatch.setattr(gm, "CURRENT_POINTER_PATH", tmp_path / "ginn" / "current.json")
    monkeypatch.setattr(gm, "SAFETY_LOG_PATH", tmp_path / "ginn_safety_gate.jsonl")
    monkeypatch.delenv("ALMANAC_DISABLE_GINN", raising=False)
    return tmp_path


def _passing_meta(**overrides) -> dict:
    """_meets_promotion_criteria の5基準すべてを満たす meta。個別テストは
    overrides で1項目だけ壊して該当チェックのみを検証する。"""
    base = {
        "trained_at": datetime.now().isoformat(),
        "data_end": datetime.now().strftime("%Y-%m-%d"),
        "n_validation": 100,
        "n_validation_tickers": 5,
        "validation_metrics": {"mse": 0.01, "garch_baseline_mse": 0.01},
        "feature_coverage": 0.98,
        "inference_contract_complete": True,
        "validation_scheme": gm.VALIDATION_SCHEME,
        "scaler_artifact": {
            "file": gm.SCALER_FILENAME,
            "sha256": "a" * 64,
            "ticker_count": 1,
        },
    }
    base.update(overrides)
    return base


def _write_inference_scaler(tmp_path: Path, *, ticker: str = "TEST") -> dict:
    payload = {
        "schema_version": 1,
        "scaler_version": gm.SCALER_VERSION,
        "feature_schema_version": gm.FEATURE_SCHEMA_VERSION,
        "feature_transformation_version": gm.FEATURE_TRANSFORMATION_VERSION,
        "tickers": {
            ticker: {"r_std": 0.01, "sigma_mu": 0.02},
        },
    }
    path = tmp_path / gm.SCALER_FILENAME
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        "file": gm.SCALER_FILENAME,
        "sha256": gm._sha256_file(path),
        "ticker_count": 1,
    }


# ---------------------------------------------------------------------------
# 欠落系: モデルファイル無し / manifest 無し
# ---------------------------------------------------------------------------


def test_no_model_file_falls_back_to_garch():
    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["forecast_vol"] == 0.5
    assert r["fallback_reason"] == "model_file_missing"
    assert r["model_version"] is None


def test_model_file_without_manifest_is_default_denied(tmp_path):
    """manifest 無しの legacy model を default-deny する。ファイルは削除しない。"""
    gm.MODEL_PATH.write_bytes(b"not a real torch checkpoint")
    assert gm.MODEL_PATH.exists()  # 隔離であって破棄ではない

    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["fallback_reason"] == "manifest_missing"
    # ファイルは消えていない
    assert gm.MODEL_PATH.exists()


def test_corrupt_manifest_is_treated_as_missing(tmp_path):
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text("{not valid json", encoding="utf-8")

    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["fallback_reason"] == "manifest_missing"


def test_incomplete_active_bundle_never_falls_back_to_legacy(tmp_path):
    gm.MODEL_PATH.write_bytes(b"legacy")
    gm.META_PATH.write_text(json.dumps(_passing_meta()), encoding="utf-8")
    gm.CURRENT_POINTER_PATH.parent.mkdir(parents=True)
    gm.CURRENT_POINTER_PATH.write_text(
        json.dumps({"version": "candidate-missing", "model_sha256": "abc"}),
        encoding="utf-8",
    )
    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["fallback_reason"] == "active_bundle_incomplete"


def test_active_bundle_checksum_mismatch_is_rejected(tmp_path):
    bundle = gm.BUNDLE_ROOT / "v1"
    bundle.mkdir(parents=True)
    (bundle / "model.pt").write_bytes(b"tampered")
    (bundle / "manifest.json").write_text(json.dumps(_passing_meta()), encoding="utf-8")
    gm.CURRENT_POINTER_PATH.write_text(
        json.dumps({"version": "v1", "model_sha256": "0" * 64}),
        encoding="utf-8",
    )
    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["fallback_reason"] == "active_bundle_checksum_mismatch"


def test_active_bundle_manifest_checksum_mismatch_is_rejected(tmp_path):
    bundle = gm.BUNDLE_ROOT / "v1"
    bundle.mkdir(parents=True)
    model_path = bundle / "model.pt"
    manifest_path = bundle / "manifest.json"
    model_path.write_bytes(b"model")
    manifest_path.write_text(json.dumps(_passing_meta()), encoding="utf-8")
    gm.CURRENT_POINTER_PATH.write_text(
        json.dumps({
            "version": "v1",
            "model_sha256": gm._sha256_file(model_path),
            "manifest_sha256": "0" * 64,
        }),
        encoding="utf-8",
    )

    result = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert result["used_model"] == "gjr_garch"
    assert result["fallback_reason"] == "active_manifest_checksum_mismatch"


def test_inference_contract_must_be_complete_for_promotion():
    ok, reason = gm._meets_promotion_criteria(
        _passing_meta(inference_contract_complete=False)
    )
    assert ok is False
    assert reason == "inference_contract_incomplete"


# ---------------------------------------------------------------------------
# 本件そのもの: n_validation=0 のモデルは拒否される
# ---------------------------------------------------------------------------


def test_unvalidated_n_validation_zero_model_is_rejected(tmp_path):
    """2026-07-27 インシデントの再現: validation サンプル無しのモデルは使われない。"""
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text(json.dumps(_passing_meta(n_validation=0)), encoding="utf-8")

    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.4798)
    assert r["used_model"] == "gjr_garch"
    assert r["forecast_vol"] == 0.4798  # GARCH 値がそのまま。膨張しない
    assert r["fallback_reason"] == f"insufficient_validation_samples:0<{gm.MIN_VALIDATION_SAMPLES}"


def test_rejection_is_logged_for_audit(tmp_path):
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text(json.dumps(_passing_meta(n_validation=0)), encoding="utf-8")

    gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)

    assert gm.SAFETY_LOG_PATH.exists()
    lines = gm.SAFETY_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
    event = json.loads(lines[-1])
    assert event["event"] == "rejected_legacy_model_load"
    assert event["reason"].startswith("insufficient_validation_samples")
    assert "ts" in event


# ---------------------------------------------------------------------------
# Stage 4A: 5つの昇格基準を個別に検証
# ---------------------------------------------------------------------------


def test_promotion_rejects_too_few_validation_tickers():
    ok, reason = gm._meets_promotion_criteria(_passing_meta(n_validation_tickers=1))
    assert ok is False
    assert reason.startswith("insufficient_validation_tickers")


def test_promotion_rejects_missing_validation_metrics():
    ok, reason = gm._meets_promotion_criteria(_passing_meta(validation_metrics=None))
    assert ok is False
    assert reason == "missing_validation_metrics"


def test_promotion_rejects_garch_ratio_degraded():
    """GINN の validation MSE が GARCH 基準の許容倍率を超えたら拒否する
    (2026-07-27 インシデントで実際に GARCH 比 1.79〜3.00倍まで膨らんでいた
    のと同種の異常を、昇格前に検知する)。"""
    meta = _passing_meta(validation_metrics={"mse": 0.05, "garch_baseline_mse": 0.01})  # 5倍
    ok, reason = gm._meets_promotion_criteria(meta)
    assert ok is False
    assert "garch_ratio_degraded" in reason


def test_promotion_accepts_garch_ratio_within_threshold():
    meta = _passing_meta(validation_metrics={"mse": 0.012, "garch_baseline_mse": 0.01})  # 1.2倍
    ok, reason = gm._meets_promotion_criteria(meta)
    assert ok is True
    assert reason is None


def test_promotion_rejects_low_feature_coverage():
    ok, reason = gm._meets_promotion_criteria(_passing_meta(feature_coverage=0.5))
    assert ok is False
    assert reason.startswith("insufficient_feature_coverage")


def test_promotion_rejects_missing_data_end():
    ok, reason = gm._meets_promotion_criteria(_passing_meta(data_end=None))
    assert ok is False
    assert reason == "missing_data_end"


def test_promotion_rejects_stale_data_end():
    """鮮度は trained_at でなく data_end で判断する。trained_at が今日でも
    data_end が古ければ拒否する。"""
    stale = (datetime.now() - timedelta(days=gm.MAX_DATA_AGE_DAYS + 5)).strftime("%Y-%m-%d")
    meta = _passing_meta(data_end=stale, trained_at=datetime.now().isoformat())
    ok, reason = gm._meets_promotion_criteria(meta)
    assert ok is False
    assert reason.startswith("stale_data_end")


def test_promotion_accepts_fully_passing_meta():
    ok, reason = gm._meets_promotion_criteria(_passing_meta())
    assert ok is True
    assert reason is None


# ---------------------------------------------------------------------------
# kill switch: ALMANAC_DISABLE_GINN は一方向 (manifest を満たしていても勝つ)
# ---------------------------------------------------------------------------


def test_disable_env_wins_even_with_valid_manifest(tmp_path, monkeypatch):
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text(json.dumps(_passing_meta()), encoding="utf-8")
    monkeypatch.setenv("ALMANAC_DISABLE_GINN", "1")

    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["fallback_reason"] == "disabled_by_env"


@pytest.mark.parametrize("value", ["0", "false", "", "no"])
def test_disable_env_only_triggers_on_truthy_values(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ALMANAC_DISABLE_GINN", value)
    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    # モデル自体が無いので model_file_missing になるはずで、disabled_by_env にはならない
    assert r["fallback_reason"] != "disabled_by_env"


# ---------------------------------------------------------------------------
# 正の経路: 検証済みモデルは実際に使われる
# ---------------------------------------------------------------------------


def test_validated_model_is_actually_used_when_torch_and_predict_succeed(tmp_path, monkeypatch):
    """5基準すべてを満たすと GINN 予測が呼ばれ、model_version が meta から反映される。"""
    pytest.importorskip("torch")
    import torch

    from ginn_model import GINNModel

    obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
    if not obj.is_available():
        pytest.skip("torch model not constructible in this environment")
    torch.save(obj.model.state_dict(), gm.MODEL_PATH)
    scaler_artifact = _write_inference_scaler(tmp_path)
    gm.META_PATH.write_text(json.dumps(_passing_meta(
        trained_at="2026-01-01T00:00:00",
        scaler_artifact=scaler_artifact,
    )), encoding="utf-8")

    r = gm.forecast_ginn_result(
        _synthetic_returns(n=300), garch_sigma=0.3, ticker="TEST"
    )
    assert r["used_model"] == "ginn"
    assert r["model_version"] == "2026-01-01T00:00:00"
    assert r["fallback_reason"] is None
    # 外れ値クランプ: GARCH の 0.3〜3.0 倍の範囲内 (round(...,4) と浮動小数の
    # 丸め誤差を吸収する小さな許容誤差を入れる)
    assert 0.3 * 0.3 - 1e-4 <= r["forecast_vol"] <= 0.3 * 3.0 + 1e-4


# ---------------------------------------------------------------------------
# 互換 wrapper
# ---------------------------------------------------------------------------


def test_compat_wrapper_returns_float_matching_result_dict():
    result = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    value = gm.forecast_ginn(_synthetic_returns(), garch_sigma=0.5)
    assert isinstance(value, float)
    assert value == result["forecast_vol"]


# ---------------------------------------------------------------------------
# risk_engine 統合: ラベルが used_model に従う (混入経路そのものの回帰テスト)
# ---------------------------------------------------------------------------


def test_risk_engine_label_reflects_actual_fallback(monkeypatch):
    """risk_engine.py:449 が無条件で 'GINN+GJR-GARCH' を表示していた欠陥の回帰。"""
    import risk_engine

    def _fake_gjr_fallback(returns, garch_sigma, seq_len=60, ticker=None):
        return {"forecast_vol": garch_sigma, "used_model": "gjr_garch",
                "fallback_reason": "insufficient_validation_samples:0<30", "model_version": None}

    monkeypatch.setattr("ginn_model.forecast_ginn_result", _fake_gjr_fallback)

    r = risk_engine.estimate_gjr_garch(_synthetic_returns(n=300), use_ginn=True)
    assert r.get("model") != "GINN+GJR-GARCH"
    assert r.get("model") == "GJR-GARCH(1,1)-skewt"


def test_risk_engine_label_is_ginn_when_actually_used(monkeypatch):
    import risk_engine

    def _fake_gjr_success(returns, garch_sigma, seq_len=60, ticker=None):
        return {"forecast_vol": garch_sigma * 1.1, "used_model": "ginn",
                "fallback_reason": None, "model_version": "2026-01-01T00:00:00"}

    monkeypatch.setattr("ginn_model.forecast_ginn_result", _fake_gjr_success)

    r = risk_engine.estimate_gjr_garch(_synthetic_returns(n=300), use_ginn=True)
    assert r.get("model") == "GINN+GJR-GARCH"


# ---------------------------------------------------------------------------
# Stage 4A: _build_sequences の min_target_pos (validation コンテキスト共有)
# ---------------------------------------------------------------------------


def test_build_sequences_without_min_target_pos_needs_seq_len_plus_2_days():
    """既存動作の確認: min_target_pos 無しだと seq_len+2 日未満の系列からは
    サンプルが1件も取れない (これが実際の n_test=0 インシデントの直接原因)。"""
    short = _synthetic_returns(n=50, seed=1)  # < 60+2
    sigma = pd.Series(0.01, index=short.index)
    X, y, _ = gm._build_sequences(short, sigma, None, None, seq_len=60)
    assert len(X) == 0


def test_build_sequences_min_target_pos_reclaims_short_validation_windows():
    """本題: train+validation を結合した系列を渡し min_target_pos で
    validation 開始位置を指定すれば、validation 区間自体が seq_len 日未満でも
    train 側の末尾を入力コンテキストとしてサンプルを構築できる。"""
    full = _synthetic_returns(n=140, seed=2)
    split_idx = 120  # validation 区間はたった 20 日 (seq_len=60 未満)
    sigma = pd.Series(0.01, index=full.index)

    # 従来方式: validation 区間だけを渡す → 0件
    X_old, y_old, _ = gm._build_sequences(full.iloc[split_idx:], sigma.iloc[split_idx:], None, None, seq_len=60)
    assert len(X_old) == 0

    # Stage 4A 方式: 結合系列 + min_target_pos → validation 区間の日数だけ得られる
    X_new, y_new, _ = gm._build_sequences(full, sigma, None, None, seq_len=60, min_target_pos=split_idx)
    assert len(X_new) > 0
    assert len(X_new) == len(full) - 2 - (split_idx - 1)


def test_build_sequences_min_target_pos_never_targets_before_boundary():
    """min_target_pos を指定したとき、ターゲット (y に対応する r.iloc[i+1]) が
    境界より前を指すサンプルが1件も無いこと (train 側との重複防止)。"""
    full = _synthetic_returns(n=200, seed=3)
    split_idx = 150
    sigma = pd.Series(0.01, index=full.index)
    r = full.dropna()

    X, y, _ = gm._build_sequences(full, sigma, None, None, seq_len=60, min_target_pos=split_idx)
    # 最初のサンプルのターゲットが r.iloc[split_idx] と一致することを確認
    assert len(X) > 0
    first_target = abs(float(r.iloc[split_idx]))
    assert y[0] == pytest.approx(first_target)


# ---------------------------------------------------------------------------
# Stage 4A: train_ginn() の end-to-end 回帰テスト (実際のインシデント再現)
# ---------------------------------------------------------------------------


def test_train_ginn_produces_nonzero_validation_with_short_lookback(tmp_path, monkeypatch):
    """本件の直接再現: lookback が短く validation 区間単独では
    seq_len+2日に満たないケースで、旧コードなら n_test=0 になっていたが
    新コードでは n_validation > 0 の候補 bundle が作られる。短い学習で
    基準未達なら current pointer は進めず、既存 active を破壊しない。"""
    pytest.importorskip("torch")
    from ginn_model import GINNModel

    if not GINNModel(input_size=4, hidden_size=64, num_layers=2).is_available():
        pytest.skip("torch model not constructible in this environment")

    n_days = 140  # 80/20 split で validation ≈ 28日 (seq_len=60 未満)
    tickers = [f"SYN{i}" for i in range(5)]
    idx = pd.date_range("2026-01-01", periods=n_days, freq="B")
    rng = np.random.default_rng(42)
    returns_df = pd.DataFrame(
        {t: rng.normal(0, 0.01, n_days) for t in tickers}, index=idx,
    )

    monkeypatch.setattr("portfolio_optimizer.load_returns", lambda tickers, lookback_days: returns_df)
    monkeypatch.setattr(
        "risk_engine.estimate_gjr_garch",
        lambda r, use_ginn=False: {"forecast_vol": float(r.std()) * np.sqrt(252)},
    )

    result = gm.train_ginn(tickers=tickers, lookback_days=n_days, seq_len=60, epochs=2)

    assert result["success"] is True
    candidate_dir = Path(result["candidate_dir"])
    meta = json.loads((candidate_dir / "manifest.json").read_text(encoding="utf-8"))
    assert (candidate_dir / "model.pt").is_file()
    assert meta["n_validation"] > 0  # 旧コードなら 0 になっていたはず
    assert meta["n_validation_tickers"] == 5
    assert meta["data_end"] == idx[-1].strftime("%Y-%m-%d")
    assert meta["validation_metrics"]["mse"] is not None
    assert meta["validation_metrics"]["garch_baseline_mse"] is not None
    assert meta["feature_coverage"] == 1.0  # 合成データに欠損なし
    assert meta["forward_observations"] == 0
    assert meta["inference_contract_complete"] is True
    assert meta["validation_scheme"] == gm.VALIDATION_SCHEME
    assert meta["promotion_policy_version"] == "4A_walk_forward_persisted_scaler_v2"
    assert (candidate_dir / gm.SCALER_FILENAME).is_file()
    assert (
        meta["scaler_artifact"]["sha256"]
        == gm._sha256_file(candidate_dir / gm.SCALER_FILENAME)
    )
    assert meta["model_sha256"] == gm._sha256_file(candidate_dir / "model.pt")
    assert result["promoted"] is False
    assert result["promotion_reason"].startswith("garch_ratio_degraded")
    assert not gm.CURRENT_POINTER_PATH.exists()
