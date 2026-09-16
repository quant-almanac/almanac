from capital_allocator import (
    allocate_actions,
    allocate_scheduled_broad_actions,
    annotate_post_trade_concentration,
    build_comparison,
    record_comparison,
    review_comparison,
)


def _silence_phase1_external_filters(monkeypatch):
    """Minimal isolation for calling the real _phase1_post_filter in tests.

    Mirrors tests/test_phase1_post_filter.py's own helper; duplicated rather
    than imported because tests/ is not a package here.
    """
    import execution_readiness

    import analyst

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
        execution_readiness, "apply_execution_readiness",
        lambda actions, **kwargs: [a.update({"execution_readiness": "ready",
                                             "execution_block_reasons": []}) for a in actions] and actions,
    )
    monkeypatch.setattr("tunable_params.get", lambda key, default=None: (
        False if key in ("disable_cumulative_recommendations", "disable_stop_loss_recommendations")
        else default
    ))


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


def test_legacy_mode_preserves_existing_ready_actions_across_lanes():
    normal = _buy("V", 4)
    swing = _buy("SWING", 10)
    swing["tier"] = "Swing"
    actions, report = allocate_actions([normal, swing], mode="legacy")

    assert report["mode"] == "legacy"
    assert all(row["execution_readiness"] == "ready" for row in actions)
    assert report["candidate_count"] == 2


def test_enforce_uses_one_household_buy_slot_across_swing_and_long():
    normal = _buy("V", 4)
    swing = _buy("SWING", 10)
    swing["tier"] = "Swing"
    actions, report = allocate_actions([normal, swing], mode="enforce")

    assert report["selected_count"] == 1
    assert sum(row["execution_readiness"] == "ready" for row in actions) == 1
    assert sum(row["execution_readiness"] == "review" for row in actions) == 1


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


def test_open_scheduled_broad_order_same_day_already_consumes_buy_slot(monkeypatch, tmp_path):
    import analyst
    import execution_reconciliation

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        execution_reconciliation,
        "load_effective_execution_records",
        lambda **_kwargs: [{
            "status": "ordered", "saved_at": "2026-08-14T09:00:00",
            "strategy_class": "scheduled_broad_deployment", "direction": "buy",
            "estimated_notional_jpy": 500_000,
        }],
    )
    synthesis = {
        "priority_actions": [_buy("V", 4)],
        "decision_summary": {
            "candidate_count": 1, "executable_count": 1, "review_count": 0,
            "deferred_count": 0, "reason_counts": {}, "count_conservation_ok": True,
        },
        "overall_stance": "neutral",
    }

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="scheduled-broad-open-order", as_of="2026-08-14T12:00:00",
    )

    assert synthesis["priority_actions"][0]["execution_readiness"] == "review"
    assert synthesis["capital_allocator"]["prior_normal_buys_today"] == 1
    assert synthesis["capital_allocator"]["prior_scheduled_actions_this_week"] == 1


def test_scenario_buy_execution_same_day_consumes_household_buy_slot(monkeypatch, tmp_path):
    import analyst
    import execution_reconciliation

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(
        execution_reconciliation,
        "load_effective_execution_records",
        lambda **_kwargs: [{
            "status": "filled", "saved_at": "2026-08-14T09:00:00",
            "strategy_class": "scenario", "direction": "buy",
            "executed_amount_jpy": 200_000,
        }],
    )
    synthesis = {
        "priority_actions": [_buy("V", 4)],
        "decision_summary": {
            "candidate_count": 1, "executable_count": 1, "review_count": 0,
            "deferred_count": 0, "reason_counts": {}, "count_conservation_ok": True,
        },
        "overall_stance": "neutral",
    }
    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="scenario-same-day", as_of="2026-08-14T12:00:00",
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


# --------------------------------------------------------------------------
# F8 (2026-09 objective readiness review): a real policy rejection alone must
# not make the phase-1 -> allocator handoff look unexplainable. These tests
# build ``decision_summary`` from the REAL ``_phase1_post_filter``, not by
# hand-setting ``count_conservation_ok: True`` -- a unit test that feeds the
# allocator a hard-coded True proves nothing about the actual handoff.
# --------------------------------------------------------------------------

