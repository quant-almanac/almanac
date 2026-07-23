import json
from datetime import date
from pathlib import Path

import pytest

import scenario_engine
import technical_signals
from almanac.observability.catalyst_layer import synthesize_from_active_scenarios
from scenario_invariants import (
    active_scenario_action_tickers,
    check_action_tickers_in_universe,
    check_currency_constraints,
    check_observe_only_measurement_lanes,
    check_outcome_log_coverage,
    check_restricted_tickers_not_in_playbook,
    check_required_signals_declared_in_detect,
    check_signal_resolvability,
    scenario_action_tickers_from_playbook,
)


ROOT = Path(__file__).resolve().parent.parent


def _load_json(name: str) -> dict:
    return json.loads((ROOT / name).read_text(encoding="utf-8"))


def test_enabled_scenario_action_tickers_are_in_tickers_all() -> None:
    playbook = _load_json("scenario_playbook.json")
    if not (ROOT / "tickers.json").exists():
        pytest.skip("private tickers.json is intentionally excluded from the public snapshot")
    tickers = _load_json("tickers.json")

    issues = check_action_tickers_in_universe(playbook, tickers)

    assert issues == []
    assert {"EWG", "EPOL", "IEV"}.issubset(set(tickers["all"]))
    assert "1570.T" not in scenario_action_tickers_from_playbook(playbook, decision_only=True)


def test_restricted_tickers_are_absent_from_scenario_playbook() -> None:
    issues = check_restricted_tickers_not_in_playbook(_load_json("scenario_playbook.json"))

    assert issues == []


def test_restricted_scenario_playbook_invariant_covers_all_action_directions() -> None:
    playbook = {
        "scenarios": [
            {"id": "bad_buy", "actions": {"phase_1": {"buy": [{"ticker": "9999.T"}]}}},
            {"id": "bad_sell", "actions": {"phase_1": {"sell": [{"ticker": "9999"}]}}},
            {"id": "bad_trigger", "actions": {"sell_on_trigger": ["9999.T"]}},
        ]
    }

    issues = check_restricted_tickers_not_in_playbook(playbook)

    assert [issue.code for issue in issues] == [
        "scenario_restricted_ticker_in_playbook",
        "scenario_restricted_ticker_in_playbook",
        "scenario_restricted_ticker_in_playbook",
    ]
    assert {issue.context["source"] for issue in issues} == {"buy", "sell", "sell_on_trigger"}
    assert {issue.context["scenario_id"] for issue in issues} == {
        "bad_buy",
        "bad_sell",
        "bad_trigger",
    }


def test_technical_universe_includes_scenario_action_tickers() -> None:
    universe = set(technical_signals._build_ticker_universe())

    assert {"EWG", "EPOL", "IEV", "1321.T"}.issubset(universe)
    assert "1570.T" not in universe


def test_active_state_action_tickers_include_trigger_only_sells() -> None:
    state = {
        "scenarios": {
            "credit_crisis": {
                "status": "active",
                "recommended_actions": {
                    "phase_1": [{"ticker": "GLD"}, {"ticker": "EPOL", "action": "sell_all"}],
                    "sell_on_trigger": ["EPOL", "6762.T"],
                },
            }
        }
    }

    assert active_scenario_action_tickers(state) == ["6762.T", "EPOL", "GLD"]


def test_catalyst_synthesizes_sell_trigger_only_tickers_without_duplicates() -> None:
    state = {
        "scenarios": {
            "credit_crisis": {
                "status": "active",
                "readiness": 0.8,
                "enabled_for_decision": True,
                "recommended_actions": {
                    "phase_1": [
                        {"ticker": "EPOL", "action": "sell_all", "reason": "risk off"},
                        {"ticker": "GLD", "allocation_usd": 5000},
                    ],
                    "sell_on_trigger": ["EPOL", "6762.T"],
                },
            }
        }
    }

    result = synthesize_from_active_scenarios(
        state, analysis_id="test", analysis_date="2026-06-17"
    )
    by_ticker = {h.ticker: h for h in result}

    assert by_ticker["EPOL"].action_type == "sell"
    assert by_ticker["6762.T"].action_type == "sell"
    assert [h.ticker for h in result].count("EPOL") == 1


