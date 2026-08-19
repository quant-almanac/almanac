"""Fail-closed contracts for paced capital deployment.

This module contains no broker mutation.  It makes cash reservations and the
promotion history of the canonical drawdown controller explicit.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import date, datetime, timedelta, timezone
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


def wallet_available_after_reservations(
    wallets: Iterable[dict[str, Any]] | None,
    reservations: Iterable[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    reserved = operational_reserve_summary(reservations)["operational_reserve_by_wallet_jpy"]
    out: list[dict[str, Any]] = []
    for wallet in wallets or []:
        if not isinstance(wallet, dict):
            continue
        key = str(wallet.get("wallet_key") or "")
        available = _number(wallet.get("available_jpy"))
        out.append({
            **wallet,
            "operational_reserved_jpy": reserved.get(key, 0),
            "available_after_operational_reservations_jpy": max(0, available - reserved.get(key, 0)),
        })
    return out


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _execution_notional(
    row: dict[str, Any],
    *,
    currency: str,
    fx_rate_usdjpy: float,
) -> tuple[int | None, float | None]:
    """Return one open order's JPY/native reservation without guessing."""
    for key in ("estimated_notional_jpy", "notional_jpy", "executed_amount_jpy"):
        value = row.get(key)
        if value not in (None, ""):
            try:
                amount_jpy = float(value)
            except (TypeError, ValueError):
                continue
            if math.isfinite(amount_jpy) and amount_jpy > 0:
                native = amount_jpy / fx_rate_usdjpy if currency == "USD" else amount_jpy
                return int(round(amount_jpy)), native
    try:
        quantity = float(row.get("quantity") or row.get("requested_buy_quantity"))
        price = float(
            row.get("limit_price")
            or row.get("decision_price")
            or row.get("price")
        )
    except (TypeError, ValueError):
        return None, None
    if not (math.isfinite(quantity) and math.isfinite(price)) or quantity <= 0 or price <= 0:
        return None, None
    native = quantity * price
    amount_jpy = native * fx_rate_usdjpy if currency == "USD" else native
    return int(round(amount_jpy)), native


def _business_days_open(started_at: datetime | None, now: datetime) -> int | None:
    if started_at is None:
        return None
    # Business-day age is a market/operator calendar concept.  Compare dates
    # in the caller's local zone; converting a Tokyo morning to UTC would
    # incorrectly drop the current business day.
    started = started_at.astimezone(now.tzinfo)
    ended = now
    if started > ended:
        return 0
    current = started.date()
    total = 0
    while current < ended.date():
        current += timedelta(days=1)
        if current.weekday() < 5:
            total += 1
    return total