def test_phase1_row_id_never_survives_into_the_returned_synthesis(monkeypatch, tmp_path):
    """The internal provenance tag _tag_phase1_row_ids stamps for
    conservation-checking (Finding 1) must be gone by the time phase-1
    returns -- it is pure same-call bookkeeping, never a persisted field."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    analyst._phase1_post_filter(synthesis, 30_639_000)

    for row in synthesis.get("priority_actions") or []:
        assert "_phase1_row_id" not in row
    for row in synthesis.get("_filtered_actions") or []:
        assert "_phase1_row_id" not in row
    for row in synthesis.get("order_intent_deferred_actions") or []:
        assert "_phase1_row_id" not in row
    # The whole synthesis must also round-trip through JSON (a stray
    # internal Python-only value would otherwise fail this silently later).
    import json
    json.dumps(synthesis)


def test_phase1_row_id_never_survives_the_early_return_path(monkeypatch, tmp_path):
    """Same guarantee as above, but for the separate early-return branch
    (every candidate already policy-rejected), which strips
    policy_rejected_rows on its own distinct code path."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [],
        "policy_filtered_actions": [
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1", "action": _buy("SYNTH_A", 4)},
        ],
    }
    analyst._phase1_post_filter(synthesis, 30_639_000)

    assert synthesis.get("_filtered_actions"), "test setup must exercise the early-return branch"
    for row in synthesis["_filtered_actions"]:
        assert "_phase1_row_id" not in row
    import json
    json.dumps(synthesis)


