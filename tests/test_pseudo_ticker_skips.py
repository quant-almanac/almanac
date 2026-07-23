import json
from datetime import datetime, timedelta, timezone

import data_fetcher
import technical_signals
import analyst
import chart_analyzer
import earnings_proximity_manager
import recommendation_verifier
from analyst import data_gatherer


def test_collect_priority_tickers_skips_operational_and_fund_pseudo_tickers():
    result = analyst._collect_priority_tickers(
        {
            "priority_actions": [
                {"ticker": "WIFE_NISA_TSUMITATE"},
                {"ticker": "SLIM_ORCAN"},
                {"ticker": "GS_MMF_USD"},
                {"ticker": "AAPL"},
            ],
            "margin_long_picks": [{"ticker": "MA"}],
        },
        positions_raw=[
            {"ticker": "CASH_JPY_SBI"},
            {"ticker": "SLIM_SP500"},
            {"ticker": "AVGO"},
        ],
        max_tickers=30,
    )

    assert result == ["AAPL", "MA", "AVGO"]


def test_chart_context_skips_operational_pseudo_tickers(monkeypatch):
    seen = []

    def fake_gather_one(ticker, *, intraday=True):
        seen.append(ticker)
        return {"ticker": ticker}

    monkeypatch.setattr(chart_analyzer, "gather_one", fake_gather_one)

    result = chart_analyzer.gather_chart_context(
        ["WIFE_NISA_TSUMITATE", "CASH_JPY_SBI", "SLIM_ORCAN", "AAPL"],
        intraday=False,
    )

    assert seen == ["AAPL"]
    assert result == {"AAPL": {"ticker": "AAPL"}}


