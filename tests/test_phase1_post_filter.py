import analyst
import kabu_mini_eligibility
import tunable_params
from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pytest


def _silence_external_filters(monkeypatch):
    import execution_readiness

    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [])
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_recent_order_intents_by_direction", lambda days=7: {}, raising=False)
    monkeypatch.setattr(analyst, "_order_state_conflicts_by_direction", lambda days=7: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_recent_executions", lambda days=14, now=None: [], raising=False)
    monkeypatch.setattr(analyst, "_open_action_state_by_direction", lambda: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr("behavioral_guard.is_rebalance_in_cooldown", lambda vix=None: (False, ""))
    monkeypatch.setattr(
        execution_readiness,
        "apply_execution_readiness",
        lambda actions, **kwargs: [a.update({"execution_readiness": "ready", "execution_block_reasons": []}) for a in actions] and actions,
    )


def _tp_get(key, default=None):
    if key == "disable_cumulative_recommendations":
        return True
    if key == "disable_stop_loss_recommendations":
        return False
    return default


def _tp_get_enforce_plan(key, default=None):
    """execution plan gate を enforce に固定した tunable stub (既定は observe)。"""
    if key == "execution_plan_gate_mode":
        return "enforce"
    if key == "execution_plan_enforce_require_readiness":
        # Classifier tests explicitly exercise enforce behavior independent of
        # the production observation-history promotion guard.
        return False
    return _tp_get(key, default)


def test_execution_plan_enforce_downgrades_until_readiness(monkeypatch):
    import execution_plan_observer

    def _get(key, default=None):
        return {
            "execution_plan_gate_mode": "enforce",
            "execution_plan_enforce_require_readiness": True,
        }.get(key, default)

    monkeypatch.setattr(tunable_params, "get", _get)
    monkeypatch.setattr(execution_plan_observer, "load_observations", lambda: [])

    mode, warning = analyst._execution_plan_gate_mode()

    assert mode == "observe"
    assert warning.startswith("execution_plan_enforce_not_ready")


def test_execution_plan_enforce_activates_after_readiness(monkeypatch):
    import execution_plan_observer

    def _get(key, default=None):
        return {
            "execution_plan_gate_mode": "enforce",
            "execution_plan_enforce_require_readiness": True,
        }.get(key, default)

    monkeypatch.setattr(tunable_params, "get", _get)
    monkeypatch.setattr(
        execution_plan_observer,
        "load_observations",
        lambda: [{
            "mode": "observe",
            "trading_date": "2026-07-10",
            "classification_count": 20,
            "classification_error_count": 0,
            "metadata_mismatch_count": 0,
            "monthly_attribution": {
                "available": True,
                "unattributed_count": 0,
                "unattributed_notional_jpy": 0,
            },
        }],
    )

    mode, warning = analyst._execution_plan_gate_mode()

    assert mode == "enforce"
    assert warning is None


def test_agent_reliability_prompt_context_uses_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    (tmp_path / "agent_reliability.json").write_text(json.dumps({
        "as_of": "2026-07-02T00:00:00+00:00",
        "horizon_days": 10,
        "agents": {
            "scenario_monitor": {
                "generator/support": {
                    "n": 16,
                    "win_rate": 0.625,
                    "mean_excess_return_bps": 45,
                    "payoff_ratio": 1.3,
                    "weight": 1.2,
                    "measured_n": 16,
                    "measured": True,
                }
            },
            "news_topic": {
                "context/support": {
                    "n": 40,
                    "win_rate": None,
                    "mean_excess_return_bps": None,
                    "payoff_ratio": None,
                    "weight": None,
                    "measured": False,
                }
            },
        },
    }), encoding="utf-8")

    block = analyst._format_agent_reliability_for_prompt()

    assert "AGENT_RELIABILITY" in block
    assert "scenario_monitor/generator/support" in block
    assert "measured_n=16" in block
    assert "news_topic" not in block
    assert "urgency/size" in block


def test_tunable_currency_targets_are_advisory(monkeypatch):
    def fake_get(key, default=None):
        values = {
            "currency_usd_target_pct": 65,
            "currency_jpy_target_pct": 35,
            "disable_stop_loss_recommendations": False,
            "disable_cumulative_recommendations": False,
        }
        return values.get(key, default)

    monkeypatch.setattr(tunable_params, "get", fake_get)

    block = analyst._fmt_tunable_limits_context()

    # 2026-07: static 目標は advisory な baseline で、外貨比率は AI が判断する。
    # ラベルは「現行 static」だが、AI 判断・±許容・currency_target_recommendation 出力を促す。
    assert "通貨配分目標（現行 static）" in block
    assert "あなたが判断" in block
    assert "currency_target_recommendation" in block
    # rebalance 適用母数が long_tier 限定であることを明示する (母数ズレ防止)。
    assert "long tier" in block


def test_information_lane_verdicts_are_made_explicit_when_missing():
    synthesis = {
        "context_blocks": {"news_topic": True, "social_topic": False, "geopolitical": True, "alpha_modules": 2},
        "information_lane_verdicts": [
            {"lane": "news_topic", "verdict": "reject", "verdict_reason": "edge不足"}
        ],
    }

    result = analyst._ensure_information_lane_verdicts(synthesis)

    verdicts = result["information_lane_verdicts"]
    assert [v["lane"] for v in verdicts] == ["news_topic", "geopolitical"]
    assert verdicts[0]["verdict"] == "reject"
    assert verdicts[1]["verdict"] == "ignore"
    assert verdicts[1]["verdict_reason"].startswith("missing_verdict")


def test_jp_no_buy_rationale_is_recorded_when_no_jp_buy():
    synthesis = {
        "priority_actions": [{"ticker": "META", "type": "buy"}],
        "post_filter": {"filtered_count": 1, "summary": {"too_small": 1}},
    }
    data = {
        "scenario_monitoring": {"observe_only_scenarios": [{"id": "japan_standalone_bull"}]},
        "screening": {"jp_screen_candidates": [{"ticker": "1306.T"}], "margin_long_candidates": []},
    }

    result = analyst._augment_no_jp_buy_rationale(synthesis, data)

    assert "jp_no_buy_rationale" in result
    assert any("observe_only_scenarios=1" in item for item in result["jp_no_buy_rationale"])
    assert any("jp_screening_candidates=1" in item for item in result["jp_no_buy_rationale"])
    assert any("jp_screening_tickers=1306.T" in item for item in result["jp_no_buy_rationale"])


def test_jp_no_buy_rationale_clears_when_jp_buy_exists():
    synthesis = {
        "priority_actions": [{"ticker": "1306.T", "type": "buy"}],
        "jp_no_buy_rationale": ["old"],
    }

    result = analyst._augment_no_jp_buy_rationale(synthesis, {})

    assert "jp_no_buy_rationale" not in result


def test_margin_and_short_no_action_rationales_record_screening_and_filter_context():
    synthesis = {
        "priority_actions": [{"ticker": "V", "type": "buy"}],
        "_filtered_actions": [{
            "ticker": "MA",
            "type": "margin_buy",
            "filtered_reason": "too_small: 推定 ¥8.3万 < 最小 ¥9万",
        }],
        "post_filter": {"filtered_count": 1, "summary": {"too_small": 1}},
        "short_opportunities": [],
        "short_not_recommended": "BULLレジームで空売り許可=False",
    }
    data = {
        "screening": {
            "margin_long_candidates": [{"ticker": "MA"}, {"ticker": "UNH"}],
            "short_candidates": [],
            "short_candidates_meta": {"scanned": 76, "shortable_count": 0},
        },
    }

    result = analyst._augment_no_margin_short_rationale(synthesis, data)

    assert any("margin_long_candidates=2" in item for item in result["margin_no_buy_rationale"])
    assert any("margin_candidate_tickers=MA,UNH" in item for item in result["margin_no_buy_rationale"])
    assert any("post_filter_margin_rejected=MA:too_small" in item for item in result["margin_no_buy_rationale"])
    assert any("short_candidates=0" in item for item in result["short_no_action_rationale"])
    assert any("shortable_count=0" in item for item in result["short_no_action_rationale"])
    assert any("short_not_recommended=BULL" in item for item in result["short_no_action_rationale"])


def test_margin_no_buy_rationale_records_deepseek_candidates_not_adopted():
    synthesis = {
        "priority_actions": [{"ticker": "META", "type": "buy"}],
        "_filtered_actions": [],
    }
    data = {
        "screening": {
            "margin_long_candidates": [{"ticker": "TSM"}, {"ticker": "META"}],
            "short_candidates": [],
        },
    }
    margin_long_analysis = {
        "_source": "deepseek:deepseek-v4-pro",
        "priority_actions": [
            {
                "ticker": "TSM",
                "type": "margin_buy",
                "urgency": "medium",
                "confidence_pct": 65,
            }
        ],
        "margin_long_picks": [{"ticker": "META", "confidence_pct": 45}],
        "margin_actions": [
            {
                "action": "証拠金状況は問題なし。現状維持",
                "reason": "維持率は無限大、現金十分のため信用不要。金利コストに見合わない",
            }
        ],
    }

    result = analyst._augment_no_margin_short_rationale(
        synthesis,
        data,
        margin_long_analysis=margin_long_analysis,
    )

    reasons = result["margin_no_buy_rationale"]
    assert any("margin_tier_source=deepseek:deepseek-v4-pro" in item for item in reasons)
    assert any("margin_tier_candidates=TSM:medium:65" in item for item in reasons)
    assert any("margin_tier_not_used_as_margin=TSM" in item for item in reasons)
    assert any("margin_candidate_adopted_as_cash_buy=META" in item for item in reasons)
    assert any("margin_tier_no_margin_reason=証拠金状況は問題なし。現状維持" in item for item in reasons)


def test_jp_no_buy_rationale_records_top_jp_candidate_context():
    synthesis = {
        "priority_actions": [{"ticker": "META", "type": "buy"}],
        "raw_priority_actions": [{"ticker": "META", "type": "buy"}],
    }
    data = {
        "screening": {
            "jp_screen_candidates": [
                {"ticker": "7012.T", "score": 42, "ai_signal": "WATCH"},
                {"ticker": "9502.T", "score": 55, "ai_signal": "BUY"},
            ],
            "margin_long_candidates": [
                {"ticker": "6367.T", "score": 88},
                {"ticker": "META", "score": 100},
            ],
        },
    }

    result = analyst._augment_no_jp_buy_rationale(synthesis, data)

    reasons = result["jp_no_buy_rationale"]
    assert any("top_jp_screen_candidate=9502.T:BUY:55" in item for item in reasons)
    assert any("top_jp_margin_candidate=6367.T:88" in item for item in reasons)
    assert any("final_non_jp_buy_tickers=META" in item for item in reasons)
    assert any("jp_candidates_not_emitted_by_synthesis=9502.T,6367.T" in item for item in reasons)


def test_jp_no_buy_rationale_records_jp_post_filter_rejections():
    synthesis = {
        "priority_actions": [{"ticker": "META", "type": "buy"}],
        "raw_priority_actions": [
            {"ticker": "9502.T", "type": "buy"},
            {"ticker": "META", "type": "buy"},
        ],
        "_filtered_actions": [
            {
                "ticker": "9502.T",
                "type": "buy",
                "filtered_reason": "too_small: 推定 ¥4万 < 最小 ¥9万",
            }
        ],
    }
    data = {
        "screening": {
            "jp_screen_candidates": [{"ticker": "9502.T", "score": 55, "ai_signal": "BUY"}],
        },
    }

    result = analyst._augment_no_jp_buy_rationale(synthesis, data)

    reasons = result["jp_no_buy_rationale"]
    assert any("post_filter_jp_rejected=9502.T:too_small" in item for item in reasons)
    assert not any("jp_candidates_not_emitted_by_synthesis=9502.T" in item for item in reasons)


def test_margin_and_short_no_action_rationales_clear_when_actions_exist():
    synthesis = {
        "priority_actions": [
            {"ticker": "MA", "type": "margin_buy"},
            {"ticker": "TSLA", "type": "short"},
        ],
        "margin_no_buy_rationale": ["old"],
        "short_no_action_rationale": ["old"],
    }

    result = analyst._augment_no_margin_short_rationale(synthesis, {})

    assert "margin_no_buy_rationale" not in result
    assert "short_no_action_rationale" not in result


def test_jp_disclosure_observe_only_boundary_is_recorded_when_no_jp_buy():
    synthesis = {
        "priority_actions": [{"ticker": "V", "type": "buy"}],
        "disclosure_brief": {
            "observe_only": True,
            "items": [
                {
                    "ticker": "4547.T",
                    "market": "JP",
                    "directional_score": -0.9,
                    "directional_confidence": 0.9,
                    "observe_only": True,
                },
                {
                    "ticker": "8154.T",
                    "market": "JP",
                    "dilution_flag": True,
                    "observe_only": True,
                },
                {
                    "ticker": "8698.T",
                    "market": "JP",
                    "directional_score": 0.2,
                    "directional_confidence": 0.6,
                    "observe_only": True,
                },
            ],
        },
        "information_lane_verdicts": [],
    }

    result = analyst._annotate_jp_disclosure_observe_only_boundary(synthesis)

    assert any(
        "jp_disclosure_observe_only=2" in reason
        and "tickers=4547.T,8154.T" in reason
        for reason in result["jp_no_buy_rationale"]
    )
    verdicts = result["information_lane_verdicts"]
    disclosure_verdicts = [
        verdict for verdict in verdicts
        if verdict.get("lane") == "disclosure"
    ]
    assert [verdict["ticker"] for verdict in disclosure_verdicts] == ["4547.T", "8154.T"]
    assert all(verdict["verdict"] == "ignore" for verdict in disclosure_verdicts)
    assert all("observe_only" in verdict["verdict_reason"] for verdict in disclosure_verdicts)


def test_jp_disclosure_observe_only_boundary_does_not_create_no_buy_when_jp_buy_exists():
    synthesis = {
        "priority_actions": [{"ticker": "1306.T", "type": "buy"}],
        "disclosure_brief": {
            "observe_only": True,
            "items": [{
                "ticker": "4547.T",
                "market": "JP",
                "directional_score": -0.9,
                "directional_confidence": 0.9,
                "observe_only": True,
            }],
        },
        "information_lane_verdicts": [],
    }

    result = analyst._annotate_jp_disclosure_observe_only_boundary(synthesis)

    assert "jp_no_buy_rationale" not in result
    assert result["information_lane_verdicts"][0]["ticker"] == "4547.T"


def test_restricted_employer_trim_uses_b2_planner_not_priority_actions(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "9999.T",
            "type": "trim",
            "action": "9999.T 100株をトリム（持株会経由）",
            "reason": "月次積立は給与天引きで継続される。",
        }],
    }
    positions = [{"ticker": "9999.T", "current_price": 2500, "currency": "JPY", "value_jpy": 2_500_000}]

    result = analyst._phase1_post_filter(synthesis, 10_000_000, positions=positions)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("insider_restricted:")


