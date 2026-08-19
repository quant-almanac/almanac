from capital_allocator import (
    allocate_actions,
    allocate_scheduled_broad_actions,
    annotate_post_trade_concentration,
    build_comparison,
    record_comparison,
    review_comparison,
)


def _buy(ticker: str, quantity: int, *, price: float = 355, fx: float = 159.452) -> dict:
    estimated = round(quantity * price * fx)
    return {
        "ticker": ticker,
        "type": "add",
        "tier": "Long",
        "currency": "USD",
        "quantity": quantity,
        "requested_buy_quantity": quantity,
        "decision_price": price,
        "estimated_notional_jpy": estimated,
        "amount_hint": f"{quantity}株",
        "action": f"{ticker}を{quantity}株、約¥{estimated:,}で買付",
        "reason": f"{quantity}株を約¥{estimated:,}で通常買付",
        "execution_readiness": "ready",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_account": "特定",
        "confidence_pct": 70,
    }


def _allocator_data() -> dict:
    return {
        "portfolio_total": 30_639_000,
        "investment_policy_observation": {
            "denominator_jpy": 30_639_000,
            "positions": [],
        },
    }


def test_frozen_v_fixture_keeps_four_shares_and_rejects_five_over_normal_cap():
    v = _buy("V", 4)
    v["execution_plan_item_id"] = "usd-financials"
    actions, report = allocate_actions([v, _buy("OTHER", 5)], fx_rate=159.452)

    selected = next(row for row in actions if row["ticker"] == "V")
    rejected = next(row for row in actions if row["ticker"] == "OTHER")
    assert selected["execution_readiness"] == "ready"
    assert selected["quantity"] == 4
    assert report["selected_ticker"] == "V"
    assert rejected["execution_readiness"] == "review"
    assert any(row["code"] == "capital_allocator_daily_buy_limit" for row in rejected["execution_block_reasons"])


def test_allocator_resizes_within_cap_but_never_forces_below_minimum():
    # $355 * 159.452: 5 shares exceed ¥250k, 4 shares remain above the floor.
    actions, report = allocate_actions([_buy("V", 5)], fx_rate=159.452)
    row = actions[0]
    assert report["selected_ticker"] == "V"
    assert row["quantity"] == 4
    assert row["requested_buy_quantity"] == 4
    assert row["amount_hint"] == "4株"
    assert row["estimated_notional_jpy"] == 226_422
    assert "4株" in row["action"]
    assert "¥226,422" in row["action"]

    tiny, tiny_report = allocate_actions([_buy("V", 1, price=2_000)], fx_rate=159.452)
    assert tiny_report["selected_ticker"] is None
    assert tiny[0]["execution_readiness"] == "review"
    assert any(row["code"] == "capital_allocator_quantity_below_minimum" for row in tiny[0]["execution_block_reasons"])


def test_allocator_preserves_jpx_regular_lot_when_capping_quantity():
    toyota = _buy("7203.T", 200, price=1_500, fx=1)
    toyota["currency"] = "JPY"
    toyota["estimated_notional_jpy"] = 300_000
    toyota["action"] = "7203.Tを200株、約¥300,000で買付"
    toyota["reason"] = "200株を約¥300,000で通常買付"

    actions, report = allocate_actions([toyota], fx_rate=1, min_trade_jpy=100_000)
    row = actions[0]
    assert report["selected_ticker"] == "7203.T"
    assert row["quantity"] == 100
    assert row["amount_hint"] == "100株"
    assert row["estimated_notional_jpy"] == 150_000


def test_scheduled_broad_uses_the_same_one_household_buy_slot():
    first = _buy("VTI", 10, price=300, fx=150)
    second = _buy("VT", 10, price=300, fx=150)
    third = _buy("VOO", 10, price=300, fx=150)
    for row in (first, second, third):
        row["source"] = "scheduled_broad_deployment"
    actions, report = allocate_scheduled_broad_actions(
        [first, second, third], fx_rate=150, min_trade_jpy=50_000,
    )
    assert report["selected_count"] == 1
    assert all(row["estimated_notional_jpy"] <= 500_000 for row in actions if row.get("scheduled_broad_selected"))
    reviewed = [row for row in actions if row["execution_readiness"] == "review"]
    assert len(reviewed) == 2
    assert all(
        any(reason["code"] == "capital_allocator_daily_buy_limit" for reason in row["execution_block_reasons"])
        for row in reviewed
    )


