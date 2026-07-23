from datetime import datetime

import pandas as pd

import chart_analyzer


def _frame(index, closes):
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [value + 1 for value in closes],
            "Low": [value - 1 for value in closes],
            "Close": closes,
            "Volume": [1000] * len(closes),
        },
        index=pd.DatetimeIndex(index),
    )


def test_newer_intraday_bar_replaces_lagged_daily_close(monkeypatch) -> None:
    daily = _frame([datetime(2026, 7, 15)], [425.6])
    intraday = _frame(
        [datetime(2026, 7, 16, 15, 15), datetime(2026, 7, 16, 15, 20)],
        [420.0, 419.6],
    )
    monkeypatch.setattr(chart_analyzer, "_load_daily_ohlcv", lambda ticker: daily)
    monkeypatch.setattr(chart_analyzer, "_load_or_fetch_intraday", lambda ticker: intraday)
    monkeypatch.setattr(chart_analyzer, "_bid_ask_snapshot", lambda ticker: None)

    result = chart_analyzer.gather_one("1306.T", intraday=True)

    assert result["last_close"] == 419.6
    assert result["price_source"] == "intraday_last_bar"
    assert result["freshness"] == "last_session"
    assert result["data_as_of"].startswith("2026-07-16T15:20:00")


def test_quote_snapshot_is_not_mislabeled_as_intraday_history(monkeypatch) -> None:
    daily = _frame([datetime(2026, 7, 16)], [419.7])
    monkeypatch.setattr(chart_analyzer, "_load_daily_ohlcv", lambda ticker: daily)
    monkeypatch.setattr(chart_analyzer, "_load_or_fetch_intraday", lambda ticker: None)
    monkeypatch.setattr(
        chart_analyzer,
        "_bid_ask_snapshot",
        lambda ticker: {"last": 420.0, "bid": 419.9, "ask": 420.1, "ts": "2026-07-17T09:10:00"},
    )

    result = chart_analyzer.gather_one("1306.T", intraday=True)

    assert result["freshness"] == "quote_snapshot"
    assert result["price_source"] == "daily_close"
