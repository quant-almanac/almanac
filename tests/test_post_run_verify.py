import json
import sys
import types

import post_run_verify as prv


def _write(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_check_vix_consistency_flags_stale_classification(tmp_path):
    _write(tmp_path / "vix_state.json", {"vix": {"level": 16.59, "classification": "ELEVATED"}})
    _write(tmp_path / "market_snapshot.json", {"VIX": {"price": 16.59, "level": "CALM"}})

    issues = prv.check_vix_consistency(tmp_path)

    codes = {i["code"] for i in issues}
    assert "vix_state_classification_mismatch" in codes
    assert "vix_sources_disagree" in codes


def test_check_scenario_null_signals_flags_critical_keys(tmp_path):
    _write(tmp_path / "scenario_state.json", {
        "scenarios": {
            "bull_pullback": {
                "signal_details": [
                    {"key": "SPY_above_MA50", "detail": "SPY データ未取得"},
                    {"key": "regime_bull_confirmed", "detail": "データ未取得"},
                    {"key": "vix", "detail": "vix 16.5 < 25"},
                ]
            }
        }
    })

    issues = prv.check_scenario_null_signals(tmp_path, max_null_ratio=0.1)

    assert any(i["code"] == "scenario_critical_signal_missing" for i in issues)
    assert any(i["code"] == "scenario_null_signal_ratio_high" for i in issues)


def test_check_action_state_alignment_uses_dedup_key(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {
            "final_priority_actions": [
                {"ticker": "META", "type": "buy", "action": "META 1株を買い", "reason": "test"},
            ]
        }
    })
    _write(tmp_path / "action_state.json", {
        "actions": {
            "abc": {
                "ticker": "META",
                "action_type": "add",
                "status": "pending",
                "action_detail": "META 1株を買い",
                "reason": "test",
            }
        }
    })

    assert prv.check_action_state_alignment(tmp_path) == []


def test_check_action_state_alignment_accepts_filled_lifecycle_entry(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {
            "final_priority_actions": [
                {"ticker": "AVGO", "type": "trim", "action": "一般口座AVGO 3株売却", "reason": "test"},
            ]
        }
    })
    _write(tmp_path / "action_state.json", {
        "actions": {
            "filled": {
                "ticker": "AVGO",
                "action_type": "trim",
                "status": "filled",
                "action_detail": "一般口座AVGO 3株売却",
                "reason": "test",
            }
        }
    })

    assert prv.check_action_state_alignment(tmp_path) == []


def test_check_action_state_alignment_flags_missing_pending(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {
            "final_priority_actions": [
                {"ticker": "META", "type": "buy", "action": "META 1株を買い"},
            ]
        }
    })
    _write(tmp_path / "action_state.json", {"actions": {}})

    issues = prv.check_action_state_alignment(tmp_path)

    assert issues[0]["code"] == "priority_actions_not_registered_pending"
    assert issues[0]["severity"] == "error"


def test_check_observability_logs_flags_missing_files(tmp_path):
    issues = prv.check_observability_logs(tmp_path)

    assert len(issues) == 3
    assert {i["code"] for i in issues} == {"observability_log_missing"}


def test_check_agent_reliability_join_flags_zero_overlap_when_logs_are_mature(tmp_path):
    attr_rows = []
    outcome_rows = []
    for i in range(10):
        attr_rows.append({
            "hypothesis_id": f"attr-{i}",
            "agent": "opus_final",
            "role": "final_decider",
            "stance": "support",
            "final_candidate_status": "adopted",
        })
        outcome_rows.append({"hypothesis_id": f"out-{i}", "horizon_days": 10})
    (tmp_path / "agent_attribution_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in attr_rows) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "catalyst_outcome_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in outcome_rows) + "\n",
        encoding="utf-8",
    )

    issues = prv.check_agent_reliability_join(tmp_path)

    assert issues[0]["code"] == "agent_reliability_join_zero"
    assert issues[0]["severity"] == "warning"
    assert issues[0]["context"]["attribution_unique_ids"] == 10