def test_broad_and_individual_candidates_compete_in_one_ranking():
    broad = _buy("VT", 10, price=300, fx=150)
    broad.update({
        "source": "scheduled_broad_deployment",
        "strategy_class": "scheduled_broad_deployment",
        "plan_item_id": "broad-plan",
        "objective_gap_closure_jpy": 450_000,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    })
    individual = _buy("V", 4)
    individual.update({"plan_item_id": "individual-plan", "objective_gap_closure_jpy": 226_000})

    actions, report = allocate_actions(
        [individual, broad], fx_rate=150, min_trade_jpy=50_000,
    )

    assert report["selected_ticker"] == "VT"
    assert next(row for row in actions if row["ticker"] == "VT")["scheduled_broad_selected"] is True
    assert next(row for row in actions if row["ticker"] == "V")["execution_readiness"] == "review"


def test_concentration_annotation_downgrades_caution_before_ranking():
    action = _buy("V", 4, price=355, fx=150)
    annotated = annotate_post_trade_concentration(
        [action],
        policy_observation={
            "denominator_jpy": 10_000_000,
            "positions": [{
                "canonical_instrument_id": "V", "value_jpy": 700_000,
                "cap_basis_tier": "long",
            }],
        },
        fx_rate=150,
    )
    assert annotated[0]["post_trade_concentration_decimal"] > 0.08
    assert annotated[0]["execution_readiness"] == "review"
    assert any(
        row["code"] == "capital_allocator_concentration_caution"
        for row in annotated[0]["execution_block_reasons"]
    )


def test_legacy_mode_preserves_existing_ready_actions_and_swing_is_outside_normal_cap():
    normal = _buy("V", 4)
    swing = _buy("SWING", 10)
    swing["tier"] = "Swing"
    actions, report = allocate_actions([normal, swing], mode="legacy")

    assert report["mode"] == "legacy"
    assert all(row["execution_readiness"] == "ready" for row in actions)
    assert report["candidate_count"] == 1


def test_allocator_comparison_review_is_explicit_and_side_effect_free(tmp_path):
    record_comparison("analysis-1", {"mode": "enforce"}, base_dir=tmp_path)
    reviewed = review_comparison("analysis-1", "approved", base_dir=tmp_path)
    assert reviewed["review"]["decision"] == "approved"


def test_allocator_preserves_jpx_regular_lot_when_capping_quantity():
    toyota = _buy("7203.T", 200, price=1_500, fx=1)
    toyota.update({"currency": "JPY", "estimated_notional_jpy": 300_000,
                   "action": "7203.Tを200株、約¥300,000で買付", "reason": "200株を約¥300,000で通常買付"})
    actions, report = allocate_actions([toyota], fx_rate=1, min_trade_jpy=100_000)
    assert report["selected_ticker"] == "7203.T"
    assert actions[0]["quantity"] == 100
    assert actions[0]["amount_hint"] == "100株"


def test_allocator_comparison_rejects_unstructured_selected_quantity_change():
    legacy = [_buy("V", 5)]
    allocated = [_buy("V", 4)]
    allocated[0].update({"capital_allocator_selected": True, "capital_allocator_size_applied": {"quantity": {"from": 5, "to": 4}}})
    comparison = build_comparison(legacy, allocated, {"mode": "enforce", "legacy_ready_tickers": ["V"], "selected_ticker": "V"}, count_conservation_ok=True)
    assert comparison["explanation_status"] == "explainable"
    allocated[0].pop("capital_allocator_size_applied")
    comparison = build_comparison(legacy, allocated, {"mode": "enforce", "legacy_ready_tickers": ["V"], "selected_ticker": "V"}, count_conservation_ok=True)
    assert "allocator_quantity_change_unexplained" in comparison["explanation_reasons"]


