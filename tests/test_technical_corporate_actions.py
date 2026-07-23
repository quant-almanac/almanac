from __future__ import annotations

import numpy as np
import pandas as pd

import technical_signals as ts


def _frame(close: list[float]) -> pd.DataFrame:
    index = pd.bdate_range("2025-09-01", periods=len(close))
    return pd.DataFrame({
        "Close": close,
        "Open": close,
        "High": [value * 1.01 for value in close],
        "Low": [value * 0.99 for value in close],
        "Volume": [1_000_000.0] * len(close),
    }, index=index)


def test_unadjusted_split_invalidates_long_window_technicals(monkeypatch):
    monkeypatch.setattr(ts, "_session_lag", lambda *args, **kwargs: 0)
    monkeypatch.setattr(ts, "_last_completed_session", lambda *args, **kwargs: pd.Timestamp("2026-07-21").date())
    close = list(np.linspace(3600, 3827, 120)) + list(np.linspace(376.4, 418.3, 100))

    result = ts._analyze_ticker("1306.T", _frame(close))

    assert result is not None
    assert result["data_quality_status"] == "blocked"
    assert result["ma200_diff_pct"] is None
    assert result["composite_score"] is None
    reason = result["data_quality_reasons"][0]
    assert reason["code"] == "unadjusted_price_discontinuity"
    assert reason["daily_change_pct"] < -80


def test_normal_price_history_keeps_technicals_available(monkeypatch):
    monkeypatch.setattr(ts, "_session_lag", lambda *args, **kwargs: 0)
    monkeypatch.setattr(ts, "_last_completed_session", lambda *args, **kwargs: pd.Timestamp("2026-07-21").date())
    close = list(np.linspace(350, 418.3, 220))

    result = ts._analyze_ticker("1306.T", _frame(close))

    assert result is not None
    assert result["data_quality_status"] == "ok"
    assert result["ma200_diff_pct"] is not None
    assert result["composite_score"] is not None