def test_phase1_row_id_never_survives_into_suppressed_reproposals(monkeypatch, tmp_path):
    """2026-09-16 Codex re-review, Finding 3 (P3): synthesis["suppressed_reproposals"]
    is populated via `[dict(action) for action in reproposal_suppressed]` --
    a SEPARATE copy made from an already-tagged action, independent of
    kept/filtered/deferred/annotated. A per-bucket strip cannot reach this
    copy; only a full recursive sweep of the returned synthesis can."""
    import analyst
    import execution_readiness

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    monkeypatch.setattr(analyst, "_load_recent_recommendations", lambda days=14: [])
    monkeypatch.setattr(analyst, "_load_earnings_blackout", lambda within_business_days=5: set())
    monkeypatch.setattr(analyst, "_done_set_by_direction", lambda days=7: set())
    monkeypatch.setattr(analyst, "_recent_order_intents_by_direction", lambda days=7: {}, raising=False)
    monkeypatch.setattr(analyst, "_order_state_conflicts_by_direction", lambda days=7: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_recent_executions", lambda days=14, now=None: [], raising=False)
    monkeypatch.setattr(analyst, "_open_action_state_by_direction", lambda: {}, raising=False)
    monkeypatch.setattr(analyst, "_load_tax_loss_harvest_tickers", lambda min_loss_jpy=30_000: set())
    monkeypatch.setattr("behavioral_guard.is_rebalance_in_cooldown", lambda vix=None: (False, ""))
    monkeypatch.setattr("tunable_params.get", lambda key, default=None: default)
    # Unlike _silence_phase1_external_filters, this candidate must reach
    # phase-1 already "blocked" for the reproposal-suppression path to run.
    monkeypatch.setattr(execution_readiness, "apply_execution_readiness", lambda actions, **kwargs: actions)
    # Bypass the real exchange-calendar session-age computation: force
    # "exactly one session apart" and a fixed recheck date.
    monkeypatch.setattr(analyst, "_reproposal_session_age", lambda ticker, earlier, later: 1)
    monkeypatch.setattr(analyst, "_reproposal_recheck_after", lambda ticker, now: "2026-09-17")

    action = _buy("SYNTH_A", 4)
    scope = analyst._action_scope_for_direction_conflict(action)
    scope_key = "|".join(str(scope.get(key) or "") for key in
                         ("owner", "broker", "account", "investment_type"))
    action["execution_readiness"] = "blocked"
    action["execution_block_reasons"] = [{"code": "cash_insufficient", "scope_key": scope_key}]
    prior_row = {
        "ticker": "SYNTH_A", "type": "add",
        "reproposal_policy_version": "candidate_retry_v1",
        "execution_readiness": "blocked",
        "as_of": "2026-09-15T01:00:00+00:00",
        "reproposal_reason_fingerprint": ["cash_insufficient", scope_key],
        "execution_scope_key": scope_key,
    }
    import json
    (tmp_path / "ai_recommendation_log.json").write_text(json.dumps([prior_row]), encoding="utf-8")

    synthesis = {"overall_stance": "neutral", "priority_actions": [action]}
    from datetime import datetime, timezone
    now = datetime(2026, 9, 16, 6, 0, tzinfo=timezone.utc)
    analyst._phase1_post_filter(synthesis, 30_639_000, base_dir=tmp_path, now=now)

    assert synthesis.get("suppressed_reproposals"), "test setup must actually exercise the suppression path"
    for row in synthesis["suppressed_reproposals"]:
        assert "_phase1_row_id" not in row
    json.dumps(synthesis)


def test_pipeline_reaches_enforce_when_a_policy_rejection_coexists_with_a_kept_action(monkeypatch, tmp_path):
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    kept_action = _buy("SYNTH_A", 4)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [kept_action],       # what the policy gate ACCEPTED
        "policy_filtered_actions": [{             # what the policy gate REJECTED
            "rule": "_rule_dd_stage",
            "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    # This is the real producer -- not a hand-written decision_summary.
    analyst._phase1_post_filter(synthesis, 30_639_000)
    assert synthesis["decision_summary"]["count_conservation_ok"] is True
    assert synthesis["decision_summary"]["candidate_identity_reconciliation"]["ok"] is True
    assert any(a.get("ticker") == "SYNTH_A" for a in synthesis["priority_actions"])

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="f8-policy-rejection-coexists", as_of="2026-09-15T06:15:00",
    )

    # The real allocator was reached and reached ENFORCE -- not the legacy
    # retreat that a stale/miscounted decision_summary used to force.
    assert synthesis["capital_allocator"]["mode"] == "enforce"
    assert synthesis["capital_allocator_comparison"]["explanation_status"] == "explainable"
    assert "decision_count_conservation_unverified" not in (
        synthesis["capital_allocator_comparison"].get("explanation_reasons") or []
    )


def test_pipeline_reaches_enforce_when_every_candidate_was_policy_rejected(monkeypatch, tmp_path):
    """0 accepted, all rejected: the early-return producer path."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [],
        "policy_filtered_actions": [
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1", "action": _buy("SYNTH_A", 4)},
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1", "action": _buy("SYNTH_B", 2)},
        ],
    }
    analyst._phase1_post_filter(synthesis, 30_639_000)
    # This is the early-return producer path: every candidate was already
    # policy-rejected before phase 1 saw anything, so its own
    # candidate_count/filtered_count are both len(policy_rejected_rows) by
    # construction -- the identity check here only needs to confirm the
    # policy stage itself did not record one candidate twice.
    assert synthesis["decision_summary"]["count_conservation_ok"] is True
    assert synthesis["decision_summary"]["candidate_count"] == 2
    assert synthesis["decision_summary"]["filtered_count"] == 2
    assert synthesis["decision_summary"]["candidate_identity_reconciliation"]["ok"] is True


def test_pipeline_falls_back_to_legacy_on_a_genuine_duplicate_identity(monkeypatch, tmp_path):
    """Negative control: a REAL inconsistency must still trigger the legacy
    retreat -- the F8 fix must not make every case look explainable."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    kept_action = _buy("SYNTH_A", 4)
    duplicate_action = _buy("SYNTH_A", 4)          # same identity, genuinely rejected too
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [kept_action],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1",
            "action": duplicate_action,
        }],
    }
    analyst._phase1_post_filter(synthesis, 30_639_000)
    ds = synthesis["decision_summary"]
    assert ds["candidate_identity_reconciliation"]["ok"] is False
    assert "identity_in_both_policy_accepted_and_rejected" in ds["candidate_identity_reconciliation"]["reasons"]
    assert ds["count_conservation_ok"] is False

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="f8-genuine-duplicate-identity", as_of="2026-09-15T06:15:00",
    )
    assert synthesis["capital_allocator"]["mode"] == "legacy"
    assert synthesis["capital_allocator"]["fallback"] == "allocator_comparison_unexplainable"
    assert "decision_count_conservation_unverified" in synthesis["capital_allocator_comparison"]["explanation_reasons"]


