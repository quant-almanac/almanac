from datetime import datetime

import market_regime_v2 as mr


def _strong_market_meta():
    return {
        "sp500_vs_ma50_pct": 6.0,
        "sp500_vs_ma200_pct": 14.0,
        "nikkei_vs_ma50_pct": 5.0,
        "nikkei_vs_ma200_pct": 12.0,
        "vix": 14.0,
        "breadth": {
            "us": {
                "above_ma50_pct": 72.0,
                "above_ma200_pct": 68.0,
                "eligible_ma50": 100,
                "eligible_ma200": 100,
            },
            "jp": {
                "above_ma50_pct": 69.0,
                "above_ma200_pct": 65.0,
                "eligible_ma50": 100,
                "eligible_ma200": 100,
            },
        },
    }


def _supportive_macro():
    return {
        "vix": 14.0,
        "hy_oas_bps": 250.0,
        "yield_10y": 4.1,
        "yield_10y_change_5d_bps": 2.0,
        "yield_10y_change_20d_bps": -10.0,
        "real_yield_10y": 1.7,
        "real_yield_10y_change_20d_bps": -5.0,
        "breakeven_10y": 2.4,
        "yield_spread_10y_3m": 0.5,
    }


def test_strong_bull_requires_broad_confirmation_and_no_rate_veto():
    result = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro=_supportive_macro(),
        market_weights={"US": 0.7, "JP": 0.3},
    )

    assert result["markets"]["US"]["raw_label"] == "strong_bull"
    assert result["markets"]["JP"]["raw_label"] == "strong_bull"
    assert result["portfolio"]["raw_label"] == "strong_bull"
    assert result["portfolio"]["eligible"] is True


def test_tightening_shock_prevents_strong_bull():
    macro = {
        **_supportive_macro(),
        "yield_10y_change_5d_bps": 35.0,
        "yield_10y_change_20d_bps": 60.0,
        "real_yield_10y": 2.2,
        "real_yield_10y_change_20d_bps": 30.0,
    }
    result = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro=macro,
    )

    assert result["markets"]["US"]["rate_regime"]["status"] == "tightening_shock"
    assert result["markets"]["US"]["risk_veto"] is True
    assert result["markets"]["US"]["raw_level"] < 2


def test_persistently_high_real_yield_is_a_negative_modifier():
    baseline = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro=_supportive_macro(),
    )
    restrictive = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro={
            **_supportive_macro(),
            "yield_10y": 5.1,
            "real_yield_10y": 2.6,
            "breakeven_10y": 3.1,
        },
    )

    assert restrictive["markets"]["US"]["rate_regime"]["status"] == "restrictive_level"
    assert (
        restrictive["markets"]["US"]["score"]
        < baseline["markets"]["US"]["score"]
    )


def test_missing_breadth_is_review_not_false_precision():
    market_meta = _strong_market_meta()
    market_meta.pop("breadth")
    result = mr.classify_regime(
        market_meta=market_meta,
        macro=_supportive_macro(),
    )

    assert result["markets"]["US"]["breadth_complete"] is False
    assert result["markets"]["US"]["eligible"] is False
    assert result["portfolio"]["eligible"] is False


def test_tiny_breadth_sample_is_not_eligible():
    market_meta = _strong_market_meta()
    for market in ("us", "jp"):
        market_meta["breadth"][market]["eligible_ma50"] = 1
        market_meta["breadth"][market]["eligible_ma200"] = 1

    result = mr.classify_regime(
        market_meta=market_meta,
        macro=_supportive_macro(),
    )

    assert result["markets"]["US"]["breadth_complete"] is False
    assert result["markets"]["US"]["minimum_breadth_observations"] == 20


def test_missing_long_rate_contract_keeps_v2_in_review():
    macro = _supportive_macro()
    macro.pop("real_yield_10y")

    result = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro=macro,
    )

    assert result["markets"]["US"]["rate_complete"] is False
    assert result["portfolio"]["eligible"] is False


def test_normal_transition_requires_two_distinct_evaluation_dates(tmp_path):
    state = tmp_path / "market_regime_v2_state.json"
    strong = _strong_market_meta()
    macro = _supportive_macro()

    first = mr.evaluate_and_record(
        market_meta=strong,
        macro=macro,
        input_snapshot_hash="input-1",
        now=datetime(2026, 7, 28, 6),
        state_path=state,
        mode="advisory",
    )
    assert first["portfolio"]["committed_level"] == 2
    assert first["input_snapshot_hash"] == "input-1"
    assert "decision_snapshot_hash" not in first

    bear = {
        **strong,
        "sp500_vs_ma50_pct": -6.0,
        "sp500_vs_ma200_pct": -12.0,
        "nikkei_vs_ma50_pct": -6.0,
        "nikkei_vs_ma200_pct": -12.0,
        "vix": 28.0,
        "breadth": {
            "us": {
                "above_ma50_pct": 25.0,
                "above_ma200_pct": 28.0,
                "eligible_ma50": 100,
                "eligible_ma200": 100,
            },
            "jp": {
                "above_ma50_pct": 24.0,
                "above_ma200_pct": 27.0,
                "eligible_ma50": 100,
                "eligible_ma200": 100,
            },
        },
    }
    weak_macro = {**macro, "vix": 28.0, "hy_oas_bps": 480.0}
    same_day = mr.evaluate_and_record(
        market_meta=bear,
        macro=weak_macro,
        now=datetime(2026, 7, 28, 18),
        state_path=state,
        mode="advisory",
    )
    assert same_day["portfolio"]["committed_level"] == 2
    assert same_day["portfolio"]["pending_count"] == 0

    day_two = mr.evaluate_and_record(
        market_meta=bear,
        macro=weak_macro,
        now=datetime(2026, 7, 29, 6),
        state_path=state,
        mode="advisory",
    )
    assert day_two["portfolio"]["committed_level"] == 2
    assert day_two["portfolio"]["pending_count"] == 1

    day_three = mr.evaluate_and_record(
        market_meta=bear,
        macro=weak_macro,
        now=datetime(2026, 7, 30, 6),
        state_path=state,
        mode="advisory",
    )
    assert day_three["portfolio"]["committed_level"] < 0
    assert day_three["portfolio"]["pending_count"] == 0


