"""Hysteretic state machine for the promoted flow-adjusted DD series.

This module is intentionally separate from shadow measurement.  Calling
``advance_state`` is an explicit Slice 3 operation; a shadow value alone can
never become an enforcement gate.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from risk_policy import POLICY


STATE_ORDER = ("ok", "caution", "block", "derisk_review", "freeze", "objective_breach")
_RANK = {name: index for index, name in enumerate(STATE_ORDER)}
_WORSEN = (
    (POLICY.dd_objective_breach_decimal, "objective_breach"),
    (POLICY.dd_freeze_decimal, "freeze"),
    (POLICY.dd_derisk_decimal, "derisk_review"),
    (POLICY.dd_block_decimal, "block"),
    (POLICY.dd_caution_decimal, "caution"),
)
_RECOVERY_ABOVE = {
    "caution": -0.03,
    "block": -0.06,
    "derisk_review": -0.08,
    "freeze": -0.10,
    # To leave an objective breach, first return to the freeze band.
    "objective_breach": -0.12,
}


def target_state(drawdown_decimal: float) -> str:
    for threshold, state in _WORSEN:
        if drawdown_decimal <= threshold:
            return state
    return "ok"


def initial_state() -> dict[str, Any]:
    return {
        "dd_state": "ok",
        "recovery_effective_days": 0,
        "last_effective_nav_date": None,
        "enforcement_enabled": False,
        "transitions": [],
    }


def advance_state(
    state: dict[str, Any] | None,
    *,
    drawdown_decimal: float,
    effective_nav_date: str,
    freeze_release_approved: bool = False,
) -> dict[str, Any]:
    """Advance at most one recovery stage, but worsen immediately."""
    out = dict(initial_state())
    out.update(state or {})
    current = str(out.get("dd_state") or "ok")
    if current not in _RANK:
        current = "ok"
    desired = target_state(float(drawdown_decimal))
    transition = None
    if _RANK[desired] > _RANK[current]:
        current = desired
        out["recovery_effective_days"] = 0
        transition = "worsened"
    elif _RANK[desired] == _RANK[current]:
        out["recovery_effective_days"] = 0
    elif current != "ok":
        above = _RECOVERY_ABOVE[current]
        if float(drawdown_decimal) > above:
            if out.get("last_effective_nav_date") != effective_nav_date:
                out["recovery_effective_days"] = int(out.get("recovery_effective_days") or 0) + 1
            if int(out.get("recovery_effective_days") or 0) >= 5:
                if current == "freeze" and not freeze_release_approved:
                    transition = "freeze_release_pending_human_approval"
                else:
                    current = STATE_ORDER[_RANK[current] - 1]
                    out["recovery_effective_days"] = 0
                    transition = "recovered_one_stage"
        else:
            out["recovery_effective_days"] = 0
    out["dd_state"] = current
    out["last_effective_nav_date"] = effective_nav_date
    out["last_drawdown_decimal"] = float(drawdown_decimal)
    out["updated_at"] = datetime.now(timezone.utc).isoformat()
    if transition:
        transitions = list(out.get("transitions") or [])[-99:]
        transitions.append({
            "at": out["updated_at"], "kind": transition, "state": current,
            "drawdown_decimal": float(drawdown_decimal),
            "effective_nav_date": effective_nav_date,
            "freeze_release_approved": bool(freeze_release_approved),
        })
        out["transitions"] = transitions
    return out


def enforcement_eligibility(
    shadow: dict[str, Any] | None,
    *,
    manual_reconciliation_recorded: bool = False,
) -> dict[str, Any]:
    shadow = shadow or {}
    effective = int(shadow.get("effective_nav_days") or 0)
    forward = int(shadow.get("forward_shadow_effective_days") or 0)
    try:
        coverage = float(shadow.get("flow_coverage") or 0.0)
    except (TypeError, ValueError):
        coverage = 0.0
    reasons = []
    if effective < 60:
        reasons.append("effective_nav_days_below_60")
    if forward < 60:
        reasons.append("forward_shadow_days_below_60")
    if coverage < 0.95:
        reasons.append("flow_coverage_below_95pct")
    if shadow.get("invalid_days"):
        reasons.append("invalid_nav_or_flow_days_present")
    if shadow.get("estimated_rows_excluded") is not True:
        reasons.append("estimated_nav_exclusion_unattested")
    if shadow.get("weekend_rows_excluded") is not True:
        reasons.append("effective_nav_day_filter_unattested")
    if shadow.get("manual_reconciliation_required", True) and not manual_reconciliation_recorded:
        reasons.append("manual_reconciliation_not_recorded")
    return {"eligible": not reasons, "reasons": reasons}
