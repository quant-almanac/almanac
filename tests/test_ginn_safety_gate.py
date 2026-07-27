"""Stage 0A: GINN 中央安全ゲート。

インシデント: 2026-07-26 18:30 に学習したモデルが n_test=0 (訓練外評価なし)
のまま 2026-07-27 06:24 の分析へ配線され、ボラティリティを GARCH 比
1.79〜3.00倍に膨らませて tier プロンプトへ混入した (AVGO/XLF が誤って
execution_readiness="ready" と判定された一因)。

forecast_ginn() は従来ファイルの存在しかチェックせず、ginn_meta.json の
中身 (n_test など) を一切見ていなかった。forecast_ginn_result() がこの
穴を、消費側ではなくモデル境界そのもので塞ぐ。
"""
from __future__ import annotations

import json
import sys
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


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gm, "MODEL_PATH", tmp_path / "ginn_model.pt")
    monkeypatch.setattr(gm, "META_PATH", tmp_path / "ginn_meta.json")
    monkeypatch.setattr(gm, "SAFETY_LOG_PATH", tmp_path / "ginn_safety_gate.jsonl")
    monkeypatch.delenv("ALMANAC_DISABLE_GINN", raising=False)
    return tmp_path


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


# ---------------------------------------------------------------------------
# 本件そのもの: n_test=0 のモデルは拒否される
# ---------------------------------------------------------------------------


def test_unvalidated_n_test_zero_model_is_rejected(tmp_path):
    """2026-07-27 インシデントの再現: n_test=0 のモデルは使われない。"""
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text(json.dumps({
        "trained_at": "2026-07-26T18:30:24.704269",
        "n_samples": 3408, "n_test": 0, "final_loss": 0.000375,
        "test_mse": None, "epochs": 50, "seq_len": 60, "garch_lambda": 0.3,
    }), encoding="utf-8")

    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.4798)
    assert r["used_model"] == "gjr_garch"
    assert r["forecast_vol"] == 0.4798  # GARCH 値がそのまま。膨張しない
    assert r["fallback_reason"] == "unvalidated_n_test=0"


def test_rejection_is_logged_for_audit(tmp_path):
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text(json.dumps({"n_test": 0, "trained_at": "x"}), encoding="utf-8")

    gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)

    assert gm.SAFETY_LOG_PATH.exists()
    lines = gm.SAFETY_LOG_PATH.read_text(encoding="utf-8").strip().split("\n")
    event = json.loads(lines[-1])
    assert event["event"] == "rejected_legacy_model_load"
    assert event["reason"] == "unvalidated_n_test=0"
    assert "ts" in event


def test_corrupt_manifest_is_treated_as_missing(tmp_path):
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text("{not valid json", encoding="utf-8")

    r = gm.forecast_ginn_result(_synthetic_returns(), garch_sigma=0.5)
    assert r["used_model"] == "gjr_garch"
    assert r["fallback_reason"] == "manifest_missing"


# ---------------------------------------------------------------------------
# kill switch: ALMANAC_DISABLE_GINN は一方向 (manifest を満たしていても勝つ)
# ---------------------------------------------------------------------------


def test_disable_env_wins_even_with_valid_manifest(tmp_path, monkeypatch):
    gm.MODEL_PATH.write_bytes(b"placeholder")
    gm.META_PATH.write_text(json.dumps({"n_test": 999, "trained_at": "x"}), encoding="utf-8")
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
    """n_test > 0 なら GINN 予測が呼ばれ、model_version が meta から反映される。"""
    pytest.importorskip("torch")
    import torch

    from ginn_model import GINNModel

    obj = GINNModel(input_size=4, hidden_size=64, num_layers=2)
    if not obj.is_available():
        pytest.skip("torch model not constructible in this environment")
    torch.save(obj.model.state_dict(), gm.MODEL_PATH)
    gm.META_PATH.write_text(json.dumps({
        "trained_at": "2026-01-01T00:00:00", "n_test": 42,
    }), encoding="utf-8")

    r = gm.forecast_ginn_result(_synthetic_returns(n=300), garch_sigma=0.3)
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

    def _fake_gjr_fallback(returns, garch_sigma, seq_len=60):
        return {"forecast_vol": garch_sigma, "used_model": "gjr_garch",
                "fallback_reason": "unvalidated_n_test=0", "model_version": None}

    monkeypatch.setattr("ginn_model.forecast_ginn_result", _fake_gjr_fallback)

    r = risk_engine.estimate_gjr_garch(_synthetic_returns(n=300), use_ginn=True)
    assert r.get("model") != "GINN+GJR-GARCH"
    assert r.get("model") == "GJR-GARCH(1,1)-skewt"


def test_risk_engine_label_is_ginn_when_actually_used(monkeypatch):
    import risk_engine

    def _fake_gjr_success(returns, garch_sigma, seq_len=60):
        return {"forecast_vol": garch_sigma * 1.1, "used_model": "ginn",
                "fallback_reason": None, "model_version": "2026-01-01T00:00:00"}

    monkeypatch.setattr("ginn_model.forecast_ginn_result", _fake_gjr_success)

    r = risk_engine.estimate_gjr_garch(_synthetic_returns(n=300), use_ginn=True)
    assert r.get("model") == "GINN+GJR-GARCH"
