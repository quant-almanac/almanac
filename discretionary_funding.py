"""Authoritative discretionary-funding guard for new risk-increasing orders.

The execution plan is the only source of discretionary buying power.  Cash
balances, stale fallback values, and a disabled plan must never manufacture an
order budget.  Historical fills are facts and are intentionally outside this
module; callers apply this guard only to recommendations and new open orders.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FUNDING_REQUIRED_ACTION_TYPES = {"buy", "add", "dca", "margin_buy"}


def _jpy(value: object) -> int:
    try:
        return max(0, int(round(float(value or 0))))
    except (TypeError, ValueError):
        return 0


def load_execution_plan_state(base_dir: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads((base_dir / "execution_plan_state.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    return raw if isinstance(raw, dict) else None


def evaluate_discretionary_funding(
    action_type: object,
    *,
    plan_state: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return a fail-closed funding decision for a recommendation/order.

    ``sell``/``trim``/``cover`` and recorded fills do not consume a new
    discretionary budget.  The latter distinction is enforced by API callers,
    which call this helper for ``ordered`` requests only.
    """
    normalized = str(action_type or "").strip().lower()
    if normalized not in FUNDING_REQUIRED_ACTION_TYPES:
        return {"required": False, "allowed": True, "reason_code": None}

    if not isinstance(plan_state, dict):
        return {
            "required": True,
            "allowed": False,
            "reason_code": "discretionary_funding_unresolved",
            "message": "裁量投資枠を確認できないため、新規買い注文を許可しません",
        }

    status = str(plan_state.get("status") or "").strip().lower()
    budgets = plan_state.get("budgets")
    contribution = plan_state.get("contribution_summary")
    if status != "active" or not isinstance(budgets, dict) or not isinstance(contribution, dict):
        return {
            "required": True,
            "allowed": False,
            "reason_code": "discretionary_funding_unresolved",
            "message": "有効な裁量投資計画を確認できないため、新規買い注文を許可しません",
            "plan_status": status or "unknown",
        }

    normal_available = _jpy(budgets.get("normal_pool_available_jpy"))
    opportunity_available = _jpy(budgets.get("opportunity_pool_available_jpy"))
    contribution_available = _jpy(contribution.get("available_jpy"))
    available = max(normal_available + opportunity_available, contribution_available)
    if available <= 0:
        return {
            "required": True,
            "allowed": False,
            "reason_code": "no_approved_discretionary_funding",
            "message": "承認済みの裁量投資資金が0円のため、新規買い注文を許可しません",
            "normal_pool_available_jpy": normal_available,
            "opportunity_pool_available_jpy": opportunity_available,
            "approved_contribution_available_jpy": contribution_available,
        }

    return {
        "required": True,
        "allowed": True,
        "reason_code": None,
        "available_jpy": available,
        "normal_pool_available_jpy": normal_available,
        "opportunity_pool_available_jpy": opportunity_available,
        "approved_contribution_available_jpy": contribution_available,
    }
