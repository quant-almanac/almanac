"""
api/routes/cash.py — Fix 8 (2026-04-24): 現金入出金 API

エンドポイント:
    POST /api/cash/deposit       — 入金登録
    POST /api/cash/withdraw      — 出金登録
    GET  /api/cash/transactions  — 直近の取引履歴
    GET  /api/cash/balances      — 現在の残高サマリ

挙動:
    1. account.json の balance / usd_balance / total_cash を atomic 更新
    2. holdings.json の CASH_JPY / CASH_USD / CASH_JPY_SBI shares を同期
    3. cash_transactions.json に append-only で監査ログを記録
    4. portfolio スナップショットの 5 分キャッシュを無効化
"""
from __future__ import annotations

import sys
from copy import deepcopy
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
BASE_DIR     = Path(__file__).parent.parent.parent
ACCOUNT_FILE = BASE_DIR / "account.json"
HOLDINGS_FILE = BASE_DIR / "holdings.json"
TX_FILE      = BASE_DIR / "cash_transactions.json"

sys.path.insert(0, str(BASE_DIR))
from utils import (  # noqa: E402
    LockBusy,
    atomic_write_json as _save_json,
    load_json_strict as _load_json_strict,
    process_lock,
)


def _load_required_dict(path: Path, label: str) -> dict:
    try:
        data = _load_json_strict(path)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{label} の読み込みに失敗: {e}") from e
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail=f"{label} が dict ではありません")
    return data


# ────────────────────────────────────────────────────────
# Models
# ────────────────────────────────────────────────────────

class CashCurrency(str, Enum):
    JPY = "JPY"
    USD = "USD"


class CashBroker(str, Enum):
    rakuten = "rakuten"   # → CASH_JPY / CASH_USD
    sbi     = "sbi"       # → CASH_JPY_SBI（USD は CASH_USD と共有）


class CashOwner(str, Enum):
    husband = "husband"
    wife = "wife"


class TxType(str, Enum):
    deposit  = "deposit"
    withdraw = "withdraw"


class CashRequest(BaseModel):
    currency:    CashCurrency
    amount:      float = Field(..., gt=0, description="正の金額。出金でも符号は付けない。")
    broker:      CashBroker = CashBroker.rakuten
    owner:       CashOwner = CashOwner.husband
    description: Optional[str] = None

    @field_validator("amount")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("amount は正の数")
        return round(v, 2)


class CashReconcileRequest(BaseModel):
    owner: CashOwner
    broker: CashBroker
    currency: CashCurrency
    reported_balance: float = Field(..., ge=0)
    reported_as_of: str
    source: str

    @field_validator("reported_as_of", "source")
    @classmethod
    def nonempty(cls, v: str) -> str:
        value = str(v or "").strip()
        if not value:
            raise ValueError("値は必須です")
        return value


# ────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────

def _holdings_key(currency: CashCurrency, broker: CashBroker, owner: CashOwner) -> str:
    """owner × broker × currency → exact cash row; undefined routes fail closed."""
    route = (owner.value, broker.value, currency.value)
    routes = {
        ("husband", "rakuten", "JPY"): "CASH_JPY",
        ("husband", "rakuten", "USD"): "CASH_USD",
        ("husband", "sbi", "JPY"): "CASH_JPY_SBI",
        ("wife", "sbi", "JPY"): "CASH_JPY_SBI_WIFE",
    }
    key = routes.get(route)
    if key is None:
        raise HTTPException(status_code=409, detail=f"未定義の現金ルートです: {route}")
    return key


def _recompute_total_cash(account: dict) -> int:
    """
    JPY 残高 + USD 残高 × FX レート → total_cash。
    SBI の円預かり金は holdings.json 側で管理するため total_cash には含めず、
    既存の portfolio_manager.build_portfolio_snapshot() が positions として加算する。
    """
    jpy = float(account.get("balance", 0) or 0)
    usd = float(account.get("usd_balance", 0) or 0)
    fx  = float(account.get("fx_rate_usdjpy", 150) or 150)
    return int(round(jpy + usd * fx))


