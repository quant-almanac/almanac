"""
cash route safety tests — 入出金が JSON と event_ledger の片方だけに反映されないことを固定する。
"""
import json

import pytest
from fastapi import HTTPException

import event_ledger as el
from api.routes import cash


@pytest.fixture
def isolated_cash(tmp_path, monkeypatch):
    account = tmp_path / "account.json"
    holdings = tmp_path / "holdings.json"
    tx = tmp_path / "cash_transactions.json"
    db = tmp_path / "ledger.db"

    account.write_text(
        json.dumps({
            "balance": 100_000,
            "usd_balance": 1_000,
            "fx_rate_usdjpy": 150.0,
            "total_cash": 250_000,
        }),
        encoding="utf-8",
    )
    holdings.write_text(
        json.dumps({
            "CASH_JPY": {"shares": 100_000, "currency": "JPY"},
            "CASH_USD": {"shares": 1_000, "currency": "USD"},
            "CASH_JPY_SBI": {"shares": 50_000, "currency": "JPY"},
        }),
        encoding="utf-8",
    )
    tx.write_text(json.dumps({"transactions": []}), encoding="utf-8")

    monkeypatch.setattr(cash, "ACCOUNT_FILE", account)
    monkeypatch.setattr(cash, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(cash, "TX_FILE", tx)
    monkeypatch.setattr(cash, "_invalidate_portfolio_cache", lambda: None)
    monkeypatch.setattr(el, "DB_PATH", db)

    return {"account": account, "holdings": holdings, "tx": tx, "db": db}


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cash_change_rolls_back_json_when_ledger_append_fails(isolated_cash, monkeypatch):
    before_account = _read(isolated_cash["account"])
    before_holdings = _read(isolated_cash["holdings"])
    before_tx = _read(isolated_cash["tx"])

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(cash, "_append_cash_flow_event", boom)

    req = cash.CashRequest(currency=cash.CashCurrency.JPY, amount=10_000, broker=cash.CashBroker.rakuten)
    with pytest.raises(HTTPException) as exc:
        cash._apply_cash_change(req, cash.TxType.deposit)

    assert exc.value.status_code == 500
    assert _read(isolated_cash["account"]) == before_account
    assert _read(isolated_cash["holdings"]) == before_holdings
    assert _read(isolated_cash["tx"]) == before_tx


def test_cash_change_records_cash_flow_event(isolated_cash):
    req = cash.CashRequest(
        currency=cash.CashCurrency.JPY,
        amount=25_000,
        broker=cash.CashBroker.rakuten,
        description="monthly deposit",
    )
    result = cash._apply_cash_change(req, cash.TxType.deposit)

    assert result["ok"] is True
    account = _read(isolated_cash["account"])
    holdings = _read(isolated_cash["holdings"])
    tx_log = _read(isolated_cash["tx"])
    events = el.query_events(types=["cash_flow"], db_path=isolated_cash["db"])

    assert account["balance"] == 125_000
    assert holdings["CASH_JPY"]["shares"] == 125_000
    assert len(tx_log["transactions"]) == 1
    assert len(events) == 1
    assert events[0]["event_id"] == tx_log["transactions"][0]["id"]
    assert events[0]["amount_jpy"] == 25_000


def test_usd_cash_change_requires_fx(isolated_cash, monkeypatch):
    monkeypatch.setattr(cash, "_event_fx_rate", lambda req, account: None)
    req = cash.CashRequest(currency=cash.CashCurrency.USD, amount=10, broker=cash.CashBroker.rakuten)

    with pytest.raises(HTTPException) as exc:
        cash._apply_cash_change(req, cash.TxType.deposit)

    assert exc.value.status_code == 500
    assert _read(isolated_cash["account"])["usd_balance"] == 1_000
    assert _read(isolated_cash["holdings"])["CASH_USD"]["shares"] == 1_000
