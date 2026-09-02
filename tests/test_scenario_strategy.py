import scenario_strategy
import analyst
from scenario_strategy import SCENARIOS
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


def test_bull_scenario_allows_leverage_but_not_shorts():
    bull = SCENARIOS["BULL"]
    assert bull["long_bias"] is True
    assert bull["leverage_allowed"] is True
    assert bull["short_allowed"] is False
    assert bull["cash_ratio_target"] == 3


def test_bull_strategy_uses_aggressive_cash_target(monkeypatch):
    monkeypatch.setattr(scenario_strategy, "_load_regime", lambda: {"spy_above": True, "nk_above": True})
    monkeypatch.setattr(scenario_strategy, "_load_guard", lambda: {})
    monkeypatch.setattr(scenario_strategy, "_load_briefing", lambda: {})
    monkeypatch.setattr(scenario_strategy, "_load_short_candidates", lambda: [])
    monkeypatch.setattr(scenario_strategy, "_load_long_term_candidates", lambda: [])
    monkeypatch.setattr(
        scenario_strategy,
        "_load_short_product_controls",
        lambda: {"US": True, "JP": True},
    )
    monkeypatch.setattr(
        scenario_strategy,
        "_tunable_value",
        lambda key, fallback: 0 if key == "target_cash_pct_aggressive" else fallback,
    )

    strategy = scenario_strategy.get_strategy()

    assert strategy["scenario"] == "BULL"
    assert strategy["cash_ratio_target"] == 0
    assert strategy["leverage_allowed"] is True
    assert strategy["short_allowed"] is False
    assert strategy["short_regime_policy"]["broad_directional_allowed"] is False
    assert strategy["short_product_enabled"] == {"US": True, "JP": True}


