from datetime import datetime, timedelta, timezone

from api.routes.scenario import _build_data_health, _refresh_result_state, _tail_text
from geopolitical_monitor import _is_material_alert, _match_keywords
from scenario_engine import _build_recommended_actions, _eval_news


def test_eval_news_uses_keyword_fallback_when_ai_assessment_failed():
    scenario = {
        "id": "tariff_war",
        "detect": {"news_keywords": ["tariff", "trade war"]},
    }
    geo_state = {
        "active_alerts": [],
        "keyword_matches": [
            {
                "scenario_key": "tariff_war",
                "score": 3,
                "matched_keywords": ["tariff", "trade war", "sanctions"],
                "assessment_status": "failed",
            }
        ],
    }

    result = _eval_news(scenario, geo_state)

    assert result["matched"] is True
    assert result["value"] == 3
    assert "fallback" in result["detail"]
    assert "tariff" in result["detail"]


def test_eval_news_does_not_use_weak_keyword_fallback():
    scenario = {
        "id": "tariff_war",
        "detect": {"news_keywords": ["tariff", "trade war"]},
    }
    geo_state = {
        "active_alerts": [],
        "keyword_matches": [
            {
                "scenario_key": "tariff_war",
                "score": 1,
                "matched_keywords": ["tariff"],
                "assessment_status": "failed",
            }
        ],
    }

    result = _eval_news(scenario, geo_state)

    assert result["matched"] is False
    assert result["detail"] == "No matching headlines"


def test_eval_news_uses_scenario_specific_min_keyword_score_for_war_end():
    scenario = {
        "id": "war_end",
        "detect": {
            "news_keywords": ["ceasefire", "Iran de-escalation"],
            "min_keyword_score": 2,
        },
    }
    geo_state = {
        "active_alerts": [],
        "keyword_matches": [
            {
                "scenario_key": "war_end",
                "score": 2,
                "matched_keywords": ["ceasefire", "Iran de-escalation"],
                "assessment_status": "assessed",
                "severity": "high",
                "confidence": 0.8,
            }
        ],
    }

    result = _eval_news(scenario, geo_state)

    assert result["matched"] is True
    assert result["threshold"] == 2
    assert result["value"] == 2


def test_eval_news_does_not_use_medium_assessed_keyword_fallback():
    scenario = {
        "id": "tariff_war",
        "detect": {"news_keywords": ["tariff", "trade war", "sanctions"]},
    }
    geo_state = {
        "active_alerts": [],
        "keyword_matches": [
            {
                "scenario_key": "tariff_war",
                "score": 4,
                "matched_keywords": ["tariff", "trade war", "sanctions"],
                "assessment_status": "assessed",
                "severity": "medium",
                "confidence": 0.9,
            }
        ],
    }

    result = _eval_news(scenario, geo_state)

    assert result["matched"] is False
    assert result["detail"] == "No matching headlines"


def test_match_keywords_accepts_dict_scenario_playbook_shape():
    playbook = {
        "scenarios": {
            "tariff_war": {
                "name": "Tariff War",
                "detect": {"news_keywords": ["tariff", "trade war"]},
            }
        }
    }
    news_items = [
        {
            "headline": "US tariff escalation revives trade war risk",
            "snippet": "Markets react to tariff headlines.",
        }
    ]

    matches = _match_keywords(news_items, playbook)

    assert len(matches) == 1
    assert matches[0]["scenario"]["id"] == "tariff_war"
    assert matches[0]["score"] == 2


def test_match_keywords_war_end_current_iran_hormuz_fixture_reaches_custom_threshold():
    playbook = {
        "scenarios": [
            {
                "id": "war_end",
                "name": "戦争終結ラリー",
                "detect": {
                    "min_keyword_score": 2,
                    "news_keywords": [
                        "ceasefire",
                        "Iran War",
                        "Hormuz resolution",
                        "General License X",
                    ],
                },
            }
        ]
    }
    news_items = [
        {
            "headline": "Iran-Strait of Hormuz Resolution follows ceasefire framework",
            "snippet": "A General License X waiver and talks to end the Iran War lifted risk sentiment.",
        }
    ]

    matches = _match_keywords(news_items, playbook)

    assert matches[0]["scenario"]["id"] == "war_end"
    assert matches[0]["score"] >= 2


def test_geopolitical_alert_materiality_requires_high_confidence():
    assert _is_material_alert({"severity": "high", "confidence": 0.8}) is True
    assert _is_material_alert({"severity": "medium", "confidence": 0.95}) is False
    assert _is_material_alert({"severity": "high", "confidence": 0.5}) is False


