"""Fail-closed contracts for paced capital deployment.

This module contains no broker mutation.  It makes cash reservations and the
promotion history of the canonical drawdown controller explicit.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable

from almanac.runtime_config import resolve_db_path
from instrument_metadata import canonical_ticker


DD_PACING_MULTIPLIERS: dict[str, float] = {
    "ok": 1.0, "caution": 0.5, "block": 0.25, "derisk_review": 0.25,
    "freeze": 0.0, "objective_breach": 0.0,
}


def _number(value: Any) -> int:
    try:
        return max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return 0


def active_operational_reservations(rows: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        reservation_id = str(row.get("reservation_id") or "")
        if not reservation_id or reservation_id in seen:
            continue
        seen.add(reservation_id)
        if str(row.get("status") or "active") != "active" or row.get("reflected_in_confirmed_cash") is True:
            continue
        if row.get("deployment_assignment_id"):
            continue
        amount = _number(row.get("amount_jpy"))
        if amount > 0:
            out.append({**row, "amount_jpy": amount})
    return out


def operational_reserve_summary(rows: Iterable[dict[str, Any]] | None) -> dict[str, Any]:
    active = active_operational_reservations(rows)
    by_wallet: dict[str, int] = {}
    for row in active:
        key = str(row.get("wallet_key") or "")
        if key:
            by_wallet[key] = by_wallet.get(key, 0) + _number(row.get("amount_jpy"))
    return {
        "operational_reservations": active,
        "operational_reserve_jpy": sum(_number(row.get("amount_jpy")) for row in active),
        "operational_reserve_by_wallet_jpy": by_wallet,
        "reservation_count": len(active),
    }


def wallet_available_after_reservations(wallets: Iterable[dict[str, Any]] | None, reservations: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    reserved = operational_reserve_summary(reservations)["operational_reserve_by_wallet_jpy"]
    return [
        {
            **wallet,
            "operational_reserved_jpy": reserved.get(str(wallet.get("wallet_key") or ""), 0),
            "available_after_operational_reservations_jpy": max(
                0, _number(wallet.get("available_jpy")) - reserved.get(str(wallet.get("wallet_key") or ""), 0)
            ),
        }
        for wallet in wallets or [] if isinstance(wallet, dict)
    ]


def _read_promotion_history(db_path: Path) -> tuple[list[dict[str, Any]], str]:
    if not db_path.exists():
        return [], "ledger_missing"
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT event_id, occurred_at, raw_payload FROM ledger_events WHERE event_type = ? ORDER BY occurred_at ASC, id ASC",
                ("drawdown_controller_promoted",),
            ).fetchall()
        finally:
            conn.close()
    except (sqlite3.Error, OSError):
        return [], "ledger_unreadable"
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row["raw_payload"]) if isinstance(row["raw_payload"], str) else row["raw_payload"]
        except json.JSONDecodeError:
            payload = None
        out.append({"event_id": row["event_id"], "occurred_at": row["occurred_at"], "payload": payload})
    return out, "ok"


def resolve_drawdown_pacing(*, base_dir: Path, db_path: Path | None = None) -> dict[str, Any]:
    base_dir = Path(base_dir)
    history, ledger_status = _read_promotion_history(Path(db_path) if db_path is not None else resolve_db_path(base_dir))
    try:
        raw = json.loads((base_dir / "drawdown_state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raw = None
    valid_enabled = isinstance(raw, dict) and raw.get("enforcement_enabled") is True
    if ledger_status == "ledger_unreadable":
        return {"dd_stage": "controller_fault", "dd_pacing_multiplier": 0.0, "dd_pacing_source": "controller_fault", "dd_enforcement_active": False, "requires_human_acknowledgement": True, "promotion_history_status": "ledger_unreadable"}
    if not history and not valid_enabled:
        return {"dd_stage": "data_confidence_caution", "dd_pacing_multiplier": 1.0, "dd_pacing_source": "prepromotion", "dd_enforcement_active": False, "requires_human_acknowledgement": True, "promotion_history_status": "never_promoted"}
    if not history or not valid_enabled:
        return {"dd_stage": "controller_fault", "dd_pacing_multiplier": 0.0, "dd_pacing_source": "controller_fault", "dd_enforcement_active": False, "requires_human_acknowledgement": True, "promotion_history_status": "promoted_state_missing_or_unattested"}
    stage = str(raw.get("dd_state") or "").strip()
    if stage not in DD_PACING_MULTIPLIERS:
        return {"dd_stage": "controller_fault", "dd_pacing_multiplier": 0.0, "dd_pacing_source": "controller_fault", "dd_enforcement_active": False, "requires_human_acknowledgement": True, "promotion_history_status": "promoted_state_invalid"}
    return {"dd_stage": stage, "dd_pacing_multiplier": DD_PACING_MULTIPLIERS[stage], "dd_pacing_source": "promoted_controller", "dd_enforcement_active": True, "requires_human_acknowledgement": False, "promotion_history_status": "promoted_valid", "drawdown_decimal": raw.get("last_drawdown_decimal"), "state_updated_at": raw.get("updated_at"), "promotion_event_id": history[-1].get("event_id")}


def _canonical_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def issue_scheduled_broad_permission(*, action: dict[str, Any], canonical_dd_stage: str, dd_pacing_multiplier: float, state_snapshot: dict[str, Any], now: datetime | None = None, ttl_days: int = 7) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    ticker, action_type = canonical_ticker(action.get("ticker")), str(action.get("type") or "").lower()
    amount = _number(action.get("estimated_notional_jpy") or action.get("amount_jpy"))
    if not ticker or action_type not in {"buy", "add", "dca"} or amount <= 0 or canonical_dd_stage not in {"block", "derisk_review"} or float(dd_pacing_multiplier) != 0.25:
        raise ValueError("invalid scheduled broad deployment permission")
    permission = {"source": "scheduled_broad_deployment", "ticker": ticker, "action_type": action_type, "max_notional_jpy": amount, "canonical_dd_stage": canonical_dd_stage, "dd_pacing_multiplier": 0.25, "state_snapshot_hash": "sha256:" + sha256(_canonical_payload(dict(state_snapshot or {})).encode()).hexdigest(), "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=max(1, int(ttl_days)))).isoformat(), "human_execution_only": True}
    permission["permission_id"] = "capital-deployment:" + sha256(_canonical_payload(permission).encode()).hexdigest()[:24]
    return permission


def validate_scheduled_broad_permission(action: dict[str, Any], *, canonical_dd_stage: str, now: datetime | None = None) -> bool:
    permission = action.get("capital_deployment_permission")
    if not isinstance(permission, dict):
        return False
    try:
        now = now or datetime.now(timezone.utc)
        expires_at = datetime.fromisoformat(str(permission.get("expires_at") or "").replace("Z", "+00:00"))
        amount = _number(action.get("estimated_notional_jpy") or action.get("amount_jpy"))
        return expires_at.tzinfo is not None and expires_at >= now and str(permission.get("source")) == str(action.get("source")) == "scheduled_broad_deployment" and bool(action.get("human_execution_only")) and bool(permission.get("human_execution_only")) and bool(permission.get("permission_id")) and str(permission.get("state_snapshot_hash") or "").startswith("sha256:") and canonical_ticker(permission.get("ticker")) == canonical_ticker(action.get("ticker")) and str(permission.get("action_type") or "").lower() == str(action.get("type") or "").lower() and amount > 0 and amount <= _number(permission.get("max_notional_jpy")) and str(permission.get("canonical_dd_stage") or "") == str(canonical_dd_stage) and float(permission.get("dd_pacing_multiplier")) == 0.25 and canonical_dd_stage in {"block", "derisk_review"}
    except (TypeError, ValueError):
        return False
