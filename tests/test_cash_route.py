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
    assert result["execution_plan_refreshed"] is True
    assert (isolated["account"].parent / "execution_plan_state.json").is_file()
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
        available_to_trade=42_500,
        reported_as_of="2026-07-17",
        source="SBI CSV",
    )))
    wife = _read(isolated["holdings"])["CASH_JPY_SBI_WIFE"]
    assert result["status"] == "confirmed"
    assert wife["shares"] == 47_500
    assert wife["available_to_trade_jpy"] == 42_500
    assert wife["unavailable_cash_jpy"] == 5_000
    assert wife["ledger_delta_since_report_jpy"] == 0
    assert wife["reconciliation_required"] is False
    assert wife["source_as_of"] == "2026-07-17"
    assert wife["broker_reconciled_at"].endswith("+09:00")
    assert wife["reconciled_at"] == wife["broker_reconciled_at"]


def test_web_confirmed_wife_sbi_cash_event_keeps_balance_authoritative(isolated) -> None:
    req = CashRequest(
        currency=CashCurrency.JPY,
        amount=10_000.0,
        broker=CashBroker.sbi,
        owner=cash_module.CashOwner.wife,
        broker_confirmed=True,
        external_transaction_id="sbi-cash-123",
        broker_reported_at="2026-07-30T09:15:00+09:00",
    )
    _apply_cash_change(req, TxType.deposit)
    wife = _read(isolated["holdings"])["CASH_JPY_SBI_WIFE"]
    tx = _read(isolated["tx_file"])["transactions"][-1]
    assert wife["balance_status"] == "confirmed"
    assert wife["reconciliation_required"] is False
    assert wife["source_as_of"] == "2026-07-30T09:15:00+09:00"
    assert tx["broker_confirmed"] is True
    assert tx["external_transaction_id"] == "sbi-cash-123"


def test_web_confirmed_cash_event_rejects_duplicate_broker_id(isolated) -> None:
    req = CashRequest(
        currency=CashCurrency.JPY,
        amount=10_000.0,
        broker=CashBroker.rakuten,
        broker_confirmed=True,
        external_transaction_id="rakuten-cash-123",
        broker_reported_at="2026-07-30T09:15:00+09:00",
    )
    _apply_cash_change(req, TxType.deposit)
    with pytest.raises(HTTPException) as exc:
        _apply_cash_change(req, TxType.deposit)
    assert exc.value.status_code == 409


def test_wife_sbi_reconcile_rejects_available_above_total(isolated) -> None:
    with pytest.raises(HTTPException) as exc:
        asyncio.run(cash_module.reconcile_cash(cash_module.CashReconcileRequest(
            owner=CashOwner.wife,
            broker=CashBroker.sbi,
            currency=CashCurrency.JPY,
            reported_balance=47_500,
            available_to_trade=50_000,
            reported_as_of="2026-07-17",
            source="SBI CSV",
        )))
    assert exc.value.status_code == 422


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


# ---------------------------------------------------------------------------
# USD 入出金: FX レートが account.json へ実際に反映されること
# ---------------------------------------------------------------------------
#
# ⚠️ 以前は _event_fx_rate() が get_fx_rate_cached(persist_live_rate=True)
# を呼び、その内部でファイルへ直接書いていた。2つの経路で成立しなかった
# (レビューで実測・再現):
#   1. キャッシュヒット時: persist_live_rate=True でもキャッシュヒットの
#      分岐は書き込みより前で return するため一切書かれない。
#   2. キャッシュミス時: _prepare_cash_change() が先に account を複製済み
#      なので、直接書き込みの直後に古いメモリ上の account で上書き
#      コミットしてしまう。
# 修正: _event_fx_rate() は (値, source) を返し、_prepare_cash_change() が
# その値をトランザクション内の account へ反映してから commit する。source
# ('live' / 'cache' / 'account_stale' / 'hardcoded') は fx_rate_usdjpy_as_of
# を「今」へ進めてよいかどうかの判断にも使う — stale な値やハードコード
# 既定値を「たった今確認できた」かのように鮮度を偽装しないため
# (レビューで指摘: watchdog.py / analysis_snapshot.py の鮮度チェックが
# 誤認する)。


