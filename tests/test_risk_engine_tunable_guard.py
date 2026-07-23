import risk_engine
import tunable_params


def test_legacy_risk_engine_reads_dynamic_short_limit(monkeypatch):
    monkeypatch.setattr(tunable_params, "get", lambda key, fallback=None: 1 if key == "max_short_positions" else fallback)
    result = risk_engine.evaluate_behavioral_guardrails(
        daily_pnl_pct=0,
        monthly_pnl_pct=0,
        active_trades=0,
        short_positions=1,
    )
    assert result["guardrails"]["max_short_positions"] == 1
    assert any("1/1" in row["message"] for row in result["alerts"])