def test_check_agent_reliability_join_accepts_nonzero_overlap(tmp_path):
    attr_rows = []
    outcome_rows = []
    for i in range(10):
        hid = f"h-{i}"
        attr_rows.append({
            "hypothesis_id": hid,
            "agent": "opus_final",
            "role": "final_decider",
            "stance": "support",
            "final_candidate_status": "adopted",
        })
        outcome_rows.append({"hypothesis_id": hid if i == 0 else f"out-{i}", "horizon_days": 10})
    (tmp_path / "agent_attribution_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in attr_rows) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "catalyst_outcome_log.jsonl").write_text(
        "\n".join(json.dumps(row) for row in outcome_rows) + "\n",
        encoding="utf-8",
    )

    assert prv.check_agent_reliability_join(tmp_path) == []


def test_check_absent_action_rationales_flags_missing_margin_and_short_reasons(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {
            "priority_actions": [{"ticker": "V", "type": "buy"}],
            "short_opportunities": [],
        }
    })
    _write(tmp_path / "short_candidates.json", {
        "scanned": 76,
        "shortable_count": 0,
        "candidates": [],
    })
    _write(tmp_path / "margin_long_candidates.json", {
        "candidates": [{"ticker": "MA"}],
    })

    issues = prv.check_absent_action_rationales(tmp_path)

    codes = {issue["code"] for issue in issues}
    assert "margin_no_buy_rationale_missing" in codes
    assert "short_no_action_rationale_missing" in codes
    assert all(issue["severity"] == "warning" for issue in issues)


def test_check_absent_action_rationales_accepts_present_reasons(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {
            "priority_actions": [{"ticker": "V", "type": "buy"}],
            "margin_no_buy_rationale": ["margin_long_candidates=1"],
            "short_no_action_rationale": ["short_candidates=0", "shortable_count=0"],
        }
    })
    _write(tmp_path / "short_candidates.json", {
        "scanned": 76,
        "shortable_count": 0,
        "candidates": [],
    })
    _write(tmp_path / "margin_long_candidates.json", {
        "candidates": [{"ticker": "MA"}],
    })

    assert prv.check_absent_action_rationales(tmp_path) == []


def test_check_decision_summary_conservation_accepts_ready_review_and_deferred(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {"synthesis": {
        "priority_actions": [
            {"ticker": "A", "type": "buy", "execution_readiness": "ready"},
            {"ticker": "B", "type": "buy", "execution_readiness": "blocked"},
        ],
        "_filtered_actions": [{"ticker": "C", "type": "buy"}],
        "order_intent_deferred_actions": [{"ticker": "D", "type": "sell"}],
        "decision_summary": {
            "candidate_count": 4, "executable_count": 1, "review_count": 2,
            "filtered_count": 1, "deferred_count": 1,
            "policy_accepted_count": 4, "policy_rejected_seed_count": 0,
            "count_conservation_ok": True,
        },
    }})

    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_check_decision_summary_conservation_flags_missing_readiness_and_bad_counts(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {"synthesis": {
        "priority_actions": [{"ticker": "A", "type": "buy"}],
        "decision_summary": {
            "candidate_count": 0, "executable_count": 0, "review_count": 0,
            "filtered_count": 0, "deferred_count": 0,
            "policy_accepted_count": 1, "policy_rejected_seed_count": 0,
            "count_conservation_ok": False,
        },
    }})

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert {issue["code"] for issue in issues} == {
        "priority_action_readiness_missing", "decision_summary_count_mismatch",
    }


def test_check_decision_summary_conservation_rejects_a_bool_masquerading_as_a_count(tmp_path):
    """`stored != real_value` alone lets a JSON boolean pass as its numeric
    value (`True == 1`, `False == 0`), silently accepting a tampered/corrupted
    `"filtered_count": true` as correct -- the exact class of bug the newer
    `_usable_nonneg_int` guard exists to prevent for policy_accepted_count/
    policy_rejected_seed_count, but which was not applied to the four older
    count fields until now (2026-09 review)."""
    _write(tmp_path / "ai_portfolio_analysis.json", {"synthesis": {
        "priority_actions": [{"ticker": "A", "type": "buy", "execution_readiness": "ready"}],
        "_filtered_actions": [{"ticker": "C", "type": "buy"}],
        "decision_summary": {
            "candidate_count": 2, "executable_count": 1, "review_count": 0,
            "filtered_count": True,  # real count is 1; True == 1 must not pass
            "deferred_count": 0,
            "policy_accepted_count": 2, "policy_rejected_seed_count": 0,
            "count_conservation_ok": True,
        },
    }})

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert "decision_summary_count_mismatch" in {issue["code"] for issue in issues}


