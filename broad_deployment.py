"""Deterministic broad-core candidate generation for approved surplus cash.

This module creates no order and mutates no financial state.  It converts one
active, exact execution-plan objective into at most one human-executed
candidate after route, wallet, price, and concentration facts are resolved.
"""
from __future__ import annotations

import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from instrument_metadata import broad_execution_metadata, canonical_ticker, quantity_label_for_ticker
from risk_policy import concentration_limits


ROUTE_FILE = "broad_execution_routes.json"
DEFAULT_TICKER = "VT"
DEFAULT_FAMILY = "global_all_country"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _state_root(base_dir: Path) -> Path:
    return Path(os.environ.get("ALMANAC_STATE_DIR") or base_dir)


def load_route_config(*, base_dir: Path) -> dict[str, Any]:
    """Load an ignored, operator-owned route contract; missing means no route."""
    path = _state_root(base_dir) / ROUTE_FILE
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "routes": [], "status": "route_config_missing"}
    routes = payload.get("routes") if isinstance(payload, dict) else None
    if not isinstance(routes, list):
        return {"schema_version": 1, "routes": [], "status": "route_config_invalid"}
    return {**payload, "routes": routes, "status": "ok"}


def _active_broad_item(execution_plan: dict[str, Any]) -> dict[str, Any] | None:
    for item in execution_plan.get("items") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("status") or "active") != "active":
            continue
        if str(item.get("objective") or "") != "deploy_surplus_broad_core":
            continue
        if _number(item.get("remaining_jpy")) <= 0:
            continue
        constraints = item.get("constraints") if isinstance(item.get("constraints"), dict) else {}
        if str(constraints.get("broad_family") or "") != DEFAULT_FAMILY:
            continue
        if DEFAULT_TICKER not in {canonical_ticker(value) for value in item.get("preferred_tickers") or []}:
            continue
        return item
    return None


def _exact_route(config: dict[str, Any], *, ticker: str) -> tuple[dict[str, Any] | None, str]:
    matches: list[dict[str, Any]] = []
    for row in config.get("routes") or []:
        if not isinstance(row, dict) or row.get("active") is not True:
            continue
        if canonical_ticker(row.get("ticker")) != ticker:
            continue
        matches.append(dict(row))
    if not matches:
        return None, "route_missing"
    if len(matches) != 1:
        return None, "route_ambiguous"
    route = matches[0]
    required = ("route_id", "owner", "broker", "account", "investment_type", "settlement_pool", "currency")
    if any(not str(route.get(key) or "").strip() for key in required):
        return None, "route_incomplete"
    if str(route.get("investment_type") or "").lower() != "long":
        return None, "route_not_long"
    if str(route.get("settlement_pool") or "").lower() != "broker_cash":
        return None, "route_not_broker_cash"
    return route, "ok"


