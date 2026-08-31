import math
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from api.routes.actions import (
    BrokerConfirmationEvidence,
    ExecutionRequest,
    PreflightRequest,
)
from discretionary_funding import evaluate_discretionary_funding


JST = ZoneInfo("Asia/Tokyo")
NOW = datetime(2026, 8, 31, 6, 0, tzinfo=JST)


def _active_plan(*, as_of: datetime = NOW - timedelta(hours=1)) -> dict:
    return {
        "schema_version": 2,
        "as_of": as_of.isoformat(),
        "horizon": {
            "month": "2026-08",
            "week_start": "2026-08-31",
            "week_end": "2026-09-06",
        },
        "status": "active",
        "budgets": {
            "normal_pool_available_jpy": 100_000,
            "opportunity_pool_available_jpy": 0,
        },
        "contribution_summary": {"available_jpy": 0},
    }


def test_current_plan_is_valid_order_authority() -> None:
    decision = evaluate_discretionary_funding(
        "buy", plan_state=_active_plan(), now=NOW
    )

    assert decision["allowed"] is True
    assert decision["available_jpy"] == 100_000


@pytest.mark.parametrize(
    ("mutate", "reason_code"),
    [
        (lambda plan: plan.pop("schema_version"), "execution_plan_contract_invalid"),
        (
            lambda plan: plan.update({"as_of": (NOW - timedelta(hours=37)).isoformat()}),
            "execution_plan_stale",
        ),
        (
            lambda plan: plan.update({"as_of": (NOW + timedelta(minutes=6)).isoformat()}),
            "execution_plan_future_dated",
        ),
        (
            lambda plan: plan.update({
                "horizon": {
                    "month": "2026-08",
                    "week_start": "2026-08-24",
                    "week_end": "2026-08-30",
                }
            }),
            "execution_plan_expired",
        ),
        (
            lambda plan: plan["budgets"].update({"normal_pool_available_jpy": math.nan}),
            "execution_plan_contract_invalid",
        ),
        (
            lambda plan: plan["budgets"].update({"normal_pool_available_jpy": True}),
            "execution_plan_contract_invalid",
        ),
        (
            lambda plan: plan["budgets"].update({"normal_pool_available_jpy": "100000"}),
            "execution_plan_contract_invalid",
        ),
    ],
)
def test_invalid_plan_never_authorizes_buy(mutate, reason_code: str) -> None:
    plan = _active_plan()
    mutate(plan)

    decision = evaluate_discretionary_funding("buy", plan_state=plan, now=NOW)

    assert decision["allowed"] is False
    assert decision["reason_code"] == reason_code


def test_invalid_plan_does_not_block_risk_reducing_sell() -> None:
    assert evaluate_discretionary_funding(
        "sell", plan_state=None, now=NOW
    ) == {"required": False, "allowed": True, "reason_code": None}


@pytest.mark.parametrize("bad", [True, False, math.nan, math.inf, -math.inf, "10"])
@pytest.mark.parametrize(
    ("model", "payload"),
    [
        (
            ExecutionRequest,
            {
                "ticker": "SPY",
                "direction": "buy",
                "quantity": 1,
                "price": 100,
                "idempotency_key": "finite-number-test",
            },
        ),
        (
            PreflightRequest,
            {"ticker": "SPY", "direction": "buy", "quantity": 1, "price": 100},
        ),
        (
            BrokerConfirmationEvidence,
            {
                "ticker": "SPY",
                "direction": "buy",
                "quantity": 1,
                "price": 100,
                "status": "executed",
            },
        ),
    ],
)
def test_order_models_reject_nonfinite_bool_and_string_numbers(model, payload, bad) -> None:
    payload = {**payload, "quantity": bad}
    with pytest.raises(ValidationError):
        model(**payload)