# ── 2026-09 independent review, Finding 2 ──────────────────────────────────
# check_decision_summary_conservation() independently recomputes
# candidate_count from priority_actions + _filtered_actions +
# order_intent_deferred_actions and compares it to decision_summary's own
# candidate_count. _filtered_actions mixes the policy stage's rejects with
# phase-1's own filtering, but candidate_count only counts the
# policy-accepted input -- so this check false-flagged every healthy
# analysis where the policy stage rejected anything. These tests exercise
# the REAL producer (analyst._phase1_post_filter) through an actual JSON
# round-trip, not a hand-built decision_summary, per the review's explicit
# requirement not to trust a hardcoded producer output.

def _silence_phase1_external_filters(monkeypatch):
    """Duplicated from tests/test_capital_allocator.py's own helper; tests/
    is not a package here, so this cannot be imported instead."""
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
        "ticker": ticker, "type": "add", "tier": "Long", "currency": "USD",
        "quantity": quantity, "requested_buy_quantity": quantity, "decision_price": price,
        "estimated_notional_jpy": estimated, "amount_hint": f"{quantity}株",
        "action": f"{ticker}を{quantity}株、約¥{estimated:,}で買付",
        "reason": f"{quantity}株を約¥{estimated:,}で通常買付",
        "execution_readiness": "ready", "execution_owner": "owner_a",
        "execution_broker": "broker_a", "execution_account": "特定", "confidence_pct": 70,
    }


def _run_real_producer_and_save(monkeypatch, tmp_path, synthesis: dict, *, portfolio_total=30_639_000) -> None:
    """Real analyst._phase1_post_filter, then an actual JSON round-trip
    through the same file post_run_verify reads -- not a hand-built
    decision_summary."""
    import analyst

    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    _silence_phase1_external_filters(monkeypatch)
    analyst._phase1_post_filter(synthesis, portfolio_total, base_dir=tmp_path)
    _write(tmp_path / "ai_portfolio_analysis.json", {"synthesis": synthesis})


