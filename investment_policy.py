"""Investment-policy observations that never mutate live actions.

The policy contract is defined in ``objective.md``.  This module makes the
approved household concentration limits measurable immediately while keeping
their first rollout in shadow mode.  It aggregates the same instrument across
owners, brokers and accounts for risk monitoring; execution identity remains
separate elsewhere.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from position_identity import canonical_instrument_id

POSITION_CAPS_BY_TIER = {
    "long": 0.10,
    "medium": 0.05,
    "swing": 0.02,
}
EMPLOYER_POSITION_CAP = 0.10
PROTECTED_CASH_RESERVE_JPY = 0
CASH_DEPLOYMENT_POLICY_VERSION = "regime_horizon_v1"
CASH_DEPLOYMENT_MONTHS_BY_LEVEL = {
    2: 2,   # strong bull
    1: 3,   # mild bull
    0: 6,   # neutral
    -1: 12, # mild bear
    -2: None,  # strong bear: ordinary deployment is disabled
}


def cash_deployment_policy() -> dict:
    """Return the explicit boundary between surplus and tactical cash.

    Every confirmed cash balance loaded into this deployment belongs to the
    investment portfolio. A market-regime target may still retain tactical
    cash, and operational reservations such as open orders, settlement,
    collateral and taxes remain unavailable for a new order.
    """
    return {
        "all_system_cash_is_surplus": True,
        "protected_cash_reserve_jpy": PROTECTED_CASH_RESERVE_JPY,
        "tactical_cash_retention_allowed": True,
        "operational_reservations_still_required": True,
        "monthly_budget_method": "deployable_surplus_divided_by_regime_horizon",
        "deployment_policy_version": CASH_DEPLOYMENT_POLICY_VERSION,
        "deployment_months_by_level": dict(CASH_DEPLOYMENT_MONTHS_BY_LEVEL),
    }


def cash_deployment_horizon(
    *,
    portfolio_level: int | None,
    portfolio_label: str | None = None,
    shock_active: bool = False,
) -> dict:
    """Return the fixed deployment horizon for the committed market regime.

    A shock or strong-bear regime creates no ordinary monthly buying budget.
    Active drawdown/DCA playbooks remain separate from this cash-deployment
    allowance.
    """
    labels = {
        "strong_bull": 2,
        "mild_bull": 1,
        "neutral": 0,
        "mild_bear": -1,
        "strong_bear": -2,
    }
    level = portfolio_level
    if level not in CASH_DEPLOYMENT_MONTHS_BY_LEVEL:
        level = labels.get(str(portfolio_label or "").strip().lower())
    if level not in CASH_DEPLOYMENT_MONTHS_BY_LEVEL:
        return {
            "resolved": False,
            "portfolio_level": None,
            "portfolio_label": portfolio_label,
            "deployment_months": None,
            "ordinary_deployment_allowed": False,
            "policy_version": CASH_DEPLOYMENT_POLICY_VERSION,
            "reason": "market_regime_unresolved",
        }
    deployment_months = None if shock_active else CASH_DEPLOYMENT_MONTHS_BY_LEVEL[level]
    return {
        "resolved": True,
        "portfolio_level": level,
        "portfolio_label": portfolio_label,
        "deployment_months": deployment_months,
        "ordinary_deployment_allowed": deployment_months is not None,
        "policy_version": CASH_DEPLOYMENT_POLICY_VERSION,
        "reason": "shock_active" if shock_active else None,
    }


def evaluate_concentration_policy(
    positions: Iterable[dict],
    *,
    portfolio_total_jpy: float | int | None,
    employer_tickers: Iterable[str] = (),
) -> dict:
    """Return a household-level, side-effect-free concentration observation."""
    employer_ids = {canonical_instrument_id(t) for t in employer_tickers}
    grouped: dict[str, dict] = {}
    issues: list[str] = []

    for row in positions or []:
        if not isinstance(row, dict):
            continue
        ticker = canonical_instrument_id(
            row.get("canonical_instrument_id") or row.get("ticker")
        )
        tier = str(row.get("investment_type") or "long").strip().lower()
        if not ticker or ticker.startswith("CASH_") or tier == "cash":
            continue
        if tier not in POSITION_CAPS_BY_TIER:
            issues.append(f"unknown_tier_assumed_long:{ticker}:{tier}")
            tier = "long"
        try:
            value_jpy = float(row.get("value_jpy") or 0)
        except (TypeError, ValueError):
            issues.append(f"value_unparseable:{ticker}")
            continue
        if value_jpy <= 0:
            continue
        slot = grouped.setdefault(
            ticker,
            {"value_jpy": 0.0, "value_by_tier": defaultdict(float)},
        )
        slot["value_jpy"] += value_jpy
        slot["value_by_tier"][tier] += value_jpy

    try:
        denominator = float(portfolio_total_jpy or 0)
    except (TypeError, ValueError):
        denominator = 0.0
    if denominator <= 0:
        denominator = sum(float(row["value_jpy"]) for row in grouped.values())
        issues.append("portfolio_total_missing_used_invested_positions")

    rows: list[dict] = []
    for ticker, slot in sorted(grouped.items()):
        by_tier = dict(slot["value_by_tier"])
        dominant_tier = max(by_tier, key=by_tier.get)
        strictest_tier = min(by_tier, key=POSITION_CAPS_BY_TIER.__getitem__)
        if len(by_tier) > 1:
            issues.append(f"mixed_tier_assignment:{ticker}")
        employer = ticker in employer_ids
        cap = (
            EMPLOYER_POSITION_CAP
            if employer
            else POSITION_CAPS_BY_TIER[strictest_tier]
        )
        weight = slot["value_jpy"] / denominator if denominator > 0 else None
        breached = weight is None or weight > cap
        rows.append(
            {
                "canonical_instrument_id": ticker,
                "value_jpy": round(slot["value_jpy"]),
                "weight": round(weight, 6) if weight is not None else None,
                "dominant_tier": dominant_tier,
                "cap_basis_tier": "employer_stock" if employer else strictest_tier,
                "tier_values_jpy": {k: round(v) for k, v in sorted(by_tier.items())},
                "cap": cap,
                "employer_stock": employer,
                "breached": breached,
                "excess_weight": (
                    round(max(0.0, weight - cap), 6) if weight is not None else None
                ),
            }
        )

    breaches = [row for row in rows if row["breached"]]
    return {
        "mode": "shadow",
        "status": "review" if breaches or issues or denominator <= 0 else "ok",
        "scope": "household",
        "denominator_jpy": round(denominator) if denominator > 0 else None,
        "caps": {
            **POSITION_CAPS_BY_TIER,
            "employer_stock": EMPLOYER_POSITION_CAP,
        },
        "cash_deployment_policy": cash_deployment_policy(),
        "positions": rows,
        "breaches": breaches,
        "breach_count": len(breaches),
        "issues": issues,
        "action_effect": "none",
        "next_step": "block_new_add_and_shadow_deterministic_exit" if breaches else None,
    }