def test_cumulative_filter_still_drops_buy_side_dca_text(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "SLIM_ORCAN",
            "type": "buy",
            "amount_jpy": 200_000,
            "action": "SLIM_ORCAN 月次積立を増額",
            "reason": "つみたて枠の消化を加速する。",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 10_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("disable_cumulative_recommendations")


def test_cumulative_filter_does_not_drop_growth_lump_sum_reason_mentions_tsumitate(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "SLIM_SP500",
            "type": "buy",
            "amount_hint": "¥1,000,000一括",
            "action": "NISA成長枠でeMAXIS Slim S&P500を一括買付",
            "reason": "成長枠¥240万・つみたて枠¥120万共に未消化。",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["_filtered_actions"] == []


def test_cumulative_filter_drops_tsumitate_frame_lump_sum_action(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "SLIM_SP500",
            "type": "buy",
            "amount_hint": "¥500,000相当",
            "action": "妻NISAつみたて枠¥120万未消化分から¥50万一括スポット買い",
            "reason": "年内消化のための単発買付。",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("disable_cumulative_recommendations")


def test_cumulative_filter_drops_tsumitate_frame_spot_even_with_negative_recurring_text(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "priority_actions": [{
            "ticker": "SLIM_ORCAN",
            "type": "buy",
            "amount_hint": "¥600,000相当",
            "action": "妻NISAつみたて枠で eMAXIS Slim 全世界株式を ¥600,000 相当スポット買付（一時買付・定期積立ではない）",
            "reason": "年内消化のための単発買付。",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("disable_cumulative_recommendations")


def _stance_guard_data(**overrides):
    data = {
        "market_meta": {"vix": 16.0},
        "cash_info": {"total_cash_jpy": 1_000_000},
        "portfolio_total": 10_000_000,
        "risk": {"actual_current_dd": -2.0, "actual_dd_stage": "ok"},
    }
    data.update(overrides)
    return data


def test_stance_guard_promotes_only_when_inputs_complete():
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "leverage_health": {"status": "ok"},
        "telegram_message": "📊 統合戦略 2026-07-10 (moderately_aggressive)\nbody",
        "stance_reason": "moderately_aggressive維持",
    }
    result = analyst._apply_stance_guard(synthesis, _stance_guard_data(), True)
    assert result["overall_stance"] == "aggressive"
    assert result["stance_guard_applied"] is True
    assert result["telegram_message"].splitlines()[0].endswith("(aggressive)")
    assert "stance_guard:" in result["stance_reason"]
    assert result["stance_guard_display_synced"] is True


def test_stance_guard_does_not_promote_when_cash_missing():
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "leverage_health": {"status": "ok"},
    }
    result = analyst._apply_stance_guard(synthesis, _stance_guard_data(cash_info={}), True)
    assert result["overall_stance"] == "moderately_aggressive"
    assert "stance_guard_applied" not in result


def test_stance_guard_does_not_promote_on_actual_dd_block():
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "leverage_health": {"status": "ok"},
    }
    data = _stance_guard_data(risk={"actual_current_dd": -8.1, "actual_dd_stage": "block"})
    result = analyst._apply_stance_guard(synthesis, data, True)
    assert result["overall_stance"] == "moderately_aggressive"
    assert "stance_guard_applied" not in result


def test_stance_guard_does_not_promote_on_emergency_leverage():
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "leverage_health": {"status": "emergency"},
    }
    result = analyst._apply_stance_guard(synthesis, _stance_guard_data(), True)
    assert result["overall_stance"] == "moderately_aggressive"
    assert "stance_guard_applied" not in result


def test_stance_guard_downgrades_unqualified_aggressive():
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {"status": "ok"},
        "telegram_message": "📊 統合戦略 2026-07-24 (aggressive)\nbody",
        "stance_reason": "LLM strong view",
    }

    result = analyst._apply_stance_guard(synthesis, _stance_guard_data(), False)

    assert result["overall_stance"] == "moderately_aggressive"
    assert result["stance_guard_detail"]["downgraded_to"] == "moderately_aggressive"
    assert result["telegram_message"].splitlines()[0].endswith("(moderately_aggressive)")
    assert "aggressive必須条件が未達" in result["stance_reason"]


def test_stance_guard_forces_defensive_on_hard_override():
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {"status": "emergency"},
    }

    result = analyst._apply_stance_guard(synthesis, _stance_guard_data(), True)

    assert result["overall_stance"] == "defensive"
    assert result["stance_guard_detail"]["downgraded_to"] == "defensive"


def test_structured_equity_notional_wins_over_mistyped_prose():
    action = {
        "ticker": "1489.T",
        "type": "add",
        "amount_hint": "20口（約¥68万相当）",
        "limit_price": 3382,
    }

    estimated = analyst._estimate_action_jpy(action, {}, 163.16)
    normalized = analyst._normalize_amount_hint_notional(action, estimated)

    assert estimated == 67_640
    assert normalized["amount_hint"] == "20口（約¥67,640相当）"
    assert normalized["notional_claim_original_jpy"] == 680_000


def test_us_equity_k_suffix_does_not_override_quantity_price():
    action = {
        "ticker": "RTX",
        "type": "trim",
        "amount_hint": "1株（約¥34K相当）",
        "limit_price": 211,
        "currency": "USD",
    }

    estimated = analyst._estimate_action_jpy(action, {}, 163.16)

    assert estimated == pytest.approx(34_426.76)


def test_non_executable_zero_share_duplicate_order_is_filtered(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "priority_actions": [{
            "ticker": "QCOM",
            "type": "trim",
            "confidence_pct": 0,
            "amount_hint": "0株（除外）",
            "action": "QCOM 残1株売却（既に1株売却注文中なので、注文約定後の残1株のさらなる利確指示は出さない → 本アクションは除外）",
            "reason": "重複注文を避けるため。",
        }],
    }
    positions = [{"ticker": "QCOM", "current_price": 140, "currency": "USD", "shares": 1, "value_jpy": 22_000}]

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=positions)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("non_executable_action")


def test_exit_action_full_sell_wording_is_corrected_when_holdings_are_larger(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "telegram_message": "📊 stance=neutral",
        "priority_actions": [{
            "ticker": "6762.T",
            "type": "trim",
            "amount_hint": "100株",
            "action": "6762.T (TDK) 100株全数売却（一部利確：100株保有→0株）",
            "reason": "日本株は100株単元で細切れ不可。分割せず一括処分。",
            "urgency": "high",
        }],
    }
    positions = [{"ticker": "6762.T", "current_price": 3715, "currency": "JPY", "shares": 200, "value_jpy": 743_000}]

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=positions)

    action = result["priority_actions"][0]
    assert "全数" not in action["action"]
    assert "200株保有→100株" in action["action"]
    assert action["position_size_corrected"] is True
    assert "実保有200株" in action["execution_note"]


def test_exit_action_resolves_duplicate_ticker_by_tier_and_account(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "XLF",
            "tier": "Medium",
            "type": "trim",
            "amount_hint": "20株",
            "action": "XLF 20株全数売却（60株保有→0株）",
            "reason": "中期枠の一括処分。",
            "urgency": "high",
        }],
    }
    positions = [
        {
            "key": "XLF", "ticker": "XLF", "current_price": 56, "currency": "USD",
            "shares": 80, "value_jpy": 672_000, "investment_type": "medium", "account": "特定",
        },
        {
            "key": "XLF_NISA", "ticker": "XLF", "current_price": 56, "currency": "USD",
            "shares": 60, "value_jpy": 504_000, "investment_type": "long", "account": "NISA成長投資枠",
        },
    ]

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=positions)

    action = result["priority_actions"][0]
    assert action["execution_account"] == "特定"
    assert action["execution_investment_type"] == "medium"
    assert action["execution_position_keys"] == ["XLF"]
    assert action["holding_shares_before"] == 80
    assert action["holding_shares_after"] == 60
    assert "80株保有→60株" in action["action"]
    assert "実保有80株" in action["execution_note"]


def test_exit_action_uses_exact_execution_account_and_marks_oversell() -> None:
    action = {
        "ticker": "AVGO",
        "tier": "Long",
        "type": "trim",
        "amount_hint": "8株",
        "execution_account": "特定",
        "action": "AVGOを特定口座から8株売却",
    }
    holdings = {
        "AVGO": {
            "ticker": "AVGO",
            "shares": 32,
            "lots": [
                {
                    "key": "AVGO_toku", "ticker": "AVGO", "shares": 5,
                    "account": "特定", "investment_type": "long",
                },
                {
                    "key": "AVGO_ippan", "ticker": "AVGO", "shares": 27,
                    "account": "一般", "investment_type": "long",
                },
            ],
        },
    }

    result = analyst._normalize_exit_action_against_holdings(action, holdings)

    assert result["execution_account"] == "特定"
    assert result["execution_position_keys"] == ["AVGO_toku"]
    assert result["holding_shares_before"] == 5
    assert result["requested_sell_quantity"] == 8
    assert result["holding_quantity_exceeds_account"] is True
    assert result["holding_quantity_shortfall"] == 3


def test_exit_action_without_account_is_ambiguous_across_taxable_accounts() -> None:
    action = {
        "ticker": "AVGO", "tier": "Long", "type": "trim", "amount_hint": "8株",
        "execution_position_keys": ["AVGO_toku", "AVGO_ippan"],
    }
    holdings = {
        "AVGO": {
            "ticker": "AVGO",
            "shares": 32,
            "lots": [
                {
                    "key": "AVGO_toku", "ticker": "AVGO", "shares": 5,
                    "account": "特定", "investment_type": "long",
                },
                {
                    "key": "AVGO_ippan", "ticker": "AVGO", "shares": 27,
                    "account": "一般", "investment_type": "long",
                },
            ],
        },
    }

    bound, _ = analyst._bind_action_to_holding(action, holdings)

    assert bound["holding_scope_ambiguous"] is True
    assert "execution_position_keys" not in bound
    assert bound["execution_position_binding"] == "withheld_ambiguous_holding_scope"


