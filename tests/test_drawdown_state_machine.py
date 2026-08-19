from drawdown_state_machine import advance_state, enforcement_eligibility, initial_state
from drawdown_enforcement import advance_enforced_state, promote, recover, status
from capital_deployment import resolve_drawdown_pacing
from event_ledger import query_events
import json
import sqlite3
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


def test_missing_promoted_state_can_be_recovered_only_from_replay(tmp_path, monkeypatch):
    (tmp_path / "flow_adjusted_dd_shadow.json").write_text(json.dumps({
        "effective_nav_days": 60, "forward_shadow_effective_days": 60,
        "flow_coverage": .95, "invalid_days": [],
        "manual_reconciliation_required": True,
        "estimated_rows_excluded": True, "weekend_rows_excluded": True,
        "flow_adjusted_current_dd_decimal": -.081, "end_date": "2026-10-30",
    }), encoding="utf-8")
    promote(base_dir=tmp_path, manual_reconciliation_reference="statement")
    (tmp_path / "drawdown_state.json").unlink()

    import nav_recorder
    monkeypatch.setattr(nav_recorder, "replay_flow_adjusted_drawdown_points", lambda **kwargs: {
        "status": "ok",
        "points": [
            {"effective_nav_date": "2026-10-29", "drawdown_decimal": -.07},
            {"effective_nav_date": "2026-10-30", "drawdown_decimal": -.081},
            {"effective_nav_date": "2026-11-02", "drawdown_decimal": -.09},
        ],
        "effective_nav_days": 61,
        "start_date": "2026-08-03",
        "end_date": "2026-11-02",
        "flow_coverage": .97,
        "invalid_days": [],
        "current_drawdown_decimal": -.09,
    })

    state = recover(
        base_dir=tmp_path,
        approval_actor="portfolio-owner",
        approval_reason="state file lost after promotion",
        approval_reference="recovery-ticket-1",
    )
    assert state["enforcement_enabled"] is True
    assert state["dd_state"] == "block"
    assert state["recovery_event_id"]
    events = query_events(types=["drawdown_controller_recovered"], db_path=tmp_path / "almanac.db")
    assert len(events) == 1
    assert resolve_drawdown_pacing(base_dir=tmp_path)["dd_pacing_multiplier"] == 0.25


def test_recovery_fails_closed_when_flow_coverage_is_low(tmp_path, monkeypatch):
    (tmp_path / "flow_adjusted_dd_shadow.json").write_text(json.dumps({
        "effective_nav_days": 60, "forward_shadow_effective_days": 60,
        "flow_coverage": .95, "invalid_days": [],
        "manual_reconciliation_required": True,
        "estimated_rows_excluded": True, "weekend_rows_excluded": True,
        "flow_adjusted_current_dd_decimal": -.02, "end_date": "2026-10-30",
    }), encoding="utf-8")
    promote(base_dir=tmp_path, manual_reconciliation_reference="statement")
    (tmp_path / "drawdown_state.json").unlink()
    import nav_recorder
    monkeypatch.setattr(nav_recorder, "replay_flow_adjusted_drawdown_points", lambda **kwargs: {
        "status": "cash_flow_coverage_below_95pct",
        "points": [],
        "flow_coverage": .94,
        "invalid_days": [],
    })
    with pytest.raises(ValueError, match="not recoverable"):
        recover(
            base_dir=tmp_path,
            approval_actor="owner",
            approval_reason="test",
            approval_reference="ticket",
        )
    assert not (tmp_path / "drawdown_state.json").exists()


def test_recovery_rejects_tampered_promotion_evidence(tmp_path):
    (tmp_path / "flow_adjusted_dd_shadow.json").write_text(json.dumps({
        "effective_nav_days": 60, "forward_shadow_effective_days": 60,
        "flow_coverage": .95, "invalid_days": [],
        "manual_reconciliation_required": True,
        "estimated_rows_excluded": True, "weekend_rows_excluded": True,
        "flow_adjusted_current_dd_decimal": -.02, "end_date": "2026-10-30",
    }), encoding="utf-8")
    promote(base_dir=tmp_path, manual_reconciliation_reference="statement")
    (tmp_path / "drawdown_state.json").unlink()
    db_path = tmp_path / "almanac.db"
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT raw_payload FROM ledger_events WHERE event_type = ?",
            ("drawdown_controller_promoted",),
        ).fetchone()
        payload = json.loads(row[0])
        payload["initial_drawdown_decimal"] = -.20
        conn.execute(
            "UPDATE ledger_events SET raw_payload = ? WHERE event_type = ?",
            (json.dumps(payload), "drawdown_controller_promoted"),
        )
    with pytest.raises(ValueError, match="hash mismatch"):
        recover(
            base_dir=tmp_path,
            approval_actor="owner",
            approval_reason="test",
            approval_reference="ticket",
        )
