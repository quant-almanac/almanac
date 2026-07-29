from __future__ import annotations

import json

import short_screener as ss
from utils import configure_yfinance_cache


def test_load_scan_tickers_uses_broad_us_and_curated_jp(tmp_path, monkeypatch):
    (tmp_path / "tickers.json").write_text(json.dumps({
        "all": ["AAPL", "MSFT", "NEW_US", "7203.T", "6758.T"],
        "short_scan_tickers": ["AAPL", "HIGH_RISK", "7203.T"],
    }), encoding="utf-8")
    monkeypatch.setattr(ss, "BASE_DIR", tmp_path)

    assert ss._load_scan_tickers() == ["AAPL", "MSFT", "NEW_US", "7203.T"]


def test_bulk_download_retries_partial_batches(monkeypatch):
    monkeypatch.setattr(ss, "DOWNLOAD_BATCH_SIZE", 3)
    monkeypatch.setattr(ss, "DOWNLOAD_RETRY_BATCH_SIZE", 1)
    calls: list[tuple[tuple[str, ...], bool]] = []

    def fake_download(tickers, *, threads):
        calls.append((tuple(tickers), threads))
        if threads:
            return {tickers[0]: {"close": object(), "volume": object()}}
        return {
            ticker: {"close": object(), "volume": object()}
            for ticker in tickers
        }

    monkeypatch.setattr(ss, "_download_price_batch", fake_download)

    result = ss._bulk_download(["AAPL", "MSFT", "NVDA"])

    assert set(result) == {"AAPL", "MSFT", "NVDA"}
    assert calls == [
        (("AAPL", "MSFT", "NVDA"), True),
        (("MSFT",), False),
        (("NVDA",), False),
    ]


def test_configure_yfinance_cache_uses_workflow_namespace(tmp_path, monkeypatch):
    import yfinance as yf

    seen: list[str] = []
    monkeypatch.setenv("ALMANAC_YFINANCE_CACHE_DIR", str(tmp_path))
    monkeypatch.setattr(yf, "set_tz_cache_location", seen.append)

    path = configure_yfinance_cache("short/screener")

    assert path == tmp_path / "short_screener"
    assert path.is_dir()
    assert seen == [str(path)]