def test_specific_taxable_account_does_not_match_other_taxable_category() -> None:
    assert analyst._account_matches("特定", "特定") is True
    assert analyst._account_matches("特定", "一般") is False
    assert analyst._account_matches("一般", "特定") is False
    assert analyst._account_matches("taxable", "特定") is True
    assert analyst._account_matches("taxable", "一般") is True


def test_generic_nisa_buy_does_not_bind_position_key_without_route() -> None:
    action = {
        "ticker": "XLF",
        "type": "buy",
        "execution_account": "NISA成長投資枠",
        "tier": "Long",
    }
    holdings = {
        "XLF": {
            "shares": 60,
            "current_price": 56,
            "currency": "USD",
            "lots": [{
                "key": "XLF_NISA",
                "shares": 60,
                "current_price": 56,
                "currency": "USD",
                "account": "NISA成長投資枠",
                "investment_type": "long",
                "owner": "husband",
                "broker": "rakuten",
            }],
        },
    }

    bound, _ = analyst._bind_action_to_holding(action, holdings)

    assert "execution_position_keys" not in bound
    assert "execution_owner" not in bound
    assert "execution_broker" not in bound
    assert bound["execution_position_binding"] == "withheld_unresolved_nisa_route"


def test_notional_equation_is_recomputed_from_limit_price() -> None:
    action = {
        "ticker": "1306.T",
        "type": "buy",
        "amount_hint": "100口",
        "limit_price": 423.0,
        "reason": "日本株UW。¥42,560×100=¥425,600で最低ロット充足。",
    }

    result = analyst._normalize_notional_equation(action)

    assert "¥423×100口=¥42,300" in result["reason"]
    assert result["notional_claim_corrected"] is True


def test_jpx_action_units_use_etf_metadata_not_blanket_100_shares() -> None:
    synthesis = {"priority_actions": [
        {"ticker": "1489.T", "amount_hint": "17口"},
        {"ticker": "1306.T", "amount_hint": "17株"},
        {"ticker": "9999.T", "amount_hint": "20株"},
    ]}

    analyst._normalize_jpx_action_units(synthesis)
    high_dividend, topix, ordinary = synthesis["priority_actions"]

    assert high_dividend["amount_hint"] == "17口"
    assert high_dividend["execution_trading_unit"] == 1
    assert topix["amount_hint"] == "20口"
    assert topix["execution_trading_unit"] == 10
    assert ordinary["amount_hint"] == "100株"
    assert ordinary["execution_trading_unit"] == 100


def test_policy_final_jpx_quantity_is_not_resized_but_all_unit_text_is_normalized() -> None:
    synthesis = {"priority_actions": [{
        "ticker": "1306.JPX",
        "amount_hint": "100株",
        "action": "1306を100株買付",
        "policy_size_final": True,
    }, {
        "ticker": "285A",
        "amount_hint": "100株",
        "action": "285Aを100株買付",
    }]}

    analyst._normalize_jpx_action_units(synthesis)
    policy_final, alphanumeric = synthesis["priority_actions"]
    assert policy_final["ticker"] == "1306.T"
    assert policy_final["amount_hint"] == "100口"
    assert "100口" in policy_final["action"]
    assert alphanumeric["ticker"] == "285A.T"
    assert alphanumeric["amount_hint"] == "100株"


def test_unknown_bare_alphanumeric_jpx_code_is_not_sized_as_one_share() -> None:
    synthesis = {"priority_actions": [{
        "ticker": "999A",
        "amount_hint": "1株",
        "action": "999Aを1株買付",
    }]}

    with pytest.raises(ValueError, match="JPXコードか判定できません"):
        analyst._normalize_jpx_action_units(synthesis)


def test_same_analysis_opposite_actions_in_same_scope_are_stopped(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "XLF", "tier": "Medium", "type": "add", "amount_jpy": 200_000,
                "amount_hint": "20株", "action": "20株を追加", "reason": "buy", "urgency": "high",
            },
            {
                "ticker": "XLF", "tier": "Medium", "type": "trim", "amount_jpy": 200_000,
                "amount_hint": "20株", "action": "20株を売却", "reason": "sell", "urgency": "high",
            },
        ],
    }
    positions = [{
        "key": "XLF", "ticker": "XLF", "current_price": 56, "currency": "USD",
        "shares": 80, "value_jpy": 672_000, "investment_type": "medium", "account": "特定",
    }]

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=positions)

    assert result["priority_actions"] == []
    conflicts = [
        row for row in result["_filtered_actions"]
        if str(row.get("filtered_reason") or "").startswith("same_analysis_opposite_conflict:")
    ]
    assert len(conflicts) == 2


def test_same_ticker_opposite_actions_are_allowed_only_for_distinct_scopes(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "XLF", "tier": "Long", "type": "add", "amount_jpy": 200_000,
                "amount_hint": "20株", "action": "20株を追加", "reason": "long buy", "urgency": "high",
            },
            {
                "ticker": "XLF", "tier": "Medium", "type": "trim", "amount_jpy": 200_000,
                "amount_hint": "20株", "action": "20株を売却", "reason": "medium sell", "urgency": "high",
            },
        ],
    }
    positions = [
        {
            "key": "XLF", "ticker": "XLF", "current_price": 56, "currency": "USD",
            "shares": 80, "value_jpy": 672_000, "investment_type": "medium", "account": "特定",
        },
        {
            "key": "XLF_NISA", "ticker": "XLF", "current_price": 56, "currency": "USD",
            "shares": 60, "value_jpy": 504_000, "investment_type": "long", "account": "NISA成長投資枠",
        },
    ]

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=positions)

    assert len(result["priority_actions"]) == 2
    by_type = {row["type"]: row for row in result["priority_actions"]}
    assert by_type["add"]["execution_account"] == "NISA成長投資枠"
    assert by_type["trim"]["execution_account"] == "特定"
    assert all(row["cross_scope_opposite_action"] is True for row in result["priority_actions"])
    assert all(row["opposite_intent_conflict"] is True for row in result["priority_actions"])


def test_raw_opposite_intent_marker_survives_when_one_side_is_filtered(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "XLF", "type": "buy", "amount_jpy": 200_000,
                "action": "XLFを買付", "reason": "buy", "urgency": "high",
            },
            {
                "ticker": "XLF", "type": "sell", "amount_jpy": 10_000,
                "action": "XLFを少額売却", "reason": "sell", "urgency": "low",
            },
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=[])

    assert len(result["priority_actions"]) == 1
    assert result["priority_actions"][0]["type"] == "buy"
    assert result["priority_actions"][0]["opposite_intent_conflict"] is True
    assert any(
        str(row.get("filtered_reason") or "").startswith("too_small:")
        for row in result["_filtered_actions"]
    )


def test_unsynced_live_opposite_order_in_execution_log_blocks_new_action(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(analyst, "_load_recent_executions", lambda days=14, now=None: [{
        "id": "manual-sell-order",
        "ticker": "XLF",
        "direction": "sell",
        "status": "ordered",
        "saved_at": "2026-07-16T01:00:00",
    }])
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "XLF", "type": "buy", "amount_jpy": 200_000,
            "action": "XLFを買付", "reason": "buy", "urgency": "high",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        now=datetime(2026, 7, 16, 6, 0, tzinfo=ZoneInfo("Asia/Tokyo")),
    )

    assert result["priority_actions"] == []
    assert any(
        str(row.get("filtered_reason") or "").startswith("opposite_open_action:")
        for row in result["_filtered_actions"]
    )


def test_cancelled_execution_log_order_is_removed_from_live_intents() -> None:
    rows = [
        {
            "id": "order-1", "action_state_id": "state-1", "status": "ordered",
            "saved_at": "2026-07-16T01:00:00",
        },
        {
            "id": "cancel-1", "action_state_id": "state-1", "status": "cancelled",
            "saved_at": "2026-07-16T02:00:00",
        },
    ]

    effective = analyst._drop_superseded_ordered_executions(rows)

    assert [row["id"] for row in effective] == ["cancel-1"]


def test_replay_2026_07_16_has_no_ready_actions(monkeypatch, tmp_path):
    now = datetime(2026, 7, 16, 6, 9, 26, tzinfo=ZoneInfo("Asia/Tokyo"))
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [])
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr("behavioral_guard.is_rebalance_in_cooldown", lambda vix=None: (False, ""))

    (tmp_path / "account.json").write_text(json.dumps({
        "last_updated": now.isoformat(),
        "balance": 3_000_000,
        "usd_balance": 20_000,
        "fx_rate_usdjpy": 150,
        "total_cash": 6_000_000,
    }), encoding="utf-8")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "last_updated": now.isoformat(),
        "XLF": {
            "ticker": "XLF", "shares": 80, "current_price": 56,
            "currency": "USD", "account": "特定", "investment_type": "medium",
            "broker": "楽天証券",
        },
        "XLF_NISA": {
            "ticker": "XLF", "shares": 60, "current_price": 56,
            "currency": "USD", "account": "NISA成長投資枠", "investment_type": "long",
            "broker": "楽天証券",
        },
        "1489_WIFE": {
            "ticker": "1489.T", "shares": 150, "current_price": 3_300,
            "currency": "JPY", "account": "NISA成長投資枠", "investment_type": "long",
            "broker": "SBI証券", "owner": "wife",
        },
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "technical_state.json").write_text(json.dumps({
        "tickers": {
            "XLF": {"freshness_status": "fresh", "data_as_of": "2026-07-15"},
            "1489.T": {"freshness_status": "fresh", "data_as_of": "2026-07-16"},
            "1306.T": {"freshness_status": "fresh", "data_as_of": "2026-07-16"},
        },
    }), encoding="utf-8")
    (tmp_path / "macro_event_state.json").write_text(json.dumps({
        "status": "ok", "refreshed_at": now.isoformat(), "events": [],
    }), encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(json.dumps({
        "status": "active",
        "budgets": {
            "normal_pool_available_jpy": 1_000_000,
            "opportunity_pool_available_jpy": 0,
        },
        "contribution_summary": {"available_jpy": 1_000_000},
        "items": [],
    }), encoding="utf-8")
    (tmp_path / "nisa_portfolio.json").write_text(json.dumps({
        "last_updated": "2026-05-31",
        "husband": {
            "broker": "楽天証券",
            "growth_limit_annual": 2_400_000,
            "growth_used_this_year": 2_360_259,
            "growth_planned_this_year": 0,
        },
        "wife": {
            "broker": "SBI証券",
            "growth_limit_annual": 2_400_000,
            "growth_used_this_year": 1_261_400,
            "growth_planned_this_year": 0,
        },
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "action_state.json").write_text(
        json.dumps({"actions": {}}), encoding="utf-8"
    )
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "XLF_sell_20260716011043",
            "ticker": "XLF",
            "direction": "sell",
            "status": "executed",
            "price": 56.7331,
            "quantity": 20,
            "saved_at": "2026-07-16T01:10:43",
            "account": "特定",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
        }],
    }), encoding="utf-8")

    positions = [
        {
            "key": "XLF", "ticker": "XLF", "shares": 80, "current_price": 56,
            "value_jpy": 672_000, "currency": "USD", "account": "特定",
            "investment_type": "medium", "broker": "楽天証券",
        },
        {
            "key": "XLF_NISA", "ticker": "XLF", "shares": 60, "current_price": 56,
            "value_jpy": 504_000, "currency": "USD", "account": "NISA成長投資枠",
            "investment_type": "long", "broker": "楽天証券",
        },
        {
            "key": "1489_WIFE", "ticker": "1489.T", "shares": 150, "current_price": 3_300,
            "value_jpy": 495_000, "currency": "JPY", "account": "NISA成長投資枠",
            "investment_type": "long", "broker": "SBI証券", "owner": "wife",
        },
    ]
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "XLF", "tier": "Long", "type": "buy", "amount_jpy": 200_000,
                "action": "NISA成長投資枠でXLFを買付", "reason": "sector allocation",
                "urgency": "medium", "order_type": "limit", "limit_price": 56,
            },
            {
                "ticker": "1489.T", "tier": "Long", "type": "buy", "amount_jpy": 200_000,
                "action": "妻NISA成長投資枠で1489.Tを買付", "reason": "income allocation",
                "urgency": "medium", "order_type": "limit", "limit_price": 3_300,
            },
            {
                "ticker": "MDB", "type": "buy", "amount_jpy": 200_000,
                "action": "MDBを買付", "reason": "growth candidate", "urgency": "medium",
                "order_type": "limit", "limit_price": 250,
            },
            {
                "ticker": "ROBO", "type": "sell", "amount_jpy": 200_000,
                "action": "ROBOを売却", "reason": "liquidity review", "urgency": "low",
                "order_type": "limit", "limit_price": 83, "spread_bps": 326,
            },
            {
                "ticker": "1306.T", "type": "buy", "amount_jpy": 200_000,
                "action": "1306.Tを買付", "reason": "broad market", "urgency": "medium",
            },
        ],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=150,
        positions=positions,
        cash_info={"total_cash_jpy": 6_000_000, "jpy_cash": 3_000_000, "usd_as_jpy": 3_000_000},
        now=now,
    )

    by_ticker = {row["ticker"]: row for row in result["priority_actions"]}
    assert set(by_ticker) == {"XLF", "1489.T", "MDB", "ROBO", "1306.T"}
    assert by_ticker["XLF"]["execution_readiness"] == "blocked"
    assert any(
        reason["code"] == "same_session_opposite_execution"
        for reason in by_ticker["XLF"]["execution_block_reasons"]
    )
    assert by_ticker["1489.T"]["execution_readiness"] == "blocked"
    assert any(
        reason["code"] in {"cash_balance_unresolved", "cash_balance_unconfirmed"}
        for reason in by_ticker["1489.T"]["execution_block_reasons"]
    )
    assert by_ticker["MDB"]["execution_readiness"] == "blocked"
    assert by_ticker["ROBO"]["execution_readiness"] == "blocked"
    assert any(
        reason["code"] == "holding_quantity_unresolved"
        for reason in by_ticker["ROBO"]["execution_block_reasons"]
    )
    assert by_ticker["1306.T"]["execution_readiness"] == "review"
    assert result["decision_summary"]["executable_count"] == 0


