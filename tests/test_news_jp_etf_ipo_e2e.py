import json
import sys
import types
from datetime import datetime
from pathlib import Path

import analyst
import geopolitical_monitor
import news_screener
import scenario_engine
from analyst import data_gatherer
from almanac.observability.catalyst_layer import synthesize_from_active_scenarios


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _repo_scenario(scenario_id: str) -> dict:
    playbook = json.loads((ROOT / "scenario_playbook.json").read_text(encoding="utf-8"))
    for scenario in playbook.get("scenarios") or []:
        if isinstance(scenario, dict) and scenario.get("id") == scenario_id:
            return scenario
    raise AssertionError(f"scenario not found: {scenario_id}")


def test_ceasefire_risk_on_activates_war_end_and_surfaces_jp_etf_candidates(tmp_path, monkeypatch):
    war_end = _repo_scenario("war_end")
    _write_json(tmp_path / "scenario_playbook.json", {"scenarios": [war_end]})
    _write_json(
        tmp_path / "geopolitical_state.json",
        {
            "active_alerts": [
                {
                    "title": "Iran ceasefire and Hormuz de-escalation confirmed",
                    "summary": "A ceasefire agreement was confirmed by both sides.",
                    "severity": "high",
                }
            ],
            "keyword_matches": [
                {
                    "scenario_key": "war_end",
                    "score": 2,
                    "threshold": 2,
                    "matched_keywords": ["ceasefire", "Iran de-escalation"],
                    "severity": "high",
                    "confidence": 0.86,
                    "assessment_status": "keyword_only",
                }
            ]
        },
    )
    _write_json(
        tmp_path / "vix_state.json",
        {
            "cached_at": datetime.now().isoformat(),
            "vix": {"level": 16.8, "change_5d": -24.0},
            "oil": {"change_5d_pct": -12.0},
        },
    )
    _write_json(
        tmp_path / "technical_state.json",
        {
            "tickers": {
                "SPY": {"rsi": 58},
                "SOXX": {"macd_crossover": "bullish"},
                "ITA": {"change_5d_pct": -6.0},
                "GLD": {"change_5d_pct": -4.0},
            }
        },
    )
    _write_json(tmp_path / "macro_state.json", {})
    _write_json(tmp_path / "regime_state.json", {})
    _write_json(tmp_path / "market_snapshot.json", {})
    _write_json(tmp_path / "guard_state.json", {})

    monkeypatch.setattr(scenario_engine, "PLAYBOOK_PATH", tmp_path / "scenario_playbook.json")
    monkeypatch.setattr(scenario_engine, "VIX_STATE_PATH", tmp_path / "vix_state.json")
    monkeypatch.setattr(scenario_engine, "GEO_STATE_PATH", tmp_path / "geopolitical_state.json")
    monkeypatch.setattr(scenario_engine, "TECH_STATE_PATH", tmp_path / "technical_state.json")
    monkeypatch.setattr(scenario_engine, "MACRO_STATE_PATH", tmp_path / "macro_state.json")
    monkeypatch.setattr(scenario_engine, "REGIME_STATE_PATH", tmp_path / "regime_state.json")
    monkeypatch.setattr(scenario_engine, "MARKET_SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    monkeypatch.setattr(scenario_engine, "GUARD_STATE_PATH", tmp_path / "guard_state.json")
    monkeypatch.setattr(scenario_engine, "SCENARIO_STATE_PATH", tmp_path / "scenario_state.json")
    monkeypatch.setattr(scenario_engine, "send_telegram", lambda _msg: None)

    state = scenario_engine.evaluate_scenarios()
    scenario = state["scenarios"]["war_end"]

    assert scenario["status"] == "active"
    assert scenario["readiness"] >= 0.6
    assert scenario["missing_required_signals"] == []
    assert scenario["required_signals"] == ["news_keywords", "vix"]

    phase_1 = {row["ticker"] for row in scenario["recommended_actions"]["phase_1"]}
    phase_2 = {row["ticker"] for row in scenario["recommended_actions"]["phase_2"]}
    assert "1306.T" in phase_1
    assert "1489.T" in phase_2

    playbook_actions = []
    for phase in ("phase_1", "phase_2", "phase_3"):
        for action in scenario["recommended_actions"].get(phase) or []:
            row = dict(action)
            row["phase"] = phase
            playbook_actions.append(row)
    context = analyst._fmt_scenario_monitoring(
        {
            "active_scenarios": [
                {
                    "id": "war_end",
                    "name": scenario["name"],
                    "description": war_end.get("description"),
                    "priority": war_end.get("priority", "high"),
                    "status": scenario["status"],
                    "readiness_pct": round(scenario["readiness"] * 100),
                    "signals_met": scenario["signals_met"],
                    "signals_total": scenario["signals_total"],
                    "matched_signals": [
                        row for row in scenario["signal_details"] if row.get("matched")
                    ],
                    "missing_signals": [
                        row for row in scenario["signal_details"] if not row.get("matched")
                    ],
                    "playbook_actions": playbook_actions,
                }
            ],
            "geo_alerts": [],
            "evaluated_at": state["evaluated_at"],
        }
    )
    assert "1306.T ¥200,000" in context
    assert "1489.T ¥200,000" in context
    assert "priority_actions" in context

    hypotheses = synthesize_from_active_scenarios(
        state,
        analysis_id="test-analysis",
        analysis_date="2026-06-23",
    )
    by_ticker = {h.ticker: h for h in hypotheses}
    assert by_ticker["1306.T"].action_type == "buy"
    assert by_ticker["1489.T"].action_type == "buy"
    assert by_ticker["1306.T"].observe_only is False


def test_geopolitical_monitor_respects_war_end_two_keyword_threshold():
    war_end = _repo_scenario("war_end")
    matches = geopolitical_monitor._match_keywords(
        [
            {
                "headline": "Iran ceasefire holds as Strait of Hormuz tensions ease",
                "snippet": "Investors price in de-escalation after the truce.",
            }
        ],
        {"scenarios": [war_end]},
    )

    assert matches
    assert matches[0]["scenario"]["id"] == "war_end"
    assert matches[0]["score"] >= geopolitical_monitor._scenario_min_keyword_score(war_end)
    assert geopolitical_monitor._scenario_min_keyword_score(war_end) == 2


def test_geopolitical_web_search_logs_llm_usage(monkeypatch):
    rows: list[dict] = []

    class FakeBetaMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="Headline\nSnippet")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(
                    input_tokens=111,
                    output_tokens=22,
                    server_tool_use=types.SimpleNamespace(web_search_requests=2),
                ),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.beta = types.SimpleNamespace(messages=FakeBetaMessages())

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(geopolitical_monitor, "anthropic", fake_anthropic)
    monkeypatch.setattr(geopolitical_monitor, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    assert geopolitical_monitor._web_search("query") == [{"headline": "Headline", "snippet": "Snippet"}]
    assert rows, "geopolitical web search should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "geopolitical_web_search"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 111
    assert row["output_tokens"] == 22
    assert row["server_tool_use"] == {"web_search_requests": 2}


def test_geopolitical_assessment_logs_llm_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        name="assess_geopolitical",
                        input={
                            "scenario_key": "tariff_war",
                            "severity": "high",
                            "headline": "Tariff escalation",
                            "detail": "Market moving tariff escalation.",
                            "confidence": 0.82,
                        },
                    )
                ],
                stop_reason="tool_use",
                usage=types.SimpleNamespace(input_tokens=333, output_tokens=44),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(geopolitical_monitor, "anthropic", fake_anthropic)
    monkeypatch.setattr(geopolitical_monitor, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    assessment = geopolitical_monitor._assess_scenario(
        {"id": "tariff_war", "name": "Tariff War", "description": "tariff risk"},
        ["tariff", "sanctions"],
        [{"headline": "Tariff headline", "snippet": "Tariff snippet"}],
    )

    assert assessment["scenario_key"] == "tariff_war"
    assert rows, "geopolitical assessment should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "geopolitical_assessment"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["scenario_key"] == "tariff_war"
    assert row["input_tokens"] == 333
    assert row["output_tokens"] == 44


def test_japan_standalone_bull_stays_observe_only_until_promotion_ready():
    not_ready = data_gatherer.scenario_context_decision_flags(
        "japan_standalone_bull",
        {"observe_only": True, "enabled_for_decision": False},
        {},
        {},
    )
    assert not_ready["enabled_for_decision"] is False
    assert not_ready["observe_only"] is True
    assert not_ready["promotion_ready"] is False

    ready = data_gatherer.scenario_context_decision_flags(
        "japan_standalone_bull",
        {"observe_only": True, "enabled_for_decision": False},
        {},
        {"japan_standalone_bull": {"promotion_ready": True, "hit_rate": 0.62}},
    )
    assert ready["enabled_for_decision"] is True
    assert ready["observe_only"] is False
    assert ready["original_observe_only"] is True
    assert ready["promotion_ready"] is True


def test_ipo_new_listings_reach_news_scan_and_decision_context(tmp_path):
    (tmp_path / "download_tickers.py").write_text(
        "NEW_LISTINGS = [\n    'SPCX',\n    'NVRB',\n]\n",
        encoding="utf-8",
    )

    assert news_screener.load_new_listing_tickers(tmp_path) == ["NVRB", "SPCX"]
    scan_tickers = news_screener.build_news_scan_tickers(base_dir=tmp_path)
    assert "SPCX" in scan_tickers
    assert "NVRB" in scan_tickers
    assert "AAPL" in scan_tickers
    assert news_screener.build_news_scan_tickers(["AAPL"], base_dir=tmp_path) == ["AAPL"]

    ipo_context = analyst._fmt_ipo_watch_context(
        {
            "updated_at": "2026-06-23T09:00:00",
            "last_scan": {"searched_items": 25, "extracted_listings": 2, "new_candidates": 1},
            "candidates": [
                {
                    "ticker": "SPCX",
                    "company": "SpaceX",
                    "exchange": "NASDAQ",
                    "ipo_date": "TBD",
                    "confidence": 0.9,
                    "status": "universe_missing",
                    "onboarding_path": "download_tickers.py:NEW_LISTINGS",
                }
            ],
        }
    )
    assert "SPCX" in ipo_context
    assert "SpaceX" in ipo_context
    assert "自動ユニバース追加・自動発注は禁止" in ipo_context
    assert "information_lane_verdicts" in ipo_context

    synthesis = analyst._ensure_information_lane_verdicts(
        {"context_blocks": {"ipo_watch": True}, "information_lane_verdicts": []}
    )
    assert synthesis["information_lane_verdicts"][0]["lane"] == "ipo_watch"