def test_data_fetcher_daily_update_skips_cash_and_mmf_pseudo_tickers(tmp_path, monkeypatch):
    holdings_path = tmp_path / "holdings.json"
    holdings_path.write_text(json.dumps({
        "CASH_JPY": {"ticker": "CASH_JPY"},
        "CASH_USD": {"ticker": "CASH_USD"},
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI"},
        "GS_MMF_USD": {"ticker": "GS_MMF_USD"},
        "SLIM_SP500_WIFE": {"ticker": "SLIM_SP500"},
        "AVGO_toku": {"ticker": "AVGO"},
    }), encoding="utf-8")

    seen = []
    monkeypatch.setattr(data_fetcher, "init_db", lambda: None)
    monkeypatch.setattr(data_fetcher, "fetch_and_save_ohlcv", lambda tickers: seen.extend(tickers) or {})

    class _FakeCon:
        def cursor(self):
            return self

        def commit(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr(data_fetcher, "_connect", lambda: _FakeCon())

    data_fetcher.daily_update(holdings_path=holdings_path)

    assert "AVGO" in seen
    assert "9999.T" in seen
    assert "CASH_JPY" not in seen
    assert "CASH_USD" not in seen
    assert "CASH_JPY_SBI" not in seen
    assert "GS_MMF_USD" not in seen
    assert "SLIM_SP500" not in seen


def test_fetch_and_save_ohlcv_skips_pseudo_tickers_before_yfinance(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(data_fetcher, "OHLCV_DIR", tmp_path)

    def fail_download(ticker, **kwargs):
        called.append(ticker)
        raise AssertionError("pseudo ticker should not reach yfinance")

    monkeypatch.setattr(data_fetcher.yf, "download", fail_download)

    result = data_fetcher.fetch_and_save_ohlcv(["CASH_JPY", "GS_MMF_USD"])

    assert result == {}
    assert called == []


def test_technical_ticker_universe_skips_cash_pseudo_tickers(monkeypatch):
    monkeypatch.setattr(technical_signals, "load_json", lambda path, default=None: {
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI"},
        "CASH_JPY_SBI_WIFE": {"ticker": "CASH_JPY_SBI_WIFE"},
        "GS_MMF_USD": {"ticker": "GS_MMF_USD"},
        "SLIM_SP500_WIFE": {"ticker": "SLIM_SP500"},
        "AVGO_toku": {"ticker": "AVGO"},
    })

    tickers = technical_signals._build_ticker_universe()

    assert "AVGO" in tickers
    assert "CASH_JPY_SBI" not in tickers
    assert "CASH_JPY_SBI_WIFE" not in tickers
    assert "GS_MMF_USD" not in tickers
    assert "SLIM_SP500" not in tickers


def test_ai_data_gatherer_skips_pseudo_tickers_for_earnings_context():
    assert data_gatherer._is_earnings_context_ticker("NVDA") is True
    assert data_gatherer._is_earnings_context_ticker("CASH_JPY") is False
    assert data_gatherer._is_earnings_context_ticker("CASH_JPY_SBI_WIFE") is False
    assert data_gatherer._is_earnings_context_ticker("GS_MMF_USD") is False
    assert data_gatherer._is_earnings_context_ticker("SLIM_ORCAN") is False
    assert data_gatherer._is_earnings_context_ticker("WIFE_NISA_TSUMITATE") is False
    assert data_gatherer._is_earnings_context_ticker("GLD") is False
    assert data_gatherer._is_earnings_context_ticker("IEV") is False
    assert data_gatherer._is_earnings_context_ticker("XLF") is False
    assert data_gatherer._is_earnings_context_ticker("^VIX") is False
    assert data_gatherer._is_earnings_context_ticker("JPY=X") is False
    assert data_gatherer._is_earnings_context_ticker("9999.T") is False


def test_earnings_proximity_skips_etfs_and_pseudo_tickers(tmp_path, monkeypatch):
    holdings_path = tmp_path / "holdings.json"
    holdings_path.write_text(json.dumps({
        "AAPL": {"ticker": "AAPL", "shares": 1, "investment_type": "long"},
        "GLD": {"ticker": "GLD", "shares": 1, "investment_type": "long"},
        "IEV": {"ticker": "IEV", "shares": 1, "investment_type": "long"},
        "XLF": {"ticker": "XLF", "shares": 1, "investment_type": "long"},
        "SLIM_ORCAN": {"ticker": "SLIM_ORCAN", "shares": 1, "investment_type": "long"},
        "WIFE_NISA_TSUMITATE": {"ticker": "WIFE_NISA_TSUMITATE", "shares": 1, "investment_type": "long"},
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 1, "investment_type": "cash"},
    }), encoding="utf-8")
    monkeypatch.setattr(earnings_proximity_manager, "HOLDINGS", holdings_path)

    rows = earnings_proximity_manager._load_holdings()

    assert [row["ticker"] for row in rows] == ["AAPL"]


def test_recommendation_verifier_skips_pseudo_tickers_before_yfinance(tmp_path, monkeypatch):
    import pandas as pd

    log_path = tmp_path / "ai_recommendation_log.json"
    as_of = datetime.now(timezone.utc) - timedelta(days=8)
    log_path.write_text(json.dumps([
        {"as_of": as_of.isoformat(), "ticker": "WIFE_NISA_TSUMITATE", "type": "rebalance", "urgency": "low", "verified": False},
        {"as_of": as_of.isoformat(), "ticker": "SLIM_ORCAN", "type": "dca", "urgency": "low", "verified": False},
        {"as_of": as_of.isoformat(), "ticker": "AAPL", "type": "buy", "urgency": "medium", "verified": False},
    ]), encoding="utf-8")
    monkeypatch.setattr(recommendation_verifier, "LOG_PATH", log_path)

    downloaded = []

    def fake_download(tickers, **kwargs):
        ticker_list = list(tickers)
        downloaded.extend(ticker_list)
        idx = pd.date_range(as_of.date(), periods=10, freq="D")
        cols = pd.MultiIndex.from_product([["Close"], ticker_list])
        return pd.DataFrame(100.0, index=idx, columns=cols)

    monkeypatch.setattr(recommendation_verifier.yf, "download", fake_download)

    recommendation_verifier.verify_recommendations()

    assert "AAPL" in downloaded
    assert "SPY" in downloaded
    assert "WIFE_NISA_TSUMITATE" not in downloaded
    assert "SLIM_ORCAN" not in downloaded


def test_delta_snapshot_uses_no_sector_fetch_source_guard():
    from pathlib import Path

    source = Path("analyzer.py").read_text(encoding="utf-8")

    assert "build_portfolio_snapshot(fetch_missing_sectors=False)" in source
