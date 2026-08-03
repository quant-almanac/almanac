import risk_engine
from risk_policy import POLICY


def test_legacy_risk_engine_uses_fixed_policy_short_limit(monkeypatch):
    """Retired tunables cannot alter the active risk policy."""
    result = risk_engine.evaluate_behavioral_guardrails(
        daily_pnl_pct=0,
        monthly_pnl_pct=0,
        active_trades=0,
        short_positions=1,
    )
    assert result["guardrails"]["max_short_positions"] == POLICY.max_short_positions
    assert not any("1/1" in row["message"] for row in result["alerts"])
