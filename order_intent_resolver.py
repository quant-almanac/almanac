"""Resolve recommendation lifecycle and broker-order intent without guessing.

``action_state.json`` tracks the lifecycle of an AI recommendation while
``action_executions.json`` tracks a user-reported broker order/fill.  They are
related, but neither file can safely overwrite the other.  This module builds a
read model used by the analyst and Today API:

* a terminal recommendation may be proposed again;
* a linked ``ordered`` execution remains an execution-safety conflict until the
  user explicitly records it as cancelled or filled;
* a later fill supersedes an older ordered row with the same action_state_id.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
import json


FILL_STATUSES = {"executed", "filled", "done", "partial"}
ACTIVE_EXECUTION_STATUSES = {"ordered"}
TERMINAL_RECOMMENDATION_STATUSES = {"cancelled", "expired"}


def _time_key(record: dict) -> str:
    return str(record.get("saved_at") or record.get("executed_at_time") or "")


def _load_actions(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    actions = raw.get("actions") if isinstance(raw, dict) else {}
    if isinstance(actions, dict):
        return {str(k): v for k, v in actions.items() if isinstance(v, dict)}
    if isinstance(actions, list):
        return {
            str(row.get("id") or i): row
            for i, row in enumerate(actions)
            if isinstance(row, dict)
        }
    return {}


def _within_days(record: dict, *, days: int, now: datetime) -> bool:
    raw = _time_key(record)
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = (
                ts.astimezone(now.tzinfo).replace(tzinfo=None)
                if now.tzinfo is not None
                else ts.astimezone().replace(tzinfo=None)
            )
    except Exception:
        return False
    return ts >= now.replace(tzinfo=None) - timedelta(days=days)


def resolve_recent_order_intents(
    execution_rows: Iterable[dict],
    *,
    action_state_path: Path,
    days: int = 7,
    now: datetime | None = None,
) -> tuple[list[dict], list[dict]]:
    """Return ``(effective_rows, conflicts)`` for recommendation generation.

    ``effective_rows`` participate in DONE_LIST/open-order sizing.  ``conflicts``
    do not suppress re-proposal, but must block an execution control until the
    linked execution is explicitly reconciled.
    """
    now = now or datetime.now()
    rows = [dict(row) for row in execution_rows if isinstance(row, dict)]
    rows = [row for row in rows if _within_days(row, days=days, now=now)]
    actions = _load_actions(action_state_path)

    # A later fill is authoritative for the same order intent.  Keep the fill
    # and drop the stale ordered representation from both active and conflict.
    filled_at_by_state: dict[str, str] = {}
    for row in rows:
        sid = str(row.get("action_state_id") or "")
        status = str(row.get("status") or "").lower()
        if sid and status in FILL_STATUSES:
            filled_at_by_state[sid] = max(filled_at_by_state.get(sid, ""), _time_key(row))

    effective: list[dict] = []
    conflicts: list[dict] = []
    for row in rows:
        status = str(row.get("status") or "").lower()
        if status not in ACTIVE_EXECUTION_STATUSES | FILL_STATUSES:
            continue
        sid = str(row.get("action_state_id") or "")
        if (
            status == "ordered"
            and sid
            and filled_at_by_state.get(sid, "") >= _time_key(row)
        ):
            continue

        state_entry = actions.get(sid) if sid else None
        state_status = str((state_entry or {}).get("status") or "").lower()
        if status == "ordered" and state_status in TERMINAL_RECOMMENDATION_STATUSES:
            conflict = dict(row)
            conflict.update({
                "order_state_conflict": True,
                "recommendation_status": state_status,
                "recommendation_expire_reason": (state_entry or {}).get("expire_reason"),
                "resolution_required": "confirm_broker_order_status",
            })
            conflicts.append(conflict)
            continue

        if status == "ordered" and state_status == "filled":
            # The recommendation projection says the order filled even if an
            # old client failed to append a second execution row.  Preserve the
            # DONE_LIST behavior without mutating historical JSON.
            projected = dict(row)
            projected["status"] = "filled"
            projected["status_projected_from_action_state"] = True
            effective.append(projected)
            continue

        effective.append(row)

    effective.sort(key=_time_key, reverse=True)
    conflicts.sort(key=_time_key, reverse=True)
    return effective, conflicts


def flag_stale_ordered_executions(
    execution_rows: Iterable[dict],
    *,
    max_business_days: int,
    business_days_since,
) -> list[dict]:
    """Return warnings for old ordered rows without changing their status."""
    warnings: list[dict] = []
    for row in execution_rows:
        if not isinstance(row, dict) or str(row.get("status") or "").lower() != "ordered":
            continue
        ts = _time_key(row)
        if not ts:
            continue
        age = int(business_days_since(ts))
        if age > max_business_days:
            warnings.append({
                "execution_id": row.get("id"),
                "action_state_id": row.get("action_state_id"),
                "ticker": row.get("ticker"),
                "direction": row.get("direction"),
                "business_days_ordered": age,
                "reason": "stale_order_requires_confirmation",
            })
    return warnings
