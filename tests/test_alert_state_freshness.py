import json

import pandas as pd

import alert


class _FakeTicker:
    def __init__(self, symbol: str, price: float = 100.0):
        self.symbol = symbol
        self.fast_info = {"lastPrice": price}

    def history(self, period: str):
        if self.symbol == "SPY":
            closes = [100.0] * 60
        elif self.symbol == "^N225":
            closes = [30_000.0] * 60
        else:
            closes = []
        return pd.DataFrame({"Close": closes})


def test_regime_change_updates_state_even_during_quiet_hours(tmp_path, monkeypatch):
    monkeypatch.setattr(alert, "BASE_DIR", tmp_path)
    monkeypatch.setattr(alert, "is_quiet_hours", lambda: True)
    monkeypatch.setattr(alert, "reset_yfinance_session", lambda: None)
    monkeypatch.setattr(alert.yf, "Ticker", lambda symbol: _FakeTicker(symbol))

    flip_calls = []
    monkeypatch.setattr(alert, "check_regime_flip_notification", lambda: flip_calls.append(True))

    alert.check_regime_change()

    regime_state = json.loads((tmp_path / "regime_state.json").read_text(encoding="utf-8"))
    assert regime_state["spy_above"] is False
    assert regime_state["nk_above"] is False
    assert regime_state["updated"]
    assert flip_calls == []


def test_check_alerts_skips_cash_and_fund_holdings(monkeypatch):
    fetched = []
    monkeypatch.setattr(alert, "is_quiet_hours", lambda: False)
    monkeypatch.setattr(alert, "reset_yfinance_session", lambda: None)
    monkeypatch.setattr(alert, "load_alert_log", lambda: {})
    monkeypatch.setattr(alert, "save_alert_log", lambda log: None)
    monkeypatch.setattr(alert, "save_holdings", lambda holdings: None)
    monkeypatch.setattr(alert, "load_holdings", lambda: {
        "SLIM_SP500_WIFE": {
            "ticker": "SLIM_SP500",
            "investment_type": "long",
            "unit": "口",
            "entry_price": 40_000,
        },
        "CASH_JPY_SBI": {
            "ticker": "CASH_JPY_SBI",
            "investment_type": "cash",
            "entry_price": 1,
        },
        "AAPL": {
            "ticker": "AAPL",
            "investment_type": "long",
            "entry_price": 100,
        },
    })

    def fake_ticker(symbol):
        fetched.append(symbol)
        return _FakeTicker(symbol, price=101.0)

    monkeypatch.setattr(alert.yf, "Ticker", fake_ticker)

    alert.check_alerts()

    assert fetched == ["AAPL"]


def test_update_guard_state_uses_sectorless_snapshot_and_resets_session(monkeypatch):
    import behavioral_guard
    import portfolio_manager

    kwargs_seen = []
    resets = []
    monkeypatch.setattr(alert, "reset_yfinance_session", lambda: resets.append(True))
    monkeypatch.setattr(behavioral_guard, "load_state", lambda: {
        "portfolio_value": 9_000_000,
        "date": "2099-01-01",
    })
    monkeypatch.setattr(behavioral_guard, "update_pnl", lambda pnl, value: {"ok": True})

    def fake_snapshot(**kwargs):
        kwargs_seen.append(kwargs)
        return {"total_jpy": 10_000_000}

    monkeypatch.setattr(portfolio_manager, "build_portfolio_snapshot", fake_snapshot)

    alert.update_guard_state()

    assert kwargs_seen == [{"fetch_missing_sectors": False}]
    assert resets == [True]