def _recompute_jpy_equivalent_usd(account: dict) -> int:
    """USD 残高 × FX レートから派生値を再計算する。"""
    usd = float(account.get("usd_balance", 0) or 0)
    fx  = float(account.get("fx_rate_usdjpy", 150) or 150)
    return int(round(usd * fx))


def _sync_account_cash_totals(account: dict) -> None:
    account["jpy_equivalent_usd"] = _recompute_jpy_equivalent_usd(account)
    account["total_cash"] = _recompute_total_cash(account)


def _invalidate_portfolio_cache() -> None:
    """portfolio.py の 5 分スナップショットキャッシュをクリア。"""
    try:
        from api.routes import portfolio as _p
        _p._invalidate_cache()
    except Exception:
        pass


def _event_fx_rate(req: CashRequest, account: dict) -> Optional[float]:
    if req.currency != CashCurrency.USD:
        return None
    try:
        from utils import get_fx_rate_cached
        fx_for_event, _ = get_fx_rate_cached()
        return float(fx_for_event)
    except Exception as e:
        stale = account.get("fx_rate_usdjpy")
        if stale and 50 < float(stale) < 500:
            return float(stale)
        raise HTTPException(status_code=500, detail=f"USD 入出金の FX レート取得に失敗: {e}") from e


def _prepare_cash_change(req: CashRequest, tx_type: TxType) -> tuple[dict, dict, dict, dict, Optional[float]]:
    """JSON 書き込み前に全ファイルを読み、残高・holdings・ledger 入力を検証する。"""
    sign = 1 if tx_type == TxType.deposit else -1
    amount_signed = sign * req.amount

    original_account = _load_required_dict(ACCOUNT_FILE, "account.json")
    original_holdings = _load_required_dict(HOLDINGS_FILE, "holdings.json")
    original_tx_log = _load_required_dict(TX_FILE, "cash_transactions.json")

    account = deepcopy(original_account)
    holdings = deepcopy(original_holdings)
    tx_log = deepcopy(original_tx_log)

    # ── account.json 更新内容の検証 ──
    if req.currency == CashCurrency.JPY and req.broker == CashBroker.rakuten and req.owner == CashOwner.husband:
        # 楽天 JPY のみ account.balance に直接反映（既存実装の慣習）
        new_jpy = float(account.get("balance", 0) or 0) + amount_signed
        if new_jpy < 0:
            raise HTTPException(status_code=400, detail=f"楽天 JPY 残高不足（現在: {account.get('balance', 0)}, 要求: -{req.amount}）")
        account["balance"] = round(new_jpy, 2)
    elif req.currency == CashCurrency.USD and req.owner == CashOwner.husband and req.broker == CashBroker.rakuten:
        new_usd = float(account.get("usd_balance", 0) or 0) + amount_signed
        if new_usd < 0:
            raise HTTPException(status_code=400, detail=f"USD 残高不足（現在: {account.get('usd_balance', 0)}, 要求: -{req.amount}）")
        account["usd_balance"] = round(new_usd, 2)
    # SBI JPY は account には載せず holdings 側だけで管理（既存設計通り）

    _sync_account_cash_totals(account)
    account["last_updated"] = datetime.now().date().isoformat()

    # ── holdings.json 同期内容の検証 ──
    key = _holdings_key(req.currency, req.broker, req.owner)
    h = holdings.get(key)
    if not isinstance(h, dict):
        raise HTTPException(status_code=500, detail=f"holdings.json に {key} がありません")
    new_shares = float(h.get("shares", 0) or 0) + amount_signed
    if new_shares < 0:
        raise HTTPException(status_code=400, detail=f"{key} 残高不足（現在: {h.get('shares', 0)}）")
    h["shares"] = round(new_shares, 2)
    if key == "CASH_JPY_SBI_WIFE":
        h.setdefault("reported_balance_jpy", float(h.get("shares", 0) or 0) - amount_signed)
        h.setdefault("reported_as_of", "2026-05-12")
        h["ledger_delta_since_report_jpy"] = round(
            float(h.get("ledger_delta_since_report_jpy", 0) or 0) + amount_signed,
            2,
        )
        h["balance_status"] = "estimated"
        h["reconciliation_required"] = True
    holdings[key] = h

    # ── 監査ログ append ──
    txs = tx_log.get("transactions", []) if isinstance(tx_log, dict) else []
    if not isinstance(txs, list):
        raise HTTPException(status_code=500, detail="cash_transactions.json の transactions が list ではありません")

    new_tx = {
        "id":          f"tx_{uuid4().hex[:12]}",
        "timestamp":   datetime.now().isoformat(timespec="seconds"),
        "type":        tx_type.value,
        "currency":    req.currency.value,
        "broker":      req.broker.value,
        "owner":       req.owner.value,
        "cash_route":  key,
        "amount":      req.amount,
        "description": req.description or "",
        "new_balance_jpy": account.get("balance"),
        "new_balance_usd": account.get("usd_balance"),
        "new_total_cash":  account.get("total_cash"),
    }
    txs.append(new_tx)
    tx_log["transactions"] = txs
    fx_for_event = _event_fx_rate(req, account)

    return original_account, original_holdings, original_tx_log, {
        "account": account,
        "holdings": holdings,
        "tx_log": tx_log,
        "transaction": new_tx,
    }, fx_for_event


