"""Weekly/monthly execution plan generation.

Phase 1 owns only the visible plan artifact:

- derive conservative weekly/monthly budgets
- build baseline plan items from existing deterministic state
- compute consumption from action_state/action_executions
- persist execution_plan_state.json

Daily AI/post-filter wiring is intentionally out of scope for this phase.
"""
from __future__ import annotations

import argparse
import calendar
import json
import math
import re
from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from instrument_metadata import canonical_ticker
from utils import atomic_write_json, load_json

BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "execution_plan_state.json"
SCHEMA_VERSION = 2

# blocking consumption は spec (execution_plan_spec.md) 通り placed / active ordered / filled のみ。
# pending = 未発注の推奨。予算を塞ぐと「推奨しただけで再提案を抑止する」問題が再発するため、
# remaining には影響しない参考集計 (pending_recommendation_*) に分離する。
OPEN_ACTION_STATE_STATUSES = {"placed"}
PENDING_ACTION_STATE_STATUSES = {"pending"}
FILLED_ACTION_STATE_STATUSES = {"filled"}
OPEN_EXECUTION_STATUSES = {"ordered"}
# actions API treats ``partial`` as an applied fill for the quantity on that
# record.  Any remaining quantity must be represented by another ordered
# record, so plan consumption must match the ledger semantics here as well.
FILLED_EXECUTION_STATUSES = {"executed", "partial", "filled", "done"}


def _tp_get(key: str, fallback: Any) -> Any:
    try:
        from tunable_params import get as get_tunable

        return get_tunable(key, fallback)
    except Exception:
        return fallback


def _jpy(value: Any) -> int:
    try:
        if value is None or value == "":
            return 0
        number = float(value)
        if not math.isfinite(number):
            return 0
        return max(0, int(round(number)))
    except (TypeError, ValueError):
        return 0


def _ratio(value: Any, fallback: float) -> float:
    try:
        number = float(value)
        if math.isfinite(number):
            return number
    except (TypeError, ValueError):
        pass
    return fallback


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value))
        except Exception:
            return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[: len(fmt)], fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _month_end(day: date) -> date:
    return date(day.year, day.month, calendar.monthrange(day.year, day.month)[1])


def horizon_for(day: date | datetime | None = None) -> dict[str, Any]:
    if day is None:
        day = date.today()
    if isinstance(day, datetime):
        day = day.date()
    week_start = day - timedelta(days=day.weekday())
    week_end = week_start + timedelta(days=6)
    month_end = _month_end(day)
    week_cursor = week_start
    remaining_weeks = 0
    while week_cursor <= month_end:
        remaining_weeks += 1
        week_cursor += timedelta(days=7)
    return {
        "month": f"{day.year:04d}-{day.month:02d}",
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "month_end": month_end.isoformat(),
        "remaining_weeks_in_month": max(1, remaining_weeks),
        "iso_week": day.isocalendar().week,
    }


def load_plan_params() -> dict[str, Any]:
    return {
        # Confirmed portfolio cash is deployable above the current tactical
        # cash target.  This legacy explicit amount remains an optional floor;
        # the shipped value is zero because the surplus-cash contract is the
        # normal source of buying power.
        "monthly_discretionary_budget_jpy": _jpy(
            _tp_get("execution_plan_monthly_discretionary_budget_jpy", 0)
        ),
        "default_monthly_budget_jpy": _jpy(_tp_get("execution_plan_default_monthly_budget_jpy", 300_000)),
        "cash_deploy_pct": _ratio(_tp_get("execution_plan_cash_deploy_pct", 0.05), 0.05),
        # Retained only for backwards-compatible tunable snapshots.  The
        # dynamic surplus policy no longer applies a fixed monthly ceiling.
        "max_monthly_budget_jpy": _jpy(_tp_get("execution_plan_max_monthly_budget_jpy", 0)),
        "weekly_normal_budget_pct": _ratio(_tp_get("execution_plan_weekly_normal_budget_pct", 0.70), 0.70),
        "opportunity_reserve_pct": _ratio(_tp_get("execution_plan_opportunity_reserve_pct", 0.25), 0.25),
        "max_single_normal_jpy": _jpy(_tp_get("execution_plan_max_single_normal_jpy", 250_000)),
        "max_single_opportunity_jpy": _jpy(_tp_get("execution_plan_max_single_opportunity_jpy", 300_000)),
        "cash_stale_hours": _jpy(_tp_get("execution_plan_cash_stale_hours", 72)),
        "max_single_action_pct_of_portfolio": _ratio(_tp_get("max_single_action_pct_of_portfolio", 0.05), 0.05),
    }


def derive_cash_info(
    account: dict[str, Any] | None,
    *,
    holdings: dict[str, Any] | list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    stale_hours: int = 72,
) -> tuple[dict[str, Any], list[str]]:
    """Return confirmed portfolio cash without wall-clock expiry.

    Cash authority is event-based: time alone does not spend cash.  A later
    order/fill is reserved and checked by ``execution_readiness`` against the
    routed account resource.  Manual activity outside ALMANAC must still be
    imported or confirmed on the Web UI.

    ``stale_hours`` remains in the signature for compatibility with older
    callers and tunable snapshots; it is intentionally not an authority rule.
    """
    del now, stale_hours
    warnings: list[str] = []
    resources: list[dict[str, Any]] = []
    wallet_totals: dict[str, dict[str, Any]] = {}
    account = account if isinstance(account, dict) else {}

    def _number(value: Any) -> float | None:
        try:
            number = float(value)
            return number if math.isfinite(number) and number >= 0 else None
        except (TypeError, ValueError):
            return None

    jpy_balance = _number(account.get("balance")) or 0.0
    usd_balance = _number(account.get("usd_balance")) or 0.0
    fx_rate = _number(account.get("fx_rate_usdjpy"))
    account_as_of = next(
        (
            account.get(key)
            for key in ("broker_reconciled_at", "source_as_of", "last_updated", "as_of")
            if _parse_dt(account.get(key)) is not None
        ),
        None,
    )
    account_confirmed = bool(account) and account_as_of is not None
    if not account:
        warnings.append("cash_info_missing: account.json missing or empty")
    elif account_as_of is None:
        warnings.append("cash_info_authority_missing: account cash has no source timestamp")

    account_jpy = jpy_balance if account_confirmed else 0.0
    account_usd = usd_balance if account_confirmed and fx_rate and fx_rate > 0 else 0.0
    def _append_resource(resource: dict[str, Any]) -> None:
        owner = str(resource.get("owner") or "")
        broker = str(resource.get("broker") or "")
        settlement_pool = str(resource.get("settlement_pool") or "")
        currency = str(resource.get("currency") or "").upper()
        if not owner or not broker or not settlement_pool or not currency:
            warnings.append(
                f"cash_resource_wallet_identity_missing:{resource.get('resource') or '?'}"
            )
            return
        wallet_key = f"{owner}|{broker}|{settlement_pool}|{currency}"
        normalized = {
            **resource,
            "owner": owner,
            "broker": broker,
            "settlement_pool": settlement_pool,
            "currency": currency,
            "wallet_key": wallet_key,
        }
        resources.append(normalized)
        wallet = wallet_totals.setdefault(wallet_key, {
            "wallet_key": wallet_key,
            "owner": owner,
            "broker": broker,
            "settlement_pool": settlement_pool,
            "currency": currency,
            "available_native": 0.0,
            "available_jpy": 0,
            "resources": [],
            "source_as_of": normalized.get("source_as_of"),
            "source_as_of_status": "resolved",
        })
        wallet["available_native"] += float(normalized.get("available_native") or 0)
        wallet["available_jpy"] += _jpy(normalized.get("available_jpy"))
        wallet["resources"].append(str(normalized.get("resource") or ""))
        incoming_as_of = normalized.get("source_as_of")
        existing_as_of = wallet.get("source_as_of")
        if _parse_dt(incoming_as_of) is None or _parse_dt(existing_as_of) is None:
            wallet["source_as_of"] = None
            wallet["source_as_of_status"] = "unresolved"
        elif _parse_dt(incoming_as_of) != _parse_dt(existing_as_of):
            wallet["source_as_of"] = None
            wallet["source_as_of_status"] = "multiple_resource_timestamps"

    if account_confirmed and jpy_balance:
        _append_resource({
            "resource": "account.json:JPY",
            "owner": "husband",
            "broker": "rakuten",
            "settlement_pool": "broker_cash",
            "currency": "JPY",
            "available_native": account_jpy,
            "available_jpy": round(account_jpy),
            "source_as_of": account_as_of,
        })
    if account_confirmed and usd_balance:
        if fx_rate and fx_rate > 0:
            _append_resource({
                "resource": "account.json:USD",
                "owner": "husband",
                "broker": "rakuten",
                "settlement_pool": "broker_cash",
                "currency": "USD",
                "available_native": usd_balance,
                "available_jpy": round(account_usd * fx_rate),
                "source_as_of": account_as_of,
            })
        else:
            warnings.append("cash_info_missing_fx: confirmed USD cash excluded from deployment budget")

    additional_cash_jpy = 0.0
    holding_rows = holdings.values() if isinstance(holdings, dict) else (holdings or [])
    for row in holding_rows:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("ticker") or "").upper()
        # CASH_JPY/CASH_USD mirror account.json and must not be double-counted.
        if not ticker.startswith("CASH_") or ticker in {"CASH_JPY", "CASH_USD"}:
            continue
        if row.get("reconciliation_required") is True:
            warnings.append(f"cash_resource_unconfirmed:{ticker}:reconciliation_required")
            continue
        status = str(row.get("balance_status") or "confirmed").lower()
        if status != "confirmed":
            warnings.append(f"cash_resource_unconfirmed:{ticker}:status={status}")
            continue
        source_as_of = next(
            (
                row.get(key)
                for key in (
                    "broker_reconciled_at", "source_as_of", "reported_as_of",
                    "reconciled_at", "last_updated", "as_of",
                )
                if _parse_dt(row.get(key)) is not None
            ),
            None,
        )
        if source_as_of is None:
            warnings.append(f"cash_resource_authority_missing:{ticker}")
            continue
        currency = "USD" if "_USD" in ticker else "JPY"
        try:
            from execution_safety import canonical_broker, canonical_owner
            owner = canonical_owner(row.get("owner"))
            broker = canonical_broker(row.get("broker"))
        except Exception:
            owner = broker = ""
        # These synthetic cash rows are stable internal routes, not market
        # instruments.  Older snapshots may predate explicit owner/broker
        # fields, so recover only identities encoded by the exact route key.
        if ticker == "CASH_JPY_SBI_WIFE":
            owner, broker = "wife", "sbi"
        elif ticker == "CASH_JPY_SBI":
            owner, broker = "husband", "sbi"
        if not owner or not broker:
            warnings.append(f"cash_resource_wallet_identity_missing:{ticker}")
            continue
        if currency == "JPY":
            native = _number(row.get("available_to_trade_jpy"))
            if native is None:
                native = _number(row.get("shares"))
            available_jpy = native
        else:
            native = _number(row.get("available_to_trade_usd"))
            if native is None:
                native = _number(row.get("shares"))
            available_jpy = native * fx_rate if native is not None and fx_rate else None
        if native is None or available_jpy is None:
            warnings.append(f"cash_resource_amount_unresolved:{ticker}")
            continue
        additional_cash_jpy += available_jpy
        _append_resource({
            "resource": ticker,
            "owner": owner,
            "broker": broker,
            "settlement_pool": "broker_cash",
            "currency": currency,
            "available_native": native,
            "available_jpy": round(available_jpy),
            "source_as_of": source_as_of,
        })

    usd_as_jpy = round(account_usd * fx_rate) if fx_rate else 0
    total_cash_jpy = account_jpy + usd_as_jpy + additional_cash_jpy
    valid_for_budget = bool(resources)
    return {
        "jpy_cash": account_jpy,
        "usd_cash": account_usd,
        "fx_rate_usdjpy": fx_rate,
        "usd_as_jpy": usd_as_jpy,
        "additional_confirmed_cash_jpy": round(additional_cash_jpy),
        "total_cash_jpy": round(total_cash_jpy),
        "account_last_updated": account_as_of,
        "valid_for_budget": valid_for_budget,
        "source": (
            "confirmed_cash_resources_event_based"
            if valid_for_budget else "missing_confirmed_cash_resources"
        ),
        "validation_mode": "event_based",
        "assumption": "no_unreported_external_activity",
        "resources": resources,
        "wallet_contract_version": 1,
        "wallets": sorted(wallet_totals.values(), key=lambda row: row["wallet_key"]),
    }, warnings