def test_pipeline_falls_back_to_legacy_on_a_duplicate_within_policy_accepted_alone(monkeypatch, tmp_path):
    """Negative control, same-side check (1) in _reconcile_candidate_identities:
    two policy-accepted candidates resolving to the same real order, with no
    policy rejection involved at all, must still be caught."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4), _buy("SYNTH_A", 4)],   # same identity, both policy-accepted
    }
    analyst._phase1_post_filter(synthesis, 30_639_000)
    ds = synthesis["decision_summary"]
    assert ds["candidate_identity_reconciliation"]["ok"] is False
    assert "duplicate_identity_within_policy_accepted" in ds["candidate_identity_reconciliation"]["reasons"]
    assert ds["count_conservation_ok"] is False

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="finding1-duplicate-within-accepted", as_of="2026-09-16T06:15:00",
    )
    assert synthesis["capital_allocator"]["mode"] == "legacy"


def test_early_return_path_catches_a_duplicate_within_policy_rejected_alone(monkeypatch, tmp_path):
    """Every candidate was policy-rejected (the early-return producer path),
    and TWO of the rejected rows share one identity -- a genuine problem the
    policy stage itself must not have, independent of anything phase 1 does."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    same_identity_twice = _buy("SYNTH_A", 4)
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [],
        "policy_filtered_actions": [
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1",
             "action": dict(same_identity_twice)},
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1",
             "action": dict(same_identity_twice)},
        ],
    }
    analyst._phase1_post_filter(synthesis, 30_639_000)
    ds = synthesis["decision_summary"]
    assert ds["candidate_identity_reconciliation"]["ok"] is False
    assert "duplicate_identity_within_policy_rejected" in ds["candidate_identity_reconciliation"]["reasons"]
    assert ds["count_conservation_ok"] is False

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="f8-duplicate-in-early-return", as_of="2026-09-15T06:15:00",
    )
    assert synthesis["capital_allocator"]["mode"] == "legacy"


def test_build_comparison_still_falls_back_when_conservation_is_false():
    """Unit-level negative control directly on build_comparison (unchanged
    contract): count_conservation_ok=False alone is still sufficient."""
    legacy = [_buy("SYNTH_A", 4)]
    allocated = [_buy("SYNTH_A", 4)]
    allocated[0]["capital_allocator_selected"] = True
    comparison = build_comparison(
        legacy, allocated, {"mode": "enforce", "legacy_ready_tickers": ["SYNTH_A"], "selected_ticker": "SYNTH_A"},
        count_conservation_ok=False,
    )
    assert comparison["explanation_status"] == "unexplainable"
    assert "decision_count_conservation_unverified" in comparison["explanation_reasons"]


# ── 2026-09 independent review, Finding 1 ──────────────────────────────────
# The F8 fix above still compared semantic (ticker, type, owner, broker,
# account) identity across the phase-1 normalization boundary, which phase-1
# itself is allowed to rewrite (bare "account" -> "execution_account" via
# holdings/NISA-route binding, "buy" -> "add" once an existing holding is
# found). A real Codex repro showed this makes count_conservation_ok flip to
# False -- and the allocator fall back to legacy -- for entirely healthy
# input, purely because the normalization did its job correctly.

