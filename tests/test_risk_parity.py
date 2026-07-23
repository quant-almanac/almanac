"""Part E-8: risk parity tier weights (inv-vol + clamp)。"""
from __future__ import annotations

import pytest
from portfolio_optimizer import compute_risk_parity_weights


def test_weights_sum_to_one():
    r = compute_risk_parity_weights(tier_vols={"long": 0.12, "medium": 0.20, "swing": 0.35})
    total = sum(r["clamped_weights"].values())
    # 4-digit round tolerance + 3 tier 合計の累積誤差
    assert abs(total - 1.0) < 1e-3


def test_low_vol_gets_larger_weight():
    r = compute_risk_parity_weights(tier_vols={"long": 0.10, "medium": 0.20, "swing": 0.40})
    w = r["clamped_weights"]
    assert w["long"] > w["medium"] > w["swing"]


def test_clamp_bounds_respected():
    # 極端な inv-vol → 上下限でクランプ
    r = compute_risk_parity_weights(tier_vols={"long": 0.01, "medium": 1.0, "swing": 1.0})
    w = r["clamped_weights"]
    assert w["long"] <= 0.70 + 1e-6
    assert w["medium"] >= 0.20 - 1e-6
    assert w["swing"] >= 0.05 - 1e-6