def _append_cash_flow_event(req: CashRequest, tx_type: TxType, tx: dict, fx_for_event: Optional[float]) -> None:
    from event_ledger import append_event

    append_event(
        event_type="cash_flow",
        direction=("in" if tx_type == TxType.deposit else "out"),
        quantity=req.amount,
        price=1.0,
        currency=req.currency.value,
        fx_rate_usdjpy=fx_for_event,
        account=req.broker.value,
        source="api",
        note=req.description or None,
        raw_payload={
            "owner": req.owner.value,
            "broker": req.broker.value,
            "cash_route": _holdings_key(req.currency, req.broker, req.owner),
        },
        event_id=tx["id"],  # cash tx id を event_id に流用（idempotency）
    )


def _commit_cash_change(
    *,
    req: CashRequest,
    tx_type: TxType,
    original_account: dict,
    original_holdings: dict,
    original_tx_log: dict,
    next_state: dict,
    fx_for_event: Optional[float],
) -> dict:
    """JSON 三点と event_ledger を一括更新する。ledger 失敗時は JSON を巻き戻す。"""
    try:
        _save_json(ACCOUNT_FILE, next_state["account"])
        _save_json(HOLDINGS_FILE, next_state["holdings"])
        _save_json(TX_FILE, next_state["tx_log"])
        _append_cash_flow_event(req, tx_type, next_state["transaction"], fx_for_event)
    except HTTPException:
        _save_json(ACCOUNT_FILE, original_account)
        _save_json(HOLDINGS_FILE, original_holdings)
        _save_json(TX_FILE, original_tx_log)
        raise
    except Exception as e:
        _save_json(ACCOUNT_FILE, original_account)
        _save_json(HOLDINGS_FILE, original_holdings)
        _save_json(TX_FILE, original_tx_log)
        raise HTTPException(status_code=500, detail=f"cash_flow 台帳反映に失敗したため変更を巻き戻しました: {e}") from e

    _invalidate_portfolio_cache()
    account = next_state["account"]

    return {
        "ok":              True,
        "transaction":     next_state["transaction"],
        "balance_jpy":     account.get("balance"),
        "balance_usd":     account.get("usd_balance"),
        "total_cash":      account.get("total_cash"),
    }


def _apply_cash_change(req: CashRequest, tx_type: TxType) -> dict:
    """
    実際に account.json / holdings.json / cash_transactions.json / event_ledger を更新する。
    書き込み前に全検証を済ませ、ledger 失敗時は JSON を巻き戻す。
    """
    try:
        with process_lock("portfolio_ledger"):
            original_account, original_holdings, original_tx_log, next_state, fx_for_event = _prepare_cash_change(req, tx_type)
            return _commit_cash_change(
                req=req,
                tx_type=tx_type,
                original_account=original_account,
                original_holdings=original_holdings,
                original_tx_log=original_tx_log,
                next_state=next_state,
                fx_for_event=fx_for_event,
            )
    except LockBusy as e:
        raise HTTPException(status_code=409, detail="portfolio ledger is busy") from e