def test_indicator_resolvers_use_ma200_and_smh_volume_ratio() -> None:
    scenario = {
        "detect": {
            "indicators": {
                "qqq_nasdaq": {"condition": "above_ma200", "description": "QQQ above MA200"},
                "smh_volume": {"condition": "above_avg_pct", "threshold": 50},
            }
        }
    }
    tech_state = {
        "tickers": {
            "QQQ": {"ma200_diff_pct": 1.25},
            "SMH": {"volume_ratio": 1.7},
        }
    }

    rows = scenario_engine._eval_indicators(
        scenario, vix_state={}, macro_state={}, tech_state=tech_state, market_state={}
    )
    by_key = {row["key"]: row for row in rows}

    assert by_key["qqq_nasdaq"]["matched"] is True
    assert by_key["qqq_nasdaq"]["value"] == pytest.approx(1.25)
    assert by_key["smh_volume"]["matched"] is True
    assert by_key["smh_volume"]["value"] == pytest.approx(70.0)


def test_current_playbook_signals_are_theoretically_resolvable() -> None:
    if not (ROOT / "technical_state.json").exists():
        pytest.skip("private runtime signal state is intentionally excluded")
    issues = check_signal_resolvability(_load_json("scenario_playbook.json"), base_dir=ROOT)
    assert issues == []


def test_required_signals_are_declared_in_detect() -> None:
    issues = check_required_signals_declared_in_detect(_load_json("scenario_playbook.json"))
    assert issues == []


def test_required_signal_invariant_catches_dead_required_key() -> None:
    playbook = {
        "scenarios": [
            {
                "id": "bad",
                "detect": {
                    "news_keywords": ["ceasefire"],
                    "indicators": {"vix": {"condition": "below", "threshold": 18}},
                    "required_signals": ["vix", "spy_dist_from_ma50_pct"],
                },
            }
        ]
    }

    issues = check_required_signals_declared_in_detect(playbook)

    assert [issue.code for issue in issues] == ["scenario_required_signal_missing_from_detect"]
    assert issues[0].context["missing_required_signals"] == ["spy_dist_from_ma50_pct"]


def test_observe_only_lanes_have_measurement_and_promotion_criteria() -> None:
    registry = _load_json("lane_registry.json")
    issues = check_observe_only_measurement_lanes(registry)
    assert issues == []
    scenario_lane = next(row for row in registry["lanes"] if row["name"] == "scenario_monitor")
    assert scenario_lane["promotion_path"] == "scenario_promotion_summary.json"


def test_scenario_shadow_book_lane_is_retired_from_repo() -> None:
    assert not (ROOT / "scenario_shadow_book.py").exists()
    assert not (ROOT / "tests" / "test_scenario_shadow_book.py").exists()
    cron = ROOT / "crontab.proposed"
    if cron.exists():
        assert "scenario_shadow_book.py" not in cron.read_text(encoding="utf-8")


def test_outcome_coverage_requires_each_generated_recommendation_type() -> None:
    generated = [
        {"event_type": "generated", "hypothesis_id": "sc1", "hypothesis_type": "scenario_war_end",
         "event_at": "2026-06-01T00:00:00+00:00", "horizon_days": 5},
        {"event_type": "generated", "hypothesis_id": "disc1", "hypothesis_type": "disclosure_catalyst",
         "event_at": "2026-06-01T00:00:00+00:00", "horizon_days": 5},
        {"event_type": "generated", "hypothesis_id": "scr1", "hypothesis_type": "screener_short",
         "event_at": "2026-06-01T00:00:00+00:00", "horizon_days": 5},
        {"event_type": "generated", "hypothesis_id": "dca1", "source_event_id": "dca:T1:2026-06-17",
         "event_at": "2026-06-01T00:00:00+00:00", "horizon_days": 5},
    ]
    outcomes = [{"hypothesis_id": row["hypothesis_id"]} for row in generated]

    assert check_outcome_log_coverage(generated, outcomes, as_of=date(2026, 6, 17)) == []
    missing = check_outcome_log_coverage(generated, outcomes[:-1], as_of=date(2026, 6, 17))
    assert [issue.code for issue in missing] == ["recommendation_type_missing_outcome_rows"]
    assert missing[0].context["recommendation_type"] == "dca"


def test_scenario_action_currency_constraints_hold() -> None:
    issues = check_currency_constraints(_load_json("scenario_playbook.json"))
    assert issues == []