def build_wallet_capacity_timeline(
    wallets: Iterable[dict[str, Any]] | None,
    *,
    now: datetime,
    month_end: date,
    fx_rate_usdjpy: float,
    executions: Iterable[dict[str, Any]] | None = None,
    schedule_reservations: Iterable[dict[str, Any]] | None = None,
    generate_schedule_reservations: bool = True,
) -> dict[str, Any]:
    """Project executable wallet cash after every known reservation.

    Broker balances remain immutable facts.  This projection is the common
    authority used by the execution plan and per-action readiness: scheduled
    broker-cash debits after the balance snapshot are split into already-due
    unreflected outflows and future operational reservations, while live buy
    orders remain a third, separately auditable reservation class.
    """
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    as_of_date = now.date()
    wallet_rows = [dict(row) for row in wallets or [] if isinstance(row, dict)]
    earliest_source: date | None = None
    for wallet in wallet_rows:
        source = _parse_datetime(wallet.get("source_as_of"))
        if source is not None:
            earliest_source = min(earliest_source, source.date()) if earliest_source else source.date()

    generated_reservations: list[dict[str, Any]] = []
    if earliest_source is not None and generate_schedule_reservations:
        try:
            from contribution_schedule import broker_cash_reservations, occurrences

            generated_reservations = broker_cash_reservations(
                occurrences(earliest_source.isoformat(), month_end.isoformat())
            )
        except Exception:
            generated_reservations = []
    reservations_by_id: dict[str, dict[str, Any]] = {}
    for reservation in [*generated_reservations, *(schedule_reservations or [])]:
        if not isinstance(reservation, dict):
            continue
        reservation_id = str(reservation.get("reservation_id") or "")
        if reservation_id:
            reservations_by_id[reservation_id] = dict(reservation)

    try:
        from execution_safety import canonical_broker, canonical_owner, economic_direction
    except Exception:
        def canonical_owner(value: Any) -> str:
            return str(value or "").strip().lower()

        def canonical_broker(value: Any) -> str:
            return str(value or "").strip().lower()

        def economic_direction(value: Any) -> str:
            return str(value or "").strip().lower()

    open_by_wallet: dict[str, list[dict[str, Any]]] = {}
    unresolved_open_by_wallet: dict[str, list[str]] = {}
    seen_execution_ids: set[str] = set()
    for execution in executions or []:
        if not isinstance(execution, dict):
            continue
        if str(execution.get("status") or "").lower() not in {"ordered", "placed"}:
            continue
        if economic_direction(
            execution.get("direction") or execution.get("type") or execution.get("action_type")
        ) != "buy":
            continue
        execution_id = str(execution.get("id") or execution.get("execution_id") or execution.get("action_state_id") or "")
        if not execution_id or execution_id in seen_execution_ids:
            continue
        seen_execution_ids.add(execution_id)
        owner = canonical_owner(execution.get("execution_owner") or execution.get("owner"))
        broker = canonical_broker(execution.get("execution_broker") or execution.get("broker"))
        ticker = canonical_ticker(execution.get("ticker"))
        currency = str(execution.get("cash_currency") or execution.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")).upper()
        settlement_pool = str(execution.get("settlement_pool") or "broker_cash")
        if not owner or not broker or currency not in {"JPY", "USD"}:
            continue
        wallet_key = f"{owner}|{broker}|{settlement_pool}|{currency}"
        amount_jpy, amount_native = _execution_notional(
            execution,
            currency=currency,
            fx_rate_usdjpy=fx_rate_usdjpy,
        )
        if amount_jpy is None or amount_native is None:
            unresolved_open_by_wallet.setdefault(wallet_key, []).append(execution_id)
            continue
        ordered_at = None
        for key_name in ("ordered_at", "placed_at", "saved_at", "recommended_at"):
            ordered_at = _parse_datetime(execution.get(key_name))
            if ordered_at is not None:
                break
        age_days = _business_days_open(ordered_at, now)
        open_by_wallet.setdefault(wallet_key, []).append({
            "reservation_id": f"open-order:{execution_id}",
            "execution_id": execution_id,
            "amount_jpy": amount_jpy,
            "amount_native": round(amount_native, 8),
            "status": str(execution.get("status") or "").lower(),
            "ordered_at": ordered_at.isoformat() if ordered_at else None,
            "business_days_open": age_days,
            "stale_requires_confirmation": bool(age_days is not None and age_days > 10),
        })

    projected: list[dict[str, Any]] = []
    for wallet in wallet_rows:
        key = str(wallet.get("wallet_key") or "")
        currency = str(wallet.get("currency") or "").upper()
        confirmed_jpy = _number(wallet.get("available_jpy"))
        try:
            confirmed_native = max(0.0, float(wallet.get("available_native") or 0.0))
        except (TypeError, ValueError):
            confirmed_native = 0.0
        source = _parse_datetime(wallet.get("source_as_of"))
        status = "ok" if source is not None else "source_as_of_unresolved"
        unreflected: list[dict[str, Any]] = []
        future: list[dict[str, Any]] = []
        for reservation in reservations_by_id.values():
            if str(reservation.get("wallet_key") or "") != key:
                continue
            try:
                due = date.fromisoformat(str(reservation.get("due_date") or "")[:10])
            except ValueError:
                status = "schedule_due_date_unresolved"
                continue
            if source is None:
                continue
            # A schedule on a date-only broker snapshot cannot be proven to be
            # reflected; reserve it conservatively with the already-due leg.
            if due < source.date():
                continue
            target = unreflected if due <= as_of_date else future
            target.append(dict(reservation))
        open_rows = open_by_wallet.get(key, [])
        unresolved_open = unresolved_open_by_wallet.get(key, [])
        if unresolved_open:
            status = "open_order_notional_unresolved"
        unreflected_jpy = sum(_number(row.get("amount_jpy")) for row in unreflected)
        future_jpy = sum(_number(row.get("amount_jpy")) for row in future)
        open_jpy = sum(_number(row.get("amount_jpy")) for row in open_rows)
        reserved_jpy = unreflected_jpy + future_jpy + open_jpy
        available_jpy = 0 if status != "ok" else max(0, confirmed_jpy - reserved_jpy)
        divisor = fx_rate_usdjpy if currency == "USD" else 1.0
        available_native = available_jpy / divisor if divisor > 0 else 0.0
        projected.append({
            **wallet,
            "confirmed_balance_jpy": confirmed_jpy,
            "confirmed_balance_native": round(confirmed_native, 8),
            "unreflected_wallet_outflows_jpy": unreflected_jpy,
            "future_operational_reservations_jpy": future_jpy,
            "open_order_reservations_jpy": open_jpy,
            "available_after_all_reservations_jpy": available_jpy,
            "available_after_all_reservations_native": round(available_native, 8),
            # Compatibility alias for readers migrated in the previous slice.
            "available_after_operational_reservations_jpy": available_jpy,
            "reservation_status": status,
            "unreflected_wallet_outflows": sorted(unreflected, key=lambda row: str(row.get("due_date") or "")),
            "future_operational_reservations": sorted(future, key=lambda row: str(row.get("due_date") or "")),
            "open_order_reservations": open_rows,
            "unresolved_open_order_ids": unresolved_open,
        })

    return {
        "schema_version": 2,
        "generated_at": now.isoformat(),
        "month_end": month_end.isoformat(),
        "wallets": sorted(projected, key=lambda row: str(row.get("wallet_key") or "")),
        "unreflected_wallet_outflows_jpy": sum(_number(row.get("unreflected_wallet_outflows_jpy")) for row in projected),
        "future_operational_reservations_jpy": sum(_number(row.get("future_operational_reservations_jpy")) for row in projected),
        "open_order_reservations_jpy": sum(_number(row.get("open_order_reservations_jpy")) for row in projected),
        "all_wallets_resolved": all(row.get("reservation_status") == "ok" for row in projected),
    }


def reachable_future_nisa_wait(
    *,
    preferences: dict[str, Any] | None,
    nisa: dict[str, Any] | None,
    wallets_after_reservations: Iterable[dict[str, Any]] | None,
) -> dict[str, Any]:
    """Resolve a human-approved future-NISA wait without household transfers.

    A wait is not buying power.  It is permitted only when its owner, that
    owner's broker wallet and an explicit next-year capacity declaration all
    agree.  In particular, another household member's unused NISA limit never
    becomes an edge in this graph.
    """
    preferences = preferences if isinstance(preferences, dict) else {}
    nisa = nisa if isinstance(nisa, dict) else {}
    requested = _number(preferences.get("approved_future_nisa_wait_jpy"))
    owner = str(preferences.get("approved_future_nisa_wait_owner") or "").strip().lower()
    declared_capacity = _number(preferences.get("future_nisa_growth_capacity_jpy"))
    actor = str(preferences.get("future_nisa_capacity_approved_by") or "").strip()
    if requested <= 0:
        return {"approved_nisa_wait_jpy": 0, "status": "not_requested"}
    if owner not in {"husband", "wife"} or declared_capacity <= 0 or not actor:
        return {
            "approved_nisa_wait_jpy": 0,
            "status": "unresolved_contract",
            "reason": "owner, future capacity, and human approval are required",
        }
    profile = nisa.get(owner)
    if not isinstance(profile, dict):
        return {"approved_nisa_wait_jpy": 0, "status": "owner_profile_missing"}
    try:
        from execution_safety import canonical_broker

        broker = canonical_broker(profile.get("broker"))
    except Exception:
        broker = ""
    wallet_total = 0
    for wallet in wallets_after_reservations or []:
        if not isinstance(wallet, dict):
            continue
        key = str(wallet.get("wallet_key") or "")
        parts = key.split("|")
        if len(parts) != 4 or parts[0] != owner or parts[1] != broker or parts[2] != "broker_cash":
            continue
        wallet_total += _number(
            wallet.get("available_after_operational_reservations_jpy", wallet.get("available_jpy"))
        )
    reachable = min(requested, declared_capacity, wallet_total)
    return {
        "approved_nisa_wait_jpy": reachable,
        "status": "reachable" if reachable == requested else "clamped_to_owner_capacity",
        "owner": owner,
        "broker": broker,
        "requested_jpy": requested,
        "declared_future_growth_capacity_jpy": declared_capacity,
        "owner_wallet_capacity_jpy": wallet_total,
        "approval_actor": actor,
        "no_cross_owner_transfer": True,
    }


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
