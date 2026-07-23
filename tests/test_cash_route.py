"""Tests for api/routes/cash.py — cash deposit/withdraw route.

cash.py is the only path that records deposits, withdrawals, and cash-flow
events. Bugs here silently corrupt NAV calculations.

Coverage:
  - _holdings_key: currency × broker → correct holdings.json key
  - _recompute_total_cash: arithmetic
  - _apply_cash_change via deposit/withdraw:
      · JPY rakuten deposit: account.balance+, CASH_JPY shares+, audit log
      · JPY SBI deposit: CASH_JPY_SBI shares+, account.balance unchanged
      · withdraw overdraft → 400, files unchanged
      · holdings key missing → 500
      · audit log append semantics (id, type, currency, amounts)
"""

from __future__ import annotations

import asyncio
import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import HTTPException

import event_ledger
from api.routes import cash as cash_module
from api.routes.cash import (
    CashBroker,
    CashCurrency,
    CashOwner,
    CashRequest,
    TxType,
    _apply_cash_change,
    _holdings_key,
    _recompute_jpy_equivalent_usd,
    _recompute_total_cash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated file environment for cash route tests."""
    account  = tmp_path / "account.json"
    holdings = tmp_path / "holdings.json"
    tx_file  = tmp_path / "cash_transactions.json"
    ledger_db = tmp_path / "ledger.db"

    _write_json(account, {
        "balance":        500_000.0,
        "usd_balance":    1_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     650_000,
    })
    _write_json(holdings, {
        "CASH_JPY":     {"ticker": "CASH_JPY",     "shares": 500_000.0, "currency": "JPY"},
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI", "shares": 100_000.0, "currency": "JPY"},
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE", "shares": 50_000.0, "currency": "JPY",
            "reported_balance_jpy": 50_000.0, "reported_as_of": "2026-05-12",
            "ledger_delta_since_report_jpy": 0, "balance_status": "confirmed",
            "reconciliation_required": False,
        },
        "CASH_USD":     {"ticker": "CASH_USD",     "shares": 1_000.0,   "currency": "USD"},
    })
    _write_json(tx_file, {"transactions": []})

    monkeypatch.setattr(cash_module, "ACCOUNT_FILE",  account)
    monkeypatch.setattr(cash_module, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(cash_module, "TX_FILE",       tx_file)
    monkeypatch.setattr(event_ledger, "DB_PATH",      ledger_db)

    @contextmanager
    def _noop_lock(name: str, *, timeout: float = 0.0) -> Iterator[Path]:
        yield tmp_path / f"{name}.lock"

    monkeypatch.setattr(cash_module, "process_lock", _noop_lock)

    return {"account": account, "holdings": holdings, "tx_file": tx_file}


# ---------------------------------------------------------------------------
# _holdings_key
# ---------------------------------------------------------------------------


def test_holdings_key_jpy_rakuten() -> None:
    assert _holdings_key(CashCurrency.JPY, CashBroker.rakuten, CashOwner.husband) == "CASH_JPY"


def test_holdings_key_jpy_sbi() -> None:
    assert _holdings_key(CashCurrency.JPY, CashBroker.sbi, CashOwner.husband) == "CASH_JPY_SBI"
    assert _holdings_key(CashCurrency.JPY, CashBroker.sbi, CashOwner.wife) == "CASH_JPY_SBI_WIFE"


def test_holdings_key_usd_rakuten() -> None:
    assert _holdings_key(CashCurrency.USD, CashBroker.rakuten, CashOwner.husband) == "CASH_USD"


def test_holdings_key_usd_sbi_is_unresolved() -> None:
    with pytest.raises(HTTPException) as exc:
        _holdings_key(CashCurrency.USD, CashBroker.sbi, CashOwner.wife)
    assert exc.value.status_code == 409


# ---------------------------------------------------------------------------
# _recompute_total_cash
# ---------------------------------------------------------------------------


def test_recompute_total_cash_basic() -> None:
    account = {"balance": 500_000.0, "usd_balance": 1_000.0, "fx_rate_usdjpy": 150.0}
    assert _recompute_total_cash(account) == 650_000


def test_recompute_total_cash_zero_usd() -> None:
    account = {"balance": 300_000.0, "usd_balance": 0.0, "fx_rate_usdjpy": 150.0}
    assert _recompute_total_cash(account) == 300_000


def test_recompute_total_cash_missing_fields() -> None:
    """Missing balance/usd_balance treated as 0."""
    account = {"fx_rate_usdjpy": 150.0}
    assert _recompute_total_cash(account) == 0


def test_recompute_total_cash_uses_default_fx_when_missing() -> None:
    """Missing fx_rate_usdjpy defaults to 150."""
    account = {"balance": 0.0, "usd_balance": 1_000.0}
    assert _recompute_total_cash(account) == 150_000


def test_recompute_jpy_equivalent_usd_uses_current_fx() -> None:
    account = {
        "usd_balance": 56_140.68,
        "fx_rate_usdjpy": 161.79600524902344,
        "jpy_equivalent_usd": 6_630_665,
    }
    assert _recompute_jpy_equivalent_usd(account) == 9_083_338


# ---------------------------------------------------------------------------
# JPY rakuten deposit
# ---------------------------------------------------------------------------


def test_jpy_rakuten_deposit_increases_balance(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=100_000.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)

    assert result["ok"] is True
    account = _read(isolated["account"])
    assert account["balance"] == pytest.approx(600_000.0)


def test_jpy_rakuten_deposit_syncs_cash_jpy_shares(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=50_000.0, broker=CashBroker.rakuten)
    _apply_cash_change(req, TxType.deposit)

    holdings = _read(isolated["holdings"])
    assert holdings["CASH_JPY"]["shares"] == pytest.approx(550_000.0)


def test_jpy_rakuten_deposit_recalculates_total_cash(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=100_000.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)

    assert result["total_cash"] == 750_000   # 600K JPY + 1K USD × 150
    account = _read(isolated["account"])
    assert account["jpy_equivalent_usd"] == 150_000



def test_jpy_rakuten_deposit_appends_audit_log(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=80_000.0, broker=CashBroker.rakuten,
                      description="給与天引き分")
    _apply_cash_change(req, TxType.deposit)

    txs = _read(isolated["tx_file"])["transactions"]
    assert len(txs) == 1
    tx = txs[0]
    assert tx["type"] == "deposit"
    assert tx["currency"] == "JPY"
    assert tx["broker"] == "rakuten"
    assert tx["amount"] == 80_000.0
    assert tx["description"] == "給与天引き分"
    assert tx["id"].startswith("tx_")


# ---------------------------------------------------------------------------
# JPY SBI deposit (CASH_JPY_SBI only — account.balance unchanged)
# ---------------------------------------------------------------------------


def test_jpy_sbi_deposit_syncs_sbi_holdings_only(isolated) -> None:
    """SBI JPY deposit goes to CASH_JPY_SBI; account.balance must NOT change."""
    req = CashRequest(currency=CashCurrency.JPY, amount=200_000.0, broker=CashBroker.sbi)
    _apply_cash_change(req, TxType.deposit)

    holdings = _read(isolated["holdings"])
    assert holdings["CASH_JPY_SBI"]["shares"] == pytest.approx(300_000.0)

    account = _read(isolated["account"])
    assert account["balance"] == pytest.approx(500_000.0)   # unchanged


def test_wife_sbi_cash_uses_separate_estimated_ledger_and_can_reconcile(isolated) -> None:
    req = CashRequest(
        currency=CashCurrency.JPY,
        amount=5_000,
        broker=CashBroker.sbi,
        owner=CashOwner.wife,
    )
    _apply_cash_change(req, TxType.withdraw)
    holdings = _read(isolated["holdings"])
    assert holdings["CASH_JPY_SBI_WIFE"]["shares"] == 45_000
    assert holdings["CASH_JPY_SBI_WIFE"]["ledger_delta_since_report_jpy"] == -5_000
    assert holdings["CASH_JPY_SBI_WIFE"]["balance_status"] == "estimated"
    assert holdings["CASH_JPY_SBI"]["shares"] == 100_000

    result = asyncio.run(cash_module.reconcile_cash(cash_module.CashReconcileRequest(
        owner=CashOwner.wife,
        broker=CashBroker.sbi,
        currency=CashCurrency.JPY,
        reported_balance=47_500,
        reported_as_of="2026-07-17",
        source="SBI CSV",
    )))
    wife = _read(isolated["holdings"])["CASH_JPY_SBI_WIFE"]
    assert result["status"] == "confirmed"
    assert wife["shares"] == 47_500
    assert wife["ledger_delta_since_report_jpy"] == 0
    assert wife["reconciliation_required"] is False


# ---------------------------------------------------------------------------
# Withdraw — overdraft protection
# ---------------------------------------------------------------------------


def test_jpy_withdraw_decreases_balance(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=200_000.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.withdraw)

    assert result["ok"] is True
    assert result["balance_jpy"] == pytest.approx(300_000.0)


def test_jpy_withdraw_overdraft_raises_400(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=600_000.0, broker=CashBroker.rakuten)
    with pytest.raises(HTTPException) as exc:
        _apply_cash_change(req, TxType.withdraw)
    assert exc.value.status_code == 400
    assert "残高不足" in exc.value.detail


def test_jpy_overdraft_leaves_account_unchanged(isolated) -> None:
    """If overdraft is raised, account.json must be rolled back."""
    before = _read(isolated["account"])
    req = CashRequest(currency=CashCurrency.JPY, amount=999_999.0, broker=CashBroker.rakuten)
    with pytest.raises(HTTPException):
        _apply_cash_change(req, TxType.withdraw)
    after = _read(isolated["account"])
    assert after["balance"] == before["balance"]


def test_jpy_holdings_overdraft_raises_400(isolated) -> None:
    """holdings.shares also guards against going negative."""
    # Set account balance high enough so account guard doesn't fire first,
    # but CASH_JPY_SBI shares are low.
    _write_json(isolated["account"], {
        "balance": 5_000_000.0, "usd_balance": 0.0,
        "fx_rate_usdjpy": 150.0, "total_cash": 5_000_000,
    })
    _write_json(isolated["holdings"], {
        "CASH_JPY":     {"ticker": "CASH_JPY",     "shares": 5_000_000.0, "currency": "JPY"},
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI", "shares": 10_000.0,    "currency": "JPY"},
        "CASH_USD":     {"ticker": "CASH_USD",     "shares": 0.0,          "currency": "USD"},
    })
    req = CashRequest(currency=CashCurrency.JPY, amount=50_000.0, broker=CashBroker.sbi)
    with pytest.raises(HTTPException) as exc:
        _apply_cash_change(req, TxType.withdraw)
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Multiple deposits build up audit log
# ---------------------------------------------------------------------------


def test_multiple_deposits_append_log_entries(isolated) -> None:
    for amount in (100_000, 50_000, 30_000):
        req = CashRequest(currency=CashCurrency.JPY, amount=float(amount),
                          broker=CashBroker.rakuten)
        _apply_cash_change(req, TxType.deposit)

    txs = _read(isolated["tx_file"])["transactions"]
    assert len(txs) == 3


# ---------------------------------------------------------------------------
# Result schema
# ---------------------------------------------------------------------------


def test_deposit_result_schema(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=10_000.0)
    result = _apply_cash_change(req, TxType.deposit)
    for key in ("ok", "transaction", "balance_jpy", "balance_usd", "total_cash"):
        assert key in result, f"missing key: {key}"


def test_deposit_returns_updated_balance_in_result(isolated) -> None:
    req = CashRequest(currency=CashCurrency.JPY, amount=100_000.0)
    result = _apply_cash_change(req, TxType.deposit)
    assert result["balance_jpy"] == pytest.approx(600_000.0)


def test_get_balances_recomputes_total_cash_when_stored_value_is_stale(isolated) -> None:
    _write_json(isolated["account"], {
        "balance": 100_000.0,
        "usd_balance": 1_000.0,
        "fx_rate_usdjpy": 151.25,
        "total_cash": 249_000,
    })

    result = asyncio.run(cash_module.get_balances())

    assert result["total_cash_jpy"] == 251_250