def test_shock_is_immediate_and_never_raises_cash_after_crash(tmp_path):
    result = mr.evaluate_and_record(
        market_meta={**_strong_market_meta(), "vix": 43.0},
        macro={**_supportive_macro(), "vix": 43.0},
        now=datetime(2026, 7, 28, 6),
        state_path=tmp_path / "state.json",
        mode="advisory",
    )

    assert result["shock"]["active"] is True
    assert result["policy"]["legacy_scenario_key"] == "CRASH"
    assert result["policy"]["cash_target_pct"] == 30.0
    assert result["policy"]["raise_cash_to_target"] is False
    assert result["policy"]["cash_action"] == "hold_or_deploy_existing_cash"


def test_guard_pnl_shock_uses_decimal_ratio_units():
    result = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro=_supportive_macro(),
        guard={"daily_pnl_pct": -0.051, "monthly_pnl_pct": -0.02},
    )

    assert result["shock"]["active"] is True
    assert "portfolio_daily_pnl_lte_minus_5pct" in result["shock"]["reasons"]
    assert result["shock"]["inputs"]["pnl_unit"] == "decimal_ratio"


def test_policy_uses_market_specific_buy_multipliers():
    result = mr.classify_regime(
        market_meta={
            **_strong_market_meta(),
            "nikkei_vs_ma50_pct": -5.0,
            "nikkei_vs_ma200_pct": -10.0,
            "breadth": {
                **_strong_market_meta()["breadth"],
                "jp": {
                    "above_ma50_pct": 28.0,
                    "above_ma200_pct": 30.0,
                    "eligible_ma50": 100,
                    "eligible_ma200": 100,
                },
            },
        },
        macro=_supportive_macro(),
        market_weights={"US": 0.8, "JP": 0.2},
    )
    for market in ("US", "JP"):
        result["markets"][market]["committed_level"] = result["markets"][market]["raw_level"]
    result["portfolio"]["committed_level"] = result["portfolio"]["raw_level"]

    policy = mr.policy_for_assessment(result)

    assert policy["market_buy_size_multipliers"]["US"] > policy["market_buy_size_multipliers"]["JP"]


def test_shadow_or_incomplete_assessment_does_not_replace_legacy_scenario():
    assessment = mr.classify_regime(
        market_meta={"sp500_vs_ma50_pct": 3.0, "nikkei_vs_ma50_pct": 3.0},
        macro=_supportive_macro(),
    )
    assessment["mode"] = "advisory"
    assessment["policy"] = mr.policy_for_assessment(assessment)
    legacy = {"scenario": "NEUTRAL", "key": "NEUTRAL", "cash_ratio_target": 5}

    result = mr.apply_policy_to_legacy_scenario(legacy, assessment)

    assert result["market_regime_v2_applied"] is False
    assert result["cash_ratio_target"] == 5


def test_advisory_shock_replaces_raise_cash_instruction():
    assessment = mr.classify_regime(
        market_meta={**_strong_market_meta(), "vix": 43.0},
        macro={**_supportive_macro(), "vix": 43.0},
    )
    for market in ("US", "JP"):
        assessment["markets"][market]["committed_level"] = assessment["markets"][market]["raw_level"]
    assessment["portfolio"]["committed_level"] = assessment["portfolio"]["raw_level"]
    assessment["mode"] = "advisory"
    assessment["policy"] = mr.policy_for_assessment(assessment)

    result = mr.apply_policy_to_legacy_scenario(
        {
            "scenario": "CRASH",
            "key": "CRASH",
            "cash_ratio_target": 50,
            "actions": ["現金比率を40〜60%まで引き上げ"],
        },
        assessment,
    )

    assert result["market_regime_v2_applied"] is True
    assert result["cash_ratio_target"] == 30.0
    assert result["raise_cash_to_target"] is False
    assert all("40〜60%" not in row for row in result["actions"])


def test_non_shock_actions_are_rebuilt_for_the_committed_level():
    assessment = mr.classify_regime(
        market_meta=_strong_market_meta(),
        macro=_supportive_macro(),
    )
    for market in ("US", "JP"):
        assessment["markets"][market]["committed_level"] = 1
    assessment["portfolio"]["committed_level"] = 1
    assessment["mode"] = "advisory"
    assessment["policy"] = mr.policy_for_assessment(assessment)

    result = mr.apply_policy_to_legacy_scenario(
        {"scenario": "BEAR", "key": "BEAR", "actions": ["現金を30%へ"]},
        assessment,
    )

    assert result["name"] == "弱い強気相場"
    assert result["cash_ratio_target"] == 7.0
    assert result["actions"] == mr.LEVEL_ACTIONS[1]
