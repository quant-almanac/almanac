"""Part E-2: vol targeting scale の regime 判定。"""
from __future__ import annotations

import importlib
import pytest


@pytest.fixture(scope="function")
def re(monkeypatch):
    m = importlib.import_module("risk_engine")
    # 前回値の影響を排除（日次 ±15% クランプが混入しないように）
    monkeypatch.setattr(m, "_vt_load_prev", lambda: {"scale": 1.0})
    return m


def test_high_vol_scales_down(re):
    r = re.compute_vol_target_scale(predicted_annual_vol=0.30, target_annual_vol=0.15, persist=False)
    assert r["regime"] == "high_vol"
    assert 0.8 <= r["scale"] <= 0.9  # raw=0.85, prev=1.0 → clamp 0.85〜1.15 → 0.85


def test_low_vol_scales_up(re):
    r = re.compute_vol_target_scale(predicted_annual_vol=0.08, target_annual_vol=0.15, persist=False)
    assert r["regime"] == "low_vol"
    assert 1.05 <= r["scale"] <= 1.2


def test_neutral_scale_is_one(re):
    r = re.compute_vol_target_scale(predicted_annual_vol=0.15, target_annual_vol=0.15, persist=False)
    assert r["regime"] == "normal"
    assert 0.95 <= r["scale"] <= 1.05