def _install_fake_fx(monkeypatch, rate: float, source: str = "live") -> None:
    """utils.get_fx_rate_cached を差し替え、cash モジュールの import 経路を通す。"""
    import utils as utils_module

    def _fake(*_a, **_kw):
        return float(rate), source

    monkeypatch.setattr(utils_module, "get_fx_rate_cached", _fake)


def test_usd_deposit_persists_the_fetched_fx_rate_on_a_cache_miss(isolated, monkeypatch) -> None:
    """キャッシュミス (source='live') でも FX レートが account.json へ残る。"""
    _install_fake_fx(monkeypatch, 151.25, source="live")

    req = CashRequest(currency=CashCurrency.USD, amount=100.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)

    assert result["ok"] is True
    account = _read(isolated["account"])
    assert account["fx_rate_usdjpy"] == 151.25, (
        "USD 入出金なのに account.json の FX レートが更新されなかった")
    assert account["fx_rate_source"] == "live"
    assert "fx_rate_usdjpy_as_of" in account
    # total_cash / jpy_equivalent_usd も新レートで再計算されていること。
    assert account["total_cash"] == _recompute_total_cash(account)


def test_usd_deposit_persists_the_fetched_fx_rate_on_a_cache_hit(isolated, monkeypatch) -> None:
    """キャッシュヒット (source='cache') でも FX レートが account.json へ残る。

    以前は get_fx_rate_cached(persist_live_rate=True) 自身がキャッシュ
    ヒットの早期 return で書き込みブロックへ到達できず、この経路だけ
    サイレントに何も保存されなかった。
    """
    _install_fake_fx(monkeypatch, 151.25, source="cache")

    req = CashRequest(currency=CashCurrency.USD, amount=100.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)

    assert result["ok"] is True
    account = _read(isolated["account"])
    assert account["fx_rate_usdjpy"] == 151.25, (
        "キャッシュヒット経路で FX レートが account.json へ反映されなかった")
    assert account["fx_rate_source"] == "cache"
    # cache は TTL (10分) 内のライブ値の再利用なので、live と同じく
    # as_of を「今」に進めてよい。
    assert "fx_rate_usdjpy_as_of" in account


def test_usd_deposit_does_not_advance_fx_as_of_on_an_account_stale_rate(isolated, monkeypatch) -> None:
    """source='account_stale' はレートを使うが、確認時刻の偽装はしない。

    yfinance 障害時に account.json の既存レートへ再度フォールバックした
    だけであり、「たった今確認できた」わけではない。ここで as_of を
    進めると、watchdog.py / analysis_snapshot.py の FX 鮮度チェックが
    実際には古いレートを新鮮と誤認する (レビューで指摘)。
    """
    _install_fake_fx(monkeypatch, 149.0, source="account_stale")
    account_before = _read(isolated["account"])
    assert "fx_rate_usdjpy_as_of" not in account_before

    req = CashRequest(currency=CashCurrency.USD, amount=100.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)

    assert result["ok"] is True
    account = _read(isolated["account"])
    assert account["fx_rate_usdjpy"] == 149.0
    assert account["fx_rate_source"] == "account_stale"
    assert "fx_rate_usdjpy_as_of" not in account, (
        "account_stale レートなのに fx_rate_usdjpy_as_of が新規追加/更新された")


def test_usd_deposit_fails_closed_when_fx_is_only_the_hardcoded_fallback(isolated, monkeypatch) -> None:
    """source='hardcoded' (yfinance も account.json の値も使えない) では
    財務台帳を確定させず、fail-closed で見送る。実勢と無関係な定数を
    現在の FX レートとして記録するより、失敗を明示する方が安全。
    """
    _install_fake_fx(monkeypatch, 150.0, source="hardcoded")
    before_account = _read(isolated["account"])
    before_holdings = _read(isolated["holdings"])
    before_tx = _read(isolated["tx_file"])

    req = CashRequest(currency=CashCurrency.USD, amount=100.0, broker=CashBroker.rakuten)
    with pytest.raises(HTTPException) as exc:
        _apply_cash_change(req, TxType.deposit)

    assert exc.value.status_code == 500
    assert _read(isolated["account"]) == before_account
    assert _read(isolated["holdings"]) == before_holdings
    assert _read(isolated["tx_file"]) == before_tx