def test_real_policy_modification_is_not_counted_as_a_rejection(monkeypatch, tmp_path):
    import policy_engine

    def modify(action, context):
        return "modify", {**action, "urgency": "low"}, "synthetic modification"

    monkeypatch.setattr(policy_engine, "RULES", [modify])
    decision = policy_engine.apply_policy_gate([_buy("SYNTH", 4)], policy_engine.PolicyContext())
    assert len(decision.accepted) == len(decision.modified) == 1
    assert decision.rejected == []
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": decision.accepted,
        "policy_filtered_actions": decision.rejected + decision.modified,
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    assert synthesis["decision_summary"]["policy_rejected_seed_count"] == 0
    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_real_producer_roundtrip_accepts_a_healthy_accept_and_reject_mix(monkeypatch, tmp_path):
    """Finding 2, acceptance #1: policy-accepted + policy-rejected coexisting
    must not false-flag once run through a real save/load round-trip."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)

    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_real_producer_roundtrip_accepts_all_candidates_policy_rejected(monkeypatch, tmp_path):
    """Finding 2, acceptance #2: the early-return producer path (0 accepted,
    all rejected) must also round-trip clean."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [],
        "policy_filtered_actions": [
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1", "action": _buy("SYNTH_A", 4)},
            {"rule": "_rule_dd_stage", "reason": "loss_guard_stage=stage_1", "action": _buy("SYNTH_B", 2)},
        ],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)

    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_real_producer_roundtrip_accepts_no_rejection_at_all(monkeypatch, tmp_path):
    """Finding 2, acceptance #2: the ordinary case with no policy rejection
    (policy_rejected_seed_count == 0) must still round-trip clean."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)

    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_real_producer_roundtrip_accepts_a_deferred_candidate(monkeypatch, tmp_path):
    """Finding 2, acceptance #2: a deferred (too-small notional) candidate
    alongside an accept+reject mix must still round-trip clean."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4), _buy("AAPL", 2, price=470)],  # AAPL: near-minimum notional -> deferred
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)

    saved = json.loads((tmp_path / "ai_portfolio_analysis.json").read_text(encoding="utf-8"))
    assert saved["synthesis"]["decision_summary"]["deferred_count"] >= 1, "test setup must actually defer AAPL"
    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_real_producer_roundtrip_detects_a_candidate_dropped_after_the_fact(monkeypatch, tmp_path):
    """Finding 2, acceptance #3: tampering with the SAVED artifact by
    dropping a kept candidate (without updating decision_summary) must be
    caught -- via the executable_count/review_count fields that a pure drop
    still perturbs, since candidate_count's own total is drop/replace-count
    invariant by construction."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["synthesis"]["priority_actions"], "test setup must have a kept candidate to drop"
    saved["synthesis"]["priority_actions"] = []   # drop it; decision_summary is left stale
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert any(issue["code"] == "decision_summary_count_mismatch" for issue in issues)


def test_real_producer_roundtrip_detects_a_duplicated_filtered_row(monkeypatch, tmp_path):
    """Finding 2, acceptance #3: duplicating a row within the saved
    _filtered_actions list must be caught via filtered_count."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["synthesis"]["_filtered_actions"], "test setup must have a filtered candidate to duplicate"
    saved["synthesis"]["_filtered_actions"] = saved["synthesis"]["_filtered_actions"] * 2
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert any(issue["code"] == "decision_summary_count_mismatch" for issue in issues)


def test_real_producer_roundtrip_does_not_catch_a_same_status_substitution(monkeypatch, tmp_path):
    """Honest limitation, not a regression: this checker only recomputes
    AGGREGATE counts from the saved lists, so swapping one filtered
    candidate for a different one that keeps every count identical is
    invisible to it. Catching that would need a persisted per-candidate
    identity in the artifact, which is a new shared-contract addition beyond
    this bug fix's scope (flagged in the handoff report, not implemented
    here). This test documents the gap rather than silently leaving it
    unstated."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["synthesis"]["_filtered_actions"][0]["ticker"] == "SYNTH_B"
    saved["synthesis"]["_filtered_actions"][0] = {
        **saved["synthesis"]["_filtered_actions"][0], "ticker": "NVDA",
    }
    _write(path, saved)

    assert prv.check_decision_summary_conservation(tmp_path) == []


def test_real_producer_roundtrip_detects_summary_only_tampering(monkeypatch, tmp_path):
    """Finding 2, acceptance #4: corrupting decision_summary alone (without
    touching the underlying lists) must still be caught."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["synthesis"]["decision_summary"]["count_conservation_ok"] = True
    saved["synthesis"]["decision_summary"]["policy_accepted_count"] = 99
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert any(issue["code"] == "decision_summary_count_mismatch" for issue in issues)


