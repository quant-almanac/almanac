import scenario_strategy
from scenario_strategy import SCENARIOS
import json


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
        "_tunable_value",
        lambda key, fallback: 0 if key == "target_cash_pct_aggressive" else fallback,
    )

    strategy = scenario_strategy.get_strategy()

    assert strategy["scenario"] == "BULL"
    assert strategy["cash_ratio_target"] == 0
    assert strategy["leverage_allowed"] is True


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
    (tmp_path / "regime_state.json").write_text(json.dumps({
        "updated": "2000-01-01 00:00",
        "spy_above": False,
        "nk_above": False,
    }), encoding="utf-8")
    (tmp_path / "screen_results.json").write_text(json.dumps({
        "timestamp": "2099-01-01 09:00",
        "market_meta": {"sp500": "上", "nikkei": "上"},
    }), encoding="utf-8")

    regime = scenario_strategy._load_regime()

    assert regime["spy_above"] is True
    assert regime["nk_above"] is True
    assert regime["_source"] == "screen_results.json"