# ────────────────────────────────────────────────────────
# Endpoints
# ────────────────────────────────────────────────────────

@router.post("/api/cash/deposit")
async def deposit(req: CashRequest):
    """現金入金を登録（口座残高 +、holdings 同期、監査ログ追加）"""
    return _apply_cash_change(req, TxType.deposit)


@router.post("/api/cash/withdraw")
async def withdraw(req: CashRequest):
    """現金出金を登録（口座残高 -、残高不足は 400）"""
    return _apply_cash_change(req, TxType.withdraw)


@router.post("/api/cash/reconcile")
async def reconcile_cash(req: CashReconcileRequest):
    """Replace an estimated cash balance with an externally confirmed snapshot."""
    key = _holdings_key(req.currency, req.broker, req.owner)
    if key != "CASH_JPY_SBI_WIFE":
        raise HTTPException(status_code=409, detail="現在は妻SBI JPYの推定台帳だけが照合対象です")
    try:
        with process_lock("portfolio_ledger"):
            holdings = _load_required_dict(HOLDINGS_FILE, "holdings.json")
            row = holdings.get(key)
            if not isinstance(row, dict):
                raise HTTPException(status_code=500, detail=f"{key} が見つかりません")
            row["reported_balance_jpy"] = round(req.reported_balance, 2)
            row["reported_as_of"] = req.reported_as_of
            row["ledger_delta_since_report_jpy"] = 0
            row["shares"] = round(req.reported_balance, 2)
            row["balance_status"] = "confirmed"
            row["reconciliation_required"] = False
            row["reconciliation_source"] = req.source
            row["reconciled_at"] = datetime.now().isoformat(timespec="seconds")
            _save_json(HOLDINGS_FILE, holdings)
            _invalidate_portfolio_cache()
            return {"ok": True, "cash_route": key, "balance": row["shares"], "status": "confirmed"}
    except LockBusy as exc:
        raise HTTPException(status_code=409, detail="portfolio ledger is busy") from exc


@router.get("/api/cash/transactions")
async def list_transactions(limit: int = Query(50, ge=1, le=500)):
    """直近の取引履歴（新しい順）"""
    log = _load_required_dict(TX_FILE, "cash_transactions.json")
    txs = log.get("transactions", []) if isinstance(log, dict) else []
    if not isinstance(txs, list):
        txs = []
    return {"transactions": list(reversed(txs))[:limit]}


@router.get("/api/cash/balances")
async def get_balances():
    """現在の残高サマリ"""
    account = _load_required_dict(ACCOUNT_FILE, "account.json")
    holdings = _load_required_dict(HOLDINGS_FILE, "holdings.json")

    sbi_jpy = 0
    wife_sbi_jpy = 0
    wife_status = None
    if isinstance(holdings, dict):
        h = holdings.get("CASH_JPY_SBI")
        if isinstance(h, dict):
            sbi_jpy = float(h.get("shares", 0) or 0)
        wife = holdings.get("CASH_JPY_SBI_WIFE")
        if isinstance(wife, dict):
            wife_sbi_jpy = float(wife.get("shares", 0) or 0)
            # The legacy ¥492,606 row is a 2026-05-12 snapshot.  Until an
            # explicit reconciliation is recorded it is an estimate, not
            # deployable buying power.
            wife_status = wife.get("balance_status") or "estimated"

    return {
        "balance_jpy_rakuten": account.get("balance", 0),
        "balance_jpy_sbi":     sbi_jpy,
        "balance_jpy_sbi_wife": wife_sbi_jpy,
        "balance_jpy_sbi_wife_status": wife_status,
        "balance_jpy_sbi_wife_available_for_new_buy": (
            wife_sbi_jpy if wife_status == "confirmed" else 0
        ),
        "balance_usd":         account.get("usd_balance", 0),
        "fx_rate_usdjpy":      account.get("fx_rate_usdjpy"),
        "total_cash_jpy":      _recompute_total_cash(account),
        "last_updated":        account.get("last_updated"),
    }
