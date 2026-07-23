"""Part E-4: pair_screener の z-score divergence 判定。"""
from __future__ import annotations

import importlib
import numpy as np
import pandas as pd


def test_low_correlation_skipped():
    m = importlib.import_module("pair_screener")
    np.random.seed(0)
    idx = pd.date_range("2025-01-01", periods=150, freq="B")
    a = pd.Series(np.random.randn(150).cumsum() + 100, index=idx, name="A")
    b = pd.Series(np.random.randn(150).cumsum() + 100, index=idx, name="B")
    close = pd.DataFrame({"A": a, "B": b})
    r = m._evaluate_pair("A", "B", close)
    # 独立乱数は corr ≈ 0 → None 返却
    assert r is None


def test_high_correlation_no_divergence_returns_none():
    m = importlib.import_module("pair_screener")
    np.random.seed(1)
    idx = pd.date_range("2025-01-01", periods=150, freq="B")
    base = np.random.randn(150).cumsum()
    close = pd.DataFrame({"A": base + 100, "B": base + 100}, index=idx)
    r = m._evaluate_pair("A", "B", close)
    # 完全一致 → spread=0 → z ほぼ 0 → None
    assert r is None
