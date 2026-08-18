from datetime import datetime, timezone

from capital_deployment import issue_scheduled_broad_permission, resolve_drawdown_pacing, validate_scheduled_broad_permission


def test_missing_controller_is_prepromotion_but_unreadable_ledger_fails_closed(tmp_path):
    assert resolve_drawdown_pacing(base_dir=tmp_path)["dd_pacing_multiplier"] == 1.0
    (tmp_path / "almanac.db").write_text("not sqlite", encoding="utf-8")
    assert resolve_drawdown_pacing(base_dir=tmp_path)["dd_pacing_multiplier"] == 0.0


def test_scheduled_broad_permission_is_action_bound():
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    action = {"ticker": "VT", "type": "buy", "source": "scheduled_broad_deployment", "human_execution_only": True, "estimated_notional_jpy": 500_000}
    action["capital_deployment_permission"] = issue_scheduled_broad_permission(action=action, canonical_dd_stage="block", dd_pacing_multiplier=.25, state_snapshot={"dd": -.09}, now=now)
    assert validate_scheduled_broad_permission(action, canonical_dd_stage="block", now=now)
    action["ticker"] = "VTI"
    assert not validate_scheduled_broad_permission(action, canonical_dd_stage="block", now=now)
