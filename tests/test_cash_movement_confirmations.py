import asyncio
import json

import pytest
from fastapi import HTTPException

from api.routes import cash


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def _seed(monkeypatch, tmp_path):
    account = tmp_path / "account.json"
    holdings = tmp_path / "holdings.json"
    tx = tmp_path / "cash_transactions.json"
    _write(account, {"balance": 10_000, "usd_balance": 1_000, "fx_rate_usdjpy": 150})
    _write(holdings, {
        "CASH_JPY": {"shares": 10_000},
        "CASH_USD": {"shares": 1_000},
        "CASH_JPY_SBI": {"shares": 2_000},
        "CASH_JPY_SBI_WIFE": {"shares": 500, "available_to_trade_jpy": 500},
    })
    _write(tx, {"transactions": []})
    monkeypatch.setattr(cash, "ACCOUNT_FILE", account)
    monkeypatch.setattr(cash, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(cash, "TX_FILE", tx)
    ledger_calls = []
    import event_ledger
    monkeypatch.setattr(event_ledger, "append_event", lambda **kwargs: ledger_calls.append(kwargs) or {"duplicate": False})
    return account, holdings, tx, ledger_calls


def test_fx_confirmation_is_idempotent_and_records_two_explicit_wallet_legs(monkeypatch, tmp_path):
    account, holdings, tx, ledger_calls = _seed(monkeypatch, tmp_path)
    req = cash.FXConversionConfirmRequest(
        from_currency="USD", to_currency="JPY", from_amount=100,
        fx_rate_usdjpy=150, idempotency_key="fx-confirm-0001", source="broker trade confirmation",
    )

    first = asyncio.run(cash.confirm_fx_conversion(req))
    second = asyncio.run(cash.confirm_fx_conversion(req))

    assert first["ok"] is True
    assert second["idempotent_replay"] is True
    assert json.loads(account.read_text())["usd_balance"] == 900
    assert json.loads(account.read_text())["balance"] == 25_000
    assert json.loads(holdings.read_text())["CASH_USD"]["shares"] == 900
    assert len(ledger_calls) == 2
    assert [call["raw_payload"]["cash_route"] for call in ledger_calls] == ["CASH_USD", "CASH_JPY"]
    assert len(json.loads(tx.read_text())["transactions"]) == 1


def test_cross_owner_confirmation_requires_tax_ack_and_never_claims_tax_exemption(monkeypatch, tmp_path):
    _, holdings, _, ledger_calls = _seed(monkeypatch, tmp_path)
    rejected = cash.CrossOwnerTransferConfirmRequest(
        from_owner="husband", from_broker="sbi", to_owner="wife", to_broker="sbi",
        currency="JPY", amount=1_000, idempotency_key="owner-transfer-0001", source="bank transfer",
    )
    with pytest.raises(HTTPException, match="tax_review_acknowledged"):
        asyncio.run(cash.confirm_cross_owner_transfer(rejected))

    accepted = rejected.model_copy(update={"tax_review_acknowledged": True})
    result = asyncio.run(cash.confirm_cross_owner_transfer(accepted))
    state = json.loads(holdings.read_text())
    assert result["kind"] == "cross_owner_transfer"
    assert state["CASH_JPY_SBI"]["shares"] == 1_000
    assert state["CASH_JPY_SBI_WIFE"]["shares"] == 1_500
    assert state["CASH_JPY_SBI_WIFE"]["balance_status"] == "estimated"
    assert all(call["event_type"] == "cross_owner_transfer" for call in ledger_calls)
    assert all(call["raw_payload"]["tax_review_acknowledged"] is True for call in ledger_calls)
