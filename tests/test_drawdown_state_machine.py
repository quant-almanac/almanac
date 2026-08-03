from drawdown_state_machine import advance_state, enforcement_eligibility, initial_state
from drawdown_enforcement import advance_enforced_state, promote, status
import json
import pytest


def test_worsening_is_immediate_but_recovery_requires_five_effective_days():
    state = advance_state(initial_state(), drawdown_decimal=-0.081, effective_nav_date="2026-08-03")
    assert state["dd_state"] == "block"
    for day in range(4, 8):
        state = advance_state(state, drawdown_decimal=-0.059, effective_nav_date=f"2026-08-{day:02d}")
    assert state["dd_state"] == "block"
    state = advance_state(state, drawdown_decimal=-0.059, effective_nav_date="2026-08-08")
    assert state["dd_state"] == "caution"


def test_freeze_needs_explicit_human_approval_to_release():
    state = advance_state(initial_state(), drawdown_decimal=-0.121, effective_nav_date="2026-08-01")
    assert state["dd_state"] == "freeze"
    for day in range(2, 7):
        state = advance_state(state, drawdown_decimal=-0.099, effective_nav_date=f"2026-08-{day:02d}")
    assert state["dd_state"] == "freeze"
    state = advance_state(state, drawdown_decimal=-0.099, effective_nav_date="2026-08-07", freeze_release_approved=True)
    assert state["dd_state"] == "derisk_review"


def test_promotion_needs_all_v7_evidence():
    assert not enforcement_eligibility({"effective_nav_days": 60, "forward_shadow_effective_days": 60, "flow_coverage": .95})["eligible"]
    result = enforcement_eligibility({
        "effective_nav_days": 60, "forward_shadow_effective_days": 60,
        "flow_coverage": .95, "invalid_days": [], "manual_reconciliation_required": False,
        "estimated_rows_excluded": True, "weekend_rows_excluded": True,
    })
    assert result["eligible"]


def test_promotion_requires_recorded_manual_reconciliation(tmp_path):
    (tmp_path / "flow_adjusted_dd_shadow.json").write_text(json.dumps({
        "effective_nav_days": 60, "forward_shadow_effective_days": 60,
        "flow_coverage": .95, "invalid_days": [],
        "manual_reconciliation_required": True,
        "estimated_rows_excluded": True, "weekend_rows_excluded": True,
        "flow_adjusted_current_dd_decimal": -.081, "end_date": "2026-10-30",
    }), encoding="utf-8")
    with pytest.raises(ValueError, match="reference"):
        promote(base_dir=tmp_path, manual_reconciliation_reference="")
    promoted = promote(base_dir=tmp_path, manual_reconciliation_reference="broker-statement-2026-10")
    assert promoted["enforcement_enabled"] is True
    assert promoted["dd_state"] == "block"
    assert status(base_dir=tmp_path)["state"]["manual_reconciliation_reference"] == "broker-statement-2026-10"


def test_freeze_release_audit_requires_actor_reason_reference(tmp_path):
    (tmp_path / "flow_adjusted_dd_shadow.json").write_text(json.dumps({
        "flow_adjusted_current_dd_decimal": -.099,
        "end_date": "2026-11-10",
    }), encoding="utf-8")
    (tmp_path / "drawdown_state.json").write_text(json.dumps({
        "dd_state": "freeze",
        "enforcement_enabled": True,
        "recovery_effective_days": 5,
        "last_effective_nav_date": "2026-11-10",
        "transitions": [],
    }), encoding="utf-8")

    with pytest.raises(ValueError, match="actor, reason, and reference"):
        advance_enforced_state(
            base_dir=tmp_path, freeze_release_approved=True,
            approval_reference="ticket-1",
        )
    released = advance_enforced_state(
        base_dir=tmp_path,
        freeze_release_approved=True,
        approval_actor="portfolio-owner",
        approval_reason="five effective recovery days above -10%",
        approval_reference="ticket-1",
    )
    assert released["dd_state"] == "derisk_review"
    audit = released["freeze_release_approvals"][-1]
    assert audit["actor"] == "portfolio-owner"
    assert audit["reason"]
    assert audit["reference"] == "ticket-1"
    assert audit["at"]
