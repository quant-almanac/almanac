"""Approved discretionary-investment contribution ledger.

The execution plan must never treat a brokerage cash balance as permission to
buy.  This small append-only-style JSON ledger holds the *separate* user
approval for salary/bonus money that may be deployed by ALMANAC.

Amounts are stored in JPY because the plan budget is JPY-denominated.  A
contribution is released over whole calendar months; released but unused money
remains available until it is consumed by an execution explicitly linked to
that contribution.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from utils import atomic_write_json, load_json


BASE_DIR = Path(__file__).parent
LEDGER_FILE = BASE_DIR / "contribution_ledger.json"
SCHEMA_VERSION = 1
FILLED_STATUSES = {"executed", "partial", "filled", "done"}
OPEN_STATUSES = {"ordered"}


def empty_ledger() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "contributions": []}


def load_ledger(path: Path = LEDGER_FILE) -> dict[str, Any]:
    value = load_json(path, default={}) or {}
    if not isinstance(value, dict):
        return empty_ledger()
    rows = value.get("contributions")
    if not isinstance(rows, list):
        return empty_ledger()
    return {
        "schema_version": int(value.get("schema_version") or SCHEMA_VERSION),
        "contributions": [row for row in rows if isinstance(row, dict)],
    }


def save_ledger(ledger: dict[str, Any], path: Path = LEDGER_FILE) -> None:
    payload = {
        "schema_version": SCHEMA_VERSION,
        "contributions": [
            row for row in (ledger.get("contributions") or []) if isinstance(row, dict)
        ],
    }
    atomic_write_json(path, payload)


def _jpy(value: Any) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def _month_key(value: date | datetime | str) -> str:
    if isinstance(value, datetime):
        return value.strftime("%Y-%m")
    if isinstance(value, date):
        return value.strftime("%Y-%m")
    text = str(value or "").strip()
    if len(text) >= 7 and text[:4].isdigit() and text[4] == "-" and text[5:7].isdigit():
        return text[:7]
    raise ValueError(f"invalid month: {value!r}")


def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7]) - 1


def _add_months(month: str, offset: int) -> str:
    idx = _month_index(month) + offset
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def release_schedule(contribution: dict[str, Any]) -> dict[str, int]:
    """Return the deterministic month -> JPY release schedule for one row."""
    amount = _jpy(contribution.get("amount_jpy"))
    months = max(1, min(24, _jpy(contribution.get("release_months")) or 1))
    try:
        start_month = _month_key(contribution.get("start_month"))
    except ValueError:
        return {}
    base, remainder = divmod(amount, months)
    return {
        _add_months(start_month, index): base + (1 if index < remainder else 0)
        for index in range(months)
    }


def _execution_notional_jpy(record: dict[str, Any], fx_rate: float) -> int:
    for key in ("notional_jpy", "executed_amount_jpy", "estimated_notional_jpy", "amount_jpy"):
        amount = _jpy(record.get(key))
        if amount > 0:
            return amount
    try:
        quantity = float(record.get("quantity"))
        price = float(record.get("price"))
    except (TypeError, ValueError):
        return 0
    if quantity <= 0 or price <= 0:
        return 0
    currency = str(record.get("currency") or "").upper()
    return _jpy(quantity * price * (fx_rate if currency == "USD" else 1.0))


def _direction_is_buy(record: dict[str, Any]) -> bool:
    direction = str(record.get("direction") or record.get("action_type") or record.get("type") or "").lower()
    return direction in {"buy", "add", "dca", "margin_buy"}


def _record_month(record: dict[str, Any], status: str) -> str | None:
    keys = ("executed_at_time", "filled_at", "saved_at") if status in FILLED_STATUSES else ("placed_at", "saved_at")
    for key in keys:
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            return _month_key(str(value))
        except ValueError:
            continue
    return None


def summarize_contributions(
    ledger: dict[str, Any] | None,
    executions: dict[str, Any] | list[dict[str, Any]] | None,
    *,
    month: str | date | datetime,
    fx_rate: float = 150.0,
) -> dict[str, Any]:
    """Summarize released, consumed and available approved funding.

    Only executions explicitly carrying ``contribution_id`` consume an
    approved contribution.  This avoids retroactively guessing the funding of
    historical trades; records without the id remain an audit warning in the
    execution-plan migration rather than silently draining newly approved cash.
    """
    target_month = _month_key(month)
    rows = (ledger or {}).get("contributions") if isinstance(ledger, dict) else []
    rows = rows if isinstance(rows, list) else []
    known: dict[str, dict[str, Any]] = {}
    released_by_id: dict[str, int] = {}
    released_month_by_id: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or str(row.get("status") or "approved") != "approved":
            continue
        contribution_id = str(row.get("id") or "")
        if not contribution_id:
            continue
        schedule = release_schedule(row)
        released_by_id[contribution_id] = sum(
            amount for release_month, amount in schedule.items() if release_month <= target_month
        )
        released_month_by_id[contribution_id] = _jpy(schedule.get(target_month))
        known[contribution_id] = row

    if isinstance(executions, dict):
        execution_rows = executions.get("executions") or []
    else:
        execution_rows = executions or []
    filled_by_id: dict[str, int] = defaultdict(int)
    reserved_by_id: dict[str, int] = defaultdict(int)
    filled_this_month_by_id: dict[str, int] = defaultdict(int)
    reserved_this_month_by_id: dict[str, int] = defaultdict(int)
    unpriced_by_id: dict[str, int] = defaultdict(int)
    for row in execution_rows:
        if not isinstance(row, dict) or not _direction_is_buy(row):
            continue
        contribution_id = str(row.get("contribution_id") or "")
        if contribution_id not in known:
            continue
        status = str(row.get("status") or "").lower()
        if status not in FILLED_STATUSES | OPEN_STATUSES:
            continue
        notional = _execution_notional_jpy(row, fx_rate)
        if notional <= 0:
            unpriced_by_id[contribution_id] += 1
            continue
        if status in FILLED_STATUSES:
            filled_by_id[contribution_id] += notional
            if _record_month(row, status) == target_month:
                filled_this_month_by_id[contribution_id] += notional
        else:
            reserved_by_id[contribution_id] += notional
            if _record_month(row, status) == target_month:
                reserved_this_month_by_id[contribution_id] += notional

    sources: list[dict[str, Any]] = []
    totals: dict[str, int] = defaultdict(int)
    for contribution_id, row in known.items():
        released = released_by_id.get(contribution_id, 0)
        filled = filled_by_id.get(contribution_id, 0)
        reserved = reserved_by_id.get(contribution_id, 0)
        available = max(0, released - filled - reserved)
        bucket = str(row.get("bucket") or "normal").lower()
        if bucket not in {"normal", "opportunity"}:
            bucket = "normal"
        totals[f"released_{bucket}_jpy"] += released
        totals[f"released_this_month_{bucket}_jpy"] += released_month_by_id.get(contribution_id, 0)
        totals[f"filled_{bucket}_jpy"] += filled
        totals[f"reserved_{bucket}_jpy"] += reserved
        totals[f"filled_this_month_{bucket}_jpy"] += filled_this_month_by_id.get(contribution_id, 0)
        totals[f"reserved_this_month_{bucket}_jpy"] += reserved_this_month_by_id.get(contribution_id, 0)
        totals[f"available_{bucket}_jpy"] += available
        sources.append({
            "id": contribution_id,
            "source": str(row.get("source") or "other"),
            "bucket": bucket,
            "owner": row.get("owner"),
            "broker": row.get("broker"),
            "currency": "JPY",
            "start_month": row.get("start_month"),
            "release_months": max(1, _jpy(row.get("release_months")) or 1),
            "amount_jpy": _jpy(row.get("amount_jpy")),
            "released_this_month_jpy": released_month_by_id.get(contribution_id, 0),
            "released_to_date_jpy": released,
            "filled_jpy": filled,
            "reserved_jpy": reserved,
            "filled_this_month_jpy": filled_this_month_by_id.get(contribution_id, 0),
            "reserved_this_month_jpy": reserved_this_month_by_id.get(contribution_id, 0),
            "available_jpy": available,
            "unpriced_execution_count": unpriced_by_id.get(contribution_id, 0),
            "note": str(row.get("note") or ""),
        })

    for key in (
        "released_normal_jpy", "released_opportunity_jpy",
        "released_this_month_normal_jpy", "released_this_month_opportunity_jpy",
        "filled_normal_jpy", "filled_opportunity_jpy",
        "reserved_normal_jpy", "reserved_opportunity_jpy",
        "filled_this_month_normal_jpy", "filled_this_month_opportunity_jpy",
        "reserved_this_month_normal_jpy", "reserved_this_month_opportunity_jpy",
        "available_normal_jpy", "available_opportunity_jpy",
    ):
        totals[key] = _jpy(totals.get(key))
    return {
        "month": target_month,
        "approved_contribution_count": len(sources),
        "sources": sorted(sources, key=lambda row: (str(row.get("start_month") or ""), str(row.get("id"))), reverse=True),
        **dict(totals),
        "released_this_month_jpy": totals["released_this_month_normal_jpy"] + totals["released_this_month_opportunity_jpy"],
        "released_to_date_jpy": totals["released_normal_jpy"] + totals["released_opportunity_jpy"],
        "available_jpy": totals["available_normal_jpy"] + totals["available_opportunity_jpy"],
        "consumed_this_month_jpy": (
            totals["filled_this_month_normal_jpy"] + totals["filled_this_month_opportunity_jpy"]
            + totals["reserved_this_month_normal_jpy"] + totals["reserved_this_month_opportunity_jpy"]
        ),
        "unpriced_execution_count": sum(unpriced_by_id.values()),
    }