def resolve_cash_target_policy(
    *,
    ai_analysis: dict[str, Any] | None = None,
    market_regime: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return the latest deterministic tactical-cash target, fail-closed."""
    candidates: list[tuple[str, Any]] = []
    if isinstance(market_regime, dict):
        candidates.append(("market_regime_v2_state", market_regime.get("assessment", market_regime)))
    if isinstance(ai_analysis, dict):
        candidates.append(("ai_analysis.market_regime_v2", ai_analysis.get("market_regime_v2")))
        synthesis = ai_analysis.get("synthesis")
        if isinstance(synthesis, dict):
            candidates.append(("ai_analysis.synthesis.market_regime_v2", synthesis.get("market_regime_v2")))

    for source, candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("mode") or "").lower() in {"off", "shadow"}:
            continue
        if str(candidate.get("status") or "").lower() in {"review", "error", "off"}:
            continue
        policy = candidate.get("policy")
        if not isinstance(policy, dict):
            continue
        try:
            target = float(policy.get("cash_target_pct"))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(target) or not 0 <= target <= 100:
            continue
        return {
            "cash_target_pct": target,
            "source": source,
            "policy_version": policy.get("policy_version") or candidate.get("policy_version"),
            "portfolio_label": policy.get("portfolio_label")
            or (candidate.get("portfolio") or {}).get("committed_label"),
            "portfolio_level": policy.get("portfolio_level")
            if policy.get("portfolio_level") is not None
            else (candidate.get("portfolio") or {}).get("committed_level"),
            "shock_active": bool((candidate.get("shock") or {}).get("active")),
        }
    return {
        "cash_target_pct": None,
        "source": "unresolved",
        "policy_version": None,
        "portfolio_label": None,
        "portfolio_level": None,
        "shock_active": False,
    }


def scheduled_contribution_amount(occurrences: list[tuple[date, dict[str, Any]]]) -> int:
    total = 0
    for _, contribution in occurrences:
        if not isinstance(contribution, dict):
            continue
        if str(contribution.get("currency") or "JPY").upper() != "JPY":
            continue
        total += _jpy(contribution.get("amount"))
    return total


def _guard_blocks_new_deployment(guard: dict[str, Any] | None) -> bool:
    if not isinstance(guard, dict):
        return False
    if guard.get("trading_allowed") is False or guard.get("new_entry_allowed") is False:
        return True
    try:
        if int(guard.get("guardrail_stage") or 0) >= 3:
            return True
    except (TypeError, ValueError):
        pass
    stage = str(guard.get("loss_guard_stage") or "").lower()
    return stage in {"block", "daily_block", "monthly_block", "stage_3"}


def _guard_caution_multiplier(guard: dict[str, Any] | None) -> float:
    # v7 has no implicit risk-factor sizing.  Loss/DD controls either block a
    # new deployment or send an explicit human-review plan; they never turn
    # into automatic 50%/25% cash deployment.
    return 1.0


def derive_budgets(
    *,
    cash_info: dict[str, Any],
    guard: dict[str, Any] | None,
    params: dict[str, Any],
    scheduled_contributions_jpy: int,
    horizon: dict[str, Any],
    contribution_summary: dict[str, Any] | None = None,
    cash_target_policy: dict[str, Any] | None = None,
    monthly_consumption: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Derive a paced budget from confirmed cash above the tactical target.

    ``scheduled_contributions_jpy`` is retained as an observability field for
    broker-managed recurring investments.  Those buys happen on another path,
    so they must not enlarge ALMANAC's discretionary pool.
    """
    warnings: list[str] = []
    contribution_summary = contribution_summary if isinstance(contribution_summary, dict) else {}
    explicit_monthly = _jpy(params.get("monthly_discretionary_budget_jpy"))
    monthly_consumption = (
        monthly_consumption if isinstance(monthly_consumption, dict) else {}
    )

    try:
        from investment_policy import cash_deployment_horizon, cash_deployment_policy

        deployment_policy = cash_deployment_policy()
        deployment_horizon = cash_deployment_horizon(
            portfolio_level=cash_target_policy.get("portfolio_level")
            if isinstance(cash_target_policy, dict) else None,
            portfolio_label=cash_target_policy.get("portfolio_label")
            if isinstance(cash_target_policy, dict) else None,
            shock_active=bool((cash_target_policy or {}).get("shock_active")),
        )
    except Exception:
        deployment_policy = {
            "all_system_cash_is_surplus": False,
            "protected_cash_reserve_jpy": 0,
        }
        deployment_horizon = {
            "resolved": False,
            "deployment_months": None,
            "ordinary_deployment_allowed": False,
            "policy_version": None,
            "reason": "deployment_policy_unavailable",
        }
    all_cash_is_surplus = bool(deployment_policy.get("all_system_cash_is_surplus"))
    protected_reserve = _jpy(deployment_policy.get("protected_cash_reserve_jpy"))
    if (
        deployment_horizon.get("resolved")
        and not deployment_horizon.get("ordinary_deployment_allowed")
    ):
        explicit_monthly = 0
    cash_target_policy = cash_target_policy if isinstance(cash_target_policy, dict) else {}
    cash_target_pct_raw = cash_target_policy.get("cash_target_pct")
    try:
        cash_target_pct = float(cash_target_pct_raw)
    except (TypeError, ValueError):
        cash_target_pct = None
    if cash_target_pct is not None and (
        not math.isfinite(cash_target_pct) or not 0 <= cash_target_pct <= 100
    ):
        cash_target_pct = None

    portfolio_total = _jpy((guard or {}).get("portfolio_value"))
    confirmed_cash = _jpy(cash_info.get("total_cash_jpy"))
    tactical_reserve = (
        _jpy(portfolio_total * cash_target_pct / 100)
        if portfolio_total > 0 and cash_target_pct is not None else None
    )
    required_reserve = (
        max(protected_reserve, tactical_reserve)
        if tactical_reserve is not None else protected_reserve
    )
    surplus_cash = 0
    surplus_monthly_capacity = 0
    contribution_filled_this_month = (
        _jpy(contribution_summary.get("filled_this_month_normal_jpy"))
        + _jpy(contribution_summary.get("filled_this_month_opportunity_jpy"))
    )
    base_filled_buys = max(
        0,
        _jpy(monthly_consumption.get("monthly_filled_consumed_jpy"))
        + _jpy(monthly_consumption.get("unattributed_monthly_buy_filled_notional_jpy"))
        - contribution_filled_this_month,
    )
    filled_sells = _jpy(
        monthly_consumption.get("unattributed_monthly_sell_filled_notional_jpy")
    )
    deployment_basis_cash = max(0, confirmed_cash + base_filled_buys - filled_sells)
    deployment_basis_surplus = 0
    deployment_months = deployment_horizon.get("deployment_months")
    if all_cash_is_surplus and cash_info.get("valid_for_budget"):
        if tactical_reserve is None:
            warnings.append("cash_target_unresolved: confirmed cash not converted into deployment budget")
        elif not deployment_horizon.get("resolved"):
            warnings.append("deployment_horizon_unresolved: ordinary deployment budget disabled")
        else:
            surplus_cash = max(0, confirmed_cash - required_reserve)
            deployment_basis_surplus = max(0, deployment_basis_cash - required_reserve)
            if deployment_months is not None and int(deployment_months) > 0:
                surplus_monthly_capacity = _jpy(
                    deployment_basis_surplus / int(deployment_months)
                )
            else:
                warnings.append(
                    "ordinary_deployment_disabled_for_regime: use active DCA/playbook only"
                )
    elif all_cash_is_surplus:
        warnings.append("cash_authority_unresolved: confirmed cash not converted into deployment budget")

    base_monthly = max(explicit_monthly, surplus_monthly_capacity)
    released_normal = _jpy(contribution_summary.get("released_this_month_normal_jpy"))
    released_opportunity = _jpy(contribution_summary.get("released_this_month_opportunity_jpy"))
    monthly_total = base_monthly + released_normal + released_opportunity
    if surplus_monthly_capacity > explicit_monthly:
        budget_source = "confirmed_surplus_cash"
    elif explicit_monthly > 0:
        budget_source = "explicit_policy"
    else:
        budget_source = "no_deployable_surplus"
    if scheduled_contributions_jpy:
        warnings.append("scheduled_contributions_excluded_from_discretionary_budget")

    deployment_multiplier = 1.0
    if _guard_blocks_new_deployment(guard):
        deployment_multiplier = 0.0
        warnings.append("budget_guard_block: trading/new entry guard blocks normal deployment")
    else:
        multiplier = _guard_caution_multiplier(guard)
        if multiplier < 1.0:
            deployment_multiplier = multiplier
            warnings.append(f"budget_guard_scaled: normal deployment scaled by {multiplier:.2f}")

    base_monthly = _jpy(base_monthly * deployment_multiplier)
    released_normal = _jpy(released_normal * deployment_multiplier)
    released_opportunity = _jpy(released_opportunity * deployment_multiplier)
    monthly_total = base_monthly + released_normal + released_opportunity

    remaining_weeks = max(1, int(horizon.get("remaining_weeks_in_month") or 1))
    # The recurring policy is normal capital.  Approved contributions carry
    # their own bucket (normal/opportunity), so reserve allocation is explicit
    # rather than silently carved out of a salary or bonus.

    h2_pct = _ratio(params.get("max_single_action_pct_of_portfolio"), 0.05)
    h2_hard_cap = min(_jpy(portfolio_total * h2_pct), 1_500_000) if portfolio_total > 0 else 1_500_000

    max_single_normal = min(_jpy(params.get("max_single_normal_jpy")), h2_hard_cap)
    max_single_opp = min(_jpy(params.get("max_single_opportunity_jpy")), h2_hard_cap)

    weekly_normal = _jpy((base_monthly + released_normal) / remaining_weeks)
    weekly_opportunity = _jpy(released_opportunity / remaining_weeks)
    return {
        "monthly_total_jpy": _jpy(monthly_total),
        "monthly_discretionary_budget_jpy": base_monthly,
        "approved_contribution_released_this_month_jpy": released_normal + released_opportunity,
        "approved_contribution_released_normal_jpy": released_normal,
        "approved_contribution_released_opportunity_jpy": released_opportunity,
        # Filled/open executions attached to a contribution are deducted later
        # by build_execution_plan.  The common pool intentionally carries
        # unused releases into later months.
        "normal_pool_available_jpy": 0,
        "opportunity_pool_available_jpy": 0,
        "weekly_normal_jpy": weekly_normal,
        "weekly_opportunity_reserve_jpy": weekly_opportunity,
        "weekly_defensive_reserve_jpy": 0,
        "max_single_normal_action_jpy": max_single_normal,
        "max_single_opportunity_action_jpy": max_single_opp,
        "h2_hard_cap_jpy": h2_hard_cap,
        "deployment_multiplier": deployment_multiplier,
        "budget_source": budget_source,
        "all_system_cash_is_surplus": all_cash_is_surplus,
        "confirmed_cash_jpy": confirmed_cash,
        "cash_target_pct": cash_target_pct,
        "cash_target_source": cash_target_policy.get("source"),
        "cash_target_policy_version": cash_target_policy.get("policy_version"),
        "tactical_cash_reserve_jpy": tactical_reserve,
        "protected_cash_reserve_jpy": protected_reserve,
        "required_cash_reserve_jpy": required_reserve,
        "surplus_cash_above_targets_jpy": surplus_cash,
        "deployment_basis_cash_jpy": deployment_basis_cash,
        "deployment_basis_surplus_jpy": deployment_basis_surplus,
        "deployment_basis_filled_buys_added_back_jpy": base_filled_buys,
        "deployment_basis_sell_proceeds_removed_jpy": filled_sells,
        "deployment_basis_method": (
            "confirmed_cash_plus_base_filled_buys_minus_filled_sell_proceeds"
        ),
        "deployment_months": deployment_months,
        "deployment_regime_level": deployment_horizon.get("portfolio_level"),
        "deployment_regime_label": deployment_horizon.get("portfolio_label"),
        "deployment_policy_version": deployment_horizon.get("policy_version"),
        "ordinary_deployment_allowed": deployment_horizon.get(
            "ordinary_deployment_allowed", False
        ),
        "surplus_cash_monthly_capacity_jpy": _jpy(
            surplus_monthly_capacity * deployment_multiplier
        ),
        "scheduled_contributions_remaining_jpy": _jpy(scheduled_contributions_jpy),
    }, warnings


def _slug(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "item"


def _account_bucket_from_hint(account_hint: str | None) -> str:
    text = (account_hint or "").lower()
    if "wife" in text or "妻" in text:
        return "wife_nisa"
    if "husband" in text or "夫" in text:
        return "husband_nisa"
    if "margin" in text or "信用" in text:
        return "margin"
    return "default"


def load_trusted_sector_catalog(
    *,
    base_dir: Path = BASE_DIR,
    now: datetime | None = None,
    cache_ttl_days: int = 8,
) -> tuple[dict[str, dict[str, str]], dict[str, Any], list[str]]:
    """Load deterministic ticker-to-sector mappings with explicit provenance.

    Curated long-term metadata and portfolio holdings are stable local sources.
    The screener cache is accepted only while ``cached_at`` is fresh.  Unknown,
    catch-all, ETF, and Cash labels are not trusted for sector plan matching.
    """
    now = now or datetime.now()
    comparison_now = now.replace(tzinfo=None) if now.tzinfo else now
    catalog: dict[str, dict[str, str]] = {}
    warnings: list[str] = []
    source_counts: dict[str, int] = {}
    invalid_labels = {"", "unknown", "other", "不明", "cash", "etf"}

    def _add(ticker: Any, sector: Any, *, source: str, as_of: Any = None) -> None:
        ticker_text = canonical_ticker(ticker)
        sector_text = str(sector or "").strip()
        if not ticker_text or sector_text.lower() in invalid_labels or ticker_text in catalog:
            return
        catalog[ticker_text] = {
            "sector": sector_text,
            "source": source,
            "as_of": str(as_of or ""),
        }
        source_counts[source] = source_counts.get(source, 0) + 1

    curated = load_json(base_dir / "long_term_meta.json", {})
    if isinstance(curated, dict):
        for ticker, info in curated.items():
            if isinstance(info, dict):
                _add(ticker, info.get("sector"), source="long_term_meta")

    # ``portfolio_manager.MANUAL_SECTORS`` is the deterministic override used
    # by the portfolio snapshot when yfinance reports a catch-all ETF label.
    # Reuse it here so allocation gaps and executable-plan preferred tickers do
    # not disagree (notably XLF: ETF -> Financial Services).
    try:
        from portfolio_manager import MANUAL_SECTORS

        override_holdings = load_json(base_dir / "holdings.json", {})
        held_tickers = {
            str((info or {}).get("ticker") or key).strip().upper()
            for key, info in (override_holdings.items() if isinstance(override_holdings, dict) else [])
            if isinstance(info, dict)
        }
        held_keys = {
            str(key).strip().upper()
            for key in (override_holdings if isinstance(override_holdings, dict) else {})
        }
        for ticker, sector in MANUAL_SECTORS.items():
            ticker_upper = str(ticker).strip().upper()
            if ticker_upper in held_tickers or ticker_upper in held_keys:
                _add(ticker, sector, source="manual_sector_override")
    except Exception as exc:
        warnings.append(f"manual_sector_override_unavailable: {type(exc).__name__}")

    holdings = load_json(base_dir / "holdings.json", {})
    if isinstance(holdings, dict):
        for key, info in holdings.items():
            if isinstance(info, dict):
                _add(info.get("ticker") or key, info.get("sector"), source="holdings")

    sector_cache = load_json(base_dir / "data" / "sector_cache.json", {})
    stale_cache_count = 0
    if isinstance(sector_cache, dict):
        for ticker, info in sector_cache.items():
            if not isinstance(info, dict):
                continue
            cached_at = _parse_dt(info.get("cached_at"))
            if cached_at is None or comparison_now - cached_at > timedelta(days=cache_ttl_days):
                stale_cache_count += 1
                continue
            _add(ticker, info.get("sector"), source="sector_cache", as_of=info.get("cached_at"))
    if stale_cache_count:
        warnings.append(f"sector_cache_stale_rows_skipped: {stale_cache_count}")

    summary = {
        "trusted_ticker_count": len(catalog),
        "source_counts": source_counts,
        "cache_ttl_days": cache_ttl_days,
    }
    if not catalog:
        warnings.append("trusted_sector_catalog_empty: sector objectives will be omitted")
    return catalog, summary, warnings


def _trusted_sector_tickers(catalog: dict[str, Any] | None, sector: str) -> list[str]:
    target = str(sector or "").strip().casefold()
    if not target or not isinstance(catalog, dict):
        return []
    matches = []
    for ticker, info in catalog.items():
        mapped = info.get("sector") if isinstance(info, dict) else info
        if str(mapped or "").strip().casefold() == target:
            matches.append(canonical_ticker(ticker))
    return sorted(set(matches))


def _dedup_keys_for_item(preferred_tickers: list[str], allowed_action_types: list[str], account_hint: str | None) -> list[str]:
    try:
        from action_state_tracker import dedup_key
    except Exception:
        def dedup_key(ticker: str, action_type: str | None, account_bucket: str = "default") -> str:  # type: ignore
            return f"{ticker}|{action_type or 'other'}|{account_bucket}"

    bucket = _account_bucket_from_hint(account_hint)
    keys: list[str] = []
    for ticker in preferred_tickers:
        ticker = canonical_ticker(ticker)
        for action_type in allowed_action_types:
            keys.append(dedup_key(ticker, action_type, bucket))
            if bucket != "default":
                keys.append(dedup_key(ticker, action_type, "default"))
    return sorted(set(keys))


def _objective_item(
    *,
    month: str,
    iso_week: int,
    seq: int,
    objective: str,
    priority: int,
    requested_jpy: int,
    source_reasons: list[str],
    constraints: dict[str, Any] | None = None,
    allowed_action_types: list[str] | None = None,
    preferred_tickers: list[str] | None = None,
    budget_bucket: str = "normal",
    horizon: str = "weekly",
    shared_pool_jpy: int | None = None,
) -> dict[str, Any]:
    allowed = allowed_action_types or ["buy", "add"]
    tickers = preferred_tickers or []
    constraints = constraints or {}
    account_hint = constraints.get("account_hint")
    return {
        "plan_item_id": f"{month}-w{iso_week:02d}-{_slug(objective)}-{seq:03d}",
        # Weekly plan items are regenerated, whereas this identifier remains
        # stable through a calendar month and is persisted on recommendations.
        # It is used only for month-to-date accounting, never as a fuzzy match.
        "monthly_objective_id": f"{month}:{budget_bucket}:{_slug(objective)}",
        "horizon": horizon,
        "objective": objective,
        "priority": priority,
        "status": "active",
        # A shared pool makes the objective a priority label rather than an
        # independent wallet.  Batch allocation reserves this capital once.
        "normal_budget_jpy": _jpy(shared_pool_jpy if shared_pool_jpy is not None else requested_jpy),
        "requested_jpy": _jpy(requested_jpy),
        "shared_pool_jpy": _jpy(shared_pool_jpy) if shared_pool_jpy is not None else None,
        "consumed_jpy": 0,
        "open_order_consumed_jpy": 0,
        "filled_consumed_jpy": 0,
        "remaining_jpy": _jpy(shared_pool_jpy if shared_pool_jpy is not None else requested_jpy),
        "budget_bucket": budget_bucket,
        "allowed_action_types": allowed,
        "preferred_tickers": tickers,
        "dedup_keys": _dedup_keys_for_item(tickers, allowed, account_hint),
        "constraints": constraints,
        "consumed_by": [],
        "source_reasons": source_reasons,
        "today_decision": {"decision": "unreviewed", "reason": ""},
    }


def _nisa_growth_remaining(owner: dict[str, Any] | None) -> int:
    if not isinstance(owner, dict):
        return 0
    return max(0, _jpy(owner.get("growth_limit_annual")) - _jpy(owner.get("growth_used_this_year")))


def _candidate_objectives(
    *,
    rebalance_report: dict[str, Any] | None,
    bottom_fishing: dict[str, Any] | None,
    nisa: dict[str, Any] | None,
    trusted_sector_catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    objectives: list[dict[str, Any]] = []

    nisa = nisa if isinstance(nisa, dict) else {}
    wife_remaining = _nisa_growth_remaining((nisa.get("wife") or {}))
    if wife_remaining > 0:
        objectives.append({
            "objective": "wife_nisa_growth_capacity",
            "priority": 1,
            "requested_jpy": wife_remaining,
            "constraints": {
                "account_hint": "wife_nisa_growth",
                "min_confidence_pct": 70,
            },
            "source_reasons": ["nisa_portfolio: wife growth capacity remains"],
        })

    report = rebalance_report if isinstance(rebalance_report, dict) else {}
    for c in ((report.get("buy_candidates") or {}).get("currencies") or [])[:2]:
        currency = str(c.get("currency") or "")
        gap = _jpy(c.get("gap_jpy"))
        if not currency or gap <= 0:
            continue
        objectives.append({
            "objective": f"add_currency_{currency.lower()}",
            "priority": 2,
            "requested_jpy": gap,
            "constraints": {
                "currency": currency,
                "min_confidence_pct": 70,
            },
            "source_reasons": [f"rebalance_report: {currency} under target"],
        })

    for idx, sector in enumerate(((report.get("buy_candidates") or {}).get("sectors") or [])[:3], start=1):
        name = str(sector.get("sector") or "")
        gap = _jpy(sector.get("gap_jpy"))
        if not name or gap <= 0 or name.lower() == "cash":
            continue
        preferred_tickers = _trusted_sector_tickers(trusted_sector_catalog, name)
        # A sector label alone is advisory and cannot safely consume or unlock
        # a plan budget.  Omit this objective until at least one deterministic
        # ticker mapping exists.
        if not preferred_tickers:
            continue
        objectives.append({
            "objective": f"add_sector_{_slug(name)}",
            "priority": 2 + idx,
            "requested_jpy": gap,
            "constraints": {
                "sector_preference": [name],
                "min_confidence_pct": 70,
            },
            "preferred_tickers": preferred_tickers,
            "source_reasons": [
                f"rebalance_report: {name} sector under target",
                f"trusted_sector_catalog: {len(preferred_tickers)} exact ticker mappings",
            ],
        })

    dca = bottom_fishing if isinstance(bottom_fishing, dict) else {}
    recommended = dca.get("recommended_buys") or []
    active_tranche = dca.get("active_tranche")
    if active_tranche and recommended:
        tickers = [str(r.get("ticker") or "") for r in recommended if r.get("ticker")]
        amount = _jpy(dca.get("actual_deploy_jpy")) or sum(_jpy(r.get("amount_jpy")) for r in recommended)
        if amount > 0:
            objectives.append({
                "objective": "drawdown_dca_active_tranche",
                "priority": 1,
                "requested_jpy": amount,
                "preferred_tickers": tickers,
                "allowed_action_types": ["buy", "add", "dca"],
                "budget_bucket": "opportunity",
                "constraints": {
                    "source": "drawdown_dca_engine",
                    "min_confidence_pct": 65,
                },
                "source_reasons": ["bottom_fishing_signals: active tranche"],
            })

    return objectives


def allocate_plan_items(
    *,
    objectives: list[dict[str, Any]],
    budgets: dict[str, Any],
    horizon: dict[str, Any],
    monthly_remaining_jpy: int | None = None,
    normal_pool_jpy: int | None = None,
    opportunity_pool_jpy: int | None = None,
) -> list[dict[str, Any]]:
    normal_objectives = [o for o in objectives if o.get("budget_bucket", "normal") == "normal"]
    opportunity_objectives = [o for o in objectives if o.get("budget_bucket") == "opportunity"]

    def _allocate(bucket_items: list[dict[str, Any]], total_budget: int, max_single: int) -> list[dict[str, Any]]:
        if total_budget <= 0 or not bucket_items:
            return []
        weights = [1.0 / max(1, int(o.get("priority") or 1)) for o in bucket_items]
        total_weight = sum(weights) or 1.0
        out: list[dict[str, Any]] = []
        remaining = total_budget
        for idx, (obj, weight) in enumerate(zip(bucket_items, weights), start=1):
            requested = _jpy(obj.get("requested_jpy"))
            share = _jpy(total_budget * weight / total_weight)
            amount = min(requested, max_single, share, remaining)
            if amount <= 0:
                continue
            out.append(_objective_item(
                month=str(horizon["month"]),
                iso_week=int(horizon["iso_week"]),
                seq=idx,
                objective=str(obj["objective"]),
                priority=int(obj.get("priority") or idx),
                requested_jpy=amount,
                source_reasons=list(obj.get("source_reasons") or []),
                constraints=dict(obj.get("constraints") or {}),
                allowed_action_types=list(obj.get("allowed_action_types") or ["buy", "add"]),
                preferred_tickers=list(obj.get("preferred_tickers") or []),
                budget_bucket=str(obj.get("budget_bucket") or "normal"),
            ))
            remaining -= amount
            if remaining <= 0:
                break
        return out

    def _shared_items(bucket_items: list[dict[str, Any]], pool: int, bucket: str) -> list[dict[str, Any]]:
        if pool <= 0 or not bucket_items:
            return []
        return [
            _objective_item(
                month=str(horizon["month"]),
                iso_week=int(horizon["iso_week"]),
                seq=index,
                objective=str(obj["objective"]),
                priority=int(obj.get("priority") or index),
                requested_jpy=_jpy(obj.get("requested_jpy")),
                source_reasons=list(obj.get("source_reasons") or []),
                constraints=dict(obj.get("constraints") or {}),
                allowed_action_types=list(obj.get("allowed_action_types") or ["buy", "add"]),
                preferred_tickers=list(obj.get("preferred_tickers") or []),
                budget_bucket=bucket,
                shared_pool_jpy=pool,
            )
            for index, obj in enumerate(bucket_items, start=1)
        ]

    normal_budget = _jpy(budgets.get("weekly_normal_jpy"))
    if monthly_remaining_jpy is not None:
        normal_budget = min(normal_budget, max(0, _jpy(monthly_remaining_jpy)))
    normal_items = (
        _shared_items(normal_objectives, _jpy(normal_pool_jpy), "normal")
        if normal_pool_jpy is not None
        else _allocate(
            normal_objectives,
            normal_budget,
            _jpy(budgets.get("max_single_normal_action_jpy")),
        )
    )
    monthly_after_normal = (
        None
        if monthly_remaining_jpy is None
        else max(0, _jpy(monthly_remaining_jpy) - sum(_jpy(item.get("normal_budget_jpy")) for item in normal_items))
    )
    opportunity_budget = _jpy(budgets.get("weekly_opportunity_reserve_jpy"))
    if monthly_after_normal is not None:
        opportunity_budget = min(opportunity_budget, monthly_after_normal)
    opportunity_items = (
        _shared_items(opportunity_objectives, _jpy(opportunity_pool_jpy), "opportunity")
        if opportunity_pool_jpy is not None
        else _allocate(
            opportunity_objectives,
            opportunity_budget,
            _jpy(budgets.get("max_single_opportunity_action_jpy")),
        )
    )
    return normal_items + opportunity_items


def build_plan_items(
    *,
    rebalance_report: dict[str, Any] | None,
    bottom_fishing: dict[str, Any] | None,
    nisa: dict[str, Any] | None,
    budgets: dict[str, Any],
    horizon: dict[str, Any],
    monthly_remaining_jpy: int | None = None,
    normal_pool_jpy: int | None = None,
    opportunity_pool_jpy: int | None = None,
    trusted_sector_catalog: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    objectives = _candidate_objectives(
        rebalance_report=rebalance_report,
        bottom_fishing=bottom_fishing,
        nisa=nisa,
        trusted_sector_catalog=trusted_sector_catalog,
    )
    return allocate_plan_items(
        objectives=objectives,
        budgets=budgets,
        horizon=horizon,
        monthly_remaining_jpy=monthly_remaining_jpy,
        normal_pool_jpy=normal_pool_jpy,
        opportunity_pool_jpy=opportunity_pool_jpy,
    )


def _record_dedup_key(record: dict[str, Any]) -> str:
    try:
        from action_state_tracker import dedup_key_for_action

        return dedup_key_for_action(record)
    except Exception:
        ticker = str(record.get("ticker") or "")
        action_type = str(record.get("type", record.get("action_type", record.get("direction", ""))) or "")
        return f"{ticker}|{action_type}|default"


def _record_direction(record: dict[str, Any]) -> str:
    try:
        from action_state_tracker import normalize_action_type

        return normalize_action_type(record.get("type", record.get("action_type", record.get("direction", ""))))
    except Exception:
        return str(record.get("type", record.get("action_type", record.get("direction", ""))) or "").lower()


def _record_account_bucket(record: dict[str, Any]) -> str:
    try:
        from action_state_tracker import account_bucket_for_action

        return account_bucket_for_action(record)
    except Exception:
        return "default"


def _record_currency(record: dict[str, Any]) -> str:
    ticker = str(record.get("ticker") or "")
    return str(record.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")).upper()


def _estimate_notional_jpy(record: dict[str, Any], *, fx_rate: float) -> int:
    for key in ("estimated_notional_jpy", "notional_jpy", "amount_jpy", "executed_amount_jpy"):
        amount = _jpy(record.get(key))
        if amount > 0:
            return amount

    quantity = record.get("quantity")
    if quantity in (None, ""):
        quantity = record.get("shares")
    if quantity in (None, ""):
        amount_hint = str(record.get("amount_hint") or "")
        match = re.search(r"([0-9][0-9,]*(?:\.[0-9]+)?)\s*株", amount_hint)
        if match:
            quantity = match.group(1).replace(",", "")
    try:
        qty = float(quantity)
    except (TypeError, ValueError):
        return 0
    if qty <= 0:
        return 0

    price = None
    for key in ("limit_price", "price", "decision_price"):
        value = record.get(key)
        if value not in (None, ""):
            try:
                price = float(value)
                break
            except (TypeError, ValueError):
                continue
    if price is None or price <= 0:
        return 0

    ticker = str(record.get("ticker") or "")
    currency = str(record.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")).upper()
    multiplier = fx_rate if currency == "USD" else 1.0
    return _jpy(qty * price * multiplier)


def _record_effective_date(record: dict[str, Any], consumption_type: str) -> date | None:
    keys = (
        ("filled_at", "executed_at_time", "saved_at", "recommended_at")
        if consumption_type == "filled"
        else ("placed_at", "saved_at", "recommended_at")
    )
    for key in keys:
        parsed = _parse_dt(record.get(key))
        if parsed:
            return parsed.date()
    return None


def _within_period(record: dict[str, Any], consumption_type: str, period_start: date | None, period_end: date | None) -> bool:
    if period_start is None and period_end is None:
        return True
    rec_date = _record_effective_date(record, consumption_type)
    if rec_date is None:
        return False
    if period_start and rec_date < period_start:
        return False
    if period_end and rec_date > period_end:
        return False
    return True


def _item_exact_match_kind(item: dict[str, Any], record: dict[str, Any], record_key: str) -> str | None:
    """Return an auditable exact match kind, never a constraint-only match.

    Currency, account, and sector objectives describe allocation intent.  They
    are not sufficiently specific to attribute an order or permit an enforce
    decision.  Exact attribution needs a plan id, shared dedup key, or a
    deterministic preferred ticker, plus direction/account/currency checks.
    """
    direction = _record_direction(record)
    allowed = {_record_direction({"type": t}) for t in item.get("allowed_action_types") or []}
    if not direction or direction not in allowed:
        return None

    constraints = item.get("constraints") or {}
    account_hint = constraints.get("account_hint")
    required_bucket = _account_bucket_from_hint(account_hint)
    if account_hint and required_bucket != "default" and _record_account_bucket(record) != required_bucket:
        return None
    required_currency = str(constraints.get("currency") or "").upper()
    if required_currency and _record_currency(record) != required_currency:
        return None

    ticker = canonical_ticker(record.get("ticker"))
    preferred_tickers = {
        canonical_ticker(value) for value in item.get("preferred_tickers") or []
    }
    # A supplied id is authoritative only when it cannot contradict the
    # deterministic guards above.  When tickers are declared, require one of
    # them too so an LLM cannot use a visible id to route an arbitrary asset.
    plan_item_id = str(record.get("plan_item_id") or "")
    if plan_item_id and plan_item_id == item.get("plan_item_id"):
        if not preferred_tickers or ticker in preferred_tickers:
            return "plan_item_id"
    if record_key and record_key in set(item.get("dedup_keys") or []):
        return "dedup_key"
    if ticker and ticker in preferred_tickers:
        return "preferred_ticker"
    return None


def _item_matches_record(item: dict[str, Any], record: dict[str, Any], record_key: str) -> bool:
    if record.get("execution_reconciliation_status") == "review":
        return False
    return _item_exact_match_kind(item, record, record_key) is not None


def _item_advisory_match(item: dict[str, Any], record: dict[str, Any]) -> bool:
    """Whether broad allocation intent is relevant without assigning budget.

    Sector preference is intentionally excluded until the system has a
    deterministic ticker-to-sector source.  Account/currency signals remain
    useful review annotations but never consume a plan item or block a trade.
    """
    direction = _record_direction(record)
    allowed = {_record_direction({"type": t}) for t in item.get("allowed_action_types") or []}
    if not direction or direction not in allowed:
        return False
    constraints = item.get("constraints") or {}
    account_hint = constraints.get("account_hint")
    required_bucket = _account_bucket_from_hint(account_hint)
    account_match = bool(
        account_hint and required_bucket != "default" and _record_account_bucket(record) == required_bucket
    )
    required_currency = str(constraints.get("currency") or "").upper()
    currency_match = bool(required_currency and _record_currency(record) == required_currency)
    return account_match or currency_match


def _apply_consumption_record(
    item: dict[str, Any],
    record: dict[str, Any],
    *,
    source: str,
    consumption_type: str,
    notional_jpy: int,
) -> None:
    if notional_jpy <= 0:
        return
    item["consumed_by"].append({
        "source": source,
        "id": record.get("id") or record.get("action_state_id"),
        "ticker": record.get("ticker"),
        "status": record.get("status"),
        "notional_jpy": notional_jpy,
        "consumption_type": consumption_type,
        "dedup_key": _record_dedup_key(record),
    })
    if consumption_type == "filled":
        item["filled_consumed_jpy"] = _jpy(item.get("filled_consumed_jpy")) + notional_jpy
    else:
        item["open_order_consumed_jpy"] = _jpy(item.get("open_order_consumed_jpy")) + notional_jpy
    item["consumed_jpy"] = _jpy(item.get("open_order_consumed_jpy")) + _jpy(item.get("filled_consumed_jpy"))
    item["remaining_jpy"] = max(0, _jpy(item.get("normal_budget_jpy")) - _jpy(item.get("consumed_jpy")))
    if item["remaining_jpy"] <= 0 and _jpy(item.get("normal_budget_jpy")) > 0:
        item["status"] = "covered"
        item["today_decision"] = {
            "decision": "covered_by_existing_activity",
            "reason": "open orders/fills already cover this plan item",
        }


def compute_consumption(
    items: list[dict[str, Any]],
    *,
    action_state: dict[str, Any] | None = None,
    executions: dict[str, Any] | list[dict[str, Any]] | None = None,
    fx_rate: float = 150.0,
    period_start: date | None = None,
    period_end: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    next_items = deepcopy(items)
    state_actions = (action_state or {}).get("actions", {}) if isinstance(action_state, dict) else {}
    pending_count = 0
    pending_notional_jpy = 0
    pending_unpriced_count = 0

    exec_items: list[dict[str, Any]]
    if isinstance(executions, dict):
        exec_items = [e for e in executions.get("executions", []) if isinstance(e, dict)]
    elif isinstance(executions, list):
        exec_items = [e for e in executions if isinstance(e, dict)]
    else:
        exec_items = []

    # A terminal execution is the authority for its linked lifecycle record.
    # In particular, a manually-cancelled action_state must not hide a later
    # executed record, and an old ordered record must not be counted alongside
    # its terminal execution.  Restrict this index to records that can consume
    # the current period, so an older fill does not erase a current state entry.
    terminal_execution_state_ids = {
        str(record.get("action_state_id"))
        for record in exec_items
        if str(record.get("action_state_id") or "")
        and str(record.get("status") or "").lower() in FILLED_EXECUTION_STATUSES
        and _within_period(record, "filled", period_start, period_end)
    }
    state_consumption_ids: set[str] = set()

    def _match(record: dict[str, Any]) -> dict[str, Any] | None:
        key = _record_dedup_key(record)
        for item in next_items:
            if _item_matches_record(item, record, key):
                return item
        return None

    if isinstance(state_actions, dict):
        for action_id, entry in state_actions.items():
            if not isinstance(entry, dict):
                continue
            status = str(entry.get("status") or "").lower()
            if status in PENDING_ACTION_STATE_STATUSES:
                pending_count += 1
                notional = _estimate_notional_jpy(
                    {**entry, "id": entry.get("id") or action_id}, fx_rate=fx_rate
                )
                if notional > 0:
                    pending_notional_jpy += notional
                else:
                    pending_unpriced_count += 1
                continue
            if status not in OPEN_ACTION_STATE_STATUSES | FILLED_ACTION_STATE_STATUSES:
                continue
            consumption_type = "filled" if status in FILLED_ACTION_STATE_STATUSES else "open"
            if status in FILLED_ACTION_STATE_STATUSES and not _within_period(entry, consumption_type, period_start, period_end):
                continue
            if action_id in terminal_execution_state_ids:
                # The linked execution will be processed below.  This gives
                # terminal execution data precedence over stale state status.
                continue
            item = _match(entry)
            if not item:
                continue
            record = {**entry, "id": entry.get("id") or action_id}
            notional = _estimate_notional_jpy(record, fx_rate=fx_rate)
            if notional <= 0:
                continue
            _apply_consumption_record(
                item,
                record,
                source="action_state",
                consumption_type=consumption_type,
                notional_jpy=notional,
            )
            state_consumption_ids.add(str(action_id))

    for record in exec_items:
        status = str(record.get("status") or "").lower()
        if status not in OPEN_EXECUTION_STATUSES | FILLED_EXECUTION_STATUSES:
            continue
        action_state_id = str(record.get("action_state_id") or "")
        consumption_type = "filled" if status in FILLED_EXECUTION_STATUSES else "open"
        if not _within_period(record, consumption_type, period_start, period_end):
            continue
        if action_state_id:
            # state already represents this live order/fill.  Conversely, an
            # ordered record that has a terminal sibling must not consume too.
            if action_state_id in state_consumption_ids:
                continue
            if consumption_type == "open" and action_state_id in terminal_execution_state_ids:
                continue
        item = _match(record)
        if not item:
            continue
        _apply_consumption_record(
            item,
            record,
            source="action_executions",
            consumption_type=consumption_type,
            notional_jpy=_estimate_notional_jpy(record, fx_rate=fx_rate),
        )

    summary = {
        "normal_consumed_jpy": sum(_jpy(i.get("consumed_jpy")) for i in next_items if i.get("budget_bucket") == "normal"),
        "open_order_consumed_jpy": sum(_jpy(i.get("open_order_consumed_jpy")) for i in next_items),
        "filled_consumed_jpy": sum(_jpy(i.get("filled_consumed_jpy")) for i in next_items),
        "remaining_normal_jpy": sum(_jpy(i.get("remaining_jpy")) for i in next_items if i.get("budget_bucket") == "normal"),
        "remaining_opportunity_jpy": sum(_jpy(i.get("remaining_jpy")) for i in next_items if i.get("budget_bucket") == "opportunity"),
        "pending_recommendation_count": pending_count,
        "pending_recommendation_notional_jpy": _jpy(pending_notional_jpy),
        "pending_unpriced_count": pending_unpriced_count,
    }
    return next_items, summary


def _execution_items(executions: dict[str, Any] | list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if isinstance(executions, dict):
        return [e for e in executions.get("executions", []) if isinstance(e, dict)]
    if isinstance(executions, list):
        return [e for e in executions if isinstance(e, dict)]
    return []


def _monthly_attribution_id(
    record: dict[str, Any],
    *,
    state_actions: dict[str, Any],
    month: str,
) -> str | None:
    """Return a persisted monthly attribution id for this calendar month.

    This deliberately accepts only explicit ``monthly_objective_id`` metadata,
    plus the old plan-item id format during the migration month.  It never
    infers monthly spend from ticker, account, currency, or sector.
    """
    if record.get("execution_reconciliation_status") == "review":
        return None
    candidates = [record]
    action_state_id = str(record.get("action_state_id") or "")
    linked = state_actions.get(action_state_id) if action_state_id else None
    if isinstance(linked, dict):
        candidates.append(linked)
    for candidate in candidates:
        monthly_id = str(candidate.get("monthly_objective_id") or "")
        if monthly_id.startswith(f"{month}:"):
            return monthly_id
        legacy_plan_id = str(candidate.get("plan_item_id") or "")
        if legacy_plan_id.startswith(f"{month}-"):
            return f"legacy:{legacy_plan_id}"
    return None


def _is_budget_consuming_buy(record: dict[str, Any]) -> bool:
    """Whether a row is a risk-increasing purchase for monthly budgeting."""
    direction = _record_direction(record)
    return direction in {"buy", "add", "dca", "margin_buy"}


def compute_monthly_consumption(
    *,
    month_start: date,
    month_end: date,
    action_state: dict[str, Any] | None = None,
    executions: dict[str, Any] | list[dict[str, Any]] | None = None,
    fx_rate: float = 150.0,
) -> dict[str, Any]:
    """Compute a month-to-date cap ledger from explicitly plan-attributed rows.

    Terminal execution records take precedence over lifecycle state exactly as
    in ``compute_consumption``.  The function intentionally has no plan-item
    fuzzy matcher. Rows without explicit attribution are reported separately
    so they remain visible during the migration, but never consume a guessed
    monthly objective.
    """
    month = month_start.strftime("%Y-%m")
    state_actions = (action_state or {}).get("actions", {}) if isinstance(action_state, dict) else {}
    state_actions = state_actions if isinstance(state_actions, dict) else {}
    exec_items = _execution_items(executions)

    terminal_execution_state_ids = {
        str(record.get("action_state_id"))
        for record in exec_items
        if str(record.get("action_state_id") or "")
        and str(record.get("status") or "").lower() in FILLED_EXECUTION_STATUSES
        and _within_period(record, "filled", month_start, month_end)
    }
    execution_attributed_state_ids = {
        str(record.get("action_state_id"))
        for record in exec_items
        if str(record.get("action_state_id") or "")
        and str(record.get("status") or "").lower() in OPEN_EXECUTION_STATUSES | FILLED_EXECUTION_STATUSES
        and (
            str(record.get("status") or "").lower() not in FILLED_EXECUTION_STATUSES
            or _within_period(record, "filled", month_start, month_end)
        )
        and _monthly_attribution_id(record, state_actions=state_actions, month=month)
    }
    state_consumption_ids: set[str] = set()
    open_total = 0
    filled_total = 0
    consumed_by: list[dict[str, Any]] = []
    unattributed_open_total = 0
    unattributed_filled_total = 0
    unattributed_open_count = 0
    unattributed_filled_count = 0
    unattributed_buy_open_total = 0
    unattributed_buy_filled_total = 0
    unattributed_buy_open_count = 0
    unattributed_buy_filled_count = 0
    unattributed_sell_open_total = 0
    unattributed_sell_filled_total = 0
    unattributed_sell_open_count = 0
    unattributed_sell_filled_count = 0
    unattributed_unpriced_count = 0
    unattributed_examples: list[dict[str, Any]] = []

    def _append(record: dict[str, Any], *, source: str, consumption_type: str, record_id: str) -> None:
        nonlocal open_total, filled_total
        notional = _estimate_notional_jpy(record, fx_rate=fx_rate)
        if notional <= 0:
            return
        attribution_id = _monthly_attribution_id(record, state_actions=state_actions, month=month)
        if not attribution_id:
            return
        if consumption_type == "filled":
            filled_total += notional
        else:
            open_total += notional
        consumed_by.append({
            "source": source,
            "id": record_id,
            "ticker": record.get("ticker"),
            "status": record.get("status"),
            "notional_jpy": notional,
            "consumption_type": consumption_type,
            "monthly_objective_id": attribution_id,
        })

    def _append_unattributed(
        record: dict[str, Any],
        *,
        source: str,
        consumption_type: str,
        record_id: str,
    ) -> bool:
        nonlocal unattributed_open_total, unattributed_filled_total
        nonlocal unattributed_open_count, unattributed_filled_count
        nonlocal unattributed_buy_open_total, unattributed_buy_filled_total
        nonlocal unattributed_buy_open_count, unattributed_buy_filled_count
        nonlocal unattributed_sell_open_total, unattributed_sell_filled_total
        nonlocal unattributed_sell_open_count, unattributed_sell_filled_count
        nonlocal unattributed_unpriced_count
        notional = _estimate_notional_jpy(record, fx_rate=fx_rate)
        if notional <= 0:
            unattributed_unpriced_count += 1
            if len(unattributed_examples) < 5:
                unattributed_examples.append({
                    "source": source,
                    "id": record_id,
                    "ticker": record.get("ticker"),
                    "status": record.get("status"),
                    "notional_jpy": None,
                    "consumption_type": consumption_type,
                    "unpriced": True,
                })
            # Mark the lifecycle row as handled as well.  Otherwise an
            # unpriced action_state and its matching execution record are
            # counted twice merely because no monetary amount is available.
            return True
        is_buy = _is_budget_consuming_buy(record)
        if consumption_type == "filled":
            unattributed_filled_total += notional
            unattributed_filled_count += 1
            if is_buy:
                unattributed_buy_filled_total += notional
                unattributed_buy_filled_count += 1
            else:
                unattributed_sell_filled_total += notional
                unattributed_sell_filled_count += 1
        else:
            unattributed_open_total += notional
            unattributed_open_count += 1
            if is_buy:
                unattributed_buy_open_total += notional
                unattributed_buy_open_count += 1
            else:
                unattributed_sell_open_total += notional
                unattributed_sell_open_count += 1
        if len(unattributed_examples) < 5:
            unattributed_examples.append({
                "source": source,
                "id": record_id,
                "ticker": record.get("ticker"),
                "status": record.get("status"),
                "notional_jpy": notional,
                "consumption_type": consumption_type,
            })
        return True

    for action_id, entry in state_actions.items():
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").lower()
        if status not in OPEN_ACTION_STATE_STATUSES | FILLED_ACTION_STATE_STATUSES:
            continue
        record_id = str(entry.get("id") or action_id)
        if record_id in terminal_execution_state_ids or str(action_id) in terminal_execution_state_ids:
            continue
        if record_id in execution_attributed_state_ids or str(action_id) in execution_attributed_state_ids:
            continue
        consumption_type = "filled" if status in FILLED_ACTION_STATE_STATUSES else "open"
        if consumption_type == "filled" and not _within_period(entry, consumption_type, month_start, month_end):
            continue
        attribution_id = _monthly_attribution_id(entry, state_actions=state_actions, month=month)
        if attribution_id:
            before = open_total + filled_total
            if _is_budget_consuming_buy(entry):
                _append(entry, source="action_state", consumption_type=consumption_type, record_id=record_id)
            recorded = open_total + filled_total > before
        else:
            recorded = _append_unattributed(
                entry,
                source="action_state",
                consumption_type=consumption_type,
                record_id=record_id,
            )
        if recorded:
            state_consumption_ids.update({str(action_id), record_id})

    for record in exec_items:
        status = str(record.get("status") or "").lower()
        if status not in OPEN_EXECUTION_STATUSES | FILLED_EXECUTION_STATUSES:
            continue
        consumption_type = "filled" if status in FILLED_EXECUTION_STATUSES else "open"
        if consumption_type == "filled" and not _within_period(record, consumption_type, month_start, month_end):
            continue
        action_state_id = str(record.get("action_state_id") or "")
        if action_state_id in state_consumption_ids:
            continue
        if consumption_type == "open" and action_state_id in terminal_execution_state_ids:
            continue
        record_id = str(record.get("id") or action_state_id)
        attribution_id = _monthly_attribution_id(record, state_actions=state_actions, month=month)
        if attribution_id:
            if _is_budget_consuming_buy(record):
                _append(
                    record,
                    source="action_executions",
                    consumption_type=consumption_type,
                    record_id=record_id,
                )
        else:
            _append_unattributed(
                record,
                source="action_executions",
                consumption_type=consumption_type,
                record_id=record_id,
            )

    return {
        "month": month,
        "monthly_open_order_consumed_jpy": open_total,
        "monthly_filled_consumed_jpy": filled_total,
        "monthly_consumed_jpy": open_total + filled_total,
        "monthly_consumption_record_count": len(consumed_by),
        "monthly_consumed_by": consumed_by,
        "unattributed_monthly_open_order_count": unattributed_open_count,
        "unattributed_monthly_open_order_notional_jpy": unattributed_open_total,
        "unattributed_monthly_filled_count": unattributed_filled_count,
        "unattributed_monthly_filled_notional_jpy": unattributed_filled_total,
        "unattributed_monthly_total_count": unattributed_open_count + unattributed_filled_count + unattributed_unpriced_count,
        "unattributed_monthly_total_notional_jpy": unattributed_open_total + unattributed_filled_total,
        "unattributed_monthly_examples": unattributed_examples,
        "unattributed_monthly_buy_open_order_count": unattributed_buy_open_count,
        "unattributed_monthly_buy_open_order_notional_jpy": unattributed_buy_open_total,
        "unattributed_monthly_buy_filled_count": unattributed_buy_filled_count,
        "unattributed_monthly_buy_filled_notional_jpy": unattributed_buy_filled_total,
        "unattributed_monthly_buy_total_count": unattributed_buy_open_count + unattributed_buy_filled_count,
        "unattributed_monthly_buy_total_notional_jpy": unattributed_buy_open_total + unattributed_buy_filled_total,
        "unattributed_monthly_sell_open_order_count": unattributed_sell_open_count,
        "unattributed_monthly_sell_open_order_notional_jpy": unattributed_sell_open_total,
        "unattributed_monthly_sell_filled_count": unattributed_sell_filled_count,
        "unattributed_monthly_sell_filled_notional_jpy": unattributed_sell_filled_total,
        "unattributed_monthly_sell_total_count": unattributed_sell_open_count + unattributed_sell_filled_count,
        "unattributed_monthly_sell_total_notional_jpy": unattributed_sell_open_total + unattributed_sell_filled_total,
        "unattributed_monthly_unpriced_count": unattributed_unpriced_count,
    }


def classify_candidate_against_plan(
    action: dict[str, Any],
    plan_state: dict[str, Any],
    *,
    done_keys: set[tuple[str, str]] | None = None,
    cooldown_keys: set[tuple[str, str]] | None = None,
    h2_cap_jpy: int | None = None,
) -> dict[str, Any]:
    """Classify a candidate against the baseline plan without bypassing guards."""
    ticker = canonical_ticker(action.get("ticker"))
    direction = _record_direction(action)
    key = (ticker, direction)
    normalized_done_keys = {
        (canonical_ticker(done_ticker), done_direction)
        for done_ticker, done_direction in (done_keys or set())
    }
    normalized_cooldown_keys = {
        (canonical_ticker(cooldown_ticker), cooldown_direction)
        for cooldown_ticker, cooldown_direction in (cooldown_keys or set())
    }
    if key in normalized_done_keys:
        return {
            "execution_plan_decision": "blocked_by_existing_guard",
            "existing_guard": "done_list",
            "executable": False,
        }
    if key in normalized_cooldown_keys:
        return {
            "execution_plan_decision": "blocked_by_existing_guard",
            "existing_guard": "cooldown",
            "executable": False,
        }
    estimated = _jpy(action.get("estimated_notional_jpy") or action.get("amount_jpy"))
    if h2_cap_jpy is not None and estimated > h2_cap_jpy:
        return {
            "execution_plan_decision": "blocked_by_existing_guard",
            "existing_guard": "h2_single_action_cap",
            "executable": False,
        }

    playbook_source = str(action.get("source") or "") == "scenario_playbook"
    playbook_gate = action.get("playbook_gate") if isinstance(action.get("playbook_gate"), dict) else {}
    if playbook_source or action.get("playbook_injected") is True or playbook_gate:
        gate_valid = (
            playbook_source
            and action.get("playbook_injected") is True
            and playbook_gate.get("version") == 1
            and playbook_gate.get("attested") is True
            and str(playbook_gate.get("scenario_status") or "") in {"active", "partial"}
            and _jpy(playbook_gate.get("entry_cap_jpy")) >= estimated > 0
            and _jpy(playbook_gate.get("run_cap_jpy")) > 0
            and 0 < _jpy(playbook_gate.get("run_used_after_jpy")) <= _jpy(playbook_gate.get("run_cap_jpy"))
            and playbook_gate.get("jp_target_check_passed") is True
        )
        if not gate_valid:
            return {
                "execution_plan_decision": "scenario_playbook_unattested",
                "executable": False,
            }
        consumption_summary = plan_state.get("consumption_summary") or {}
        opportunity_remaining_raw = consumption_summary.get("remaining_opportunity_jpy")
        opportunity_remaining = _jpy(opportunity_remaining_raw)
        if opportunity_remaining_raw is None:
            return {
                "execution_plan_decision": "scenario_playbook_missing_opportunity_pool",
                "executable": False,
            }
        if estimated > opportunity_remaining:
            return {
                "execution_plan_decision": "plan_monthly_cap_reached",
                "opportunity_remaining_jpy": opportunity_remaining,
                "executable": False,
            }
        month = str((plan_state.get("horizon") or {}).get("month") or "")
        scenario_id = _slug(str(action.get("scenario_id") or "scenario"))
        return {
            "execution_plan_decision": "scenario_playbook_bounded",
            "execution_plan_override": "scenario_playbook",
            "ai_bounded_gate": "scenario_playbook_bounded",
            "provisional_decision": True,
            "monthly_objective_id": f"{month}:scenario:{scenario_id}" if month else None,
            "opportunity_remaining_before_jpy": opportunity_remaining,
            "opportunity_remaining_after_jpy": opportunity_remaining - estimated,
            "cap_applied_jpy": estimated,
            "budget_impact_jpy": estimated,
            "override_reason": "deterministically injected scenario playbook action within attested caps",
            "executable": True,
        }

    def _num(value: Any, fallback: float = 0.0) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else fallback
        except (TypeError, ValueError):
            return fallback

    def _int(value: Any, fallback: int = 99) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return fallback

    urgency = str(action.get("urgency") or "medium").lower()
    confidence = _num(action.get("confidence_pct"), 0.0)
    rank = _int(action.get("rank"), 99)
    consumption_summary = plan_state.get("consumption_summary") or {}
    remaining_opportunity = _jpy(consumption_summary.get("remaining_opportunity_jpy"))
    budgets = plan_state.get("budgets") or {}
    shared_normal_raw = budgets.get("normal_pool_available_jpy")
    shared_normal_active = shared_normal_raw is not None
    shared_normal = _jpy(shared_normal_raw)
    # Older persisted plan files do not carry a month-to-date ledger.  They
    # remain readable in observe mode, but are not silently treated as a zero
    # monthly allowance.
    monthly_remaining_raw = consumption_summary.get("monthly_remaining_jpy")
    monthly_cap_active = monthly_remaining_raw is not None
    monthly_remaining = _jpy(monthly_remaining_raw)
    high_conviction = confidence >= 80 and rank <= 2 and urgency in {"medium", "high"}

    def _opportunity() -> dict[str, Any] | None:
        if direction not in {"buy", "add"}:
            return None
        if not high_conviction:
            return None
        if estimated <= 0 or estimated > remaining_opportunity:
            return None
        if monthly_cap_active and estimated > monthly_remaining:
            return None
        return {
            "execution_plan_decision": "opportunistic_override",
            "execution_plan_override": "opportunistic",
            "ai_bounded_gate": "execution_plan_opportunistic",
            "provisional_decision": True,
            "cap_applied_jpy": estimated,
            "budget_impact_jpy": estimated,
            "override_reason": "high-conviction candidate outside or beyond normal execution plan",
            "executable": True,
        }

    record_key = _record_dedup_key(action)
    matched_item: dict[str, Any] | None = None
    matched_kind: str | None = None
    advisory_item_ids: list[str] = []
    supplied_plan_item_id = str(action.get("plan_item_id") or "")
    supplied_plan_item_known = False
    supplied_plan_item_mismatch = False
    for item in plan_state.get("items") or []:
        if not isinstance(item, dict):
            continue
        if supplied_plan_item_id and supplied_plan_item_id == str(item.get("plan_item_id") or ""):
            supplied_plan_item_known = True
        match_kind = _item_exact_match_kind(item, action, record_key)
        if match_kind:
            matched_item = item
            matched_kind = match_kind
            break
        if supplied_plan_item_known:
            supplied_plan_item_mismatch = True
        if _item_advisory_match(item, action):
            item_id = str(item.get("plan_item_id") or "")
            if item_id:
                advisory_item_ids.append(item_id)
    if supplied_plan_item_id and (not supplied_plan_item_known or supplied_plan_item_mismatch):
        return {
            "execution_plan_decision": "plan_metadata_mismatch",
            "plan_item_id": supplied_plan_item_id,
            "metadata_mismatch": "unknown_plan_item_id" if not supplied_plan_item_known else "direction_account_currency_or_ticker",
            "executable": False,
        }
    if matched_item:
        remaining = (
            shared_normal
            if str(matched_item.get("budget_bucket") or "normal") == "normal" and shared_normal_active
            else _jpy(matched_item.get("remaining_jpy"))
        )
        if remaining <= 0:
            return _opportunity() or {
                "execution_plan_decision": "plan_consumed_by_open_order",
                "plan_item_id": matched_item.get("plan_item_id"),
                "execution_plan_match_kind": matched_kind,
                "executable": False,
            }
        min_confidence = _num((matched_item.get("constraints") or {}).get("min_confidence_pct"), 70.0)
        if confidence < min_confidence or (
            urgency == "low" and "nisa" not in str(matched_item.get("objective") or "").lower()
        ):
            return {
                "execution_plan_decision": "plan_wait_for_better_candidate",
                "plan_item_id": matched_item.get("plan_item_id"),
                "execution_plan_match_kind": matched_kind,
                "executable": False,
                "required_confidence_pct": min_confidence,
            }
        if estimated > remaining:
            return _opportunity() or {
                "execution_plan_decision": "plan_over_budget",
                "plan_item_id": matched_item.get("plan_item_id"),
                "execution_plan_match_kind": matched_kind,
                "executable": False,
                "plan_remaining_jpy": remaining,
            }
        if monthly_cap_active and estimated > monthly_remaining:
            return {
                "execution_plan_decision": "plan_monthly_cap_reached",
                "plan_item_id": matched_item.get("plan_item_id"),
                "execution_plan_match_kind": matched_kind,
                "monthly_remaining_jpy": monthly_remaining,
                "executable": False,
            }
        return {
            "execution_plan_decision": "plan_new_order",
            "plan_item_id": matched_item.get("plan_item_id"),
            "monthly_objective_id": matched_item.get("monthly_objective_id"),
            "execution_plan_match_kind": matched_kind,
            "plan_remaining_before_jpy": remaining,
            "plan_remaining_after_jpy": max(0, remaining - estimated),
            "monthly_remaining_before_jpy": monthly_remaining if monthly_cap_active else None,
            "monthly_remaining_after_jpy": max(0, monthly_remaining - estimated) if monthly_cap_active else None,
            "executable": True,
        }

    opportunity = _opportunity()
    if opportunity:
        return opportunity
    if direction == "sell":
        # Risk-reducing exits must remain available, but an active plan that
        # explicitly names the same ticker for accumulation is a policy
        # conflict. Keep the exit executable and require human review.
        opposite_plan_items: list[str] = []
        for item in plan_state.get("items") or []:
            if not isinstance(item, dict):
                continue
            if str(item.get("status") or "active").lower() not in {"active", "open"}:
                continue
            if _jpy(item.get("remaining_jpy")) <= 0:
                continue
            allowed = {
                str(value or "").lower()
                for value in (item.get("allowed_action_types") or [])
            }
            preferred = {
                canonical_ticker(value)
                for value in (item.get("preferred_tickers") or [])
            }
            if allowed.intersection({"buy", "add", "dca", "margin_buy"}) and ticker in preferred:
                item_id = str(item.get("plan_item_id") or "")
                if item_id:
                    opposite_plan_items.append(item_id)
        if opposite_plan_items:
            return {
                "execution_plan_decision": "defensive_exit_with_opposite_plan",
                "execution_plan_override": "defensive",
                "execution_plan_direction_conflict": True,
                "execution_plan_requires_review": True,
                "execution_plan_conflict_item_ids": opposite_plan_items[:3],
                "execution_plan_conflict_reason": (
                    f"{ticker} の売却候補と、同銘柄を明示した active buy/add plan が併存。"
                    "スリーブ目標と全体配分目標の優先順位を人間が確認してください"
                ),
                "executable": True,
            }
    if direction in {"sell", "cover"}:
        return {
            "execution_plan_decision": "defensive_or_exit_outside_plan",
            "execution_plan_override": "defensive",
            "executable": True,
        }
    if advisory_item_ids:
        return {
            "execution_plan_decision": "plan_advisory_match",
            "execution_plan_advisory_item_ids": advisory_item_ids[:3],
            "execution_plan_match_kind": "advisory",
            # Broad allocation objectives are visible review context only.  A
            # dedicated exact mapping is required before a risk-increasing
            # order can become executable.  In observe mode this remains
            # visible with execution_plan_would_filter=true; enforce mode
            # rejects it.
            "reason": "broad account/currency intent cannot authorize an order",
            "executable": False,
        }
    return {
        "execution_plan_decision": "plan_unmatched_no_override",
        "executable": False,
    }


def allocate_candidate_batch_against_plan(
    actions: list[dict[str, Any]],
    plan_state: dict[str, Any],
) -> list[dict[str, Any]]:
    """Allocate final post-filter candidates against shared plan pools.

    ``classify_candidate_against_plan`` is intentionally stateless so it can
    explain a candidate in isolation.  Calling it once per candidate otherwise
    lets several individually-valid orders oversubscribe the same plan item.
    This function runs after all non-plan filters and allocates the surviving
    candidates in one deterministic order.

    The result list is aligned with ``actions``.  Only ``plan_new_order`` and
    ``opportunistic_override`` are budget-consuming candidates; defensive and
    advisory decisions are left untouched.
    """
    if not isinstance(plan_state, dict):
        return [{"applicable": False} for _ in actions]
    raw_items = plan_state.get("items") or []
    item_remaining = {
        str(item.get("plan_item_id")): _jpy(item.get("remaining_jpy"))
        for item in raw_items
        if isinstance(item, dict) and item.get("plan_item_id")
    }
    summary = plan_state.get("consumption_summary") or {}
    opportunity_remaining = _jpy(summary.get("remaining_opportunity_jpy"))
    budgets = plan_state.get("budgets") or {}
    shared_normal_raw = budgets.get("normal_pool_available_jpy")
    shared_normal_active = shared_normal_raw is not None
    shared_normal_remaining = _jpy(shared_normal_raw)
    monthly_raw = summary.get("monthly_remaining_jpy")
    monthly_remaining: int | None = _jpy(monthly_raw) if monthly_raw is not None else None
    cash_info = plan_state.get("cash_info") or {}
    wallet_contract_active = cash_info.get("wallet_contract_version") == 1
    wallet_remaining = {
        str(row.get("wallet_key")): _jpy(row.get("available_jpy"))
        for row in cash_info.get("wallets") or []
        if isinstance(row, dict) and row.get("wallet_key")
    }

    def _wallet_key(action: dict[str, Any]) -> str | None:
        try:
            from execution_safety import canonical_broker, canonical_owner
            owner = canonical_owner(action.get("execution_owner") or action.get("owner"))
            broker = canonical_broker(action.get("execution_broker") or action.get("broker"))
        except Exception:
            owner = broker = ""
        ticker = str(action.get("ticker") or "").upper()
        currency = str(
            action.get("currency") or ("JPY" if ticker.endswith(".T") else "USD")
        ).upper()
        settlement_pool = str(action.get("settlement_pool") or "broker_cash")
        if not owner or not broker or currency not in {"JPY", "USD", "EUR"}:
            return None
        return f"{owner}|{broker}|{settlement_pool}|{currency}"

    def _number(value: Any) -> float:
        try:
            number = float(value)
            return number if math.isfinite(number) else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _rank(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 99

    urgency_order = {"high": 0, "medium": 1, "low": 2}
    results: list[dict[str, Any]] = [{"applicable": False} for _ in actions]
    candidates: list[tuple[int, dict[str, Any], str]] = []
    for index, action in enumerate(actions):
        if not isinstance(action, dict):
            continue
        decision = str(
            action.get("execution_plan_decision")
            or action.get("execution_plan_observed_decision")
            or ""
        )
        if decision in {"plan_new_order", "opportunistic_override", "scenario_playbook_bounded"}:
            candidates.append((index, action, decision))

    candidates.sort(
        key=lambda row: (
            _rank(row[1].get("rank")),
            -_number(row[1].get("confidence_pct")),
            urgency_order.get(str(row[1].get("urgency") or "medium").lower(), 3),
            str(row[1].get("ticker") or ""),
            str(row[1].get("type") or ""),
            row[0],
        )
    )

    for index, action, decision in candidates:
        estimated = _jpy(action.get("estimated_notional_jpy") or action.get("amount_jpy"))
        if estimated <= 0:
            results[index] = {
                "applicable": True,
                "executable": False,
                "execution_plan_decision": "plan_over_budget",
                "reason": "plan batch allocation requires a positive estimated notional",
            }
            continue
        wallet_key = _wallet_key(action) if wallet_contract_active else None
        if wallet_contract_active and (
            wallet_key is None or wallet_key not in wallet_remaining
        ):
            results[index] = {
                "applicable": True,
                "executable": False,
                "execution_plan_decision": "cash_wallet_unresolved",
                "cash_wallet_key": wallet_key,
                "reason": (
                    "owner, broker, settlement pool and currency do not resolve "
                    "to confirmed buying power"
                ),
            }
            continue
        wallet_before = wallet_remaining.get(wallet_key, 0) if wallet_key else None
        if wallet_before is not None and estimated > wallet_before:
            results[index] = {
                "applicable": True,
                "executable": False,
                "execution_plan_decision": "cash_wallet_over_budget",
                "cash_wallet_key": wallet_key,
                "cash_wallet_remaining_jpy": wallet_before,
                "reason": "candidate exceeds confirmed buying power in its routed wallet",
            }
            continue

        pool_before: int
        pool_after: int
        item_id = str(action.get("plan_item_id") or "")
        if decision == "plan_new_order":
            pool_before = shared_normal_remaining if shared_normal_active else item_remaining.get(item_id, 0)
        elif decision in {"opportunistic_override", "scenario_playbook_bounded"}:
            pool_before = opportunity_remaining
        else:
            pool_before = monthly_remaining or 0
        # Shared-pool plans already carry the authoritative available amount.
        # Legacy plan files retain the monthly cap compatibility check.
        effective_remaining = (
            pool_before
            if shared_normal_active or decision in {"opportunistic_override", "scenario_playbook_bounded"}
            else min(pool_before, monthly_remaining) if monthly_remaining is not None else pool_before
        )
        if estimated > effective_remaining:
            results[index] = {
                "applicable": True,
                "executable": False,
                "execution_plan_decision": "plan_over_budget",
                "plan_item_id": item_id or None,
                "plan_remaining_jpy": pool_before,
                "monthly_remaining_jpy": monthly_remaining,
                "reason": "final candidate set exceeds the remaining execution-plan pool",
            }
            continue

        pool_after = pool_before - estimated
        if decision == "plan_new_order":
            if shared_normal_active:
                shared_normal_remaining = pool_after
            else:
                item_remaining[item_id] = pool_after
        elif decision in {"opportunistic_override", "scenario_playbook_bounded"}:
            opportunity_remaining = pool_after
        if monthly_remaining is not None and not shared_normal_active:
            monthly_remaining -= estimated
        wallet_after = None
        if wallet_key is not None and wallet_before is not None:
            wallet_after = wallet_before - estimated
            wallet_remaining[wallet_key] = wallet_after
        results[index] = {
            "applicable": True,
            "executable": True,
            "execution_plan_decision": decision,
            "plan_item_id": item_id or None,
            "plan_remaining_before_jpy": pool_before if decision == "plan_new_order" else None,
            "plan_remaining_after_jpy": pool_after if decision == "plan_new_order" else None,
            "opportunity_remaining_before_jpy": pool_before if decision in {"opportunistic_override", "scenario_playbook_bounded"} else None,
            "opportunity_remaining_after_jpy": pool_after if decision in {"opportunistic_override", "scenario_playbook_bounded"} else None,
            "monthly_remaining_before_jpy": (
                monthly_remaining + estimated if monthly_remaining is not None and not shared_normal_active else None
            ),
            "monthly_remaining_after_jpy": monthly_remaining if not shared_normal_active else None,
            "cash_wallet_key": wallet_key,
            "cash_wallet_remaining_before_jpy": wallet_before,
            "cash_wallet_remaining_after_jpy": wallet_after,
        }
    return results


def no_action_rationale(items: list[dict[str, Any]], consumption_summary: dict[str, Any]) -> list[dict[str, str]]:
    if not items:
        return [{"reason_code": "no_active_plan_items", "message": "No active execution plan items were generated."}]
    if all(_jpy(item.get("remaining_jpy")) <= 0 for item in items):
        return [{
            "reason_code": "covered_by_open_orders",
            "message": "Current plan items are already covered by open orders or fills.",
        }]
    if _jpy(consumption_summary.get("remaining_normal_jpy")) > 0:
        return [{
            "reason_code": "plan_remaining_waiting_for_candidate",
            "message": "Plan budget remains; daily post-filter must decide whether candidate quality is sufficient.",
        }]
    return []


def build_execution_plan(
    *,
    account: dict[str, Any] | None,
    guard: dict[str, Any] | None,
    rebalance_report: dict[str, Any] | None,
    bottom_fishing: dict[str, Any] | None,
    nisa: dict[str, Any] | None,
    action_state: dict[str, Any] | None,
    executions: dict[str, Any] | list[dict[str, Any]] | None,
    holdings: dict[str, Any] | list[dict[str, Any]] | None = None,
    ai_analysis: dict[str, Any] | None = None,
    market_regime: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    now: datetime | None = None,
    contribution_occurrences: list[tuple[date, dict[str, Any]]] | None = None,
    contribution_ledger: dict[str, Any] | None = None,
    trusted_sector_catalog: dict[str, Any] | None = None,
    sector_catalog_summary: dict[str, Any] | None = None,
    sector_catalog_warnings: list[str] | None = None,
) -> dict[str, Any]:
    now = now or datetime.now()
    today = now.date()
    horizon = horizon_for(today)
    params = params or load_plan_params()
    cash_info, cash_warnings = derive_cash_info(
        account,
        holdings=holdings,
        now=now,
        stale_hours=int(params.get("cash_stale_hours") or 72),
    )
    cash_target_policy = resolve_cash_target_policy(
        ai_analysis=ai_analysis,
        market_regime=market_regime,
    )

    if contribution_occurrences is None:
        try:
            from contribution_schedule import occurrences

            contribution_occurrences = occurrences(today.isoformat(), str(horizon["month_end"]))
        except Exception:
            contribution_occurrences = []
    scheduled_jpy = scheduled_contribution_amount(contribution_occurrences)
    try:
        from contribution_ledger import summarize_contributions

        contribution_summary = summarize_contributions(
            contribution_ledger,
            executions,
            month=horizon["month"],
            fx_rate=float(cash_info.get("fx_rate_usdjpy") or 150.0),
        )
    except Exception as exc:
        contribution_summary = {
            "month": horizon["month"],
            "sources": [],
            "available_normal_jpy": 0,
            "available_opportunity_jpy": 0,
            "available_jpy": 0,
            "released_this_month_jpy": 0,
            "summary_error": str(exc)[:160],
        }
    month_start = date(today.year, today.month, 1)
    month_end = date.fromisoformat(str(horizon["month_end"]))
    monthly_consumption = compute_monthly_consumption(
        month_start=month_start,
        month_end=month_end,
        action_state=action_state,
        executions=executions,
        fx_rate=float(cash_info.get("fx_rate_usdjpy") or 150.0),
    )
    budgets, budget_warnings = derive_budgets(
        cash_info=cash_info,
        guard=guard,
        params=params,
        scheduled_contributions_jpy=scheduled_jpy,
        horizon=horizon,
        contribution_summary=contribution_summary,
        cash_target_policy=cash_target_policy,
        monthly_consumption=monthly_consumption,
    )
    # Existing unlinked buys are shown and consume a recurring policy amount,
    # but can never be guessed against a newly approved salary/bonus source.
    # Otherwise an old manual fill could silently erase newly approved money.
    all_monthly_buys = (
        _jpy(monthly_consumption.get("monthly_consumed_jpy"))
        + _jpy(monthly_consumption.get("unattributed_monthly_buy_total_notional_jpy"))
    )
    contribution_monthly_consumed = _jpy(contribution_summary.get("consumed_this_month_jpy"))
    base_consumed = max(0, all_monthly_buys - contribution_monthly_consumed)
    base_remaining = max(
        0,
        _jpy(budgets.get("monthly_discretionary_budget_jpy")) - base_consumed,
    )
    multiplier = _ratio(budgets.get("deployment_multiplier"), 1.0)
    normal_pool = base_remaining + _jpy(_jpy(contribution_summary.get("available_normal_jpy")) * multiplier)
    opportunity_pool = _jpy(_jpy(contribution_summary.get("available_opportunity_jpy")) * multiplier)
    budgets["monthly_base_consumed_jpy"] = base_consumed
    budgets["monthly_base_remaining_jpy"] = base_remaining
    budgets["normal_pool_available_jpy"] = normal_pool
    budgets["opportunity_pool_available_jpy"] = opportunity_pool
    budgets["monthly_remaining_jpy"] = normal_pool + opportunity_pool
    monthly_remaining = normal_pool + opportunity_pool
    requested_sector_names = [
        str(row.get("sector") or "")
        for row in (((rebalance_report or {}).get("buy_candidates") or {}).get("sectors") or [])[:3]
        if isinstance(row, dict) and row.get("sector") and str(row.get("sector")).lower() != "cash"
    ] if isinstance(rebalance_report, dict) else []
    omitted_sector_names = [
        name for name in requested_sector_names
        if not _trusted_sector_tickers(trusted_sector_catalog, name)
    ]
    sector_objective_warnings = [
        f"sector_objective_omitted_no_trusted_ticker: {name}"
        for name in omitted_sector_names
    ]
    items = build_plan_items(
        rebalance_report=rebalance_report,
        bottom_fishing=bottom_fishing,
        nisa=nisa,
        budgets=budgets,
        horizon=horizon,
        monthly_remaining_jpy=monthly_remaining,
        normal_pool_jpy=normal_pool,
        opportunity_pool_jpy=opportunity_pool,
        trusted_sector_catalog=trusted_sector_catalog,
    )
    items, consumption_summary = compute_consumption(
        items,
        action_state=action_state,
        executions=executions,
        fx_rate=float(cash_info.get("fx_rate_usdjpy") or 150.0),
        period_start=date.fromisoformat(str(horizon["week_start"])),
        period_end=date.fromisoformat(str(horizon["week_end"])),
    )
    consumption_summary.update(monthly_consumption)
    # Unlinked legacy buys cannot evade the new shared monthly pool. They are
    # conservatively charged to the base allowance above, so downstream
    # observe-to-enforce readiness must not treat their missing objective ID as
    # an unbounded-budget gap.
    consumption_summary["unattributed_buy_budget_treatment"] = (
        "charged_to_monthly_base_budget"
    )
    consumption_summary["unattributed_sell_budget_treatment"] = (
        "removed_from_deployment_basis"
        if _jpy(consumption_summary.get("unattributed_monthly_sell_total_count")) > 0
        else "not_applicable"
    )
    consumption_summary["unattributed_unpriced_budget_treatment"] = (
        "excluded_fail_closed"
        if _jpy(consumption_summary.get("unattributed_monthly_unpriced_count")) > 0
        else "not_applicable"
    )
    consumption_summary["monthly_remaining_jpy"] = monthly_remaining
    # The shared pools are authoritative.  Item-level consumption remains an
    # explanatory trace only and must not recreate the old split-wallet model.
    consumption_summary["remaining_normal_jpy"] = normal_pool
    consumption_summary["remaining_opportunity_jpy"] = opportunity_pool
    consumption_summary["normal_pool_available_jpy"] = normal_pool
    consumption_summary["opportunity_pool_available_jpy"] = opportunity_pool
    rationale = no_action_rationale(items, consumption_summary)
    if normal_pool <= 0 and opportunity_pool <= 0:
        if (
            _jpy(budgets.get("monthly_discretionary_budget_jpy")) > 0
            and base_consumed >= _jpy(budgets.get("monthly_discretionary_budget_jpy"))
        ):
            funding_reason = {
                "reason_code": "monthly_surplus_deployment_budget_consumed",
                "message": (
                    "確認済み余剰現金はありますが、今月の配備ペース上限を"
                    "既存の買付で消化済みです。翌月に自動で再計算します。"
                ),
            }
        elif budgets.get("cash_target_pct") is None:
            funding_reason = {
                "reason_code": "cash_target_unresolved",
                "message": "相場別の戦術現金目標を確定できないため、新規配備枠を作成しません。",
            }
        elif _jpy(budgets.get("surplus_cash_above_targets_jpy")) <= 0:
            funding_reason = {
                "reason_code": "cash_at_or_below_tactical_target",
                "message": "確認済み現金は相場別の戦術現金目標以下のため、新規配備枠はありません。",
            }
        else:
            funding_reason = {
                "reason_code": "no_deployable_cash_authority",
                "message": "確認済みで利用可能な余剰現金を確定できないため、新規配備枠を作成しません。",
            }
        rationale = [funding_reason] + rationale
    return {
        "schema_version": SCHEMA_VERSION,
        "as_of": now.astimezone().isoformat(timespec="seconds"),
        "horizon": {
            "month": horizon["month"],
            "week_start": horizon["week_start"],
            "week_end": horizon["week_end"],
        },
        "status": "active",
        "source_versions": {
            "rebalance_report_as_of": (rebalance_report or {}).get("as_of") if isinstance(rebalance_report, dict) else None,
            "bottom_fishing_evaluated_at": (bottom_fishing or {}).get("evaluated_at") if isinstance(bottom_fishing, dict) else None,
            "ai_analysis_as_of": (ai_analysis or {}).get("as_of") if isinstance(ai_analysis, dict) else None,
        },
        "budgets": budgets,
        "consumption_summary": consumption_summary,
        "contribution_summary": contribution_summary,
        "items": items,
        "no_action_rationale": rationale,
        "warnings": cash_warnings + budget_warnings + list(sector_catalog_warnings or []) + sector_objective_warnings,
        "sector_mapping": {
            **(sector_catalog_summary or {}),
            "requested_sector_objectives": requested_sector_names,
            "omitted_sector_objectives": omitted_sector_names,
        },
        "cash_info": cash_info,
        "generated_by": "execution_plan_engine.py",
    }


def generate_execution_plan(*, base_dir: Path = BASE_DIR, now: datetime | None = None, write: bool = True) -> dict[str, Any]:
    sector_catalog, sector_summary, sector_warnings = load_trusted_sector_catalog(
        base_dir=base_dir,
        now=now,
    )
    try:
        from execution_reconciliation import load_effective_execution_records
        effective_executions = {
            "executions": load_effective_execution_records(base_dir=base_dir),
        }
    except Exception:
        effective_executions = {"executions": []}
    plan = build_execution_plan(
        account=load_json(base_dir / "account.json", {}),
        holdings=load_json(base_dir / "holdings.json", []),
        guard=load_json(base_dir / "guard_state.json", {}),
        rebalance_report=load_json(base_dir / "rebalance_report.json", {}),
        bottom_fishing=load_json(base_dir / "bottom_fishing_signals.json", {}),
        nisa=load_json(base_dir / "nisa_portfolio.json", {}),
        action_state=load_json(base_dir / "action_state.json", {"actions": {}}),
        executions=effective_executions,
        ai_analysis=load_json(base_dir / "ai_portfolio_analysis.json", {}),
        market_regime=load_json(base_dir / "market_regime_v2_state.json", {}),
        contribution_ledger=load_json(base_dir / "contribution_ledger.json", {"contributions": []}),
        now=now,
        trusted_sector_catalog=sector_catalog,
        sector_catalog_summary=sector_summary,
        sector_catalog_warnings=sector_warnings,
    )
    try:
        from cash_wallet_projection import build_wallet_projection, write_wallet_projection

        projection = build_wallet_projection(
            plan.get("cash_info") or {},
            base_dir=base_dir,
            now=now,
        )
        plan["cash_wallet_projection"] = projection
        if write:
            write_wallet_projection(projection, base_dir=base_dir)
    except Exception as exc:
        # The reported cash resource remains authoritative. A derived audit
        # projection must not break plan generation or invent buying power.
        plan.setdefault("warnings", []).append(
            f"cash_wallet_projection_unavailable:{type(exc).__name__}"
        )
    if write:
        atomic_write_json(base_dir / "execution_plan_state.json", plan)
    return plan


def format_inspection(plan: dict[str, Any]) -> str:
    budgets = plan.get("budgets") or {}
    summary = plan.get("consumption_summary") or {}
    lines = [
        f"Execution plan as_of={plan.get('as_of')} status={plan.get('status')}",
        f"Horizon: {plan.get('horizon', {}).get('week_start')}..{plan.get('horizon', {}).get('week_end')} / {plan.get('horizon', {}).get('month')}",
        (
            "Budgets: "
            f"monthly=¥{_jpy(budgets.get('monthly_total_jpy')):,} "
            f"weekly_normal=¥{_jpy(budgets.get('weekly_normal_jpy')):,} "
            f"opportunity=¥{_jpy(budgets.get('weekly_opportunity_reserve_jpy')):,} "
            f"h2_cap=¥{_jpy(budgets.get('h2_hard_cap_jpy')):,}"
        ),
        (
            "Consumption: "
            f"open=¥{_jpy(summary.get('open_order_consumed_jpy')):,} "
            f"filled=¥{_jpy(summary.get('filled_consumed_jpy')):,} "
            f"remaining_normal=¥{_jpy(summary.get('remaining_normal_jpy')):,}"
        ),
    ]
    warnings = plan.get("warnings") or []
    if warnings:
        lines.append("Warnings:")
        lines.extend(f"- {w}" for w in warnings)
    items = plan.get("items") or []
    if items:
        lines.append("Items:")
        for item in items:
            lines.append(
                f"- {item.get('plan_item_id')} {item.get('objective')} "
                f"budget=¥{_jpy(item.get('normal_budget_jpy')):,} "
                f"consumed=¥{_jpy(item.get('consumed_jpy')):,} "
                f"remaining=¥{_jpy(item.get('remaining_jpy')):,} "
                f"status={item.get('status')}"
            )
    else:
        lines.append("Items: none")
    return "\n".join(lines)


def inspect_execution_plan(*, base_dir: Path = BASE_DIR) -> str:
    plan = load_json(base_dir / "execution_plan_state.json", {})
    if not plan:
        return "execution_plan_state.json not found. Run: python execution_plan_engine.py generate"
    return format_inspection(plan)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate or inspect execution_plan_state.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="generate execution_plan_state.json")
    sub.add_parser("inspect", help="print a human-readable execution plan summary")
    args = parser.parse_args(argv)

    if args.cmd == "generate":
        plan = generate_execution_plan()
        print(format_inspection(plan))
        return 0
    if args.cmd == "inspect":
        print(inspect_execution_plan())
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