def test_real_producer_roundtrip_trusts_a_producer_reported_false_even_when_arithmetic_balances(monkeypatch, tmp_path):
    """The producer's own count_conservation_ok can legitimately be False
    even when every persisted count still adds up: it also reflects the
    same-side semantic checks and the row-id conservation check, neither of
    which this file can independently re-derive from a persisted artifact
    (the per-row provenance id is intentionally never persisted). A
    scope-usable artifact must not silently drop that producer-reported
    signal just because the arithmetic alone looks fine."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    ds = saved["synthesis"]["decision_summary"]
    assert ds["count_conservation_ok"] is True, "test setup must start from a healthy, arithmetically-balanced artifact"
    ds["count_conservation_ok"] = False   # only this changes; every count is left untouched
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert any(issue["code"] == "decision_summary_count_mismatch" for issue in issues)
    mismatch = next(i for i in issues if i["code"] == "decision_summary_count_mismatch")
    assert mismatch["context"]["mismatches"] == {}, "the arithmetic itself must still look balanced"


def test_legacy_artifact_without_seed_count_is_unverifiable_not_confirmed_either_way(tmp_path):
    """Finding 2, acceptance #5: an artifact saved before
    policy_accepted_count/policy_rejected_seed_count existed must be labeled
    unverifiable, and that label must be distinct from both 'confirmed
    healthy' and 'confirmed broken' -- never promoted to verified without
    grounds, and never treated as a confirmed error either, since the
    pre-fix producer's own count_conservation_ok was itself known to
    false-flag healthy runs."""
    _write(tmp_path / "ai_portfolio_analysis.json", {"synthesis": {
        "priority_actions": [{"ticker": "SYNTH_A", "type": "add", "execution_readiness": "ready"}],
        "_filtered_actions": [{"ticker": "SYNTH_B", "type": "add"}],
        "decision_summary": {
            # No policy_accepted_count/policy_rejected_seed_count keys at
            # all -- pre-Finding-2 shape.
            "candidate_count": 1, "executable_count": 1, "review_count": 0,
            "filtered_count": 1, "deferred_count": 0,
            "count_conservation_ok": False,  # the historically-unreliable claim
        },
    }})

    issues = prv.check_decision_summary_conservation(tmp_path)
    codes = {issue["code"] for issue in issues}
    assert "decision_summary_conservation_unverifiable_legacy_format" in codes
    assert "decision_summary_count_mismatch" not in codes
    unverifiable = next(i for i in issues if i["code"] == "decision_summary_conservation_unverifiable_legacy_format")
    assert unverifiable["severity"] == "warning"


# ── 2026-09-16 Codex re-review of Finding 2 (P2) ───────────────────────────
# A present-but-invalid scope field pair was being folded into the same
# "unverifiable legacy format" warning as a genuinely absent one, silently
# dropping the producer's own count_conservation_ok=False report. A
# corrupted MODERN-format artifact is a confirmed defect, not an open
# question, and must surface as an error even when every other field looks
# fine.

def test_real_producer_roundtrip_detects_an_invalid_seed_count_as_an_error_not_legacy(monkeypatch, tmp_path):
    """The exact repro Codex used: start from a healthy, modern-format
    artifact and corrupt policy_rejected_seed_count to a negative value
    while also reporting count_conservation_ok=False. Both fields are
    PRESENT (one valid, one not) -- this must not be downgraded to the
    legacy-format warning, and the producer's False must not be dropped."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["synthesis"]["decision_summary"]["policy_accepted_count"] == 1, "test setup sanity check"
    saved["synthesis"]["decision_summary"]["policy_rejected_seed_count"] = -1
    saved["synthesis"]["decision_summary"]["count_conservation_ok"] = False
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    codes = {issue["code"] for issue in issues}
    assert "decision_summary_conservation_unverifiable_legacy_format" not in codes
    assert "decision_summary_scope_fields_invalid" in codes
    invalid = next(i for i in issues if i["code"] == "decision_summary_scope_fields_invalid")
    assert invalid["severity"] == "error"
    assert invalid["context"]["count_conservation_ok"] is False


def test_partial_scope_fields_is_treated_as_invalid_not_legacy(tmp_path):
    """Only one of the two paired fields present is not the shape any real
    producer (old or new) writes -- treat it the same as a corrupted
    modern-format artifact (error), not as a genuinely old one (warning)."""
    _write(tmp_path / "ai_portfolio_analysis.json", {"synthesis": {
        "priority_actions": [{"ticker": "SYNTH_A", "type": "add", "execution_readiness": "ready"}],
        "decision_summary": {
            "candidate_count": 1, "executable_count": 1, "review_count": 0,
            "filtered_count": 0, "deferred_count": 0,
            "policy_accepted_count": 1,   # present
            # policy_rejected_seed_count: absent
            "count_conservation_ok": True,
        },
    }})

    issues = prv.check_decision_summary_conservation(tmp_path)
    codes = {issue["code"] for issue in issues}
    assert "decision_summary_scope_fields_invalid" in codes
    assert "decision_summary_conservation_unverifiable_legacy_format" not in codes


