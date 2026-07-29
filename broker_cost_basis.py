"""Broker-authoritative cost-basis readiness for deterministic exits."""
from __future__ import annotations

import math
from typing import Iterable, Optional

from execution_reconciliation import (
    execution_temporal_order,
    resolve_effective_execution_record,
)
from execution_safety import ACTIVE_ORDER_STATUSES, is_fill_record
from position_identity import (
    PositionIdentity,
    position_identity_for_action,
    position_identity_for_holding,
)
from tax_lot import CostBasisEstimate


def validate_broker_cost_basis(
    *,
    position: PositionIdentity,
    quantity: float,
    holding: dict,
    executions: Iterable[dict],
) -> dict:
    """Validate exact-route broker basis and post-snapshot execution ordering."""
    holding_identity = position_identity_for_holding(
        holding, key=str(holding.get("key") or "")
    )
    if holding_identity != position:
        return {"status": "review", "reason": "holding_position_identity_mismatch"}

    snapshot_as_of = (
        holding.get("broker_cost_basis_as_of")
        or holding.get("source_as_of")
        or holding.get("reported_as_of")
    )
    if not snapshot_as_of:
        return {"status": "review", "reason": "broker_cost_basis_as_of_missing"}

    try:
        holding_qty = float(holding.get("shares"))
        broker_qty = float(holding.get("broker_quantity"))
    except (TypeError, ValueError):
        return {"status": "review", "reason": "broker_quantity_missing"}
    if not math.isclose(holding_qty, broker_qty, rel_tol=0, abs_tol=1e-6):
        return {
            "status": "review",
            "reason": "broker_quantity_mismatch",
            "holding_quantity": holding_qty,
            "broker_quantity": broker_qty,
        }
    if abs(float(quantity)) > holding_qty + 1e-6:
        return {"status": "review", "reason": "exit_quantity_exceeds_broker_quantity"}

    for raw in executions:
        if not isinstance(raw, dict):
            continue
        ticker = str(raw.get("ticker") or "").upper()
        if ticker != position.canonical_instrument_id:
            continue
        effective = resolve_effective_execution_record(raw)
        reconciliation_status = effective.get("execution_reconciliation_status")
        if reconciliation_status == "review":
            return {
                "status": "review",
                "reason": "execution_reconciliation_requires_review",
                "execution_id": raw.get("id"),
            }
        execution_identity = position_identity_for_action(effective)
        status = str(effective.get("status") or "").lower()
        affects_position = is_fill_record(effective) or status in ACTIVE_ORDER_STATUSES
        if not affects_position:
            continue
        if execution_identity is None:
            return {
                "status": "review",
                "reason": "execution_position_identity_unknown",
                "execution_id": raw.get("id"),
            }
        if execution_identity != position:
            continue
        if status in ACTIVE_ORDER_STATUSES:
            return {
                "status": "review",
                "reason": "active_order_after_broker_snapshot_unknown",
                "execution_id": raw.get("id"),
            }
        order = execution_temporal_order(effective, snapshot_as_of)
        if order["requires_review"]:
            return {
                "status": "review",
                "reason": order["temporal_order"],
                "execution_id": raw.get("id"),
                "temporal_order": order,
            }

    if position.account.startswith("nisa"):
        return {
            "status": "ready",
            "reason": "nisa_tax_exempt",
            "estimate": CostBasisEstimate(
                amount_jpy=0.0,
                source="nisa_tax_exempt",
                method="tax_not_applicable",
                as_of=str(snapshot_as_of),
                reconciled=True,
                data_quality_issues=("economic_cost_basis_not_used_for_tax",),
            ),
        }

    try:
        total_cost = float(holding.get("broker_total_cost_basis_jpy"))
    except (TypeError, ValueError):
        return {"status": "review", "reason": "broker_total_cost_basis_missing"}
    if not math.isfinite(total_cost) or total_cost < 0:
        return {"status": "review", "reason": "broker_total_cost_basis_invalid"}
    if holding.get("broker_cost_basis_source") != "rakuten_assetbalance_csv":
        return {"status": "review", "reason": "broker_cost_basis_source_unverified"}

    amount = total_cost * abs(float(quantity)) / broker_qty if broker_qty else 0.0
    return {
        "status": "ready",
        "reason": None,
        "estimate": CostBasisEstimate(
            amount_jpy=amount,
            source="broker_report",
            method="broker_reported",
            as_of=str(snapshot_as_of),
            reconciled=True,
            data_quality_issues=(),
        ),
    }


def resolve_broker_cost_basis(
    *,
    position: PositionIdentity,
    quantity: float,
    holding: dict,
    executions: Iterable[dict],
) -> Optional[CostBasisEstimate]:
    result = validate_broker_cost_basis(
        position=position,
        quantity=quantity,
        holding=holding,
        executions=executions,
    )
    estimate = result.get("estimate")
    return estimate if isinstance(estimate, CostBasisEstimate) else None
