import backup_manager


def test_backup_keeps_risk_enforcement_audit_state():
    assert {
        "execution_preflight_acknowledgements.jsonl",
        "flow_adjusted_dd_shadow.json",
        "drawdown_state.json",
    }.issubset(set(backup_manager.TARGETS))