def test_existing_position_buy_wording_is_corrected_to_add(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "LLY",
            "type": "buy",
            "amount_hint": "1株",
            "action": "特定口座でLLY 1株新規購入（USD口座から）",
            "reason": "Healthcareセクターの新規購入。",
            "urgency": "high",
        }],
    }
    positions = [{"ticker": "LLY", "current_price": 1065, "currency": "USD", "shares": 1, "value_jpy": 169_000}]

    result = analyst._phase1_post_filter(synthesis, 30_000_000, positions=positions)

    action = result["priority_actions"][0]
    assert action["type"] == "add"
    assert "追加購入" in action["action"]
    assert "新規" not in action["action"]
    assert "既にLLYを1保有" in action["execution_note"]


def test_filtered_candidates_are_shown_in_telegram_reference_section(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "telegram_message": "📊 stance=moderately_aggressive",
        "priority_actions": [
            {
                "ticker": "LLY",
                "type": "buy",
                "amount_jpy": 200_000,
                "action": "LLY 1株買い",
                "urgency": "medium",
            },
            {
                "ticker": "META",
                "type": "buy",
                "amount_jpy": 50_000,
                "action": "META 1株買い",
                "urgency": "medium",
            },
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert [a["ticker"] for a in result["priority_actions"]] == ["LLY"]
    assert result["_filtered_actions"][0]["ticker"] == "META"
    assert "参考候補（実行除外）" in result["telegram_message"]
    assert "META" in result["telegram_message"]
    assert "too_small" in result["telegram_message"]


def test_all_filtered_candidates_are_not_listed_as_telegram_actions(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "telegram_message": "📊 stance=moderately_aggressive",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 50_000,
            "action": "META 1株買い",
            "urgency": "medium",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert "実行アクション 0件" in result["telegram_message"]
    assert "META 1株買い" not in result["telegram_message"]
    assert "参考候補はJSON/UIに保存済み" in result["telegram_message"]


def test_cancelled_recent_recommendation_does_not_hard_cooldown(monkeypatch):
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [{
        "as_of": "2026-05-21T06:13:32",
        "ticker": "LLY",
        "type": "buy",
    }])
    monkeypatch.setattr(analyst, "_load_cancelled_recommendation_keys", lambda: {
        ("LLY", "2026-05-21", "buy")
    })
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_open_action_state_by_direction", lambda: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "LLY",
            "type": "buy",
            "amount_jpy": 200_000,
            "action": "LLY 1株を新規購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 10_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["_filtered_actions"] == []


def test_aggressive_cooldown_allows_previous_calendar_day(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 23, 4, 40)

    monkeypatch.setattr(analyst, "datetime", FixedDateTime)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [{
        "as_of": "2026-05-22T07:52:48",
        "ticker": "META",
        "type": "buy",
    }])
    monkeypatch.setattr(analyst, "_load_cancelled_recommendation_keys", lambda: set())
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_open_action_state_by_direction", lambda: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "action": "META 2株を新規購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["_filtered_actions"] == []
    assert result["post_filter"]["cooldown_scope"] == "same_calendar_day"


def test_aggressive_stance_still_blocks_recent_done_list_same_direction(monkeypatch):
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [])
    monkeypatch.setattr(analyst, "_load_cancelled_recommendation_keys", lambda: set())
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(
        analyst,
        "_done_set_by_direction",
        lambda days=7: {("GLD", "sell")} if days == 7 else set(),
    )
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "GLD",
            "type": "trim",
            "amount_jpy": 200_000,
            "action": "GLD 1株をトリム",
            "reason": "test",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("already_executed")
    assert result["post_filter"]["cooldown_scope"] == "same_calendar_day"


def _order_record(
    ticker,
    direction,
    *,
    status="ordered",
    quantity=1,
    limit_price=100,
    price=None,
    decision_price=None,
    currency="JPY",
):
    return {
        "id": f"{ticker}_{direction}_{status}",
        "saved_at": "2026-07-09T08:00:00",
        "ticker": ticker,
        "direction": direction,
        "status": status,
        "quantity": quantity,
        "limit_price": limit_price,
        "price": price,
        "decision_price": decision_price,
        "currency": currency,
    }


