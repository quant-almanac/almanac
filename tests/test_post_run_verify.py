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
            "count_conservation_ok": False,
        },
    }})

    issues = prv.check_decision_summary_conservation(tmp_path)
    assert {issue["code"] for issue in issues} == {
        "priority_action_readiness_missing", "decision_summary_count_mismatch",
    }


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