def test_phase1_provenance_tag_does_not_mutate_caller_owned_candidate(monkeypatch, tmp_path):
    import copy
    import json

    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)
    original = _buy("SYNTH", 4)
    before = copy.deepcopy(original)
    input_rows = [original]
    synthesis = {"overall_stance": "neutral", "priority_actions": input_rows}

    analyst._phase1_post_filter(synthesis, 30_639_000, side_effects=False)

    assert input_rows[0] is original
    assert original == before
    assert "_phase1_row_id" not in json.dumps(synthesis)
    assert synthesis["decision_summary"]["count_conservation_ok"] is True


def test_bare_account_field_normalizes_and_still_reaches_enforce(monkeypatch, tmp_path):
    """Finding 1, acceptance #1: a real account -> execution_account
    normalization (via holdings binding) must not break conservation."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    kept_action = _buy("SYNTH_A", 4)
    del kept_action["execution_account"]
    kept_action["account"] = "特定"          # bare field; phase-1 must resolve it
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [kept_action],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage",
            "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    positions = [{
        "ticker": "SYNTH_A", "shares": 5, "account": "特定",
        "broker": "broker_a", "owner": "owner_a",
        "current_price": 355, "currency": "USD", "value_jpy": 5 * 355 * 159.452,
    }]
    analyst._phase1_post_filter(synthesis, 30_639_000, positions=positions)
    kept = synthesis["priority_actions"]
    assert any(a.get("ticker") == "SYNTH_A" for a in kept), "SYNTH_A must survive phase-1 to prove the normalization ran"
    v_row = next(a for a in kept if a.get("ticker") == "SYNTH_A")
    assert v_row.get("execution_account") == "特定", "phase-1 must have resolved the bare account field"
    assert synthesis["decision_summary"]["count_conservation_ok"] is True
    assert synthesis["decision_summary"]["candidate_identity_reconciliation"]["ok"] is True

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="finding1-bare-account", as_of="2026-09-16T06:15:00",
    )
    assert synthesis["capital_allocator"]["mode"] == "enforce"
    assert synthesis["capital_allocator_comparison"]["explanation_status"] == "explainable"


def test_buy_to_add_normalization_still_reaches_enforce(monkeypatch, tmp_path):
    """Finding 1, acceptance #2: the buy->add correction
    (_normalize_entry_action_against_holdings) is another legitimate
    Phase-1 rewrite of the same identity fields; conservation must survive
    it too."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)

    kept_action = _buy("SYNTH_A", 4)
    kept_action["type"] = "buy"
    kept_action["action"] = "SYNTH_Aを4株、新規購入"
    kept_action["reason"] = "新規のポジションとして検討"
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [kept_action],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage",
            "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    # An existing holding of SYNTH_A is what makes "新規" (new position) wording
    # incorrect, and triggers the buy->add rewrite.
    positions = [{
        "ticker": "SYNTH_A", "shares": 5, "account": "特定",
        "broker": "broker_a", "owner": "owner_a",
        "current_price": 355, "currency": "USD", "value_jpy": 5 * 355 * 159.452,
    }]
    analyst._phase1_post_filter(synthesis, 30_639_000, positions=positions)
    kept = synthesis["priority_actions"]
    v_rows = [a for a in kept if a.get("ticker") == "SYNTH_A"]
    assert v_rows, "SYNTH_A must survive phase-1 to prove the buy->add rewrite ran"
    assert v_rows[0].get("type") == "add", "phase-1 must have corrected buy->add against the existing holding"
    assert synthesis["decision_summary"]["count_conservation_ok"] is True
    assert synthesis["decision_summary"]["candidate_identity_reconciliation"]["ok"] is True

    analyst._apply_capital_allocator(
        synthesis, _allocator_data(), fx_rate=159.452,
        analysis_id="finding1-buy-to-add", as_of="2026-09-16T06:15:00",
    )
    assert synthesis["capital_allocator"]["mode"] == "enforce"
    assert synthesis["capital_allocator_comparison"]["explanation_status"] == "explainable"


