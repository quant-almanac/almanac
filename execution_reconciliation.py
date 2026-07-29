"""Broker-backed execution reconciliation and route-correction overlay.

Raw execution records remain immutable audit evidence.  Phase-1 corrections
may change only ``execution_owner``, ``execution_broker`` and
``execution_account``.  Quantity, price and trade date require a separate
correction type and therefore fail closed here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from execution_safety import (
    SYSTEM_LOCAL_TZ,
    action_account,
    canonical_account,
    canonical_broker,
    canonical_owner,
    effective_execution_timestamp,
    explicit_action_broker,
    explicit_action_owner,
    parse_timestamp,
)
from utils import atomic_write_json, process_lock


BASE_DIR = Path(__file__).parent
STATE_PATH = BASE_DIR / "execution_reconciliation_state.json"
SCHEMA_VERSION = 1
ROUTE_FIELDS = ("execution_owner", "execution_broker", "execution_account")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _state_path(path: Optional[Path] = None) -> Path:
    if path is not None:
        return Path(path)
    state_root = os.environ.get("ALMANAC_STATE_DIR")
    if state_root:
        return Path(state_root) / STATE_PATH.name
    return STATE_PATH


def execution_record_hash(record: dict) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def canonical_route(record: dict) -> dict[str, str]:
    return {
        "execution_owner": canonical_owner(
            record.get("execution_owner")
            or record.get("target_owner")
            or record.get("owner")
            or explicit_action_owner(record)
        ),
        "execution_broker": canonical_broker(
            record.get("execution_broker")
            or record.get("target_broker")
            or record.get("broker")
            or explicit_action_broker(record)
        ),
        "execution_account": canonical_account(
            record.get("execution_account")
            or record.get("target_account")
            or record.get("account")
            or record.get("account_type")
            or action_account(record)
        ),
    }


def _load_state(path: Optional[Path] = None) -> dict:
    resolved = _state_path(path)
    if not resolved.exists():
        return {"schema_version": SCHEMA_VERSION, "corrections": []}
    try:
        state = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "corrections": []}
    if not isinstance(state, dict) or not isinstance(state.get("corrections"), list):
        return {"schema_version": SCHEMA_VERSION, "corrections": []}
    return state


def _active_corrections(state: dict, execution_id: str) -> list[dict]:
    rows = [
        row
        for row in state.get("corrections", [])
        if isinstance(row, dict)
        and row.get("correction_type") == "route"
        and row.get("execution_id") == execution_id
    ]
    superseded = {
        str(row.get("supersedes_correction_id"))
        for row in rows
        if row.get("supersedes_correction_id")
    }
    return [row for row in rows if str(row.get("correction_id")) not in superseded]


def record_route_correction(
    *,
    execution_record: dict,
    corrected_route: dict,
    evidence: dict,
    reason: str,
    approved_by: str,
    supersedes_correction_id: Optional[str] = None,
    state_path: Optional[Path] = None,
) -> dict:
    """Append an auditable route correction without rewriting raw execution.

    A second correction must explicitly supersede the sole active correction.
    This prevents two concurrent overlays from silently winning by file order.
    """
    execution_id = str(execution_record.get("id") or "").strip()
    if not execution_id:
        raise ValueError("execution_record.id is required")
    if not approved_by:
        raise ValueError("approved_by is required")
    if not reason:
        raise ValueError("reason is required")
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("broker evidence is required")

    expected = canonical_route(execution_record)
    corrected = {
        "execution_owner": canonical_owner(corrected_route.get("execution_owner")),
        "execution_broker": canonical_broker(corrected_route.get("execution_broker")),
        "execution_account": canonical_account(corrected_route.get("execution_account")),
    }
    if any(not corrected[field] for field in ROUTE_FIELDS):
        raise ValueError("corrected route must include owner, broker and account")
    unsupported = set(corrected_route) - set(ROUTE_FIELDS)
    if unsupported:
        raise ValueError(
            "route correction cannot change quantity/price/trade_date or other fields: "
            + ", ".join(sorted(unsupported))
        )

    entry = {
        "schema_version": SCHEMA_VERSION,
        "correction_id": uuid4().hex,
        "correction_type": "route",
        "execution_id": execution_id,
        "base_record_hash": execution_record_hash(execution_record),
        "expected_route": expected,
        "corrected_route": corrected,
        "evidence": evidence,
        "reason": reason,
        "approved_by": approved_by,
        "recorded_at": _now_iso(),
        "supersedes_correction_id": supersedes_correction_id,
    }

    resolved = _state_path(state_path)
    with process_lock("execution_reconciliation", timeout=10):
        state = _load_state(resolved)
        active = _active_corrections(state, execution_id)
        if active:
            if len(active) != 1:
                raise RuntimeError("multiple active route corrections; manual review required")
            current_id = str(active[0].get("correction_id") or "")
            if supersedes_correction_id != current_id:
                raise ValueError(
                    "an active route correction exists; supersedes_correction_id is required"
                )
        elif supersedes_correction_id:
            raise ValueError("supersedes_correction_id does not reference an active correction")
        state.setdefault("corrections", []).append(entry)
        state["schema_version"] = SCHEMA_VERSION
        state["last_updated"] = _now_iso()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_json(resolved, state)
    return entry


def resolve_effective_execution_record(
    record: dict,
    *,
    state_path: Optional[Path] = None,
) -> dict:
    """Return the raw record plus the sole valid route overlay, if any.

    Resolution never mutates the caller's object.  Ambiguous corrections or a
    base-record hash mismatch produce ``execution_reconciliation_status=review``
    and leave all route fields unchanged.
    """
    result = dict(record)
    execution_id = str(record.get("id") or "").strip()
    if not execution_id:
        result["execution_reconciliation_status"] = "review"
        result["execution_reconciliation_reasons"] = ["missing_execution_id"]
        return result

    active = _active_corrections(_load_state(state_path), execution_id)
    if not active:
        result["execution_reconciliation_status"] = "clean"
        return result
    if len(active) != 1:
        result["execution_reconciliation_status"] = "review"
        result["execution_reconciliation_reasons"] = ["multiple_active_route_corrections"]
        return result

    correction = active[0]
    if correction.get("base_record_hash") != execution_record_hash(record):
        result["execution_reconciliation_status"] = "review"
        result["execution_reconciliation_reasons"] = ["base_record_hash_mismatch"]
        return result
    if correction.get("expected_route") != canonical_route(record):
        result["execution_reconciliation_status"] = "review"
        result["execution_reconciliation_reasons"] = ["expected_route_mismatch"]
        return result

    corrected = correction.get("corrected_route")
    if not isinstance(corrected, dict) or set(corrected) != set(ROUTE_FIELDS):
        result["execution_reconciliation_status"] = "review"
        result["execution_reconciliation_reasons"] = ["invalid_corrected_route"]
        return result
    for field in ROUTE_FIELDS:
        result[field] = corrected[field]
    result["execution_reconciliation_status"] = "corrected"
    result["execution_reconciliation_correction_id"] = correction.get("correction_id")
    result["execution_reconciliation_evidence"] = correction.get("evidence")
    return result


def load_effective_execution_records(
    *,
    base_dir: Optional[Path] = None,
    execution_path: Optional[Path] = None,
    state_path: Optional[Path] = None,
) -> list[dict]:
    """Load execution rows with the route-correction overlay applied.

    This is the read-side boundary for route-sensitive consumers.  The raw
    ledger remains the write/audit authority; callers receive copies and must
    treat ``execution_reconciliation_status == "review"`` as unattributed.
    """
    root = Path(base_dir) if base_dir is not None else BASE_DIR
    source = Path(execution_path) if execution_path is not None else root / "action_executions.json"
    overlay = (
        Path(state_path)
        if state_path is not None
        else root / STATE_PATH.name
    )
    if not source.exists():
        return []
    overlay_invalid = False
    if overlay.exists():
        try:
            overlay_payload = json.loads(overlay.read_text(encoding="utf-8"))
            overlay_invalid = not (
                isinstance(overlay_payload, dict)
                and isinstance(overlay_payload.get("corrections"), list)
            )
        except (OSError, json.JSONDecodeError):
            overlay_invalid = True
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    rows = payload.get("executions") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return []
    effective: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not str(row.get("id") or "").strip():
            legacy = dict(row)
            legacy["execution_reconciliation_status"] = "clean_legacy_no_execution_id"
            effective.append(legacy)
            continue
        if overlay_invalid:
            unresolved = dict(row)
            unresolved["execution_reconciliation_status"] = "review"
            unresolved["execution_reconciliation_reasons"] = [
                "reconciliation_state_invalid"
            ]
            effective.append(unresolved)
            continue
        effective.append(resolve_effective_execution_record(row, state_path=overlay))
    return effective


def _parse_date(value: object) -> Optional[date]:
    if value in (None, ""):
        return None
    text = str(value).strip().replace("/", "-")
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def classify_temporal_order(
    *,
    snapshot_as_of: object,
    trade_date: object = None,
    trade_timestamp: object = None,
) -> dict:
    """Classify a broker fill relative to a broker snapshot.

    Exact timestamps are compared when both sides contain a time.  Broker rows
    with only ``trade_date`` follow the explicit three-way date contract:
    before -> safe, after -> reconciliation required, same day -> unknown.
    """
    snapshot_has_time = "T" in str(snapshot_as_of or "") or " " in str(snapshot_as_of or "")
    trade_has_time = "T" in str(trade_timestamp or "") or " " in str(trade_timestamp or "")
    snapshot_dt = (
        parse_timestamp(snapshot_as_of, naive_tz=SYSTEM_LOCAL_TZ)
        if snapshot_has_time else None
    )
    trade_dt = (
        parse_timestamp(trade_timestamp, naive_tz=SYSTEM_LOCAL_TZ)
        if trade_has_time else None
    )
    if snapshot_dt is not None and trade_dt is not None:
        if trade_dt < snapshot_dt:
            status = "before_snapshot"
        elif trade_dt > snapshot_dt:
            status = "after_snapshot"
        else:
            status = "temporal_order_unknown"
        return {
            "temporal_order": status,
            "requires_review": status != "before_snapshot",
            "comparison_basis": "exact_timestamp",
        }

    snapshot_date = _parse_date(snapshot_as_of)
    broker_date = _parse_date(trade_date)
    if snapshot_date is not None and broker_date is not None:
        if broker_date < snapshot_date:
            status = "before_snapshot"
        elif broker_date > snapshot_date:
            status = "after_snapshot"
        else:
            status = "temporal_order_unknown"
        return {
            "temporal_order": status,
            "requires_review": status != "before_snapshot",
            "comparison_basis": "date_only",
        }
    return {
        "temporal_order": "temporal_order_unknown",
        "requires_review": True,
        "comparison_basis": "insufficient_timestamp",
    }


def execution_temporal_order(record: dict, snapshot_as_of: object) -> dict:
    timestamp, source = effective_execution_timestamp(record)
    trade_date = record.get("trade_date")
    if not trade_date and timestamp is not None:
        trade_date = timestamp.astimezone(SYSTEM_LOCAL_TZ).date().isoformat()
    result = classify_temporal_order(
        snapshot_as_of=snapshot_as_of,
        trade_date=trade_date,
        trade_timestamp=timestamp.isoformat() if timestamp else None,
    )
    result["execution_timestamp_source"] = source
    return result