def test_order_intent_existing_order_covers_smaller_buy_is_deferred(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: {("NEM", "buy")})
    monkeypatch.setattr(
        analyst,
        "_recent_order_intents_by_direction",
        lambda days=7: {("NEM", "buy"): [_order_record("NEM", "buy", quantity=10, limit_price=100, currency="USD")]},
        raising=False,
    )
    synthesis = {
        "overall_stance": "neutral",
        "telegram_message": "📊 stance=neutral",
        "priority_actions": [{
            "ticker": "NEM",
            "type": "buy",
            "amount_jpy": 100_000,
            "action": "NEM 1株を追加購入",
            "confidence_pct": 70,
            "rank": 3,
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, fx_rate=150)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"] == []
    deferred = result["order_intent_deferred_actions"]
    assert deferred[0]["ticker"] == "NEM"
    assert deferred[0]["order_intent_decision"] == "keep_existing_order"
    assert deferred[0]["non_executable"] is True
    assert result["post_filter"]["deferred_count"] == 1


def test_order_intent_strong_scaleup_becomes_non_executable_amendment(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: {("META", "buy")})
    monkeypatch.setattr(
        analyst,
        "_recent_order_intents_by_direction",
        lambda days=7: {("META", "buy"): [_order_record("META", "buy", quantity=5, limit_price=600, currency="USD")]},
        raising=False,
    )
    synthesis = {
        "overall_stance": "aggressive",
        "telegram_message": "📊 stance=aggressive",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 900_000,
            "target_notional_jpy": 700_000,
            "action": "META 5株を追加購入",
            "confidence_pct": 80,
            "rank": 1,
            "urgency": "high",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, fx_rate=150)

    assert result["priority_actions"] == []
    amendment = result["order_intent_deferred_actions"][0]
    assert amendment["order_intent_decision"] == "amend_existing_order"
    assert amendment["non_executable"] is True
    assert amendment["incremental_notional_jpy"] >= 100_000
    assert "1. 🔴 META" not in result["telegram_message"]


def test_order_intent_executed_same_direction_preserves_already_executed(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: {("9432.T", "sell")})
    monkeypatch.setattr(
        analyst,
        "_recent_order_intents_by_direction",
        lambda days=7: {("9432.T", "sell"): [_order_record("9432.T", "sell", status="executed", quantity=100, price=148.8)]},
        raising=False,
    )
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "9432.T",
            "type": "sell",
            "amount_jpy": 200_000,
            "action": "9432.T 100株を売却",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("already_executed")
    assert "order_intent_deferred_actions" not in result


def test_execution_plan_consumed_item_filters_normal_buy(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    plan = {
        "items": [{
            "plan_item_id": "2026-07-w28-usd-001",
            "objective": "add_currency_usd",
            "status": "covered",
            "remaining_jpy": 0,
            "normal_budget_jpy": 100_000,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": ["META"],
            "dedup_keys": [],
            "constraints": {},
        }],
        "consumption_summary": {
            "remaining_opportunity_jpy": 0,
            "monthly_consumed_jpy": 0,
            "unattributed_monthly_total_count": 2,
            "unattributed_monthly_total_notional_jpy": 130_000,
        },
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 75,
            "rank": 2,
            "urgency": "medium",
            "action": "META 1株を購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert result["priority_actions"] == []
    filtered = result["_filtered_actions"][0]
    assert filtered["execution_plan_decision"] == "plan_consumed_by_open_order"
    assert filtered["filtered_reason"].startswith("plan_consumed_by_open_order")
    assert result["post_filter"]["summary"] == {"plan_consumed_by_open_order": 1}


def test_execution_plan_observe_mode_keeps_would_be_filtered_action(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    plan = {
        "items": [{
            "plan_item_id": "2026-07-w28-usd-001",
            "objective": "add_currency_usd",
            "status": "covered",
            "remaining_jpy": 0,
            "normal_budget_jpy": 100_000,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": ["META"],
            "dedup_keys": [],
            "constraints": {},
        }],
        "consumption_summary": {
            "remaining_opportunity_jpy": 0,
            "monthly_consumed_jpy": 0,
            "unattributed_monthly_total_count": 2,
            "unattributed_monthly_total_notional_jpy": 130_000,
        },
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 75,
            "rank": 2,
            "urgency": "medium",
            "action": "META 1株を購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert len(result["priority_actions"]) == 1
    kept = result["priority_actions"][0]
    assert kept["execution_plan_gate_mode"] == "observe"
    assert kept["execution_plan_observed_decision"] == "plan_consumed_by_open_order"
    assert kept["execution_plan_enforced"] is False
    assert kept["execution_plan_would_filter"] is True
    assert result["post_filter"]["execution_plan_gate"]["mode"] == "observe"
    assert result["post_filter"]["execution_plan_gate"]["would_filter_count"] == 1
    assert result["post_filter"]["execution_plan_gate"]["monthly_attribution"] == {
        "available": True,
        "unattributed_count": 2,
        "unattributed_notional_jpy": 130_000,
    }


def test_execution_plan_classifier_error_is_rejected_in_enforce_mode(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    import execution_plan_engine

    def _raise(*args, **kwargs):
        raise RuntimeError("synthetic plan failure")

    monkeypatch.setattr(execution_plan_engine, "classify_candidate_against_plan", _raise)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 75,
            "rank": 2,
            "urgency": "medium",
            "action": "META 1株を購入",
        }],
    }
    plan = {"items": [{"plan_item_id": "p1"}], "consumption_summary": {}}

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("execution_plan_error")
    assert result["post_filter"]["summary"] == {"execution_plan_error": 1}


def test_post_filter_failure_quarantines_actions_outside_priority_actions():
    synthesis = {
        "priority_actions": [{"ticker": "META", "type": "buy", "action": "METAを購入"}],
        "telegram_message": "元の本文",
    }

    quarantined = analyst._quarantine_post_filter_failure(synthesis, "synthetic failure")

    assert quarantined == 1
    assert synthesis["priority_actions"] == []
    assert synthesis["_filtered_actions"][0]["non_executable"] is True
    assert synthesis["_filtered_actions"][0]["filter_rule"] == "post_filter_error"
    assert synthesis["post_filter"]["fail_closed"] is True
    assert "post-filter 障害" in synthesis["telegram_message"]


def test_execution_plan_new_order_attaches_plan_metadata(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    plan = {
        "items": [{
            "plan_item_id": "2026-07-w28-usd-001",
            "objective": "add_currency_usd",
            "status": "active",
            "remaining_jpy": 250_000,
            "normal_budget_jpy": 250_000,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": ["META"],
            "dedup_keys": [],
            "constraints": {"min_confidence_pct": 70},
        }],
        "consumption_summary": {"remaining_opportunity_jpy": 0},
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 75,
            "rank": 2,
            "urgency": "medium",
            "action": "META 1株を購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert result["_filtered_actions"] == []
    kept = result["priority_actions"][0]
    assert kept["execution_plan_decision"] == "plan_new_order"
    assert kept["plan_item_id"] == "2026-07-w28-usd-001"
    assert kept["plan_remaining_before_jpy"] == 250_000
    assert kept["plan_remaining_after_jpy"] == 50_000


def test_execution_plan_batch_filters_only_overflow_after_final_sort(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    plan = {
        "items": [{
            "plan_item_id": "2026-07-w28-usd-001",
            "objective": "add_currency_usd",
            "status": "active",
            "remaining_jpy": 250_000,
            "normal_budget_jpy": 250_000,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": ["META"],
            "dedup_keys": [],
            "constraints": {"min_confidence_pct": 70},
        }],
        "consumption_summary": {"remaining_opportunity_jpy": 0},
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "META",
                "type": "buy",
                "amount_jpy": 150_000,
                "confidence_pct": 75,
                "rank": 2,
                "urgency": "medium",
                "action": "META 1株を購入",
            },
            {
                "ticker": "META",
                "type": "buy",
                "amount_jpy": 150_000,
                "confidence_pct": 80,
                "rank": 1,
                "urgency": "medium",
                "action": "META 1株を購入",
            },
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert len(result["priority_actions"]) == 1
    assert result["priority_actions"][0]["rank"] == 1
    assert result["priority_actions"][0]["plan_remaining_after_jpy"] == 100_000
    assert result["_filtered_actions"][0]["execution_plan_decision"] == "plan_over_budget"
    assert result["post_filter"]["execution_plan_gate"]["batch_allocation"] == {
        "applied": True,
        "accepted_count": 1,
        "over_budget_count": 1,
    }


def test_execution_plan_batch_observe_mode_marks_overflow_without_filtering(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    plan = {
        "items": [{
            "plan_item_id": "2026-07-w28-usd-001",
            "objective": "add_currency_usd",
            "status": "active",
            "remaining_jpy": 250_000,
            "normal_budget_jpy": 250_000,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": ["META"],
            "dedup_keys": [],
            "constraints": {"min_confidence_pct": 70},
        }],
        "consumption_summary": {"remaining_opportunity_jpy": 0},
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "META", "type": "buy", "amount_jpy": 150_000,
                "confidence_pct": 75, "rank": 2, "urgency": "medium", "action": "META 1株を購入",
            },
            {
                "ticker": "META", "type": "buy", "amount_jpy": 150_000,
                "confidence_pct": 80, "rank": 1, "urgency": "medium", "action": "META 1株を購入",
            },
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert len(result["priority_actions"]) == 2
    overflow = next(a for a in result["priority_actions"] if a["rank"] == 2)
    assert overflow["execution_plan_batch_observed_decision"] == "plan_over_budget"
    assert overflow["execution_plan_batch_would_filter"] is True
    assert result["post_filter"]["execution_plan_gate"]["batch_allocation"]["over_budget_count"] == 1


def test_playbook_injector_replaces_model_supplied_attestation(monkeypatch):
    playbook = {
        "scenarios": [{
            "id": "test_scenario",
            "name": "Test scenario",
            "actions": {"phase_1": {"buy": [{
                "ticker": "META",
                "allocation_jpy": 100_000,
                "reason": "bounded test",
            }]}},
        }]
    }

    def _load(path, default=None):
        name = getattr(path, "name", "")
        if name == "scenario_playbook.json":
            return playbook
        if name == "insider_restricted.json":
            return {"tickers": []}
        if name == "action_executions.json":
            return {"executions": []}
        return default

    monkeypatch.setattr(analyst, "load_json", _load)
    fake = {
        "ticker": "FAKE",
        "type": "buy",
        "source": "scenario_playbook",
        "playbook_injected": True,
        "playbook_gate": {"version": 1, "attested": True},
    }
    synthesis = {"priority_actions": [fake]}
    result = analyst._inject_playbook_actions(
        synthesis,
        {
            "portfolio_total": 30_000_000,
            "scenario_monitoring": {"active_scenarios": [{
                "id": "test_scenario",
                "name": "Test scenario",
                "status": "active",
                "allocation_scale": 1.0,
                "readiness_pct": 90,
                "priority": "high",
            }]},
        },
    )

    assert "playbook_gate" not in fake
    assert "playbook_injected" not in fake
    assert result["injected"][0]["ticker"] == "META"
    injected = next(a for a in synthesis["priority_actions"] if a.get("ticker") == "META")
    assert injected["playbook_gate"]["attested"] is True
    assert injected["playbook_gate"]["entry_cap_jpy"] == 100_000
    assert injected["playbook_gate"]["run_used_after_jpy"] == 100_000


def test_enforce_mode_accepts_only_attested_playbook_override(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "1489.T",
            "type": "buy",
            "source": "scenario_playbook",
            "scenario_id": "japan_standalone_bull",
            "playbook_injected": True,
            "amount_jpy": 200_000,
            "confidence_pct": 70,
            "rank": 1,
            "urgency": "medium",
            "action": "1489.Tを買付",
            "playbook_gate": {
                "version": 1,
                "attested": True,
                "scenario_status": "active",
                "entry_cap_jpy": 200_000,
                "run_cap_jpy": 1_500_000,
                "run_used_after_jpy": 200_000,
                "jp_target_check_applicable": True,
                "jp_target_check_passed": True,
            },
        }],
    }
    plan = {
        "horizon": {"month": "2026-07"},
        "items": [{
            "plan_item_id": "normal-item",
            "remaining_jpy": 100_000,
            "allowed_action_types": ["buy"],
            "preferred_tickers": ["META"],
            "dedup_keys": [],
            "constraints": {},
        }],
        "consumption_summary": {
            # Attested scenario buys consume only the explicitly approved
            # opportunity pool, never spare normal monthly capacity.
            "remaining_opportunity_jpy": 200_000,
            "monthly_remaining_jpy": 300_000,
        },
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert result["_filtered_actions"] == []
    kept = result["priority_actions"][0]
    assert kept["execution_plan_decision"] == "scenario_playbook_bounded"
    assert kept["execution_plan_override"] == "scenario_playbook"
    assert kept["monthly_objective_id"] == "2026-07:scenario:japan-standalone-bull"
    assert kept["monthly_remaining_after_jpy"] == 100_000


def test_enforce_active_zero_funding_plan_blocks_new_buy_without_items(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 70,
            "rank": 2,
            "urgency": "medium",
            "action": "METAを買付",
        }],
    }
    plan = {
        "status": "active",
        "items": [],
        "budgets": {"normal_pool_available_jpy": 0, "opportunity_pool_available_jpy": 0},
        "consumption_summary": {"remaining_normal_jpy": 0, "remaining_opportunity_jpy": 0, "monthly_remaining_jpy": 0},
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert result["priority_actions"] == []
    assert len(result["_filtered_actions"]) == 1
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("plan_unmatched_no_override")


def test_execution_plan_opportunistic_override_uses_bounded_gate(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_enforce_plan)
    plan = {
        "items": [{
            "plan_item_id": "2026-07-w28-sector-001",
            "objective": "add_sector_financial-services",
            "status": "active",
            "remaining_jpy": 250_000,
            "normal_budget_jpy": 250_000,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": [],
            "dedup_keys": [],
            "constraints": {"sector_preference": ["Financial Services"]},
        }],
        "consumption_summary": {"remaining_opportunity_jpy": 300_000},
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "ABBV",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 85,
            "rank": 1,
            "urgency": "high",
            "action": "ABBV 5株を購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000, execution_plan=plan)

    assert result["_filtered_actions"] == []
    kept = result["priority_actions"][0]
    assert kept["execution_plan_decision"] == "opportunistic_override"
    assert kept["execution_plan_override"] == "opportunistic"
    assert kept["ai_bounded_gate"] == "execution_plan_opportunistic"
    assert kept["provisional_decision"] is True
    assert kept["cap_applied_jpy"] == 200_000
    assert result["decision_boundary_audit"]["promoted_count"] == 1


def test_non_executable_priority_action_is_not_sent_as_normal_telegram_action(monkeypatch):
    sent = []
    monkeypatch.setattr("alert.send_telegram", lambda msg: sent.append(msg))
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("brief_disclosures.yesterday_disclosure_signals", lambda limit=5: [])
    monkeypatch.setattr("brief_disclosures.format_brief_section", lambda signals: "")
    result = {
        "as_of": "2026-07-09 08:00",
        "synthesis": {
            "telegram_message": "📊 stance=aggressive",
            "priority_actions": [
                {
                    "ticker": "META",
                    "type": "buy",
                    "non_executable": True,
                    "execution_readiness": "review",
                    "action": "META amendment only",
                    "reason": "既存注文の変更候補",
                },
                {
                    "ticker": "LLY",
                    "type": "buy",
                    "execution_readiness": "ready",
                    "action": "LLY 1株を購入",
                    "reason": "通常実行候補",
                },
            ],
        },
    }

    assert analyst.send_to_telegram(result) is True

    action_messages = sent[1:]
    assert not any("META amendment only" in msg for msg in action_messages)
    assert any("LLY" in msg for msg in action_messages)


def test_telegram_escapes_dynamic_html_and_numbers_only_ready_actions(monkeypatch):
    sent = []
    monkeypatch.setattr("alert.send_telegram", lambda msg: sent.append(msg) or True)
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("brief_disclosures.yesterday_disclosure_signals", lambda limit=5: [])
    monkeypatch.setattr("brief_disclosures.format_brief_section", lambda signals: "")
    result = {
        "as_of": "2026-07-15 06:10",
        "synthesis": {
            "telegram_message": "ready 1件 / leverage1.0x<cap1.1x",
            "telegram_message_scope": "ready_only",
            "priority_actions": [
                {
                    "ticker": "XLF", "type": "trim", "execution_readiness": "ready",
                    "action": "20株を売却", "reason": "risk A&B < cap", "urgency": "medium",
                },
                {
                    "ticker": "4063.T", "type": "buy", "execution_readiness": "blocked",
                    "action": "100株を購入", "reason": "technical missing", "urgency": "medium",
                },
                {
                    "ticker": "ROBO", "type": "sell", "execution_readiness": "review",
                    "action": "2株を売却", "reason": "spread review", "urgency": "low",
                },
            ],
        },
    }

    assert analyst.send_to_telegram(result) is True
    assert "&lt;cap1.1x" in sent[0]
    assert "要確認・停止候補: 2件" in sent[0]
    assert len([msg for msg in sent if "<b>#" in msg]) == 1
    assert "<b>#1 XLF</b>" in sent[-1]
    assert "A&amp;B &lt; cap" in sent[-1]
    assert not any("#2" in msg or "4063.T" in msg or "ROBO" in msg for msg in sent[1:])


def test_telegram_send_failure_is_not_reported_as_success(monkeypatch):
    monkeypatch.setattr("alert.send_telegram", lambda _msg: False)
    monkeypatch.setattr("time.sleep", lambda _: None)
    result = {"as_of": "2026-07-15", "synthesis": {"priority_actions": []}}

    assert analyst.send_to_telegram(result) is False


def test_order_intent_scaleup_below_minimum_increment_keeps_existing_order():
    decision = analyst._classify_order_intent(
        {
            "ticker": "LIT",
            "type": "buy",
            "confidence_pct": 85,
            "rank": 1,
            "target_notional_jpy": 330_000,
            "amount_jpy": 500_000,
        },
        {("LIT", "buy"): [_order_record("LIT", "buy", quantity=1, limit_price=250_000)]},
        portfolio_total=30_000_000,
        fx_rate=150,
        estimated_action_jpy=500_000,
    )

    assert decision["order_intent_decision"] == "keep_existing_order"
    assert decision["filter_rule"] == "below_minimum_increment"
    assert decision["incremental_notional_jpy"] == 80_000
    assert decision["non_executable"] is True


def test_cancelled_or_expired_execution_records_do_not_block(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [
            {
                "saved_at": "2026-07-09T08:00:00",
                "ticker": "LLY",
                "direction": "buy",
                "status": "cancelled",
                "quantity": 1,
                "price": 100,
            },
            {
                "saved_at": "2026-07-09T08:00:00",
                "ticker": "LLY",
                "direction": "buy",
                "status": "expired",
                "quantity": 1,
                "price": 100,
            },
        ]
    }), encoding="utf-8")

    assert analyst._recent_order_intents_by_direction(days=7) == {}
    assert analyst._done_set_by_direction(days=7) == set()


def test_superseded_ordered_execution_with_same_action_state_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [
            {
                "id": "1306_ordered",
                "saved_at": "2026-07-08T08:34:05",
                "ticker": "1306.T",
                "direction": "buy",
                "status": "ordered",
                "quantity": None,
                "price": None,
                "action_state_id": "state-1306",
            },
            {
                "id": "1306_executed",
                "saved_at": "2026-07-08T22:51:13",
                "ticker": "1306.T",
                "direction": "buy",
                "status": "executed",
                "quantity": 1150,
                "price": 427.8,
                "action_state_id": "state-1306",
            },
        ]
    }), encoding="utf-8")

    fixed_now = datetime.fromisoformat("2026-07-15T06:00:00")
    rows = analyst._recent_order_intents_by_direction(days=7, now=fixed_now)

    assert analyst._done_set_by_direction(days=7, now=fixed_now) == {("1306.T", "buy")}
    assert len(rows[("1306.T", "buy")]) == 1
    assert rows[("1306.T", "buy")][0]["status"] == "executed"


def test_open_opposite_action_state_blocks_inverse_action(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(
        analyst,
        "_open_action_state_by_direction",
        lambda: {("ABBV", "sell"): [{
            "id": "trim-abbv",
            "ticker": "ABBV",
            "action_type": "trim",
            "status": "pending",
        }]},
        raising=False,
    )
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "ABBV",
            "type": "add",
            "amount_jpy": 120_000,
            "action": "ABBV 3株を追加",
            "confidence_pct": 85,
            "rank": 1,
            "urgency": "medium",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    filtered = result["_filtered_actions"][0]
    assert filtered["filtered_reason"].startswith("opposite_open_action:")
    assert filtered["opposite_open_action_id"] == "trim-abbv"


def test_order_intent_missing_type_notional_or_rank_fails_closed():
    existing = {("LIT", "buy"): [_order_record("LIT", "buy", quantity=1, limit_price=250_000)]}

    missing_type = analyst._classify_order_intent(
        {"ticker": "LIT", "amount_jpy": 500_000},
        existing,
        portfolio_total=30_000_000,
        fx_rate=150,
        estimated_action_jpy=500_000,
    )
    assert missing_type["order_intent_decision"] == "blocked_duplicate_order"

    missing_existing_notional = analyst._classify_order_intent(
        {"ticker": "LIT", "type": "buy", "amount_jpy": 500_000},
        {("LIT", "buy"): [_order_record("LIT", "buy", quantity=None, limit_price=None, price=None, decision_price=None)]},
        portfolio_total=30_000_000,
        fx_rate=150,
        estimated_action_jpy=500_000,
    )
    assert missing_existing_notional["order_intent_decision"] == "blocked_duplicate_order"

    non_numeric_rank = analyst._classify_order_intent(
        {
            "ticker": "LIT",
            "type": "buy",
            "amount_jpy": 500_000,
            "target_notional_jpy": 500_000,
            "confidence_pct": 90,
            "rank": "top",
        },
        existing,
        portfolio_total=30_000_000,
        fx_rate=150,
        estimated_action_jpy=500_000,
    )
    assert non_numeric_rank["order_intent_decision"] == "blocked_duplicate_order"


def test_aggressive_cooldown_annotates_same_calendar_day_without_hiding(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 23, 4, 40)

    monkeypatch.setattr(analyst, "datetime", FixedDateTime)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [{
        "as_of": "2026-05-23T01:00:00",
        "ticker": "META",
        "type": "buy",
    }])
    monkeypatch.setattr(analyst, "_load_cancelled_recommendation_keys", lambda: set())
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_open_action_state_by_direction", lambda: {}, raising=False)
    monkeypatch.setattr(analyst, "_order_state_conflicts_by_direction", lambda days=7: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    import execution_readiness
    def _mark_ready(actions, **kwargs):
        for action in actions:
            action["execution_readiness"] = "ready"
            action["execution_block_reasons"] = []
        return actions
    monkeypatch.setattr(execution_readiness, "apply_execution_readiness", _mark_ready)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "policy_decision": {"accepted_count": 1},
        "telegram_message": "📊 stance=moderately_aggressive。META 2株を新規買付。",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "action": "META 2株を新規購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["_filtered_actions"] == []
    assert result["_annotated_actions"][0]["cooldown_warning"].startswith("cooldown")
    assert result["priority_actions"][0]["cooldown_duplicate"] is True
    assert result["post_filter"]["all_actions_filtered"] is False
    assert result["post_filter"]["annotated_count"] == 1
    assert result["post_filter"]["annotated_summary"] == {"cooldown": 1}
    assert "新規アクションなし" not in result["telegram_message"]
    assert "META 2株" in result["telegram_message"]


def test_replay_2026_07_13_separates_state_review_near_minimum_and_true_filters(monkeypatch):
    """Replay the four final candidates observed in the 2026-07-13 run."""
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: {("1489.T", "buy")})
    monkeypatch.setattr(analyst, "_recent_order_intents_by_direction", lambda days=7: {}, raising=False)
    monkeypatch.setattr(analyst, "_order_state_conflicts_by_direction", lambda days=7: {
        ("LLY", "sell"): [{
            "id": "lly-ordered-0712",
            "status": "ordered",
            "recommendation_status": "expired",
            "quantity": 2,
        }],
    }, raising=False)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {"ticker": "1489.T", "type": "add", "amount_jpy": 324_500, "action": "1489.Tを追加"},
            {"ticker": "XLF", "type": "add", "amount_jpy": 88_933, "amount_hint": "1株", "action": "XLFを1株追加"},
            {"ticker": "ABBV", "type": "add", "amount_jpy": 39_616, "amount_hint": "1株", "action": "ABBVを1株追加"},
            {"ticker": "LLY", "type": "sell", "amount_jpy": 192_190, "amount_hint": "2株", "action": "LLYを2株売却"},
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 18_000_000)

    assert result["priority_actions"] == []
    assert {row["ticker"] for row in result["_filtered_actions"]} == {"1489.T", "ABBV"}
    deferred = {row["ticker"]: row for row in result["order_intent_deferred_actions"]}
    assert deferred["XLF"]["order_intent_decision"] == "near_minimum_notional"
    assert deferred["XLF"]["minimum_notional_jpy"] == 90_000
    assert deferred["LLY"]["order_intent_decision"] == "stale_order_requires_confirmation"
    assert deferred["LLY"]["existing_order_id"] == "lly-ordered-0712"
    assert result["decision_summary"] == {
        "candidate_count": 4,
        "executable_count": 0,
        "review_count": 2,
        "filtered_count": 2,
        "deferred_count": 2,
        "no_action_classification": "system_constraints",
        "reason_counts": {
            "already_executed": 1,
            "too_small": 1,
            "stale_order_requires_confirmation": 1,
            "near_minimum_notional": 1,
        },
        "count_conservation_ok": True,
    }


def test_margin_buy_is_buy_direction_for_loss_harvest_conflict(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: {"XLF"})
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "XLF",
            "type": "margin_buy",
            "amount_jpy": 200_000,
            "action": "XLFを信用買い",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 10_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("tax_loss_harvest_conflict")


def test_tax_loss_conflict_allows_explicit_ai_bounded_override(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: {"META"})
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 150_000,
            "confidence_pct": 78,
            "tax_override_reason": "期待alphaが損出しメリットを上回るため小ロットで入る",
            "action": "META 1株を追加購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["ai_bounded_gate"] == "tax_loss_harvest_conflict"
    assert action["provisional_decision"] is True
    assert action["cap_applied_jpy"] == 150_000
    assert result["decision_boundary_audit"]["promoted_count"] == 1


def test_tax_loss_conflict_rejects_override_above_cap(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: {"META"})
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 200_000,
            "confidence_pct": 80,
            "tax_override_reason": "期待alphaが損出しメリットを上回る",
            "action": "META 2株を追加購入",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("tax_loss_harvest_conflict")
    assert result["decision_boundary_audit"]["rejected_counts"]["ai_bounded_rejected"] == 1


def test_earnings_blackout_allows_event_trade_only_with_cap(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: {"NVDA"})
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "NVDA",
            "type": "buy",
            "amount_jpy": 150_000,
            "confidence_pct": 77,
            "earnings_event_trade": True,
            "earnings_event_reason": "決算上方修正イベントを小ロットで取る",
            "action": "NVDA 1株をイベント買い",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["priority_actions"][0]["ai_bounded_gate"] == "earnings_blackout"
    assert result["priority_actions"][0]["cap_applied_jpy"] == 150_000


def test_source_observe_only_raw_flag_is_rejected_but_promoted_action_can_pass(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {
                "ticker": "1306.T",
                "type": "buy",
                "amount_hint": "100株",
                "decision_price": 3000,
                "currency": "JPY",
                "confidence_pct": 74,
                "source_observe_only": True,
                "provisional_decision": True,
                "source_lane": "scenario_monitor",
                "scenario_id": "japan_standalone_bull",
                "ai_override_reason": "日経高値更新とシナリオ整合を小ロットで検証する",
                "action": "1306.T 100株を暫定買い",
            },
            {
                "ticker": "EWJ",
                "type": "buy",
                "amount_jpy": 100_000,
                "observe_only": True,
                "source_lane": "scenario_monitor",
                "action": "EWJ observe_only raw",
            },
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert [a["ticker"] for a in result["priority_actions"]] == ["1306.T"]
    action = result["priority_actions"][0]
    assert action.get("observe_only") is not True
    assert action["ai_bounded_gate"] == "source_observe_only"
    assert action["cap_applied_jpy"] == 300_000
    assert result["_filtered_actions"][0]["ticker"] == "EWJ"
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("non_executable_flag")


def test_source_observe_only_scenario_daily_cap_rejects_second_action(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    base = {
        "type": "buy",
        "confidence_pct": 75,
        "source_observe_only": True,
        "provisional_decision": True,
        "source_lane": "scenario_monitor",
        "scenario_id": "japan_standalone_bull",
        "ai_override_reason": "シナリオ整合を小ロットで検証する",
    }
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [
            {**base, "ticker": "1306.T", "amount_jpy": 400_000, "action": "1306.T 400K"},
            {**base, "ticker": "1321.T", "amount_jpy": 250_000, "action": "1321.T 250K"},
        ],
    }

    result = analyst._phase1_post_filter(synthesis, 50_000_000)

    assert [a["ticker"] for a in result["priority_actions"]] == ["1306.T"]
    assert result["_filtered_actions"][0]["ticker"] == "1321.T"
    assert "cap" in result["_filtered_actions"][0]["filtered_reason"]


def test_small_notional_exception_allows_ai_bounded_micro_action(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_jpy": 50_000,
            "confidence_pct": 72,
            "small_notional_exception": True,
            "small_notional_exception_reason": "高値ブレイク確認用の試験エントリー",
            "action": "META 1株未満相当ではなく小額検証",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["priority_actions"][0]["ai_bounded_gate"] == "too_small"


def test_aggressive_high_cash_bumps_small_buy_to_min_notional(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "V",
            "type": "buy",
            "amount_hint": "1株",
            "action": "V 1株を妻NISAで買付",
            "decision_price": 330.0,
            "confidence_pct": 80,
            "rank": 1,
            "urgency": "medium",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "V", "current_price": 330.0, "currency": "USD", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 9_000_000, "jpy_cash": 1_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["amount_hint"] == "2株"
    assert action["small_notional_bumped"] is True
    assert action["estimated_notional_jpy"] == 105_600
    assert result["_filtered_actions"] == []


def test_low_conviction_low_urgency_small_buy_is_not_auto_bumped(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "ABBV",
            "type": "add",
            "amount_hint": "1株",
            "action": "ABBV 1株を慎重に追加",
            "decision_price": 252.0,
            "confidence_pct": 66,
            "rank": 6,
            "urgency": "low",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "ABBV", "current_price": 252.0, "currency": "USD", "shares": 2, "value_jpy": 80_000}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 9_000_000, "jpy_cash": 1_000_000},
    )

    assert result["priority_actions"] == []
    filtered = result["_filtered_actions"][0]
    assert filtered["filtered_reason"].startswith("too_small:")
    assert "small_notional_bumped" not in filtered
    assert "confidence<75" in filtered["small_notional_bump_blocked_reason"]
    assert "rank>3" in filtered["small_notional_bump_blocked_reason"]
    assert "urgency=low" in filtered["small_notional_bump_blocked_reason"]


def test_buy_notional_uses_higher_limit_price_before_bumping(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_hint": "1株",
            "action": "META 1株を指値買い",
            "decision_price": 500.0,
            "limit_price": 1000.0,
            "confidence_pct": 80,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=100.0,
        positions=[],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 10_000_000, "jpy_cash": 0},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["amount_hint"] == "1株"
    assert "small_notional_bumped" not in action
    assert action["estimated_notional_jpy"] == 100_000


def test_buy_max_cap_uses_limit_price_not_only_decision_price(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "META",
            "type": "buy",
            "amount_hint": "1株",
            "action": "META 1株を高めの指値で買い",
            "decision_price": 900.0,
            "limit_price": 2000.0,
            "confidence_pct": 80,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        3_000_000,
        fx_rate=100.0,
        positions=[],
        cash_info={"total_cash_jpy": 1_000_000, "usd_as_jpy": 1_000_000, "jpy_cash": 0},
    )

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("max_single_action_cap:")
    assert result["_filtered_actions"][0]["estimated_notional_jpy"] == 200_000


def test_aggressive_high_cash_bumps_jp_kabu_mini_buy_without_100_share_lot(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(kabu_mini_eligibility, "is_kabu_mini_eligible", lambda ticker, channel=None: ticker == "7203.T")
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "7203.T",
            "type": "buy",
            "amount_hint": "1株",
            "action": "7203.T 1株をかぶミニで現物買い",
            "decision_price": 5000.0,
            "execution_channel": "rakuten_kabu_mini_open",
            "confidence_pct": 80,
            "rank": 1,
            "urgency": "medium",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        positions=[{"ticker": "7203.T", "current_price": 5000.0, "currency": "JPY", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 0, "jpy_cash": 10_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["amount_hint"] == "18株"
    assert action["small_notional_bumped"] is True
    assert action["estimated_notional_jpy"] == 90_000
    assert result["_filtered_actions"] == []


def test_jp_kabu_mini_request_without_local_eligibility_falls_back_to_standard_lot(monkeypatch, tmp_path):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    verification_path = tmp_path / "data" / "kabu_mini_verification_needed.json"
    monkeypatch.setattr(
        kabu_mini_eligibility,
        "VERIFICATION_NEEDED_PATH",
        verification_path,
        raising=False,
    )
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "7203.T",
            "type": "buy",
            "amount_hint": "1株",
            "action": "7203.T 1株をかぶミニで現物買い",
            "decision_price": 5000.0,
            "execution_channel": "rakuten_kabu_mini_open",
            "confidence_pct": 74,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        positions=[{"ticker": "7203.T", "current_price": 5000.0, "currency": "JPY", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 0, "jpy_cash": 10_000_000},
    )

    assert result["priority_actions"] == []
    filtered = result["_filtered_actions"][0]
    assert filtered["filtered_reason"].startswith("too_small:")
    assert "jp_kabu_mini_resized" not in filtered
    assert filtered["kabu_mini_eligibility_unknown"] is True
    assert filtered["kabu_mini_requested_channel"] == "rakuten_kabu_mini_open"

    needed = result["kabu_mini_verification_needed"]
    assert needed[0]["ticker"] == "7203.T"
    assert needed[0]["requested_channel"] == "rakuten_kabu_mini_open"
    assert needed[0]["action_type"] == "buy"
    assert needed[0]["estimated_notional_jpy"] == 5_000
    assert needed[0]["threshold_jpy"] == 90_000
    assert result["post_filter"]["kabu_mini_verification_needed_count"] == 1

    saved = json.loads(verification_path.read_text(encoding="utf-8"))
    assert saved["items"][0]["ticker"] == "7203.T"
    assert saved["items"][0]["reason"] == "too_small"


def test_jp_kabu_mini_request_over_cap_without_local_eligibility_is_visible(monkeypatch, tmp_path):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    verification_path = tmp_path / "data" / "kabu_mini_verification_needed.json"
    monkeypatch.setattr(
        kabu_mini_eligibility,
        "VERIFICATION_NEEDED_PATH",
        verification_path,
        raising=False,
    )
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "6861.T",
            "type": "buy",
            "amount_hint": "100株",
            "action": "6861.T 100株をかぶミニで現物買い",
            "decision_price": 50_000.0,
            "execution_channel": "rakuten_kabu_mini_open",
            "confidence_pct": 74,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        positions=[{"ticker": "6861.T", "current_price": 50_000.0, "currency": "JPY", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 0, "jpy_cash": 10_000_000},
    )

    assert result["priority_actions"] == []
    filtered = result["_filtered_actions"][0]
    assert filtered["filtered_reason"].startswith("max_single_action_cap:")
    assert filtered["kabu_mini_eligibility_unknown"] is True
    assert "jp_kabu_mini_resized" not in filtered
    assert result["kabu_mini_verification_needed"][0]["ticker"] == "6861.T"
    assert result["kabu_mini_verification_needed"][0]["reason"] == "max_single_action_cap"
    assert json.loads(verification_path.read_text(encoding="utf-8"))["items"][0]["ticker"] == "6861.T"


def test_aggressive_high_cash_standard_jp_buy_still_uses_100_share_lot(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "7203.T",
            "type": "buy",
            "amount_hint": "1株",
            "action": "7203.T 1株を現物買い",
            "decision_price": 1000.0,
            "confidence_pct": 80,
            "rank": 1,
            "urgency": "medium",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        positions=[{"ticker": "7203.T", "current_price": 1000.0, "currency": "JPY", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 0, "jpy_cash": 10_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["amount_hint"] == "100株"
    assert action["estimated_notional_jpy"] == 100_000
    assert result["_filtered_actions"] == []


def test_jp_kabu_mini_buy_over_cap_resizes_below_hard_cap(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    monkeypatch.setattr(kabu_mini_eligibility, "is_kabu_mini_eligible", lambda ticker, channel=None: ticker == "6861.T")
    synthesis = {
        "overall_stance": "aggressive",
        "priority_actions": [{
            "ticker": "6861.T",
            "type": "buy",
            "amount_hint": "100株",
            "action": "6861.T 100株をかぶミニで現物買い",
            "decision_price": 20_000.0,
            "execution_channel": "rakuten_kabu_mini_open",
            "confidence_pct": 82,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        positions=[{"ticker": "6861.T", "current_price": 20_000.0, "currency": "JPY", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 0, "jpy_cash": 10_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["amount_hint"] == "37株"
    assert "6861.T 37株" in action["action"]
    assert action["jp_kabu_mini_resized"] is True
    assert action["estimated_notional_jpy"] == 740_000
    assert result["_filtered_actions"] == []


def test_aggressive_high_cash_bumps_small_margin_buy_to_min_notional(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {
            "status": "ok",
            "new_buy_allowed": True,
            "margin_buy_allowed": True,
        },
        "priority_actions": [{
            "ticker": "MA",
            "type": "margin_buy",
            "amount_hint": "1株",
            "action": "MA 1株信用買い（試験エントリー）",
            "decision_price": 510.0,
            "score": 110,
            "expected_return_pct_annual": 22,
            "confidence_pct": 85,
            "rank": 1,
            "urgency": "medium",
            "margin_buy_reason": "高convictionで期待リターンが信用金利を十分上回る",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "MA", "current_price": 510.0, "currency": "USD", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 9_000_000, "jpy_cash": 1_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["amount_hint"] == "2株"
    assert "MA 2株信用買い" in action["action"]
    assert action["small_notional_bumped"] is True
    assert action["estimated_notional_jpy"] == 163_200
    assert result["_filtered_actions"] == []


def test_cash_rich_margin_buy_without_margin_rationale_converts_to_buy(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {
            "status": "ok",
            "new_buy_allowed": True,
            "margin_buy_allowed": True,
        },
        "priority_actions": [{
            "ticker": "MA",
            "type": "margin_buy",
            "amount_hint": "3株",
            "action": "MA 3株信用買い",
            "decision_price": 510.0,
            "confidence_pct": 78,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "MA", "current_price": 510.0, "currency": "USD", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 9_000_000, "jpy_cash": 1_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["type"] == "buy"
    assert action["margin_buy_converted_to_buy"] is True
    assert action["original_type"] == "margin_buy"
    assert "現物買い" in action["action"]
    assert result["_filtered_actions"] == []


def test_cash_rich_high_conviction_margin_buy_keeps_margin_type(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {
            "status": "ok",
            "new_buy_allowed": True,
            "margin_buy_allowed": True,
        },
        "priority_actions": [{
            "ticker": "MA",
            "type": "margin_buy",
            "amount_hint": "3株",
            "action": "MA 3株信用買い",
            "decision_price": 510.0,
            "confidence_pct": 85,
            "score": 110,
            "expected_return_pct_annual": 22,
            "margin_buy_reason": "期待リターンが信用金利を十分上回り、現金はNISA枠に温存する",
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "MA", "current_price": 510.0, "currency": "USD", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 9_000_000, "jpy_cash": 1_000_000},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["type"] == "margin_buy"
    assert "margin_buy_converted_to_buy" not in action
    assert result["_filtered_actions"] == []


def test_margin_buy_without_cash_to_cover_is_not_converted(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {
            "status": "ok",
            "new_buy_allowed": True,
            "margin_buy_allowed": True,
        },
        "priority_actions": [{
            "ticker": "MA",
            "type": "margin_buy",
            "amount_hint": "3株",
            "action": "MA 3株信用買い",
            "decision_price": 510.0,
            "confidence_pct": 78,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "MA", "current_price": 510.0, "currency": "USD", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 20_000, "usd_as_jpy": 20_000, "jpy_cash": 0},
    )

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action["type"] == "margin_buy"
    assert "margin_buy_converted_to_buy" not in action
    assert result["_filtered_actions"] == []


def test_margin_buy_not_bumped_when_margin_buy_not_allowed(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "aggressive",
        "leverage_health": {
            "status": "deleverage",
            "new_buy_allowed": True,
            "margin_buy_allowed": False,
        },
        "priority_actions": [{
            "ticker": "MA",
            "type": "margin_buy",
            "amount_hint": "1株",
            "action": "MA 1株信用買い",
            "decision_price": 510.0,
            "confidence_pct": 78,
        }],
    }

    result = analyst._phase1_post_filter(
        synthesis,
        30_000_000,
        fx_rate=160.0,
        positions=[{"ticker": "MA", "current_price": 510.0, "currency": "USD", "shares": 0, "value_jpy": 0}],
        cash_info={"total_cash_jpy": 10_000_000, "usd_as_jpy": 9_000_000, "jpy_cash": 1_000_000},
    )

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("leverage_health: margin_buy_allowed=False")


def test_rebalance_cooldown_is_warning_not_filter(monkeypatch):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr("behavioral_guard.is_rebalance_in_cooldown", lambda vix=None: (True, "recent trim"))
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "GLD",
            "type": "trim",
            "amount_jpy": 200_000,
            "action": "GLDを一部利確",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    assert result["_filtered_actions"] == []
    assert result["_annotated_actions"][0]["rebal_cooldown_warning"].startswith("rebalance_cooldown")


def test_policy_rejected_actions_remain_visible_when_all_blocked():
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "priority_actions": [],
        "policy_decision": {"accepted_count": 0, "rejected_count": 1},
        "policy_filtered_actions": [{
            "rule": "_rule_ledger_integrity",
            "reason": "Portfolio Ledger Integrity ok=False",
            "action": {
                "ticker": "META",
                "type": "buy",
                "action": "META 1株を追加購入",
            },
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert result["priority_actions"] == []
    assert result["_filtered_actions"][0]["ticker"] == "META"
    assert result["_filtered_actions"][0]["filtered_reason"].startswith("policy__rule_ledger_integrity")
    assert result["post_filter"]["all_actions_filtered"] is True
    assert "META" in result["telegram_message"]
    assert "参考候補" in result["telegram_message"]


def test_policy_rejected_non_executable_action_keeps_intrinsic_reason(monkeypatch):
    def _tp_get_intrinsic(key, default=None):
        if key == "disable_stop_loss_recommendations":
            return True
        if key == "disable_cumulative_recommendations":
            return True
        return default

    monkeypatch.setattr(tunable_params, "get", _tp_get_intrinsic)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "priority_actions": [],
        "policy_decision": {"accepted_count": 0, "rejected_count": 1},
        "policy_filtered_actions": [{
            "rule": "_rule_ledger_integrity",
            "reason": "Portfolio Ledger Integrity ok=False",
            "action": {
                "ticker": "TXN",
                "type": "stop_loss",
                "amount_hint": "0株（ルール除外）",
                "action": "（除外）TXN の逆指値は手動修正",
                "skip_reason": "stop_loss推奨全銘柄禁止ルール",
            },
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    row = result["_filtered_actions"][0]
    assert row["ticker"] == "TXN"
    assert row["filtered_reason"].startswith("non_executable_action")
    assert row["policy_filtered_reason"].startswith("policy__rule_ledger_integrity")
    assert result["_filtered_action_summary"] == {"non_executable_action": 1}


def test_policy_rejected_disabled_stop_loss_keeps_intrinsic_reason(monkeypatch):
    def _tp_get_intrinsic(key, default=None):
        if key == "disable_stop_loss_recommendations":
            return True
        if key == "disable_cumulative_recommendations":
            return True
        return default

    monkeypatch.setattr(tunable_params, "get", _tp_get_intrinsic)
    synthesis = {
        "overall_stance": "moderately_aggressive",
        "priority_actions": [],
        "policy_decision": {"accepted_count": 0, "rejected_count": 1},
        "policy_filtered_actions": [{
            "rule": "_rule_ledger_integrity",
            "reason": "Portfolio Ledger Integrity ok=False",
            "action": {
                "ticker": "ANET",
                "type": "stop_loss",
                "amount_hint": "1株",
                "action": "ANET の stop loss を更新",
            },
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    row = result["_filtered_actions"][0]
    assert row["ticker"] == "ANET"
    assert row["filtered_reason"].startswith("disable_stop_loss_recommendations")
    assert row["policy_filtered_reason"].startswith("policy__rule_ledger_integrity")


# ── H2契約テスト: 通常buy/add/dca/margin_buyへの単発最大金額ハードキャップ ──────
#
# 背景: 既存の絶対金額capはsource_observe_only(1%)/earnings_blackout(0.5%)/
# tax_loss_harvest_conflict(0.5%)の3特殊ゲートのみに掛かっており、それ以外の
# 通常buy/add/dca/margin_buyには下限(too_small)しかなく上限が無かった。
# H2はAI上書き不可のhard cap(初期値5%)を新設し、continuous DCA(inf)は対象外、
# 金額推定不能(amt<0)は通常buy系でもfail-closedでrejectする。

def _tp_get_h2(key, default=None):
    if key == "disable_cumulative_recommendations":
        return False
    if key == "disable_stop_loss_recommendations":
        return False
    return default


def _h2_run(monkeypatch, actions, portfolio_total=10_000_000):
    _silence_external_filters(monkeypatch)
    monkeypatch.setattr(tunable_params, "get", _tp_get_h2)
    synthesis = {"overall_stance": "neutral", "priority_actions": actions}
    return analyst._phase1_post_filter(synthesis, portfolio_total, positions=[])


def _h2_filtered_reason(result, ticker):
    for f in result["_filtered_actions"]:
        if f.get("ticker") == ticker:
            return f.get("filtered_reason")
    return None


def _h2_kept_tickers(result):
    return [a.get("ticker") for a in result["priority_actions"]]


@pytest.mark.parametrize("atype", ["buy", "add", "dca", "margin_buy"])
def test_ordinary_action_over_max_cap_is_rejected(monkeypatch, atype):
    action = {
        "ticker": "CAPX1", "type": atype, "amount_jpy": 600_000,
        "action": f"CAPX1 を{atype}", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX1" not in _h2_kept_tickers(result)
    reason = _h2_filtered_reason(result, "CAPX1")
    assert reason is not None and reason.startswith("max_single_action_cap")


@pytest.mark.parametrize(
    "atype,amount_jpy",
    [
        ("buy", 250_000),
        ("add", 250_000),
        ("dca", 500_000),
        ("margin_buy", 150_000),
    ],
)
def test_ordinary_action_at_or_under_cap_is_accepted(monkeypatch, atype, amount_jpy):
    action = {
        "ticker": "CAPX2", "type": atype, "amount_jpy": amount_jpy,
        "action": f"CAPX2 を{atype}", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX2" in _h2_kept_tickers(result)
    assert _h2_filtered_reason(result, "CAPX2") is None


def test_individual_buy_uses_tighter_class_cap(monkeypatch):
    action = {
        "ticker": "CAPX_INDIV", "type": "buy", "amount_jpy": 300_000,
        "action": "CAPX_INDIV をbuy", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX_INDIV" not in _h2_kept_tickers(result)
    reason = _h2_filtered_reason(result, "CAPX_INDIV")
    assert reason is not None and "individual" in reason


def test_self_declared_etf_text_does_not_upgrade_cap_class(monkeypatch):
    """AI 出力の asset_class 等の自己申告では core 枠 (¥150万) に昇格しないこと。"""
    action = {
        "ticker": "CAPX_FAKE_ETF", "type": "buy", "amount_jpy": 300_000,
        "asset_class": "ETF", "instrument_type": "index fund 投資信託",
        "action": "CAPX_FAKE_ETF をbuy", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX_FAKE_ETF" not in _h2_kept_tickers(result)
    reason = _h2_filtered_reason(result, "CAPX_FAKE_ETF")
    assert reason is not None and "individual" in reason


def test_core_etf_buy_keeps_core_cap(monkeypatch):
    action = {
        "ticker": "GLD", "type": "buy", "amount_jpy": 500_000,
        "action": "GLD をbuy", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "GLD" in _h2_kept_tickers(result)
    assert _h2_filtered_reason(result, "GLD") is None


def test_short_action_uses_speculative_class_cap(monkeypatch):
    action = {
        "ticker": "CAPX_SHORT", "type": "short", "amount_jpy": 350_000,
        "action": "CAPX_SHORT をshort", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX_SHORT" not in _h2_kept_tickers(result)
    reason = _h2_filtered_reason(result, "CAPX_SHORT")
    assert reason is not None and "short" in reason


@pytest.mark.parametrize("atype", ["sell", "trim", "reduce", "stop_loss", "take_profit"])
def test_exit_actions_not_subject_to_max_cap(monkeypatch, atype):
    action = {
        "ticker": "CAPX3", "type": atype, "amount_jpy": 5_000_000,
        "action": f"CAPX3 を{atype}", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    reason = _h2_filtered_reason(result, "CAPX3")
    assert reason is None or not reason.startswith("max_single_action_cap")


def test_continuous_dca_inf_amount_not_capped(monkeypatch):
    action = {
        "ticker": "CAPX4", "type": "add", "amount_hint": "毎月¥80,000",
        "action": "CAPX4 を毎月¥80,000 積立", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX4" in _h2_kept_tickers(result)
    reason = _h2_filtered_reason(result, "CAPX4")
    assert reason is None or not reason.startswith("max_single_action_cap")


def test_unparseable_amount_ordinary_buy_is_rejected_fail_closed(monkeypatch):
    action = {
        "ticker": "CAPX5", "type": "buy", "amount_hint": "USD 500 相当",
        "action": "CAPX5 を購入", "reason": "test",
    }
    result = _h2_run(monkeypatch, [action])

    assert "CAPX5" not in _h2_kept_tickers(result)
    reason = _h2_filtered_reason(result, "CAPX5")
    assert reason is not None and reason.startswith("max_single_action_cap")


# ── reduce action方向分類: _SELL_LIKE に "reduce" が漏れていた問題 ────────
#
# 背景: action_stage_log.py の _SELL_DIRECTION と behavior_coverage_report.py の
# _SELL_TYPES は "reduce" を sell方向として正しく分類しているが、analyst/__init__.py
# の _direction_of (内部で _SELL_LIKE を参照) には "reduce" が追加されておらず、
# _direction_of("reduce") が "other" を返していた。これにより reduce アクションは
# 7日cooldown重複抑制・DONE_LIST重複抑制・14日flip-warningの対象から漏れる。

def test_direction_of_classifies_reduce_as_sell():
    assert analyst._direction_of("reduce") == "sell"
    assert analyst._direction_of("trim") == "sell"


def test_reduce_action_is_subject_to_same_direction_cooldown(monkeypatch):
    """直近のtrim推奨歴がある銘柄へのreduce推奨は、sell方向の重複としてcooldown対象になる。"""
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [{
        "as_of": (datetime.now()).isoformat(),
        "ticker": "GLD",
        "type": "trim",
    }])
    monkeypatch.setattr(analyst, "_load_cancelled_recommendation_keys", lambda: set())
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr(tunable_params, "get", _tp_get)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [{
            "ticker": "GLD",
            "type": "reduce",
            "amount_jpy": 200_000,
            "action": "GLD を一部reduce",
            "reason": "test",
        }],
    }

    result = analyst._phase1_post_filter(synthesis, 30_000_000)

    assert len(result["priority_actions"]) == 1
    action = result["priority_actions"][0]
    assert action.get("cooldown_duplicate") is True
    assert "cooldown" in (action.get("cooldown_warning") or "")


def test_final_display_ranks_are_contiguous_while_source_ranks_are_retained():
    rows = analyst._reindex_final_action_ranks([
        {"ticker": "1489.T", "rank": 1},
        {"ticker": "ROBO", "rank": 3},
    ])

    assert [row["rank"] for row in rows] == [1, 2]
    assert [row["display_rank"] for row in rows] == [1, 2]
    assert [row["source_rank"] for row in rows] == [1, 3]


def test_fund_quantity_without_jpy_is_never_interpreted_as_nav_times_units():
    assert analyst._estimate_action_jpy(
        {"ticker": "SLIM_ORCAN", "amount_hint": "100口"},
        {"SLIM_ORCAN": {"current_price": 7_514_000, "currency": "JPY"}},
        150,
    ) == -1.0


def test_operational_stance_separates_closed_market_from_model_stance():
    synthesis = {"overall_stance": "aggressive"}

    analyst._set_operational_stance(
        synthesis,
        {"market_closed_reprice_required": 2},
        executable_count=0,
    )

    assert synthesis["overall_stance"] == "aggressive"
    assert synthesis["operational_stance"]["code"] == "await_market_reprice"


def test_operational_stance_without_candidates_or_gate_reasons_is_observe():
    synthesis = {"overall_stance": "neutral"}

    analyst._set_operational_stance(synthesis, {}, executable_count=0)

    assert synthesis["operational_stance"]["code"] == "observe"


def test_operational_stance_low_urgency_exit_only_is_optional():
    synthesis = {"overall_stance": "aggressive"}
    actions = [{
        "ticker": "AVGO",
        "type": "trim",
        "urgency": "low",
        "execution_readiness": "ready",
    }]

    analyst._set_operational_stance(
        synthesis,
        {},
        executable_count=1,
        actions=actions,
    )

    assert synthesis["operational_stance"]["code"] == "optional_exit_only"
    assert synthesis["operational_stance"]["label"] == "任意整理のみ"


def test_operational_stance_ready_buy_remains_actionable():
    synthesis = {"overall_stance": "neutral"}
    actions = [{
        "ticker": "XLF",
        "type": "buy",
        "urgency": "medium",
        "execution_readiness": "ready",
    }]

    analyst._set_operational_stance(
        synthesis,
        {},
        executable_count=1,
        actions=actions,
    )

    assert synthesis["operational_stance"]["code"] == "actionable"