def test_reconcile_candidate_identities_rejects_a_missing_candidate():
    """Finding 1, acceptance #3: a row that vanishes from every outcome
    bucket must not be silently treated as conserved."""
    from analyst import _reconcile_candidate_identities, _tag_phase1_row_ids

    actions = [{"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
                "execution_broker": "broker_a", "execution_account": "特定"},
               {"ticker": "SYNTH_B", "type": "add", "execution_owner": "owner_a",
                "execution_broker": "broker_a", "execution_account": "特定"}]
    _tag_phase1_row_ids(actions, prefix="accepted")
    kept = [dict(actions[0])]   # SYNTH_B's row never made it into any bucket

    ok, reasons = _reconcile_candidate_identities(
        actions=actions, policy_rejected_rows=[], kept=kept, own_filtered=[], deferred=[],
    )
    assert ok is False
    assert "phase1_outcome_provenance_ids_do_not_match_input" in reasons


def test_reconcile_candidate_identities_rejects_a_duplicated_output_row():
    """Finding 1, acceptance #4: the same accepted row appearing twice
    across phase-1's own outcome buckets must be rejected."""
    from analyst import _reconcile_candidate_identities, _tag_phase1_row_ids

    actions = [{"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
                "execution_broker": "broker_a", "execution_account": "特定"}]
    _tag_phase1_row_ids(actions, prefix="accepted")
    kept = [dict(actions[0])]
    deferred = [dict(actions[0])]   # duplicated into a second bucket

    ok, reasons = _reconcile_candidate_identities(
        actions=actions, policy_rejected_rows=[], kept=kept, own_filtered=[], deferred=deferred,
    )
    assert ok is False
    assert "duplicate_provenance_id_across_phase1_outcomes" in reasons


def test_reconcile_candidate_identities_rejects_a_same_count_substitution():
    """Finding 1, acceptance #5: swapping one accepted candidate for an
    unrelated one must be rejected even though the counts still balance."""
    from analyst import _reconcile_candidate_identities, _tag_phase1_row_ids

    actions = [{"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
                "execution_broker": "broker_a", "execution_account": "特定"}]
    _tag_phase1_row_ids(actions, prefix="accepted")
    # Same count (1), but an entirely different, untagged candidate --
    # never assigned a provenance id, so it cannot be confused with SYNTH_A's.
    substituted = {"ticker": "SYNTH_B", "type": "add", "execution_owner": "owner_a",
                   "execution_broker": "broker_a", "execution_account": "特定"}

    ok, reasons = _reconcile_candidate_identities(
        actions=actions, policy_rejected_rows=[], kept=[substituted], own_filtered=[], deferred=[],
    )
    assert ok is False
    assert "phase1_outcome_row_missing_provenance_id" in reasons
    assert "phase1_outcome_provenance_ids_do_not_match_input" in reasons


def test_reconcile_candidate_identities_rejects_an_id_collision():
    """Finding 1, acceptance #6: two different rows carrying the same
    provenance id must not be treated as successfully resolved."""
    from analyst import _reconcile_candidate_identities

    row_a = {"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
             "execution_broker": "broker_a", "execution_account": "特定",
             "_phase1_row_id": "accepted#0"}
    row_b = {"ticker": "SYNTH_B", "type": "add", "execution_owner": "owner_a",
             "execution_broker": "broker_a", "execution_account": "特定",
             "_phase1_row_id": "accepted#0"}   # collision, not a real duplicate

    ok, reasons = _reconcile_candidate_identities(
        actions=[row_a, row_b], policy_rejected_rows=[],
        kept=[row_a, row_b], own_filtered=[], deferred=[],
    )
    assert ok is False
    assert "duplicate_provenance_id_within_policy_accepted" in reasons
    assert "duplicate_provenance_id_across_phase1_outcomes" in reasons


