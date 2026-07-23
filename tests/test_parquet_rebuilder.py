"""
tests/test_parquet_rebuilder.py — P2-23

yfinance への実アクセスはモックして、純粋なロジック (skip list / swap / holdings 抽出) のみ検証。
"""
import json
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest

import parquet_rebuilder as pr


@pytest.fixture
def tmp_ohlcv(tmp_path):
    d = tmp_path / "data" / "ohlcv"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def tmp_holdings(tmp_path, monkeypatch):
    p = tmp_path / "holdings.json"
    p.write_text(json.dumps({
        "AAPL":     {"ticker": "AAPL", "shares": 10},
        "7203.T":   {"ticker": "7203.T", "shares": 100},
        "SLIM_SP500": {"ticker": "SLIM_SP500"},   # skip
        "CASH_JPY":   {"shares": 1000000},        # skip
    }))
    monkeypatch.setattr(pr, "HOLDINGS_FILE", p)
    return p


def test_list_holdings_tickers_excludes_skip(tmp_holdings):
    tickers = pr._list_holdings_tickers()
    assert "AAPL" in tickers
    assert "7203.T" in tickers
    assert "SLIM_SP500" not in tickers
    assert "CASH_JPY" not in tickers


def test_list_holdings_empty_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pr, "HOLDINGS_FILE", tmp_path / "nonexistent.json")
    assert pr._list_holdings_tickers() == []


def test_rebuild_one_writes_parquet_and_returns_meta(tmp_ohlcv):
    """yf.download をモックして parquet 出力までを確認。"""
    fake_df = pd.DataFrame({
        "Open":  [100, 101],
        "High":  [102, 103],
        "Low":   [99, 100],
        "Close": [101, 102],
        "Volume": [1000, 1100],
    }, index=pd.date_range("2026-05-01", periods=2, freq="D"))

    with patch("yfinance.download", return_value=fake_df):
        r = pr.rebuild_one("AAPL", period="2y", ohlcv_dir=tmp_ohlcv)

    assert r["updated"] is True
    assert r["rows"] == 2
    assert r["error"] is None
    assert (tmp_ohlcv / "AAPL.parquet").exists()


def test_rebuild_one_swaps_existing_to_bak(tmp_ohlcv):
    """既存ファイルがある場合は .bak へ退避される。"""
    dst = tmp_ohlcv / "AAPL.parquet"
    # 既存 parquet を pandas で作る (実 parquet として有効に)
    pd.DataFrame({"Close": [50]}).to_parquet(dst)
    assert dst.exists()

    fake_df = pd.DataFrame({"Close": [100, 200]},
                           index=pd.date_range("2026-05-01", periods=2, freq="D"))

    with patch("yfinance.download", return_value=fake_df):
        r = pr.rebuild_one("AAPL", period="2y", ohlcv_dir=tmp_ohlcv)

    assert r["updated"] is True
    assert (tmp_ohlcv / "AAPL.parquet").exists()
    assert (tmp_ohlcv / "AAPL.parquet.bak").exists()
    # before_rows は退避前の既存
    assert r["before_rows"] == 1


def test_rebuild_one_empty_response_returns_error(tmp_ohlcv):
    with patch("yfinance.download", return_value=pd.DataFrame()):
        r = pr.rebuild_one("BAD", period="2y", ohlcv_dir=tmp_ohlcv)
    assert r["updated"] is False
    assert "empty" in r["error"]
    assert not (tmp_ohlcv / "BAD.parquet").exists()


def test_rebuild_one_yf_exception_caught(tmp_ohlcv):
    with patch("yfinance.download", side_effect=RuntimeError("network failure")):
        r = pr.rebuild_one("AAPL", period="2y", ohlcv_dir=tmp_ohlcv)
    assert r["updated"] is False
    assert "yf.download failed" in r["error"]


def test_rebuild_many_runs_in_order(tmp_ohlcv):
    fake_df = pd.DataFrame({"Close": [100]},
                           index=pd.date_range("2026-05-01", periods=1, freq="D"))
    with patch("yfinance.download", return_value=fake_df):
        rs = pr.rebuild_many(["A", "B", "C"], period="2y", sleep_sec=0, ohlcv_dir=tmp_ohlcv)
    assert [r["ticker"] for r in rs] == ["A", "B", "C"]
    assert all(r["updated"] for r in rs)


def test_rebuild_all_holdings_uses_filter(tmp_holdings, tmp_ohlcv):
    fake_df = pd.DataFrame({"Close": [100]},
                           index=pd.date_range("2026-05-01", periods=1, freq="D"))
    with patch("yfinance.download", return_value=fake_df):
        rs = pr.rebuild_all_holdings(period="2y", ohlcv_dir=tmp_ohlcv)
    tickers = [r["ticker"] for r in rs]
    assert "AAPL" in tickers
    assert "7203.T" in tickers
    assert "SLIM_SP500" not in tickers
    assert "CASH_JPY" not in tickers