def test_analyst_integration_updates_decision_summary_after_allocator(monkeypatch, tmp_path):
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    synthesis = {
        "priority_actions": [_buy("V", 4), _buy("OTHER", 4)],
        "decision_summary": {"candidate_count": 2, "executable_count": 2, "review_count": 0, "deferred_count": 0, "reason_counts": {}, "count_conservation_ok": True},
        "overall_stance": "neutral",
    }
    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="analysis-allocator-test", as_of="2026-08-14T06:15:00",
    )

    assert synthesis["capital_allocator"]["mode"] == "enforce"
    assert synthesis["decision_summary"]["executable_count"] == 1
    assert synthesis["decision_summary"]["review_count"] == 1
    assert synthesis["capital_allocator_comparison"]["run_id"] == "analysis-allocator-test"


def test_scheduled_broad_execution_same_day_blocks_ordinary_allocator(monkeypatch, tmp_path):
    import analyst
    import execution_reconciliation

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        execution_reconciliation,
        "load_effective_execution_records",
        lambda **_kwargs: [{
            "status": "executed", "saved_at": "2026-08-14T09:00:00",
            "strategy_class": "scheduled_broad_deployment", "direction": "buy",
            "executed_amount_jpy": 500_000,
        }],
    )
    synthesis = {
        "priority_actions": [_buy("V", 4)],
        "decision_summary": {"candidate_count": 1, "executable_count": 1, "review_count": 0, "deferred_count": 0, "reason_counts": {}, "count_conservation_ok": True},
        "overall_stance": "neutral",
    }
    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="scheduled-broad-same-day", as_of="2026-08-14T12:00:00",
    )
    assert synthesis["priority_actions"][0]["execution_readiness"] == "review"
    assert synthesis["capital_allocator"]["prior_normal_buys_today"] == 1


def test_allocator_comparison_write_failure_keeps_legacy_actions(monkeypatch, tmp_path):
    import analyst
    import capital_allocator

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(capital_allocator, "record_comparison", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")))
    synthesis = {
        "priority_actions": [_buy("V", 4), _buy("OTHER", 4)],
        "decision_summary": {
            "candidate_count": 2, "executable_count": 2, "review_count": 0,
            "deferred_count": 0, "reason_counts": {}, "count_conservation_ok": True,
        },
        "overall_stance": "neutral",
    }
    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="comparison-write-failure", as_of="2026-08-14T06:15:00",
    )
    assert [row["execution_readiness"] for row in synthesis["priority_actions"]] == ["ready", "ready"]
    assert synthesis["capital_allocator"]["mode"] == "legacy"
    assert synthesis["capital_allocator"]["fallback"] == "allocator_error_kept_existing_post_filter_result"


def test_allocator_does_not_double_count_existing_readiness_reasons(monkeypatch, tmp_path):
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    already_review = _buy("REVIEW", 4)
    already_review["execution_readiness"] = "review"
    already_review["execution_block_reasons"] = [{"code": "existing_gate", "message": "already reviewed"}]
    synthesis = {
        "priority_actions": [_buy("V", 4), _buy("OTHER", 4), already_review],
        "decision_summary": {
            "candidate_count": 3, "executable_count": 2, "review_count": 1,
            "deferred_count": 0, "reason_counts": {"existing_gate": 1}, "count_conservation_ok": True,
        },
        "overall_stance": "neutral",
    }
    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="reason-count-test", as_of="2026-08-14T06:15:00",
    )
    assert synthesis["decision_summary"]["reason_counts"]["existing_gate"] == 1
    assert synthesis["decision_summary"]["reason_counts"]["capital_allocator_daily_buy_limit"] == 1


def test_readiness_narrative_separates_analysis_from_preflight_state():
    import analyst

    synthesis = {
        "weekly_theme": "強気相場で押し目を拾う",
        "priority_actions": [{
            "ticker": "VT", "type": "buy", "execution_readiness": "ready",
        }],
    }
    analyst._rebuild_readiness_narrative(synthesis)

    assert synthesis["analytical_summary"] == "強気相場で押し目を拾う"
    assert synthesis["weekly_theme"] == "分析選定済み・発注前preflight待ち: VT buy"
    assert synthesis["executable_plan_summary"]["analysis_ready_count"] == 1
    assert synthesis["executable_plan_summary"]["preflight_pending_count"] == 1
    assert synthesis["executable_plan_summary"]["executable_now_count"] == 0
    assert synthesis["selection_consistency"]["status"] == "ok"
