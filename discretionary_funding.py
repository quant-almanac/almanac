"""Authoritative discretionary-funding guard for new risk-increasing orders.

The execution plan is the only source of discretionary buying power.  Cash
balances, stale fallback values, and a disabled plan must never manufacture an
order budget.  Historical fills are facts and are intentionally outside this
module; callers apply this guard only to recommendations and new open orders.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from numbers import Real
from pathlib import Path
from typing import Any


FUNDING_REQUIRED_ACTION_TYPES = {"buy", "add", "dca", "margin_buy"}
EXECUTION_PLAN_SCHEMA_VERSION = 2
MAX_ACTIVE_PLAN_AGE = timedelta(hours=36)
MAX_FUTURE_CLOCK_SKEW = timedelta(minutes=5)


def _jpy(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        return 0
    return int(round(numeric))


def _parse_aware_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _invalid_plan(reason_code: str, message: str, **details: Any) -> dict[str, Any]:
    return {
        "valid": False,
        "reason_code": reason_code,
        "message": message,
        **details,
    }


def validate_active_execution_plan(
    plan_state: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate the plan as current order authority, not merely as JSON.

    The plan is regenerated as part of each formal analysis.  A stale file,
    future-dated file, legacy schema, or invalid budget must never preserve
    buying authority when that refresh fails.
    """
    if not isinstance(plan_state, dict):
        return _invalid_plan(
            "discretionary_funding_unresolved",
            "裁量投資計画を読み込めないため、新規買い注文を許可しません",
        )
    if plan_state.get("schema_version") != EXECUTION_PLAN_SCHEMA_VERSION:
        return _invalid_plan(
            "execution_plan_contract_invalid",
            "裁量投資計画のschemaを確認できないため、新規買い注文を許可しません",
            plan_schema_version=plan_state.get("schema_version"),
        )

    status = str(plan_state.get("status") or "").strip().lower()
    if status != "active":
        return _invalid_plan(
            "discretionary_funding_unresolved",
            "有効な裁量投資計画を確認できないため、新規買い注文を許可しません",
            plan_status=status or "unknown",
        )

    as_of = _parse_aware_timestamp(plan_state.get("as_of"))
    if as_of is None:
        return _invalid_plan(
            "execution_plan_contract_invalid",
            "裁量投資計画の基準時刻を確認できないため、新規買い注文を許可しません",
        )
    current = now or datetime.now().astimezone()
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
    age = current.astimezone(timezone.utc) - as_of.astimezone(timezone.utc)
    if age < -MAX_FUTURE_CLOCK_SKEW:
        return _invalid_plan(
            "execution_plan_future_dated",
            "裁量投資計画の基準時刻が未来のため、新規買い注文を許可しません",
            plan_as_of=as_of.isoformat(),
        )
    if age > MAX_ACTIVE_PLAN_AGE:
        return _invalid_plan(
            "execution_plan_stale",
            "裁量投資計画が古いため、新規買い注文を許可しません",
            plan_as_of=as_of.isoformat(),
            plan_age_hours=round(age.total_seconds() / 3600, 2),
        )

    horizon = plan_state.get("horizon")
    if not isinstance(horizon, dict):
        return _invalid_plan(
            "execution_plan_contract_invalid",
            "裁量投資計画の有効期間を確認できないため、新規買い注文を許可しません",
        )
    week_start = _parse_date(horizon.get("week_start"))
    week_end = _parse_date(horizon.get("week_end"))
    month = str(horizon.get("month") or "")
    plan_day = current.astimezone(as_of.tzinfo).date()
    if (
        week_start is None
        or week_end is None
        or week_start.weekday() != 0
        or week_end != week_start + timedelta(days=6)
        or month != f"{plan_day.year:04d}-{plan_day.month:02d}"
    ):
        return _invalid_plan(
            "execution_plan_contract_invalid",
            "裁量投資計画の有効期間が不正なため、新規買い注文を許可しません",
        )
    if not week_start <= plan_day <= week_end:
        return _invalid_plan(
            "execution_plan_expired",
            "裁量投資計画の週次有効期間外のため、新規買い注文を許可しません",
            plan_week_start=week_start.isoformat(),
            plan_week_end=week_end.isoformat(),
        )

    budgets = plan_state.get("budgets")
    contribution = plan_state.get("contribution_summary")
    if not isinstance(budgets, dict) or not isinstance(contribution, dict):
        return _invalid_plan(
            "execution_plan_contract_invalid",
            "裁量投資計画の予算内訳を確認できないため、新規買い注文を許可しません",
        )
    numeric_fields = (
        (budgets, "normal_pool_available_jpy"),
        (budgets, "opportunity_pool_available_jpy"),
        (contribution, "available_jpy"),
    )
    invalid_fields = [
        key
        for container, key in numeric_fields
        if (
            key not in container
            or isinstance(container.get(key), bool)
            or not isinstance(container.get(key), Real)
            or not math.isfinite(float(container.get(key)))
            or float(container.get(key)) < 0
        )
    ]
    if invalid_fields:
        return _invalid_plan(
            "execution_plan_contract_invalid",
            "裁量投資計画の予算値が不正なため、新規買い注文を許可しません",
            invalid_budget_fields=invalid_fields,
        )
    return {
        "valid": True,
        "reason_code": None,
        "plan_as_of": as_of.isoformat(),
        "plan_week_end": week_end.isoformat(),
    }


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
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a fail-closed funding decision for a recommendation/order.

    ``sell``/``trim``/``cover`` and recorded fills do not consume a new
    discretionary budget.  The latter distinction is enforced by API callers,
    which call this helper for ``ordered`` requests only.
    """
    normalized = str(action_type or "").strip().lower()
    if normalized not in FUNDING_REQUIRED_ACTION_TYPES:
        return {"required": False, "allowed": True, "reason_code": None}

    validation = validate_active_execution_plan(plan_state, now=now)
    if not validation.get("valid"):
        return {
            "required": True,
            "allowed": False,
            "reason_code": validation.get("reason_code"),
            "message": validation.get("message"),
            **{
                key: value
                for key, value in validation.items()
                if key not in {"valid", "reason_code", "message"}
            },
        }

    assert isinstance(plan_state, dict)
    budgets = plan_state.get("budgets")
    contribution = plan_state.get("contribution_summary")
    assert isinstance(budgets, dict) and isinstance(contribution, dict)

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