def test_usd_withdrawal_with_insufficient_balance_never_fetches_fx(isolated, monkeypatch) -> None:
    """残高不足は FX 取得より先にローカルだけで判定され、400 を返す。

    FX 取得を残高検証より前に置くと、本来 400 になるべきリクエストが
    ネットワーク/yfinance 障害で先に 500 になり得る (レビューで指摘)。

    ⚠️ get_fx_rate_cached を「呼ばれたら例外」にして検知する方式は使わない
    — _event_fx_rate() 自身が except Exception でフォールバックするため、
    その例外は account_stale 扱いに吸収されてしまい、呼ばれたことを
    検知できない (実際にこの書き方で書いたところミューテーションを
    検出できず、書き直した)。代わりに呼び出し回数を数える。
    """
    calls = {"n": 0}

    def _counting_fake(*_a, **_kw):
        calls["n"] += 1
        return 999.0, "live"

    import utils as utils_module
    monkeypatch.setattr(utils_module, "get_fx_rate_cached", _counting_fake)

    # isolated フィクスチャの usd_balance は 1,000.0
    req = CashRequest(currency=CashCurrency.USD, amount=2_000.0, broker=CashBroker.rakuten)
    with pytest.raises(HTTPException) as exc:
        _apply_cash_change(req, TxType.withdraw)

    assert exc.value.status_code == 400
    assert calls["n"] == 0, "残高不足の判定より前に get_fx_rate_cached が呼ばれた"


def test_usd_deposit_uses_a_genuinely_warmed_cache_from_a_prior_read(isolated, monkeypatch) -> None:
    """utils.get_fx_rate_cached そのものは差し替えず、実際に GET 相当の
    読み取り呼び出しで TTL キャッシュを温めてから USD POST を発行する。
    source='cache' がモックなしで _prepare_cash_change() まで正しく
    伝わることを確認する (以前の _install_fake_fx 経由の2テストは
    source 文字列を直接指定するだけで、実際のキャッシュ分岐を踏んで
    いなかった)。
    """
    import utils as utils_module
    utils_module._fx_cache_clear()
    calls = {"n": 0}

    class _Fake:
        @property
        def fast_info(self):
            calls["n"] += 1
            return {"lastPrice": 153.4}

    class _FakeMod:
        def Ticker(self, _pair):
            return _Fake()

    monkeypatch.setitem(__import__("sys").modules, "yfinance", _FakeMod())

    # dashboard/Today の GET 相当。read-only (persist_live_rate 既定 False)
    # でも、yfinance のライブ取得自体はモジュール内 TTL キャッシュへ格納する。
    warm_rate, warm_source = utils_module.get_fx_rate_cached()
    assert warm_source == "live"
    assert calls["n"] == 1

    req = CashRequest(currency=CashCurrency.USD, amount=100.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)

    assert result["ok"] is True
    assert calls["n"] == 1, "2回目はキャッシュヒットのはずが yfinance を再度叩いた"
    account = _read(isolated["account"])
    assert account["fx_rate_usdjpy"] == warm_rate == 153.4
    assert account["fx_rate_source"] == "cache"
    assert "fx_rate_usdjpy_as_of" in account


def test_a_jpy_transaction_does_not_require_an_fx_fetch(isolated, monkeypatch) -> None:
    """JPY 取引は FX を取りに行かない (既存スコープ通り)。呼ばれたら失敗させる。"""
    def _must_not_be_called(*_a, **_kw):
        raise AssertionError("JPY 取引で get_fx_rate_cached が呼ばれた")

    import utils as utils_module
    monkeypatch.setattr(utils_module, "get_fx_rate_cached", _must_not_be_called)

    req = CashRequest(currency=CashCurrency.JPY, amount=100_000.0, broker=CashBroker.rakuten)
    result = _apply_cash_change(req, TxType.deposit)
    assert result["ok"] is True
