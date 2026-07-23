from __future__ import annotations

import pandas as pd

from ingest_disclosures import resolve_scan_universe
from sync_jp_universe_prices import sync_prices


def _frame(dates: list[str], values: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": values,
            "High": [v + 1.0 for v in values],
            "Low": [v - 1.0 for v in values],
            "Close": values,
            "Volume": [1000] * len(values),
        },
        index=pd.to_datetime(dates),
    )


def test_sync_is_network_gated(tmp_path):
    called = []
    result = sync_prices(
        ["1377.T"],
        output_dir=tmp_path,
        live=False,
    )
    assert result["status"] == "skipped"
    assert called == []


def test_sync_appends_and_deduplicates_parquet(tmp_path):
    calls = []

    def first_fetch(ticker, start):
        calls.append((ticker, start))
        return _frame(["2026-06-10", "2026-06-11"], [100.0, 101.0])

    first = sync_prices(
        ["1377.T"],
        output_dir=tmp_path,
        fetch=first_fetch,
    )
    assert first["updated"] == 1
    assert calls == [("1377.T", None)]

    def second_fetch(ticker, start):
        assert start == "2026-06-12"
        return _frame(["2026-06-11", "2026-06-12"], [999.0, 102.0])

    second = sync_prices(
        ["1377.T"],
        output_dir=tmp_path,
        fetch=second_fetch,
    )
    assert second["updated"] == 1
    stored = pd.read_parquet(tmp_path / "1377.T.parquet")
    assert list(stored.index.strftime("%Y-%m-%d")) == [
        "2026-06-10",
        "2026-06-11",
        "2026-06-12",
    ]
    assert float(stored.loc["2026-06-11", "Close"]) == 999.0


def test_sync_rejects_incomplete_ohlcv_without_writing(tmp_path):
    def close_only_fetch(ticker, start):
        return pd.DataFrame(
            {"Close": [100.0, 101.0]},
            index=pd.to_datetime(["2026-06-10", "2026-06-11"]),
        )

    result = sync_prices(["1377.T"], output_dir=tmp_path, fetch=close_only_fetch)

    assert result["updated"] == 0
    assert result["failed"] == [{
        "ticker": "1377.T",
        "error": "invalid_ohlcv:missing columns: Open, High, Low, Volume",
    }]
    assert not (tmp_path / "1377.T.parquet").exists()


def test_sync_runs_price_sanity_gate_before_writing(tmp_path, monkeypatch):
    seen = []

    def fake_detect(ticker, frame):
        seen.append((ticker, list(frame.index.strftime("%Y-%m-%d"))))
        return [{"flag_id": "f1", "ticker": ticker}]

    def fake_append(flags, *, path=None):
        seen.append(("append", flags, path))
        return len(flags)

    monkeypatch.setattr("sync_jp_universe_prices.detect_price_sanity_flags", fake_detect)
    monkeypatch.setattr("sync_jp_universe_prices.append_price_sanity_flags", fake_append)

    result = sync_prices(
        ["1377.T"],
        output_dir=tmp_path,
        fetch=lambda ticker, start: _frame(
            ["2026-06-10", "2026-06-11"],
            [100.0, 140.0],
        ),
    )

    assert result["updated"] == 1
    assert seen == [
        ("1377.T", ["2026-06-10", "2026-06-11"]),
        ("append", [{"flag_id": "f1", "ticker": "1377.T"}], tmp_path / "price_sanity_flags.jsonl"),
    ]
    assert (tmp_path / "1377.T.parquet").exists()


def test_default_output_dir_writes_price_sanity_log_next_to_ohlcv(tmp_path, monkeypatch):
    output_dir = tmp_path / "data" / "ohlcv"
    seen = []

    monkeypatch.setattr(
        "sync_jp_universe_prices.detect_price_sanity_flags",
        lambda ticker, frame: [{"flag_id": "f1", "ticker": ticker}],
    )
    monkeypatch.setattr(
        "sync_jp_universe_prices.append_price_sanity_flags",
        lambda flags, *, path=None: seen.append(path) or len(flags),
    )

    result = sync_prices(
        ["1377.T"],
        output_dir=output_dir,
        fetch=lambda ticker, start: _frame(
            ["2026-06-10", "2026-06-11"],
            [100.0, 140.0],
        ),
    )

    assert result["updated"] == 1
    assert seen == [tmp_path / "data" / "price_sanity_flags.jsonl"]


def test_default_jp_universe_is_fixed_and_restricted_free():
    tickers = resolve_scan_universe(market="JP")
    assert len(tickers) == 50
    assert "9999.T" not in tickers
