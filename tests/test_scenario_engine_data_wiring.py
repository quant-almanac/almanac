import json

import scenario_engine


def _bull_pullback_scenario() -> dict:
    return {
        "id": "bull_pullback",
        "name": "強気相場の押し目買い",
        "detect": {
            "news_keywords": [],
            "indicators": {
                "spy_dist_from_ma50_pct": {
                    "condition": "between",
                    "lower": -0.08,
                    "upper": -0.03,
                    "key": "spy_dist_from_ma50_pct",
                }
            },
            "technical": {
                "SPY_above_MA50": {"condition": "true"},
                "regime_bull_confirmed": {"condition": "true"},
            },
        },
        "actions": {
            "phase_1_conservative": {"buy": [{"ticker": "SPY"}]},
            "phase_2_aggressive": {"buy": [{"ticker": "NVDA"}]},
            "phase_3_tactical": {"buy": [{"ticker": "TQQQ"}]},
        },
    }


def test_indicator_reads_spy_ma50_distance_from_market_snapshot():
    scenario = _bull_pullback_scenario()

    rows = scenario_engine._eval_indicators(
        scenario,
        vix_state={},
        macro_state={},
        market_state={"SPY": {"ma50_diff": -5.0}},
    )

    assert rows[0]["key"] == "spy_dist_from_ma50_pct"
    assert rows[0]["matched"] is True
    assert rows[0]["detail"] != scenario_engine.INCONCLUSIVE_DETAIL
    assert "-5.00%" in rows[0]["detail"]


def test_indicator_reports_above_ma50_as_data_not_missing():
    scenario = _bull_pullback_scenario()

    rows = scenario_engine._eval_indicators(
        scenario,
        vix_state={},
        macro_state={},
        market_state={"SPY": {"ma50_diff": 7.06}},
    )

    assert rows[0]["matched"] is False
    assert rows[0]["detail"] != scenario_engine.INCONCLUSIVE_DETAIL
    assert "outside" in rows[0]["detail"]


def test_technical_reads_spy_ma50_and_regime_state_without_technical_ticker():
    scenario = _bull_pullback_scenario()

    rows = scenario_engine._eval_technical(
        scenario,
        tech_state={},
        market_state={"SPY": {"price": 745.64, "ma50": 696.49, "ma50_diff": 7.06}},
        regime_state={"regime": "A_強気", "macro_score": 10, "spy_above": True, "nk_above": True},
    )
    by_key = {row["key"]: row for row in rows}

    assert by_key["SPY_above_MA50"]["matched"] is True
    assert by_key["SPY_above_MA50"]["detail"] != scenario_engine.INCONCLUSIVE_DETAIL
    assert by_key["regime_bull_confirmed"]["matched"] is True
    assert "A_強気" in by_key["regime_bull_confirmed"]["detail"]


def test_evaluate_scenarios_uses_market_snapshot_and_regime_state(tmp_path, monkeypatch):
    playbook_path = tmp_path / "scenario_playbook.json"
    playbook_path.write_text(json.dumps({"scenarios": [_bull_pullback_scenario()]}, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "vix_state.json").write_text(json.dumps({"vix": {"level": 16.5}}), encoding="utf-8")
    (tmp_path / "geopolitical_state.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "technical_state.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "macro_state.json").write_text(json.dumps({}), encoding="utf-8")
    (tmp_path / "regime_state.json").write_text(json.dumps({
        "regime": "A_強気",
        "macro_score": 10,
        "spy_above": True,
        "nk_above": True,
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "market_snapshot.json").write_text(json.dumps({
        "SPY": {"price": 745.64, "ma50": 696.49, "ma50_diff": 7.06}
    }), encoding="utf-8")
    (tmp_path / "guard_state.json").write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(scenario_engine, "PLAYBOOK_PATH", playbook_path)
    monkeypatch.setattr(scenario_engine, "VIX_STATE_PATH", tmp_path / "vix_state.json")
    monkeypatch.setattr(scenario_engine, "GEO_STATE_PATH", tmp_path / "geopolitical_state.json")
    monkeypatch.setattr(scenario_engine, "TECH_STATE_PATH", tmp_path / "technical_state.json")
    monkeypatch.setattr(scenario_engine, "MACRO_STATE_PATH", tmp_path / "macro_state.json")
    monkeypatch.setattr(scenario_engine, "REGIME_STATE_PATH", tmp_path / "regime_state.json")
    monkeypatch.setattr(scenario_engine, "MARKET_SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    monkeypatch.setattr(scenario_engine, "GUARD_STATE_PATH", tmp_path / "guard_state.json")
    monkeypatch.setattr(scenario_engine, "SCENARIO_STATE_PATH", tmp_path / "scenario_state.json")

    state = scenario_engine.evaluate_scenarios()
    bull = state["scenarios"]["bull_pullback"]
    details = {row["key"]: row for row in bull["signal_details"]}

    assert details["spy_dist_from_ma50_pct"]["matched"] is False
    assert details["SPY_above_MA50"]["matched"] is True
    assert details["regime_bull_confirmed"]["matched"] is True
    assert "spy_dist_from_ma50_pct" in bull["missing_required_signals"]
    assert bull["status"] == "watching"
    assert bull["recommended_actions"]["phase_1"] == [{"ticker": "SPY"}]
    assert bull["recommended_actions"]["phase_2"] == [{"ticker": "NVDA"}]
    assert bull["recommended_actions"]["phase_3"] == [{"ticker": "TQQQ"}]
