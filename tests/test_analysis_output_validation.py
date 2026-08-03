from analysis_output_validation import (
    apply_execution_plan_display_conflicts,
    normalize_risk_warning_claims,
    normalize_stance_drawdown_language,
    risk_context_for_prompt,
    synthesis_for_display,
)


def test_prompt_risk_context_never_exposes_synthetic_dd_as_actual() -> None:
    context = risk_context_for_prompt({
        "source": "parquet_reconstruction",
        "var_95_decimal": 0.0095,
        "synthetic_current_dd": -1.12,
        "clean_nav_current_dd_decimal": -0.0303,
        "enforced_flow_adjusted_dd_decimal": None,
        "daily_pnl_decimal": -0.001,
        "rolling_30_pnl_decimal": -0.0233,
        "loss_guard_stage": "ok",
    })

    assert "synthetic_current_dd" not in context
    assert "current_dd" not in context
    drawdowns = context["drawdown_metrics"]
    assert drawdowns["canonical_flow_adjusted"]["authority"] == "unavailable"
    assert drawdowns["synthetic_ex_ante_current_weights"]["decimal"] == -0.0112
    assert drawdowns["synthetic_ex_ante_current_weights"]["may_be_called_actual_dd"] is False
    assert context["loss_guard"]["metric_type"] == "realized_pnl_shock_not_drawdown"


def test_stance_reason_relabels_unavailable_actual_and_clean_shadow_dd() -> None:
    synthesis = {
        "stance_guard_applied": True,
        "stance_guard_detail": {"downgraded_to": "neutral"},
        "stance_reason": (
            "VIX15.99・実DD-1.12%（clean -3.03%）でdefensive強制条件はいずれも未該当、"
            "現金29.4%>3%のためaggressive昇格条件を実データで満たす。"
            "短期警戒はurgencyで対応し、stance の格下げは行わない / "
            "stance_guard: aggressive→neutral"
        )
    }

    normalize_stance_drawdown_language(
        synthesis,
        {"enforced_flow_adjusted_dd_decimal": None},
    )

    assert "実DD" not in synthesis["stance_reason"]
    assert "canonical flow-adjusted DD未確定" in synthesis["stance_reason"]
    assert "clean NAV shadow -3.03%（自動ゲート非使用）" in synthesis["stance_reason"]
    assert "DD由来のdefensive条件は未判定" in synthesis["stance_reason"]
    assert "aggressive昇格条件を実データで満たす" not in synthesis["stance_reason"]
    assert "aggressive昇格条件はcanonical DD欠損のため未確定" in synthesis["stance_reason"]
    assert "最終stanceは決定論的guard補正を優先" in synthesis["stance_reason"]
    assert synthesis["stance_reason_original"].startswith("VIX15.99")
    assert synthesis["stance_reason_dd_validation"]["actual_dd_claims_replaced"] == 1


def test_semiconductor_loss_warning_is_recalculated_from_whole_portfolio() -> None:
    synthesis = {
        "risk_warnings": [
            "半導体集中約24%（NVDA14.5%+AVGO10%）: SMH 20d-8.7%の調整継続なら"
            "推定損は¥250万規模。新規半導体追加は回避。"
        ]
    }
    observation = {
        "denominator_jpy": 29_107_451,
        "positions": [
            {"canonical_instrument_id": "NVDA", "value_jpy": 2_371_630, "weight": 0.081478},
            {"canonical_instrument_id": "AVGO", "value_jpy": 1_962_195, "weight": 0.067412},
        ],
    }

    normalize_risk_warning_claims(synthesis, observation)

    warning = synthesis["risk_warnings"][0]
    assert "NVDA 8.15% + AVGO 6.74% = 14.89%" in warning
    assert "約¥377,043" in warning
    assert "¥250万" not in warning
    validation = synthesis["risk_warning_claim_validation"][0]
    assert validation["status"] == "recalculated"
    assert validation["denominator"] == "whole_portfolio"
    assert validation["estimated_loss_jpy"] == 377_043


def test_unresolvable_loss_claim_is_redacted_instead_of_passed_through() -> None:
    synthesis = {"risk_warnings": ["未知テーマ急落時の推定損失は約¥900万。要注意。"]}

    normalize_risk_warning_claims(synthesis, {"denominator_jpy": 10_000_000, "positions": []})

    assert "¥900万" not in synthesis["risk_warnings"][0]
    assert "表示保留" in synthesis["risk_warnings"][0]
    assert synthesis["risk_warning_claim_validation"][0]["status"] == "amount_redacted_unverified"


def test_display_overlay_repairs_legacy_analysis_without_mutating_it() -> None:
    analysis = {
        "risk_snapshot": {"enforced_flow_adjusted_dd_decimal": None},
        "synthesis": {
            "stance_reason": "実DD-1.12%（clean -3.03%）",
            "risk_warnings": [
                "半導体集中24%（NVDA14.5%+AVGO10%）: SMH 20d-8.7%なら"
                "推定損¥250万。"
            ],
            "investment_policy_observation": {
                "denominator_jpy": 29_107_451,
                "positions": [
                    {"canonical_instrument_id": "NVDA", "value_jpy": 2_371_630, "weight": 0.081478},
                    {"canonical_instrument_id": "AVGO", "value_jpy": 1_962_195, "weight": 0.067412},
                ],
            },
            "priority_actions": [{
                "ticker": "GOOGL",
                "amount_hint": "3株",
                "action": "GOOGLを3株購入（約¥28万）",
                "reason": "最低ロット規則に沿い5株。",
                "estimated_notional_jpy": 166_339,
                "policy_size_applied": {
                    "amount_hint": {"from": "5株", "to": "3株"},
                    "estimated_notional_jpy": {"from": 277_232, "to": 166_339},
                },
            }],
        },
    }

    displayed = synthesis_for_display(analysis)

    assert "canonical flow-adjusted DD未確定" in displayed["stance_reason"]
    assert "約¥377,043" in displayed["risk_warnings"][0]
    assert "約¥166,339" in displayed["priority_actions"][0]["action"]
    assert analysis["synthesis"]["stance_reason"].startswith("実DD")
    assert "¥250万" in analysis["synthesis"]["risk_warnings"][0]
    assert "約¥28万" in analysis["synthesis"]["priority_actions"][0]["action"]


def test_display_overlay_adds_opposite_plan_review_to_cached_exit() -> None:
    synthesis = {
        "priority_actions": [{
            "ticker": "XLF",
            "type": "trim",
            "estimated_notional_jpy": 493_299,
            "execution_readiness": "ready",
            "execution_block_reasons": [],
        }]
    }
    plan_item_id = "2026-08-w32-add-financials-003"
    plan = {
        "items": [{
            "plan_item_id": plan_item_id,
            "status": "active",
            "remaining_jpy": 2_302_672,
            "allowed_action_types": ["buy", "add"],
            "preferred_tickers": ["XLF"],
        }],
        "consumption_summary": {"remaining_opportunity_jpy": 0},
    }

    apply_execution_plan_display_conflicts(synthesis, plan)

    action = synthesis["priority_actions"][0]
    assert action["execution_readiness"] == "review"
    assert action["execution_plan_direction_conflict"] is True
    assert action["execution_plan_conflict_item_ids"] == [plan_item_id]
    assert action["execution_block_reasons"][-1]["code"] == "execution_plan_direction_conflict"
