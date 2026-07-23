"""Explicit salary/bonus approvals for ALMANAC discretionary investment funds."""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

from contribution_ledger import (  # noqa: E402
    load_ledger,
    save_ledger,
    summarize_contributions,
)
from utils import LockBusy, process_lock  # noqa: E402


class ContributionSource(str, Enum):
    salary = "salary"
    bonus = "bonus"
    other = "other"


class ContributionBucket(str, Enum):
    normal = "normal"
    opportunity = "opportunity"


class ContributionOwner(str, Enum):
    husband = "husband"
    wife = "wife"


class ContributionBroker(str, Enum):
    rakuten = "rakuten"
    sbi = "sbi"


class ContributionApprovalRequest(BaseModel):
    source: ContributionSource
    amount_jpy: int = Field(..., gt=0, le=10_000_000)
    bucket: ContributionBucket = ContributionBucket.normal
    owner: ContributionOwner = ContributionOwner.husband
    broker: ContributionBroker = ContributionBroker.rakuten
    # Omitted means the current calendar month, never an anticipated deposit.
    start_month: str | None = None
    release_months: int | None = Field(default=None, ge=1, le=24)
    confirmed_at: str | None = None
    note: str = ""
    idempotency_key: str = Field(..., min_length=8, max_length=200)

    @field_validator("start_month")
    @classmethod
    def validate_start_month(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        text = str(value).strip()
        try:
            date.fromisoformat(f"{text}-01")
        except ValueError as exc:
            raise ValueError("start_month は YYYY-MM") from exc
        return text

    @field_validator("confirmed_at")
    @classmethod
    def validate_confirmed_at(cls, value: str | None) -> str | None:
        if value is None or not str(value).strip():
            return None
        try:
            datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("confirmed_at は ISO 時刻で指定してください") from exc
        return str(value)

    @field_validator("note")
    @classmethod
    def normalize_note(cls, value: str) -> str:
        return str(value or "").strip()[:500]

    @field_validator("idempotency_key")
    @classmethod
    def normalize_idempotency_key(cls, value: str) -> str:
        text = str(value or "").strip()
        if len(text) < 8:
            raise ValueError("idempotency_key は8文字以上で指定してください")
        return text


def _current_month() -> str:
    return datetime.now().astimezone().strftime("%Y-%m")


def _executions() -> dict:
    from utils import load_json

    value = load_json(BASE_DIR / "action_executions.json", default={}) or {}
    return value if isinstance(value, dict) else {"executions": []}


def _refresh_plan() -> str | None:
    """Update the read-only Today plan after a user explicitly approves funds."""
    try:
        from execution_plan_engine import generate_execution_plan

        generate_execution_plan(base_dir=BASE_DIR, write=True)
        return None
    except Exception as exc:
        # The approval is authoritative and has already been saved.  Do not
        # roll it back merely because a derived display artifact could not be
        # refreshed; the next morning analysis will regenerate it.
        return f"承認は保存されました。計画表示の更新は次回分析で反映されます: {exc}"


@router.get("/api/contributions")
async def get_contributions():
    ledger = load_ledger(BASE_DIR / "contribution_ledger.json")
    return {
        "ok": True,
        "ledger": ledger,
        "summary": summarize_contributions(ledger, _executions(), month=_current_month()),
    }


@router.post("/api/contributions/approve")
async def approve_contribution(req: ContributionApprovalRequest):
    """Record a user-confirmed investable salary/bonus amount.

    This endpoint intentionally accepts no transfer/sale/borrowing source.
    Registering an approval does not mutate cash balances; actual execution is
    still bounded by the routed account's confirmed cash and execution safety.
    """
    now = datetime.now().astimezone()
    release_months = req.release_months
    if release_months is None:
        release_months = 4 if req.source == ContributionSource.bonus else 1
    request_payload = {
        "source": req.source.value,
        "amount_jpy": int(req.amount_jpy),
        "bucket": req.bucket.value,
        "owner": req.owner.value,
        "broker": req.broker.value,
        "start_month": req.start_month or now.strftime("%Y-%m"),
        "release_months": int(release_months),
        "confirmed_at": req.confirmed_at or "",
        "note": req.note,
    }
    request_hash = hashlib.sha256(
        json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    record = {
        "id": f"contribution_{uuid4().hex}",
        "source": req.source.value,
        "bucket": req.bucket.value,
        "owner": req.owner.value,
        "broker": req.broker.value,
        "currency": "JPY",
        "amount_jpy": int(req.amount_jpy),
        "start_month": req.start_month or now.strftime("%Y-%m"),
        "release_months": int(release_months),
        "confirmed_at": req.confirmed_at or now.isoformat(timespec="seconds"),
        "approved_at": now.isoformat(timespec="seconds"),
        "status": "approved",
        "note": req.note,
        "idempotency_key": req.idempotency_key,
        "approval_request_hash": request_hash,
    }
    replay_record: dict | None = None
    try:
        with process_lock("contribution_ledger"):
            ledger = load_ledger(BASE_DIR / "contribution_ledger.json")
            contributions = ledger.setdefault("contributions", [])
            if not isinstance(contributions, list):
                raise HTTPException(status_code=500, detail="contribution_ledger.json の contributions が list ではありません")
            for existing in contributions:
                if not isinstance(existing, dict) or existing.get("idempotency_key") != req.idempotency_key:
                    continue
                if existing.get("approval_request_hash") != request_hash:
                    raise HTTPException(status_code=409, detail="同じidempotency_keyが異なる追加資金payloadで使用されています")
                replay_record = existing
                break
            if replay_record is None:
                contributions.append(record)
                save_ledger(ledger, BASE_DIR / "contribution_ledger.json")
    except LockBusy as exc:
        raise HTTPException(status_code=409, detail="contribution ledger is busy") from exc

    ledger = load_ledger(BASE_DIR / "contribution_ledger.json")
    if replay_record is not None:
        return {
            "ok": True,
            "contribution": replay_record,
            "summary": summarize_contributions(ledger, _executions(), month=_current_month()),
            "warning": None,
            "idempotent_replay": True,
        }
    refresh_warning = _refresh_plan()
    return {
        "ok": True,
        "contribution": record,
        "summary": summarize_contributions(ledger, _executions(), month=_current_month()),
        "warning": refresh_warning,
        "idempotent_replay": False,
    }