def test_real_producer_roundtrip_detects_a_same_total_accept_reject_swap(monkeypatch, tmp_path):
    """Codex's second undetected case: policy_accepted_count/
    policy_rejected_seed_count changed from the true 1/1 to a self-consistent
    2/0 (same sum, so the total-conservation law alone cannot catch it).
    The independent cross-check against the separately-persisted
    policy_filtered_actions list (untouched by the tamper) must catch it."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
        "policy_filtered_actions": [{
            "rule": "_rule_dd_stage", "reason": "loss_guard_stage=data_confidence_caution",
            "action": _buy("SYNTH_B", 2),
        }],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    ds = saved["synthesis"]["decision_summary"]
    assert (ds["policy_accepted_count"], ds["policy_rejected_seed_count"]) == (1, 1), "test setup sanity check"
    ds["policy_accepted_count"] = 2
    ds["policy_rejected_seed_count"] = 0
    ds["candidate_count"] = 2   # kept self-consistent with the tampered accepted count
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert any(issue["code"] == "decision_summary_count_mismatch" for issue in issues)
    mismatch = next(i for i in issues if i["code"] == "decision_summary_count_mismatch")
    assert "policy_rejected_seed_count" in mismatch["context"]["mismatches"]
    assert "candidate_total" not in mismatch["context"]["mismatches"], "the total alone must still look balanced"


def test_real_producer_roundtrip_detects_candidate_count_tampered_alone(monkeypatch, tmp_path):
    """Codex's third undetected case: candidate_count changed to an
    unrelated value (999) while policy_accepted_count/
    policy_rejected_seed_count (the fields this file actually derives
    conservation from) are left correct."""
    synthesis = {
        "overall_stance": "neutral",
        "priority_actions": [_buy("SYNTH_A", 4)],
    }
    _run_real_producer_and_save(monkeypatch, tmp_path, synthesis)
    path = tmp_path / "ai_portfolio_analysis.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["synthesis"]["decision_summary"]["candidate_count"] = 999
    _write(path, saved)

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert any(issue["code"] == "decision_summary_count_mismatch" for issue in issues)
    mismatch = next(i for i in issues if i["code"] == "decision_summary_count_mismatch")
    assert mismatch["context"]["mismatches"]["candidate_count"] == {"stored": 999, "actual": 1}


def test_check_action_stage_executed_alignment_flags_orphan_stage_rows(tmp_path):
    _write(tmp_path / "action_executions.json", {
        "executions": [{
            "saved_at": "2026-07-02T01:15:49",
            "ticker": "V",
            "direction": "buy",
            "status": "executed",
            "portfolio_applied": True,
        }]
    })
    (tmp_path / "action_stage_log.jsonl").write_text(
        "\n".join([
            json.dumps({
                "as_of": "2026-07-02T01:15:49",
                "stage": "executed",
                "ticker": "V",
                "canonical_action_type": "buy",
            }),
            json.dumps({
                "as_of": "2026-07-02T14:04:06",
                "stage": "executed",
                "ticker": "7203.T",
                "canonical_action_type": "buy",
            }),
        ]) + "\n",
        encoding="utf-8",
    )

    issues = prv.check_action_stage_executed_alignment(tmp_path)

    assert issues[0]["code"] == "action_stage_executed_orphan_rows"
    assert issues[0]["severity"] == "error"
    assert issues[0]["context"]["orphan_count"] == 1
    assert issues[0]["context"]["examples"][0]["ticker"] == "7203.T"


def test_execution_reconciliation_integrity_flags_stale_and_orphan_overlay(tmp_path):
    execution = {
        "id": "ABC_sell_demo",
        "saved_at": "2026-07-16T01:00:00",
        "ticker": "ABC",
        "direction": "sell",
        "quantity": 5,
        "price": 42,
        "account": "特定",
        "status": "executed",
    }
    _write(tmp_path / "action_executions.json", {"executions": [execution]})
    from execution_reconciliation import record_route_correction

    record_route_correction(
        execution_record=execution,
        corrected_route={
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_account": "NISA成長投資枠",
        },
        evidence={"row_hash": "proof"},
        reason="test",
        approved_by="test",
        state_path=tmp_path / "execution_reconciliation_state.json",
    )
    changed = {**execution, "quantity": 6}
    _write(tmp_path / "action_executions.json", {"executions": [changed]})
    issues = prv.check_execution_reconciliation_integrity(tmp_path)
    assert issues[0]["code"] == "execution_reconciliation_requires_review"

    state_path = tmp_path / "execution_reconciliation_state.json"
    state = json.loads(state_path.read_text())
    state["corrections"].append({
        "correction_type": "route",
        "correction_id": "orphan",
        "execution_id": "missing",
    })
    _write(state_path, state)
    codes = {
        issue["code"]
        for issue in prv.check_execution_reconciliation_integrity(tmp_path)
    }
    assert "execution_reconciliation_orphan_corrections" in codes


def test_check_action_stage_executed_alignment_ignores_pre_execution_window_rows(tmp_path):
    _write(tmp_path / "action_executions.json", {
        "executions": [{
            "saved_at": "2026-07-02T01:15:49",
            "ticker": "V",
            "direction": "buy",
            "status": "executed",
            "portfolio_applied": True,
        }]
    })
    (tmp_path / "action_stage_log.jsonl").write_text(
        json.dumps({
            "as_of": "2026-06-01T00:00:00",
            "stage": "executed",
            "ticker": "OLD",
            "canonical_action_type": "buy",
        }) + "\n",
        encoding="utf-8",
    )

    assert prv.check_action_stage_executed_alignment(tmp_path) == []


def test_check_synthesis_risk_warnings_includes_very_stale_warning_context(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {
            "risk_warnings": [
                "⚠️ データ鮮度0.51（holdings 144h前VERY_STALE）",
                "other warning",
            ]
        }
    })

    issues = prv.check_synthesis_risk_warnings(tmp_path)

    issue = next(i for i in issues if i["code"] == "synthesis_mentions_very_stale_data")
    assert issue["context"]["warnings"] == ["⚠️ データ鮮度0.51（holdings 144h前VERY_STALE）"]


def test_check_portfolio_integrity_repairs_account_derived_cash_before_check(tmp_path, monkeypatch):
    _write(tmp_path / "account.json", {
        "balance": 100_000,
        "usd_balance": 1_000,
        "fx_rate_usdjpy": 151.25,
        "jpy_equivalent_usd": 149_000,
        "total_cash": 249_000,
    })

    def fake_run_integrity_check(*, base_dir):
        account = json.loads((base_dir / "account.json").read_text(encoding="utf-8"))
        ok = (
            account["jpy_equivalent_usd"] == 151_250
            and account["total_cash"] == 251_250
        )
        return {"ok": ok, "blocking_issue_count": 0, "summary": {}}

    monkeypatch.setitem(
        sys.modules,
        "portfolio_integrity",
        types.SimpleNamespace(run_integrity_check=fake_run_integrity_check),
    )

    before = (tmp_path / "account.json").read_bytes()
    assert prv.check_portfolio_integrity(tmp_path)
    assert (tmp_path / "account.json").read_bytes() == before
    assert prv.check_portfolio_integrity(tmp_path, repair=True) == []
    saved = json.loads((tmp_path / "account.json").read_text(encoding="utf-8"))
    assert saved["jpy_equivalent_usd"] == 151_250
    assert saved["total_cash"] == 251_250


def test_verify_post_run_returns_non_ok_for_errors(tmp_path):
    _write(tmp_path / "scenario_state.json", {"scenarios": []})
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "synthesis": {"final_priority_actions": [{"ticker": "META", "type": "buy"}]}
    })
    _write(tmp_path / "action_state.json", {"actions": {}})

    report = prv.verify_post_run(tmp_path)

    assert report["ok"] is False
    assert report["issue_count"] >= 1