def test_reconcile_candidate_identities_rejects_an_id_collision_within_rejected():
    """Same as the accepted-side collision above, but within
    policy_rejected_rows -- a distinct code path in row_ids()'s per-list
    duplicate check."""
    from analyst import _reconcile_candidate_identities

    row_a = {"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
             "execution_broker": "broker_a", "execution_account": "特定",
             "_phase1_row_id": "rejected#0"}
    row_b = {"ticker": "SYNTH_B", "type": "add", "execution_owner": "owner_a",
             "execution_broker": "broker_a", "execution_account": "特定",
             "_phase1_row_id": "rejected#0"}   # collision, not a real duplicate

    ok, reasons = _reconcile_candidate_identities(
        actions=[], policy_rejected_rows=[row_a, row_b], kept=[], own_filtered=[], deferred=[],
    )
    assert ok is False
    assert "duplicate_provenance_id_within_policy_rejected" in reasons


def test_reconcile_candidate_identities_rejects_a_provenance_id_collision_across_accept_and_reject():
    """A provenance id that (by construction should never, but defensively
    must not silently pass if it does) collides across the accepted and
    rejected sets is a genuine, unresolvable ambiguity."""
    from analyst import _reconcile_candidate_identities

    accepted_row = {"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
                     "execution_broker": "broker_a", "execution_account": "特定",
                     "_phase1_row_id": "shared#0"}
    rejected_row = {"ticker": "SYNTH_B", "type": "add", "execution_owner": "owner_a",
                     "execution_broker": "broker_a", "execution_account": "特定",
                     "_phase1_row_id": "shared#0"}   # same id, different row -- a collision

    ok, reasons = _reconcile_candidate_identities(
        actions=[accepted_row], policy_rejected_rows=[rejected_row],
        kept=[accepted_row], own_filtered=[], deferred=[],
    )
    assert ok is False
    assert "provenance_id_in_both_policy_accepted_and_rejected" in reasons


def test_reconcile_candidate_identities_rejects_a_row_missing_its_provenance_id():
    """Finding 1, acceptance #6 (ID absence, not just collision): a
    dict-shaped input row that somehow reached this function without a
    provenance id must not be silently excluded from consideration -- unlike
    a missing ticker (which _action_identity cannot use either way), a
    missing _phase1_row_id here means the tagging contract itself was
    violated somewhere upstream. _tag_phase1_row_ids makes this unreachable
    via the real _phase1_post_filter (every dict in actions/
    policy_rejected_rows is tagged before anything else runs), so this
    exercises the function's own declared contract directly rather than
    forcing a contrived path through the full pipeline."""
    from analyst import _reconcile_candidate_identities, _tag_phase1_row_ids

    tagged = {"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
              "execution_broker": "broker_a", "execution_account": "特定"}
    untagged = {"ticker": "SYNTH_B", "type": "add", "execution_owner": "owner_a",
                "execution_broker": "broker_a", "execution_account": "特定"}
    _tag_phase1_row_ids([tagged], prefix="accepted")
    # untagged deliberately never goes through _tag_phase1_row_ids.

    ok, reasons = _reconcile_candidate_identities(
        actions=[tagged], policy_rejected_rows=[untagged],
        kept=[tagged], own_filtered=[untagged], deferred=[],
    )
    assert ok is False
    assert "policy_input_row_missing_provenance_id" in reasons


def test_reconcile_candidate_identities_rejects_a_duplicate_real_order_within_kept():
    """A genuine double-order risk: two rows in the FINAL kept set resolve to
    the same real order, even though each has its own distinct provenance
    (e.g. two independently-sourced proposals for the same trade)."""
    from analyst import _reconcile_candidate_identities, _tag_phase1_row_ids

    actions = [
        {"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
         "execution_broker": "broker_a", "execution_account": "特定"},
        {"ticker": "SYNTH_A", "type": "add", "execution_owner": "owner_a",
         "execution_broker": "broker_a", "execution_account": "特定"},
    ]
    _tag_phase1_row_ids(actions, prefix="accepted")

    ok, reasons = _reconcile_candidate_identities(
        actions=actions, policy_rejected_rows=[],
        kept=list(actions), own_filtered=[], deferred=[],
    )
    assert ok is False
    assert "duplicate_real_order_identity_within_kept" in reasons


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