def _wallet_for_route(execution_plan: dict[str, Any], route: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    wallet_key = "|".join((
        str(route.get("owner") or "").lower(),
        str(route.get("broker") or "").lower(),
        str(route.get("settlement_pool") or "").lower(),
        str(route.get("currency") or "").upper(),
    ))
    timeline = ((execution_plan.get("cash_info") or {}).get("wallet_capacity_timeline") or {})
    if not isinstance(timeline, dict) or timeline.get("all_wallets_resolved") is not True:
        return None, "wallet_timeline_unresolved"
    matches = [
        row for row in timeline.get("wallets") or []
        if isinstance(row, dict) and str(row.get("wallet_key") or "") == wallet_key
    ]
    if len(matches) != 1:
        return None, "wallet_missing_or_ambiguous"
    if str(matches[0].get("reservation_status") or "") != "ok":
        return None, "wallet_reservations_unresolved"
    return matches[0], "ok"


def _concentration(
    *,
    ticker: str,
    family: str,
    notional_jpy: float,
    policy_observation: dict[str, Any],
) -> dict[str, Any]:
    denominator = _number(policy_observation.get("denominator_jpy"))
    if denominator <= 0:
        return {"status": "policy_denominator_unresolved"}
    instrument_value = 0.0
    family_value = 0.0
    for row in policy_observation.get("positions") or []:
        if not isinstance(row, dict):
            continue
        row_ticker = canonical_ticker(row.get("canonical_instrument_id"))
        value = _number(row.get("value_jpy"), default=-1)
        if value < 0:
            return {"status": "policy_position_value_unresolved"}
        if row_ticker == ticker:
            instrument_value += value
        metadata = broad_execution_metadata(row_ticker)
        if metadata and metadata.get("broad_family") == family:
            family_value += value
    limits = concentration_limits(broad_family=family, investment_type="long")
    return {
        "status": "ok",
        "denominator_jpy": round(denominator),
        "current_instrument_value_jpy": round(instrument_value),
        "current_family_value_jpy": round(family_value),
        "post_trade_instrument_decimal": (instrument_value + notional_jpy) / denominator,
        "post_trade_family_decimal": (family_value + notional_jpy) / denominator,
        **limits,
    }


def generate_candidate(
    *,
    execution_plan: dict[str, Any] | None,
    policy_observation: dict[str, Any] | None,
    route_config: dict[str, Any] | None,
    price: float | None,
    fx_rate_usdjpy: float,
    existing_actions: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    max_single_jpy: int = 500_000,
    min_notional_jpy: int = 150_000,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Return one exact VT candidate plus a complete no-candidate reason."""
    now = now or datetime.now(timezone.utc)
    plan = execution_plan if isinstance(execution_plan, dict) else {}
    observation: dict[str, Any] = {
        "version": 1,
        "source": "scheduled_broad_deployment",
        "default_ticker": DEFAULT_TICKER,
        "broad_family": DEFAULT_FAMILY,
        "generated_at": now.isoformat(),
        "status": "unresolved",
        "alternate_selected": False,
    }
    item = _active_broad_item(plan)
    if item is None:
        return None, {**observation, "status": "no_active_global_all_country_gap"}
    observation["plan_item_id"] = item.get("plan_item_id")
    observation["objective_gap_jpy"] = round(_number(item.get("remaining_jpy")))

    if any(
        isinstance(row, dict)
        and canonical_ticker(row.get("ticker")) == DEFAULT_TICKER
        and str(row.get("type") or "").lower() in {"buy", "add", "dca"}
        for row in existing_actions or []
    ):
        return None, {**observation, "status": "existing_default_candidate"}

    metadata = broad_execution_metadata(DEFAULT_TICKER)
    if not metadata or metadata.get("broad_family") != DEFAULT_FAMILY:
        return None, {**observation, "status": "metadata_unresolved"}
    route, route_status = _exact_route(route_config or {}, ticker=DEFAULT_TICKER)
    if route is None:
        return None, {**observation, "status": route_status}
    if str(route.get("currency") or "").upper() != str(metadata.get("listing_currency") or ""):
        return None, {**observation, "status": "route_currency_mismatch"}
    wallet, wallet_status = _wallet_for_route(plan, route)
    if wallet is None:
        return None, {**observation, "status": wallet_status, "route_id": route.get("route_id")}
    price_value = _number(price)
    fx = _number(fx_rate_usdjpy)
    if price_value <= 0 or (route.get("currency") == "USD" and not 50 < fx < 500):
        return None, {**observation, "status": "price_or_fx_unresolved", "route_id": route.get("route_id")}

    budgets = plan.get("budgets") if isinstance(plan.get("budgets"), dict) else {}
    available_jpy = _number(wallet.get("available_after_all_reservations_jpy"))
    amount_cap = min(
        float(max_single_jpy),
        _number(item.get("remaining_jpy")),
        _number(budgets.get("normal_pool_available_jpy")),
        _number(budgets.get("weekly_normal_jpy")),
        available_jpy,
    )
    unit_jpy = price_value * (fx if route.get("currency") == "USD" else 1.0)
    quantity = int(math.floor(amount_cap / unit_jpy))
    notional_jpy = quantity * unit_jpy
    if quantity <= 0 or notional_jpy < float(min_notional_jpy):
        return None, {
            **observation,
            "status": "below_minimum_executable_notional",
            "route_id": route.get("route_id"),
            "amount_cap_jpy": round(amount_cap),
        }

    concentration = _concentration(
        ticker=DEFAULT_TICKER,
        family=DEFAULT_FAMILY,
        notional_jpy=notional_jpy,
        policy_observation=policy_observation or {},
    )
    observation["concentration"] = concentration
    if concentration.get("status") != "ok":
        return None, {**observation, "status": str(concentration.get("status"))}
    post_instrument = _number(concentration.get("post_trade_instrument_decimal"))
    post_family = _number(concentration.get("post_trade_family_decimal"))
    if (
        post_instrument >= _number(concentration.get("cap_decimal"))
        or post_family >= _number(concentration.get("family_cap_decimal"))
    ):
        return None, {**observation, "status": "default_concentration_cap_reached"}
    caution = post_instrument >= _number(concentration.get("caution_decimal"))
    label = quantity_label_for_ticker(DEFAULT_TICKER)
    action = {
        "ticker": DEFAULT_TICKER,
        "type": "buy",
        "tier": "Long",
        "source": "scheduled_broad_deployment",
        "strategy_class": "scheduled_broad_deployment",
        "human_execution_only": True,
        "deterministic_candidate": True,
        "confidence_not_applicable": True,
        "urgency": "medium",
        "quantity": quantity,
        "requested_buy_quantity": quantity,
        "amount_hint": f"{quantity}{label}",
        "currency": route.get("currency"),
        "decision_price": price_value,
        "limit_price": price_value,
        "order_type": "limit",
        "estimated_notional_jpy": round(notional_jpy),
        "execution_owner": route.get("owner"),
        "execution_broker": route.get("broker"),
        "execution_account": route.get("account"),
        "execution_investment_type": "long",
        "settlement_pool": "broker_cash",
        "cash_route": route.get("cash_route"),
        "cash_wallet_key": wallet.get("wallet_key"),
        "broad_family": DEFAULT_FAMILY,
        "plan_item_id": item.get("plan_item_id"),
        "execution_plan_objective_match": True,
        "objective_gap_jpy": round(_number(item.get("remaining_jpy"))),
        "objective_gap_closure_jpy": round(notional_jpy),
        "post_trade_concentration_decimal": post_instrument,
        "post_trade_family_concentration_decimal": post_family,
        "concentration_caution_decimal": concentration.get("caution_decimal"),
        "concentration_cap_decimal": concentration.get("cap_decimal"),
        "concentration_review_required": caution,
        "route_id": route.get("route_id"),
        "price_as_of": now.isoformat(),
        "action": f"{DEFAULT_TICKER}を{quantity}{label}、指値{price_value:g}で広域コア配備候補として買付",
        "reason": (
            f"余剰現金のactive plan item {item.get('plan_item_id')} に対する決定論的広域配備。"
            f"gap ¥{_number(item.get('remaining_jpy')):,.0f}のうち約¥{notional_jpy:,.0f}を閉じる"
        ),
    }
    return action, {
        **observation,
        "status": "caution_review" if caution else "candidate_generated",
        "route_id": route.get("route_id"),
        "wallet_key": wallet.get("wallet_key"),
        "candidate_notional_jpy": round(notional_jpy),
        "concentration_review_required": caution,
    }


def generate_from_analysis_context(
    *,
    base_dir: Path,
    execution_plan: dict[str, Any] | None,
    policy_observation: dict[str, Any] | None,
    existing_actions: list[dict[str, Any]] | None,
    fx_rate_usdjpy: float,
    now: datetime,
    price_provider: Callable[[str, str], float | None] | None = None,
    min_notional_jpy: int = 150_000,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    route_config = load_route_config(base_dir=base_dir)
    should_fetch_price = (
        _active_broad_item(execution_plan or {}) is not None
        and _exact_route(route_config, ticker=DEFAULT_TICKER)[0] is not None
        and not any(
            isinstance(row, dict)
            and canonical_ticker(row.get("ticker")) == DEFAULT_TICKER
            and str(row.get("type") or "").lower() in {"buy", "add", "dca"}
            for row in existing_actions or []
        )
    )
    if price_provider is None:
        from portfolio_manager import get_current_price

        price_provider = get_current_price
    price = price_provider(DEFAULT_TICKER, "USD") if should_fetch_price else None
    return generate_candidate(
        execution_plan=execution_plan,
        policy_observation=policy_observation,
        route_config=route_config,
        price=price,
        fx_rate_usdjpy=fx_rate_usdjpy,
        existing_actions=existing_actions,
        now=now,
        min_notional_jpy=min_notional_jpy,
    )