def test_scenario_data_health_flags_stale_and_assessment_errors():
    now = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)
    old = (now - timedelta(hours=30)).isoformat()
    fresh = (now - timedelta(hours=1)).isoformat()

    health = _build_data_health(
        {"evaluated_at": old},
        {
            "cached_at": fresh,
            "news_items": [{"headline": "x"}],
            "active_alerts": [],
            "keyword_matches": [{"scenario_key": "tariff_war"}],
            "assessment_errors": [{"scenario_key": "tariff_war"}],
        },
        {"cached_at": fresh},
        {"cached_at": fresh},
        {"cached_at": fresh},
        now=now,
    )

    assert health["scenario"]["stale"] is True
    assert health["geopolitical"]["stale"] is False
    assert health["geopolitical"]["news_count"] == 1
    assert health["geopolitical"]["keyword_match_count"] == 1
    assert health["geopolitical"]["assessment_error_count"] == 1
    assert health["has_stale_sources"] is True
    assert health["has_collection_warnings"] is True


def test_scenario_data_health_applies_weekend_grace_to_weekday_cron_sources():
    jst = timezone(timedelta(hours=9))
    monday_morning = datetime(2026, 5, 25, 7, 30, tzinfo=jst)
    friday_evening = (monday_morning - timedelta(hours=61)).isoformat()
    fresh = (monday_morning - timedelta(hours=1)).isoformat()

    health = _build_data_health(
        {"evaluated_at": friday_evening},
        {"cached_at": friday_evening, "news_items": [{"headline": "x"}]},
        {"cached_at": friday_evening},
        {"cached_at": fresh},
        {"cached_at": fresh},
        now=monday_morning,
    )

    assert health["scenario"]["stale_after_hours"] == 72
    assert health["scenario"]["stale"] is False
    assert health["technical"]["stale_after_hours"] == 72
    assert health["technical"]["stale"] is False
    assert health["geopolitical"]["stale_after_hours"] == 72
    assert health["geopolitical"]["stale"] is False


def test_scenario_data_health_expires_weekday_cron_sources_after_monday_first_run():
    jst = timezone(timedelta(hours=9))
    monday_after_run = datetime(2026, 5, 25, 10, 0, tzinfo=jst)
    friday_evening = (monday_after_run - timedelta(hours=61)).isoformat()
    fresh = (monday_after_run - timedelta(hours=1)).isoformat()

    health = _build_data_health(
        {"evaluated_at": friday_evening},
        {"cached_at": friday_evening, "news_items": [{"headline": "x"}]},
        {"cached_at": friday_evening},
        {"cached_at": fresh},
        {"cached_at": fresh},
        now=monday_after_run,
    )

    assert health["scenario"]["stale_after_hours"] == 24
    assert health["scenario"]["stale"] is True
    assert health["technical"]["stale_after_hours"] == 24
    assert health["technical"]["stale"] is True
    assert health["geopolitical"]["stale_after_hours"] == 8
    assert health["geopolitical"]["stale"] is True


def test_refresh_result_state_marks_failures_and_no_update_warning():
    assert _refresh_result_state(1, "before", "after") == "failed"
    assert _refresh_result_state(0, "before", None) == "warning"
    assert _refresh_result_state(0, "same", "same") == "warning"
    assert _refresh_result_state(0, "before", "after") == "succeeded"


def test_tail_text_decodes_bytes_and_limits_output():
    assert _tail_text("abcdef", limit=3) == "def"
    assert _tail_text("日本語".encode("utf-8"), limit=1) == "語"


def test_recommended_actions_preserve_phase3_sell_triggers_and_confirmations():
    scenario = {
        "actions": {
            "phase_1": {
                "buy": [{"ticker": "SPY", "allocation_usd": 1000}],
                "confirmation_required": ["VIX below 20"],
            },
            "phase_2": {
                "buy": [{"ticker": "NVDA", "allocation_usd": 1000}],
                "confirmation_required": ["SOXX above MA50"],
            },
            "phase_3": {
                "buy": [{"ticker": "TQQQ", "allocation_usd": 500}],
                "confirmation_required": ["momentum acceleration"],
            },
            "sell_on_trigger": ["GLD", "TLT"],
        }
    }

    actions = _build_recommended_actions(scenario)

    assert actions["phase_1"][0]["ticker"] == "SPY"
    assert actions["phase_2"][0]["ticker"] == "NVDA"
    assert actions["phase_3"][0]["ticker"] == "TQQQ"
    assert actions["sell_on_trigger"] == ["GLD", "TLT"]
    assert actions["confirmation_required"]["phase_3"] == ["momentum acceleration"]
