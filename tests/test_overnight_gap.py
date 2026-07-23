"""Part E-3: overnight_gap_scanner の US→JP 投射ロジック。"""
from __future__ import annotations

import importlib


def test_projection_positive_bias():
    m = importlib.import_module("overnight_gap_scanner")
    gaps = [{"us_ticker": "NVDA", "gap_pct": 0.05, "ah_volume": 500_000, "direction": "up"}]
    mappings = [{"us": "NVDA", "jp": "6857.T", "name_jp": "アドバンテスト",
                 "theme": "semiconductor_ai", "bias": "positive", "beta": 0.9}]
    signals = m._project_to_japan(gaps, mappings)
    assert len(signals) == 1
    s = signals[0]
    assert s["jp_ticker"] == "6857.T"
    assert s["recommended_action"] == "buy"
    assert abs(s["expected_jp_move"] - 0.05 * 0.9) < 1e-6


def test_projection_negative_bias_inverts():
    m = importlib.import_module("overnight_gap_scanner")
    gaps = [{"us_ticker": "X", "gap_pct": -0.05, "ah_volume": 200_000, "direction": "down"}]
    mappings = [{"us": "X", "jp": "Y.T", "bias": "negative", "beta": 0.5}]
    signals = m._project_to_japan(gaps, mappings)
    assert len(signals) == 1
    assert signals[0]["recommended_action"] == "buy"  # US -5% × -1 = +2.5%
