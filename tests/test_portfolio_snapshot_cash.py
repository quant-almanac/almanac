"""
tests/test_portfolio_snapshot_cash.py — account cash mirror の二重計上防止
"""
import portfolio_manager as pm


def test_build_snapshot_does_not_double_count_account_cash(monkeypatch):
    monkeypatch.setattr(pm, "get_fx_rate", lambda: 150.0)
    monkeypatch.setattr(pm, "get_current_price", lambda ticker, currency, current_nav=None: current_nav or 1.0)
    monkeypatch.setattr(pm, "load_espp_data", lambda: {})
    monkeypatch.setattr(pm, "load_account", lambda: {
        "balance": 100.0,
        "usd_balance": 10.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 1_600.0,
    })
    monkeypatch.setattr(pm, "load_holdings", lambda: {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 100.0, "entry_price": 1.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 10.0, "entry_price": 1.0, "currency": "USD"},
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI", "shares": 50.0, "entry_price": 1.0, "currency": "JPY"},
        "AAPL": {"ticker": "AAPL", "shares": 1.0, "entry_price": 1.0, "currency": "USD", "current_nav": 1.0},
    })

    snap = pm.build_portfolio_snapshot(include_espp=False)
    keys = {p["key"] for p in snap["positions"]}

    assert "CASH_JPY" not in keys
    assert "CASH_USD" not in keys
    assert "CASH_JPY_SBI" in keys
    assert snap["cash_jpy"] == 1_600
    assert snap["cash_total_jpy"] == 1_600
    assert snap["total_jpy"] == 1_800
    assert snap["cash_jpy_native"] == 100
    assert snap["cash_usd"] == 10
    assert snap["cash_usd_native"] == 10
    assert snap["cash_usd_jpy"] == 1_500
    assert snap["sector_breakdown"]["Cash"]["value_jpy"] == 1_650
    assert snap["currency_breakdown"]["USD"]["value_jpy"] == 1_650
    assert snap["currency_breakdown"]["JPY"]["value_jpy"] == 150


def test_build_snapshot_recomputes_cash_total_when_account_total_cash_is_stale(monkeypatch):
    monkeypatch.setattr(pm, "get_fx_rate", lambda: 151.25)
    monkeypatch.setattr(pm, "get_current_price", lambda ticker, currency, current_nav=None: current_nav or 100.0)
    monkeypatch.setattr(pm, "load_espp_data", lambda: {})
    monkeypatch.setattr(pm, "load_account", lambda: {
        "balance": 100_000.0,
        "usd_balance": 1_000.0,
        "fx_rate_usdjpy": 151.25,
        "jpy_equivalent_usd": 149_000.0,
        "total_cash": 249_000.0,
    })
    monkeypatch.setattr(pm, "load_holdings", lambda: {
        "AAPL": {
            "ticker": "AAPL",
            "shares": 1.0,
            "entry_price": 100.0,
            "currency": "USD",
            "current_nav": 100.0,
        },
    })

    snap = pm.build_portfolio_snapshot(include_espp=False, fetch_missing_sectors=False)

    assert snap["cash_usd_jpy"] == 151_250
    assert snap["cash_jpy"] == 251_250
    assert snap["total_jpy"] == 266_375
    assert snap["sector_breakdown"]["Cash"]["value_jpy"] == 251_250
    assert snap["currency_breakdown"]["USD"]["value_jpy"] == 166_375
    assert snap["currency_breakdown"]["JPY"]["value_jpy"] == 100_000


def test_build_snapshot_allocates_usd_cash_to_usd_currency(monkeypatch):
    monkeypatch.setattr(pm, "get_fx_rate", lambda: 150.0)
    monkeypatch.setattr(pm, "get_current_price", lambda ticker, currency, current_nav=None: current_nav or 100.0)
    monkeypatch.setattr(pm, "load_espp_data", lambda: {})
    monkeypatch.setattr(pm, "load_account", lambda: {
        "balance": 800_000.0,
        "usd_balance": 40_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 6_800_000.0,
    })
    monkeypatch.setattr(pm, "load_holdings", lambda: {
        "AAPL": {"ticker": "AAPL", "shares": 10.0, "entry_price": 100.0, "currency": "USD", "current_nav": 100.0},
        "1306.T": {"ticker": "1306.T", "shares": 100.0, "entry_price": 3000.0, "currency": "JPY", "current_nav": 3000.0},
    })

    snap = pm.build_portfolio_snapshot(include_espp=False, fetch_missing_sectors=False)

    # USD positions: 10 * $100 * 150 = ¥150,000; USD cash: $40,000 * 150 = ¥6,000,000.
    # JPY positions: ¥300,000; JPY cash: ¥800,000.
    assert snap["total_jpy"] == 7_250_000
    assert snap["currency_breakdown"]["USD"]["value_jpy"] == 6_150_000
    assert snap["currency_breakdown"]["JPY"]["value_jpy"] == 1_100_000
    assert snap["currency_breakdown"]["USD"]["ratio"] == 0.8483
    assert snap["currency_breakdown"]["JPY"]["ratio"] == 0.1517


def test_cash_holdings_do_not_fetch_market_price(monkeypatch):
    called = []
    monkeypatch.setattr(pm, "get_fx_rate", lambda: 150.0)
    monkeypatch.setattr(pm, "load_espp_data", lambda: {})
    monkeypatch.setattr(pm, "load_account", lambda: {"total_cash": 0})
    monkeypatch.setattr(pm, "load_holdings", lambda: {
        "CASH_JPY_SBI": {
            "ticker": "CASH_JPY_SBI",
            "shares": 195_151,
            "entry_price": 1.0,
            "currency": "JPY",
            "investment_type": "cash",
        },
    })

    def fail_if_called(ticker, currency, current_nav=None):
        called.append(ticker)
        raise AssertionError("cash holding should not fetch market price")

    monkeypatch.setattr(pm, "get_current_price", fail_if_called)
    snap = pm.build_portfolio_snapshot(include_espp=False)

    assert called == []
    assert snap["positions"][0]["value_jpy"] == 195_151


def test_build_snapshot_can_skip_yfinance_sector_lookup(monkeypatch):
    monkeypatch.setattr(pm, "get_fx_rate", lambda: 150.0)
    monkeypatch.setattr(pm, "get_current_price", lambda ticker, currency, current_nav=None: current_nav or 100.0)
    monkeypatch.setattr(pm, "load_espp_data", lambda: {})
    monkeypatch.setattr(pm, "load_account", lambda: {"total_cash": 0})
    monkeypatch.setattr(pm, "load_holdings", lambda: {
        "UNKNOWN": {
            "ticker": "UNKNOWN",
            "shares": 1.0,
            "entry_price": 100.0,
            "currency": "USD",
            "investment_type": "long",
        },
    })

    def fail_if_sector_lookup(symbol):
        raise AssertionError("yf.info sector lookup should be skipped")

    monkeypatch.setattr(pm.yf, "Ticker", fail_if_sector_lookup)
    snap = pm.build_portfolio_snapshot(include_espp=False, fetch_missing_sectors=False)

    assert snap["positions"][0]["sector"] == "Other"
