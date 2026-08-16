"""Read-only, wallet-scoped cash projection for execution-plan observability.

This module intentionally does *not* alter a broker-reported cash balance.
It separates events that can be attributed to one concrete cash wallet from
events that are only useful for TWR accounting (for example a card-funded
scheduled contribution).  The resulting projection is an audit aid until a
broker reconciliation confirms the balance again.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import atomic_write_json

BASE_DIR = Path(__file__).parent
STATE_FILE_NAME = "cash_wallet_projection.json"
SCHEMA_VERSION = 1

_OBSERVED_TYPES = {
    "cash_flow", "trade", "dividend", "tax", "fee", "fx_conversion",
    "cross_owner_transfer", "reconcile",
}
_EXCLUDED_FUNDING_SOURCES = {"external_card", "external_payroll", "schedule_derived"}


def _parse_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _native_amount(event: dict[str, Any]) -> float | None:
    try:
        quantity = float(event.get("quantity"))
        price = float(event.get("price") if event.get("price") is not None else 1.0)
    except (TypeError, ValueError):
        return None
    return abs(quantity * price)


def _event_sign(event: dict[str, Any], payload: dict[str, Any]) -> int | None:
    explicit = payload.get("cash_sign")
    if explicit in {1, -1}:
        return int(explicit)
    direction = str(event.get("direction") or "").lower()
    if direction in {"in", "sell", "cover"}:
        return 1
    if direction in {"out", "buy", "margin_buy", "short"}:
        return -1
    return None


def _event_wallet_key(event: dict[str, Any], payload: dict[str, Any], wallet_by_resource: dict[str, str]) -> str | None:
    explicit = payload.get("wallet_key")
    if isinstance(explicit, str) and explicit:
        return explicit
    cash_route = str(payload.get("cash_route") or "")
    if cash_route and cash_route in wallet_by_resource:
        return wallet_by_resource[cash_route]
    owner = str(payload.get("owner") or "").strip().lower()
    broker = str(payload.get("broker") or event.get("account") or "").strip().lower()
    settlement_pool = str(payload.get("settlement_pool") or "broker_cash").strip()
    currency = str(event.get("currency") or payload.get("currency") or "").upper()
    if owner and broker and settlement_pool and currency:
        return f"{owner}|{broker}|{settlement_pool}|{currency}"
    return None


def build_wallet_projection(
    cash_info: dict[str, Any],
    *,
    base_dir: Path = BASE_DIR,
    now: datetime | None = None,
    event_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a side-effect-free projection and a full attribution audit trail."""
    now = now or datetime.now()
    wallets = [dict(row) for row in cash_info.get("wallets") or [] if isinstance(row, dict)]
    wallet_by_key = {str(row.get("wallet_key")): row for row in wallets if row.get("wallet_key")}
    wallet_by_resource = {
        str(resource): str(row.get("wallet_key"))
        for row in wallets
        for resource in row.get("resources") or []
        if resource and row.get("wallet_key")
    }
    projections = {
        key: {
            "wallet_key": key,
            "owner": row.get("owner"),
            "broker": row.get("broker"),
            "settlement_pool": row.get("settlement_pool"),
            "currency": row.get("currency"),
            "base_available_native": float(row.get("available_native") or 0.0),
            "base_available_jpy": int(row.get("available_jpy") or 0),
            "base_as_of": row.get("source_as_of"),
            "source_hash": None,
            "ledger_delta_native": 0.0,
            "included_event_ids": [],
            "excluded_events": [],
            "status": "observed_not_authoritative",
            # A ledger projection never upgrades source freshness. A broker
            # reconciliation supplies the only valid-until boundary.
            "valid_until": None,
        }
        for key, row in wallet_by_key.items()
    }
    unattributed: list[dict[str, Any]] = []
    excluded_cash_events: list[dict[str, Any]] = []

    if event_rows is None:
        try:
            from event_ledger import query_events
            event_rows = query_events(types=sorted(_OBSERVED_TYPES))
        except Exception as exc:
            event_rows = []
            unattributed.append({"code": "ledger_unreadable", "detail": type(exc).__name__})

    for event in event_rows or []:
        if not isinstance(event, dict):
            continue
        payload = _parse_payload(event.get("raw_payload"))
        event_id = str(event.get("event_id") or "")
        funding_source = str(payload.get("funding_source") or "")
        source = str(event.get("source") or "")
        wallet_key = _event_wallet_key(event, payload, wallet_by_resource)
        exclusion_code = None
        if funding_source in _EXCLUDED_FUNDING_SOURCES or source in {"schedule", "schedule_backfill"}:
            exclusion_code = "schedule_or_external_funding_not_broker_cash"
        elif payload.get("nominal_date") or payload.get("schedule_id"):
            exclusion_code = "nominal_schedule_event_not_broker_cash"
        if exclusion_code:
            row = {"event_id": event_id, "code": exclusion_code, "event_type": event.get("event_type")}
            excluded_cash_events.append(row)
            if wallet_key in projections:
                projections[wallet_key]["excluded_events"].append(row)
            continue
        if wallet_key not in projections:
            unattributed.append({
                "event_id": event_id,
                "code": "cash_event_wallet_unattributed",
                "event_type": event.get("event_type"),
            })
            continue
        amount = _native_amount(event)
        sign = _event_sign(event, payload)
        if amount is None or sign is None:
            projections[wallet_key]["excluded_events"].append({
                "event_id": event_id,
                "code": "cash_event_amount_or_direction_unresolved",
            })
            continue
        projections[wallet_key]["ledger_delta_native"] += sign * amount
        projections[wallet_key]["included_event_ids"].append(event_id)

    for row in projections.values():
        row["ledger_delta_native"] = round(float(row["ledger_delta_native"]), 2)
        row["projected_available_native"] = round(
            float(row["base_available_native"]) + float(row["ledger_delta_native"]), 2,
        )
        # A projection is intentionally informational: only a confirmed
        # reconciliation can make it buying power.
        row["available_for_new_buy_native"] = float(row["base_available_native"])
        source_material = {
            "wallet_key": row["wallet_key"],
            "base_available_native": row["base_available_native"],
            "base_as_of": row["base_as_of"],
            "included_event_ids": row["included_event_ids"],
            "excluded_events": row["excluded_events"],
        }
        row["source_hash"] = hashlib.sha256(
            json.dumps(source_material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()

    result = {
        "schema_version": SCHEMA_VERSION,
        "provenance": "wallet_ledger_projection_v1",
        "generated_at": now.isoformat(timespec="seconds"),
        "wallets": sorted(projections.values(), key=lambda row: row["wallet_key"]),
        "unattributed_cash_events": unattributed,
        "excluded_cash_events": excluded_cash_events,
        "authoritative_for_new_buys": False,
        "note": "投影値はbroker確認残高を置換せず、発注余力には使わない",
    }
    result["source_hash"] = hashlib.sha256(
        json.dumps(result["wallets"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


def projection_path(base_dir: Path = BASE_DIR) -> Path:
    return Path(base_dir) / STATE_FILE_NAME


def write_wallet_projection(projection: dict[str, Any], *, base_dir: Path = BASE_DIR) -> Path:
    """Persist only a derived artifact; no financial source is changed."""
    path = projection_path(base_dir)
    atomic_write_json(path, projection)
    return path