def test_short_product_controls_are_not_the_regime_permission(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    (tmp_path / "disclosure_shadow_config.json").write_text(
        json.dumps({"us_short_enabled": False, "jp_short_enabled": True}),
        encoding="utf-8",
    )
    (tmp_path / "feature_control_state.json").write_text(
        json.dumps({
            "features": {
                "us_short": {"enabled": True},
                "jp_short": {"enabled": False},
            }
        }),
        encoding="utf-8",
    )
    assert scenario_strategy._load_short_product_controls() == {
        "US": True,
        "JP": False,
    }


def test_short_permission_contract_corrects_product_off_conflation():
    result = analyst._apply_short_permission_contract(
        {
            "short_not_recommended": "システム全体で空売り許可がFalse",
            "crisis_strategy": "空売り許可が無いためショート不可",
            "news_impact": "空売り禁止のため見送り",
        },
        {
            "scenario": {
                "short_allowed": False,
                "short_product_enabled": {"US": True, "JP": True},
            },
            "screening": {
                "short_candidates": [
                    {"ticker": "DE", "shortable": True},
                    {"ticker": "GM", "shortable": False},
                ]
            },
        },
    )
    assert result["short_permission_contract"]["product_enabled"]["US"] is True
    assert result["short_permission_contract"]["broad_directional_regime_allowed"] is False
    assert result["short_permission_contract"]["shortable_candidates"] == 1
    assert set(result["permission_conflict_corrected"]) == {
        "short_not_recommended",
        "crisis_strategy",
        "news_impact",
    }
    assert "システム全体で空売り許可がFalse" not in result["short_not_recommended"]


def test_crash_does_not_raise_cash_after_the_drop():
    crash = SCENARIOS["CRASH"]

    assert crash["cash_ratio_target"] == 30
    assert all("40〜60%" not in row for row in crash["actions"])
    assert any("換金売り" in row for row in crash["actions"])


def test_guard_decimal_loss_ratio_triggers_crash():
    assert (
        scenario_strategy.detect_scenario(
            {"spy_above": True, "nk_above": True},
            {"daily_pnl_pct": -0.051, "monthly_pnl_pct": -0.02},
        )
        == "CRASH"
    )


def test_stale_regime_falls_back_to_fresh_screen_market_meta(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    (tmp_path / "regime_state.json").write_text(json.dumps({
        "updated": "2000-01-01 00:00",
        "spy_above": False,
        "nk_above": False,
    }), encoding="utf-8")
    (tmp_path / "screen_results.json").write_text(json.dumps({
        "timestamp": (now - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M"),
        "market_meta": {"sp500": "上", "nikkei": "上"},
    }), encoding="utf-8")

    regime = scenario_strategy._load_regime(now=now)

    assert regime["spy_above"] is True
    assert regime["nk_above"] is True
    assert regime["_source"] == "screen_results.json"


def test_future_screen_timestamp_cannot_manufacture_a_bull_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    (tmp_path / "screen_results.json").write_text(json.dumps({
        "timestamp": "2099-01-01 09:00",
        "market_meta": {"sp500": "上", "nikkei": "上"},
    }), encoding="utf-8")

    regime = scenario_strategy._load_regime(now=now)

    assert regime == {
        "spy_above": None,
        "nk_above": None,
        "_source": "unavailable",
        "_stale": True,
    }
    assert scenario_strategy.detect_scenario(regime, {}) == "NEUTRAL"


def test_missing_or_invalid_regime_timestamps_are_stale():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    assert scenario_strategy._is_regime_stale({}, now=now) is True
    assert scenario_strategy._is_regime_stale({"updated": "not-a-time"}, now=now) is True
    assert scenario_strategy._is_regime_stale(
        {"updated": "2099-01-01 09:00"}, now=now,
    ) is True


def test_screen_market_regime_requires_a_verifiable_timestamp(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    (tmp_path / "screen_results.json").write_text(json.dumps({
        "market_meta": {"sp500": "上", "nikkei": "上"},
    }), encoding="utf-8")

    assert scenario_strategy._load_screen_market_regime() is None


def test_stale_regime_is_diagnostic_only_and_cannot_drive_direction(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    (tmp_path / "regime_state.json").write_text(json.dumps({
        "updated": "2000-01-01 00:00",
        "spy_above": True,
        "nk_above": True,
    }), encoding="utf-8")

    regime = scenario_strategy._load_regime(now=now)

    assert regime["spy_above"] is None
    assert regime["nk_above"] is None
    assert regime["updated"] == "2000-01-01 00:00"
    assert regime["_source"] == "regime_state.json"
    assert regime["_stale"] is True
    assert scenario_strategy.detect_scenario(regime, {}) == "NEUTRAL"


def test_corrupt_regime_file_falls_back_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    (tmp_path / "regime_state.json").write_text("{broken", encoding="utf-8")

    regime = scenario_strategy._load_regime()

    assert regime["_source"] == "unavailable"
    assert regime["_stale"] is True
    assert scenario_strategy.detect_scenario(regime, {}) == "NEUTRAL"


def test_long_term_loader_consumes_canonical_passed_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    (tmp_path / "long_term_screen_results.json").write_text(json.dumps({
        "as_of": "2026-08-10 07:00",
        "passed": [{"ticker": "SYNTH", "score": 91}],
    }), encoding="utf-8")

    assert scenario_strategy._load_long_term_candidates(now=now) == [
        {"ticker": "SYNTH", "score": 91},
    ]


def test_candidate_loaders_reject_unverifiable_or_future_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr(scenario_strategy, "BASE_DIR", tmp_path)
    now = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Tokyo"))
    (tmp_path / "short_candidates.json").write_text(json.dumps({
        "candidates": [{"ticker": "SHORT_SYNTH"}],
    }), encoding="utf-8")
    (tmp_path / "long_term_screen_results.json").write_text(json.dumps({
        "as_of": "2099-01-01 09:00",
        "passed": [{"ticker": "LONG_SYNTH"}],
    }), encoding="utf-8")

    assert scenario_strategy._load_short_candidates(now=now) == []
    assert scenario_strategy._load_long_term_candidates(now=now) == []
