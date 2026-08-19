import json

import pytest

from deployment_rollout import (
    load_state,
    promote,
    quarantine_analysis_if_needed,
    record_production_canary,
    record_replay_cycle,
    rollback,
    validate_analysis,
)


def _analysis(*, extra_actions=None):
    buy = {
        "ticker": "VT",
        "type": "buy",
        "tier": "Long",
        "source": "scheduled_broad_deployment",
        "execution_readiness": "ready",
        "capital_allocator_selected": True,
        "estimated_notional_jpy": 480_000,
        "human_execution_only": True,
        "execution_owner": "example_owner",
        "execution_broker": "example_broker",
        "execution_account": "example_taxable",
        "cash_wallet_key": "example_owner|example_broker|broker_cash|USD",
        "plan_item_id": "plan-broad",
        "route_id": "example-route",
    }
    sell = {
        "ticker": "OLD",
        "type": "sell",
        "execution_readiness": "ready",
    }
    actions = [buy, sell, *(extra_actions or [])]
    return {
        "analysis_id": "analysis-canary",
        "as_of": "2026-08-19T06:30:00+09:00",
        "priority_actions": actions,
        "executable_plan_summary": {"analysis_ready_count": len(actions)},
        "selection_consistency": {
            "status": "ok",
            "household_ready_risk_buy_count": 1,
            "household_ready_risk_buy_limit": 1,
        },
    }


def test_valid_analysis_preserves_risk_reducing_action():
    result = validate_analysis(_analysis())
    assert result["status"] == "ok"
    assert result["ready_household_cash_buy_count"] == 1
    assert result["risk_reducing_ready_count"] == 1


def test_enforce_invariant_failure_quarantines_risk_increase_only(tmp_path):
    payload = _analysis(extra_actions=[{
        "ticker": "SECOND",
        "type": "add",
        "tier": "Long",
        "execution_readiness": "ready",
        "capital_allocator_selected": True,
        "estimated_notional_jpy": 200_000,
    }, {
        "ticker": "HEDGE",
        "type": "cover",
        "execution_readiness": "ready",
    }])
    payload["executable_plan_summary"]["analysis_ready_count"] = 4
    payload["selection_consistency"] = {
        "status": "inconsistent",
        "household_ready_risk_buy_count": 2,
        "household_ready_risk_buy_limit": 1,
    }
    calls = []

    result = quarantine_analysis_if_needed(
        payload,
        base_dir=tmp_path,
        effective_gate_mode="enforce",
        set_mode=lambda mode, **kwargs: calls.append((mode, kwargs)) or {},
    )

    assert result["status"] == "failed"
    assert calls[0][0] == "observe"
    by_ticker = {row["ticker"]: row for row in payload["priority_actions"]}
    assert by_ticker["VT"]["execution_readiness"] == "review"
    assert by_ticker["SECOND"]["execution_readiness"] == "review"
    assert by_ticker["OLD"]["execution_readiness"] == "ready"
    assert by_ticker["HEDGE"]["execution_readiness"] == "ready"
    assert load_state(base_dir=tmp_path)["mode"] == "quarantine"


def test_promotion_requires_five_replays_canary_and_observer_readiness(tmp_path):
    payload = _analysis()
    for index, scenario in enumerate((
        "same_day", "next_business_day", "week_boundary", "month_boundary", "regime_refresh"
    ), start=1):
        record_replay_cycle(
            base_dir=tmp_path,
            synthesis=payload,
            cycle_id=f"cycle-{index}",
            simulated_as_of=f"2026-08-{18 + index:02d}T06:30:00+09:00",
            scenario=scenario,
            actor="test-operator",
            reference="isolated-suite",
        )
    record_production_canary(
        base_dir=tmp_path,
        synthesis=payload,
        actor="test-operator",
        reference="canary-1",
    )
    modes = []
    state = promote(
        base_dir=tmp_path,
        actor="test-operator",
        reference="rollout-approval",
        observer_readiness={"ready_for_enforce": True},
        set_mode=lambda mode, **kwargs: modes.append(mode) or {},
    )
    assert modes == ["enforce"]
    assert state["mode"] == "enforce"
    assert state["last_transition"]["event_type"] == "deployment_rollout_promoted"
    audit = (tmp_path / "deployment_rollout_audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(audit) == 8  # five cycles, canary, request, promotion


def test_promotion_failure_rolls_gate_back_to_observe(tmp_path):
    payload = _analysis()
    for index in range(5):
        record_replay_cycle(
            base_dir=tmp_path,
            synthesis=payload,
            cycle_id=f"cycle-{index}",
            simulated_as_of=f"2026-09-{index + 1:02d}T06:30:00+09:00",
            scenario=f"scenario-{index}",
            actor="tester",
            reference="suite",
        )
    record_production_canary(
        base_dir=tmp_path, synthesis=payload, actor="tester", reference="canary",
    )
    calls = []

    def _mode(mode, **kwargs):
        calls.append(mode)
        if mode == "enforce":
            raise RuntimeError("synthetic gate failure")
        return {}

    with pytest.raises(RuntimeError, match="synthetic gate failure"):
        promote(
            base_dir=tmp_path,
            actor="tester",
            reference="approval",
            observer_readiness={"ready_for_enforce": True},
            set_mode=_mode,
        )
    assert calls == ["enforce", "observe"]
    assert load_state(base_dir=tmp_path)["mode"] == "quarantine"


def test_explicit_rollback_restores_observe(tmp_path):
    calls = []
    state = rollback(
        base_dir=tmp_path,
        actor="operator",
        reason="unexpected candidate count",
        reference="incident-1",
        quarantine=False,
        set_mode=lambda mode, **kwargs: calls.append(mode) or {},
    )
    assert calls == ["observe"]
    assert state["mode"] == "observe"
    assert json.loads(
        (tmp_path / "deployment_rollout_audit.jsonl").read_text(encoding="utf-8")
    )["event_type"] == "deployment_rollout_rolled_back"
