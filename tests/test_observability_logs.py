"""Tests for almanac.observability.logs (typed writers for all 7 log files).

These tests pin down the schemas the rest of the codebase will consume.
Coverage focuses on the boundaries that historically caused dialectic
churn:

- Status taxonomy coercion (enum member OR string, validated either way).
- Append-only discipline (no mutate API exists; tests verify only writes).
- Benchmark basket invariants from Round 9 #6.
- Computed-field consistency (return_pct / excess_return_bps are derived,
  not passed in, so callers cannot desync them).
- R11 #1 flat-row attribution (one row per agent, never a nested array).
- R11 #2 cash_deployment_log event_type split.
- R8-6 sell decision recommended/ordered/executed/cancelled separation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Make repo root importable so ``almanac.*`` resolves even when pytest is
# invoked from a different cwd inside the worktree.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.append_only_log import MeasurementQuality  # noqa: E402
from almanac.observability.ids import (  # noqa: E402
    compute_hypothesis_id,
    new_analysis_id,
    new_cash_decision_id,
)
from almanac.observability.logs import (  # noqa: E402
    write_agent_attribution,
    write_belief_adjustment,
    write_cash_critic_triggered,
    write_cash_follow_up_outcome,
    write_catalyst_hypothesis_filtered,
    write_catalyst_hypothesis_generated,
    write_catalyst_hypothesis_status_transition,
    write_catalyst_outcome,
    write_portfolio_decision,
    write_sell_decision,
    write_sell_outcome,
)
from almanac.observability.status import (  # noqa: E402
    CandidateStatus,
    ExecutionState,
    PortfolioDecisionState,
)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def hid() -> str:
    """A realistic hypothesis_id used across tests."""
    return compute_hypothesis_id(
        ticker="NVDA",
        action_type="buy",
        hypothesis_type="earnings_revision_pullback",
        horizon_days=10,
        source_event_id="news:guidance_raise_2026_q1",
    )


@pytest.fixture
def aid() -> str:
    return new_analysis_id()


def _read_rows(path: Path) -> list[dict]:
    """Return parsed JSONL rows for assertions."""
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# catalyst_hypothesis_log — generated
# ---------------------------------------------------------------------------


def _generated_kwargs(hid: str, aid: str) -> dict:
    """Minimal valid kwargs for write_catalyst_hypothesis_generated."""
    return dict(
        hypothesis_id=hid,
        analysis_id=aid,
        analysis_date="2026-05-24",
        hypothesis_type="earnings_revision_pullback",
        primary_ticker="NVDA",
        catalyst_score=0.78,
        scenario_readiness=0.55,
        priced_in_penalty=0.15,
        surprise_score=0.72,
        gross_expected_return_bps=250,
        conviction_at_generation=68,
        price_at_event=120.5,
        benchmark_basket=["QQQ", "SOXX"],
        benchmark_weights=[0.5, 0.5],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 478.3, "SOXX": 245.7},
        usdjpy_at_event=156.2,
    )


def test_generated_writes_event_type_generated(tmp_path: Path, hid, aid) -> None:
    log = tmp_path / "catalyst_hypothesis_log.jsonl"
    write_catalyst_hypothesis_generated(log, fsync=False, **_generated_kwargs(hid, aid))
    rows = _read_rows(log)
    assert len(rows) == 1
    assert rows[0]["event_type"] == "generated"
    assert rows[0]["candidate_status"] == "generated"
    assert rows[0]["observe_only"] is False


def test_generated_returns_row_id(tmp_path: Path, hid, aid) -> None:
    log = tmp_path / "log.jsonl"
    rid = write_catalyst_hypothesis_generated(log, fsync=False, **_generated_kwargs(hid, aid))
    rows = _read_rows(log)
    assert rows[0]["row_id"] == rid


def test_generated_carries_full_benchmark_metadata(tmp_path: Path, hid, aid) -> None:
    """Round 9 #6 — every benchmark field must hit disk."""
    log = tmp_path / "log.jsonl"
    write_catalyst_hypothesis_generated(log, fsync=False, **_generated_kwargs(hid, aid))
    row = _read_rows(log)[0]
    assert row["benchmark_basket"] == ["QQQ", "SOXX"]
    assert row["benchmark_weights"] == [0.5, 0.5]
    assert row["benchmark_currency_normalized_to"] == "USD"
    assert row["usdjpy_at_event"] == 156.2


def test_generated_can_flag_observe_only(tmp_path: Path, hid, aid) -> None:
    log = tmp_path / "log.jsonl"
    kw = _generated_kwargs(hid, aid)
    kw["observe_only"] = True
    write_catalyst_hypothesis_generated(log, fsync=False, **kw)
    assert _read_rows(log)[0]["observe_only"] is True


def test_generated_carries_source_metadata_for_scenario_rollups(tmp_path: Path, hid, aid) -> None:
    log = tmp_path / "log.jsonl"
    kw = _generated_kwargs(hid, aid)
    kw.update(
        source_event_id="scenario:japan_standalone_bull",
        primary_source_agent="scenario:japan_standalone_bull",
        action_type="buy",
        horizon_days=20,
    )

    write_catalyst_hypothesis_generated(log, fsync=False, **kw)

    row = _read_rows(log)[0]
    assert row["source_event_id"] == "scenario:japan_standalone_bull"
    assert row["primary_source_agent"] == "scenario:japan_standalone_bull"
    assert row["action_type"] == "buy"
    assert row["horizon_days"] == 20


def test_generated_rejects_empty_benchmark_basket(tmp_path: Path, hid, aid) -> None:
    kw = _generated_kwargs(hid, aid)
    kw["benchmark_basket"] = []
    kw["benchmark_weights"] = []
    with pytest.raises(ValueError, match="benchmark_basket must not be empty"):
        write_catalyst_hypothesis_generated(tmp_path / "log.jsonl", fsync=False, **kw)


def test_generated_rejects_basket_weight_length_mismatch(tmp_path: Path, hid, aid) -> None:
    kw = _generated_kwargs(hid, aid)
    kw["benchmark_weights"] = [0.5, 0.3, 0.2]  # 3 weights for 2 tickers
    with pytest.raises(ValueError, match="length mismatch"):
        write_catalyst_hypothesis_generated(tmp_path / "log.jsonl", fsync=False, **kw)


def test_generated_rejects_unnormalized_currency(tmp_path: Path, hid, aid) -> None:
    kw = _generated_kwargs(hid, aid)
    kw["benchmark_currency_normalized_to"] = "EUR"
    with pytest.raises(ValueError, match="benchmark_currency_normalized_to"):
        write_catalyst_hypothesis_generated(tmp_path / "log.jsonl", fsync=False, **kw)


def test_generated_rejects_weights_summing_above_one(tmp_path: Path, hid, aid) -> None:
    """Common mistake: passing percentages instead of fractions."""
    kw = _generated_kwargs(hid, aid)
    kw["benchmark_basket"] = ["QQQ", "SOXX"]
    kw["benchmark_weights"] = [50, 50]  # someone forgot to divide
    with pytest.raises(ValueError, match="benchmark_weights must sum"):
        write_catalyst_hypothesis_generated(tmp_path / "log.jsonl", fsync=False, **kw)


def test_generated_handles_jp_basket_normalized_to_jpy(tmp_path: Path, hid, aid) -> None:
    kw = _generated_kwargs(hid, aid)
    kw["benchmark_basket"] = ["TOPIX", "ARM", "QQQ"]
    kw["benchmark_weights"] = [0.5, 0.3, 0.2]
    kw["benchmark_currency_normalized_to"] = "JPY"
    kw["benchmark_price_at_event"] = {"TOPIX": 2768.5, "ARM": 135.2, "QQQ": 478.3}
    rid = write_catalyst_hypothesis_generated(tmp_path / "log.jsonl", fsync=False, **kw)
    assert rid  # smoke


# ---------------------------------------------------------------------------
# catalyst_hypothesis_log — status_transition
# ---------------------------------------------------------------------------


def test_status_transition_accepts_enum_members(tmp_path: Path, hid, aid) -> None:
    log = tmp_path / "log.jsonl"
    write_catalyst_hypothesis_status_transition(
        log,
        fsync=False,
        hypothesis_id=hid,
        analysis_id=aid,
        analysis_date="2026-05-24",
        candidate_status=CandidateStatus.injected_rejected,
        previous_status=CandidateStatus.injected,
        reason="opus rejection: priced_in",
        price_at_event=86.1,
    )
    row = _read_rows(log)[0]
    assert row["event_type"] == "status_transition"
    assert row["candidate_status"] == "injected_rejected"
    assert row["previous_status"] == "injected"


def test_status_transition_accepts_string_status(tmp_path: Path, hid, aid) -> None:
    """Coercion path: callers in legacy code may pass strings."""
    log = tmp_path / "log.jsonl"
    write_catalyst_hypothesis_status_transition(
        log,
        fsync=False,
        hypothesis_id=hid,
        analysis_id=aid,
        analysis_date="2026-05-24",
        candidate_status="adopted",
        previous_status="injected",
        reason="opus adopted",
        price_at_event=120.5,
    )
    assert _read_rows(log)[0]["candidate_status"] == "adopted"


def test_status_transition_rejects_invalid_status(tmp_path: Path, hid, aid) -> None:
    """Invalid string surfaces as ValueError at call time, not at parse time."""
    with pytest.raises(ValueError):
        write_catalyst_hypothesis_status_transition(
            tmp_path / "log.jsonl",
            fsync=False,
            hypothesis_id=hid,
            analysis_id=aid,
            analysis_date="2026-05-24",
            candidate_status="user_not_executed",  # belongs to ExecutionState
            previous_status="injected",
            reason="bad",
            price_at_event=1.0,
        )


# ---------------------------------------------------------------------------
# catalyst_hypothesis_log — filtered (evidence sufficiency gate)
# ---------------------------------------------------------------------------


def test_filtered_records_missing_fields(tmp_path: Path, hid, aid) -> None:
    log = tmp_path / "log.jsonl"
    write_catalyst_hypothesis_filtered(
        log,
        fsync=False,
        hypothesis_id=hid,
        analysis_id=aid,
        analysis_date="2026-05-24",
        filter_reason="evidence_sufficiency_gate",
        missing_fields=["invalidation", "source_event"],
    )
    row = _read_rows(log)[0]
    assert row["event_type"] == "filtered"
    assert row["candidate_status"] == "not_injected"  # CandidateStatus.not_injected
    assert row["missing_fields"] == ["invalidation", "source_event"]
    assert row["filter_rule_version"].startswith("esg:")


# ---------------------------------------------------------------------------
# catalyst_outcome_log — computed fields
# ---------------------------------------------------------------------------


def test_outcome_computes_return_and_excess(tmp_path: Path, hid) -> None:
    log = tmp_path / "outcome.jsonl"
    write_catalyst_outcome(
        log,
        fsync=False,
        hypothesis_id=hid,
        horizon_days=5,
        reference_event_at="2026-05-24T18:30:00",
        price_at_event=100.0,
        price_at_measure=105.0,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_return_pct=0.02,
        primary_ticker_currency="USD",
        usdjpy_at_event=156.2,
        usdjpy_at_measure=157.1,
    )
    row = _read_rows(log)[0]
    assert row["return_pct"] == pytest.approx(0.05)
    # excess = (5% - 2%) * 10000 = 300 bps
    assert row["excess_return_bps"] == pytest.approx(300.0)
    assert row["after_cost_excess_return_bps"] is None  # Phase 2-C placeholder
    assert row["measurement_quality"] == "ok"


def test_outcome_rejects_zero_reference_price(tmp_path: Path, hid) -> None:
    with pytest.raises(ValueError, match="price_at_event must be non-zero"):
        write_catalyst_outcome(
            tmp_path / "log.jsonl",
            fsync=False,
            hypothesis_id=hid,
            horizon_days=5,
            reference_event_at="2026-05-24T18:30:00",
            price_at_event=0.0,
            price_at_measure=105.0,
            benchmark_basket=["QQQ"],
            benchmark_weights=[1.0],
            benchmark_currency_normalized_to="USD",
            benchmark_return_pct=0.02,
            primary_ticker_currency="USD",
            usdjpy_at_event=156.2,
            usdjpy_at_measure=157.1,
        )


def test_outcome_carries_measurement_quality(tmp_path: Path, hid) -> None:
    log = tmp_path / "log.jsonl"
    write_catalyst_outcome(
        log,
        fsync=False,
        hypothesis_id=hid,
        horizon_days=5,
        reference_event_at="2026-05-24T18:30:00",
        price_at_event=100.0,
        price_at_measure=100.0,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_return_pct=0.0,
        primary_ticker_currency="USD",
        usdjpy_at_event=156.2,
        usdjpy_at_measure=156.2,
        measurement_quality=MeasurementQuality.STALE,
    )
    assert _read_rows(log)[0]["measurement_quality"] == "stale"


# ---------------------------------------------------------------------------
# sell_decision_log (R8-6 timestamp separation)
# ---------------------------------------------------------------------------


def test_sell_decision_separates_lifecycle_timestamps(tmp_path: Path) -> None:
    """R8-6 — recommended_at / ordered_at / executed_at / cancelled_at must
    be independent fields, not collapsed into one."""
    log = tmp_path / "sell.jsonl"
    write_sell_decision(
        log,
        fsync=False,
        sell_decision_id="dec-1",
        ticker="NVDA",
        action_type="trim",
        shares_recommended=50,
        price_at_recommend=120.5,
        reason="hypothesis invalidated",
        conviction_at_sell=35,
        benchmark_basket=["QQQ", "SOXX"],
        benchmark_weights=[0.5, 0.5],
    )
    row = _read_rows(log)[0]
    for field in ("recommended_at", "ordered_at", "executed_at", "cancelled_at"):
        assert field in row, f"missing lifecycle field: {field}"
    assert row["recommended_at"] is not None
    assert row["ordered_at"] is None
    assert row["executed_at"] is None
    assert row["cancelled_at"] is None
    assert row["execution_state"] == "not_ordered"


def test_sell_decision_rejects_invalid_action_type(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sell_decision_log accepts"):
        write_sell_decision(
            tmp_path / "log.jsonl",
            fsync=False,
            sell_decision_id="dec",
            ticker="X",
            action_type="buy",  # not allowed in sell log
            shares_recommended=1,
            price_at_recommend=1.0,
            reason="x",
            conviction_at_sell=1,
            benchmark_basket=["QQQ"],
            benchmark_weights=[1.0],
        )


def test_sell_decision_accepts_execution_state_enum_or_string(tmp_path: Path) -> None:
    log = tmp_path / "log.jsonl"
    write_sell_decision(
        log,
        fsync=False,
        sell_decision_id="dec",
        ticker="NVDA",
        action_type="sell",
        shares_recommended=10,
        price_at_recommend=100.0,
        reason="trim",
        conviction_at_sell=30,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        execution_state=ExecutionState.executed,
    )
    write_sell_decision(
        log,
        fsync=False,
        sell_decision_id="dec2",
        ticker="NVDA",
        action_type="sell",
        shares_recommended=10,
        price_at_recommend=100.0,
        reason="trim",
        conviction_at_sell=30,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        execution_state="ordered",
    )
    rows = _read_rows(log)
    assert rows[0]["execution_state"] == "executed"
    assert rows[1]["execution_state"] == "ordered"


# ---------------------------------------------------------------------------
# sell_outcome_log
# ---------------------------------------------------------------------------


def test_sell_outcome_computes_missed_gain_and_excess(tmp_path: Path) -> None:
    log = tmp_path / "sell_outcome.jsonl"
    write_sell_outcome(
        log,
        fsync=False,
        sell_decision_id="dec-1",
        horizon_days=10,
        price_at_recommend=100.0,
        counterfactual_price=108.0,
        benchmark_return_pct=0.03,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        primary_ticker_currency="USD",
        usdjpy_at_recommend=156.2,
        usdjpy_at_measure=157.0,
    )
    row = _read_rows(log)[0]
    # 8% gain missed, 3% benchmark → 5% excess = +500 bps opportunity cost
    assert row["missed_gain_pct"] == pytest.approx(0.08)
    assert row["missed_excess_return_bps"] == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# agent_attribution_log (R11 #1: 1 row per agent, no nested array)
# ---------------------------------------------------------------------------


def test_attribution_is_one_row_per_agent(tmp_path: Path, hid, aid) -> None:
    """R11 #1 — each agent appends its own row; reports reconstruct the
    full agents list via group-by, never via mutate."""
    log = tmp_path / "attribution.jsonl"
    common = dict(
        hypothesis_id=hid,
        analysis_id=aid,
        analysis_date="2026-05-24",
        ticker="NVDA",
        hypothesis_type="earnings_revision_pullback",
        time_horizon_days=10,
    )
    write_agent_attribution(
        log,
        fsync=False,
        agent="catalyst_layer",
        role="originator",
        stance="support",
        confidence_pct=72,
        evidence_ids=["news:abc", "scenario:tech_boom"],
        **common,
    )
    write_agent_attribution(
        log,
        fsync=False,
        agent="red_team",
        role="critic",
        stance="veto",
        severity="high",
        issue_type="priced_in",
        **common,
    )
    write_agent_attribution(
        log,
        fsync=False,
        agent="opus_final",
        role="final_decider",
        stance="reject",
        confidence_pct=61,
        final_candidate_status=CandidateStatus.injected_rejected,
        reason="priced_in + low conviction",
        **common,
    )
    rows = _read_rows(log)
    assert len(rows) == 3
    # No row contains a nested `agents` array — the regression we are guarding.
    for r in rows:
        assert "agents" not in r, "attribution rows must be flat (R11 #1)"
    agents = {r["agent"] for r in rows}
    assert agents == {"catalyst_layer", "red_team", "opus_final"}
    # All share the same hypothesis_id so group-by works.
    assert {r["hypothesis_id"] for r in rows} == {hid}
    # final_decider serialized its final_candidate_status.
    final = next(r for r in rows if r["role"] == "final_decider")
    assert final["final_candidate_status"] == "injected_rejected"


def test_attribution_rejects_invalid_role(tmp_path: Path, hid, aid) -> None:
    with pytest.raises(ValueError, match="role must be in"):
        write_agent_attribution(
            tmp_path / "log.jsonl",
            fsync=False,
            hypothesis_id=hid,
            analysis_id=aid,
            analysis_date="2026-05-24",
            ticker="NVDA",
            hypothesis_type="x",
            time_horizon_days=10,
            agent="some_agent",
            role="cheerleader",  # not in _VALID_ROLES
            stance="support",
        )


def test_attribution_rejects_invalid_stance(tmp_path: Path, hid, aid) -> None:
    with pytest.raises(ValueError, match="stance must be in"):
        write_agent_attribution(
            tmp_path / "log.jsonl",
            fsync=False,
            hypothesis_id=hid,
            analysis_id=aid,
            analysis_date="2026-05-24",
            ticker="NVDA",
            hypothesis_type="x",
            time_horizon_days=10,
            agent="some_agent",
            role="critic",
            stance="hostile",  # not in _VALID_STANCES
        )


# ---------------------------------------------------------------------------
# portfolio_decision_log
# ---------------------------------------------------------------------------


def test_portfolio_decision_validates_cash_ratio(tmp_path: Path, aid) -> None:
    with pytest.raises(ValueError, match="cash_ratio must be in"):
        write_portfolio_decision(
            tmp_path / "log.jsonl",
            fsync=False,
            analysis_date="2026-05-24",
            analysis_id=aid,
            portfolio_decision_state=PortfolioDecisionState.cash_retained,
            risk_mode="aggressive",
            cash_ratio=1.5,  # out of [0, 1]
            total_assets_jpy=30_000_000,
            active_scenarios=[],
            generated_candidates=5,
            injected_candidates=2,
            adopted_candidates=0,
            rejected_count_by_reason={},
            cash_critic_triggered=True,
            benchmark_return_today=0.01,
            portfolio_return_today=0.0,
            opportunity_cost_today_bps=10,
        )


def test_portfolio_decision_validates_risk_mode(tmp_path: Path, aid) -> None:
    with pytest.raises(ValueError, match="risk_mode invalid"):
        write_portfolio_decision(
            tmp_path / "log.jsonl",
            fsync=False,
            analysis_date="2026-05-24",
            analysis_id=aid,
            portfolio_decision_state="action_taken",
            risk_mode="yolo",  # invalid
            cash_ratio=0.3,
            total_assets_jpy=30_000_000,
            active_scenarios=[],
            generated_candidates=0,
            injected_candidates=0,
            adopted_candidates=0,
            rejected_count_by_reason={},
            cash_critic_triggered=False,
            benchmark_return_today=0.0,
            portfolio_return_today=0.0,
            opportunity_cost_today_bps=0,
        )


def test_portfolio_decision_round_trips(tmp_path: Path, aid) -> None:
    log = tmp_path / "log.jsonl"
    write_portfolio_decision(
        log,
        fsync=False,
        analysis_date="2026-05-24",
        analysis_id=aid,
        portfolio_decision_state=PortfolioDecisionState.cash_retained,
        risk_mode="aggressive",
        cash_ratio=0.34,
        total_assets_jpy=30_638_926,
        active_scenarios=[{"key": "bull_pullback", "readiness": 0.68}],
        generated_candidates=8,
        injected_candidates=3,
        adopted_candidates=0,
        rejected_count_by_reason={"priced_in": 2, "low_conviction": 1},
        cash_critic_triggered=True,
        opus_no_buy_reason="VIX 28 で押し目待ち",
        benchmark_return_today=0.012,
        portfolio_return_today=0.000,
        opportunity_cost_today_bps=12,
    )
    row = _read_rows(log)[0]
    assert row["portfolio_decision_state"] == "cash_retained"
    assert row["cash_ratio"] == 0.34
    assert row["rejected_count_by_reason"]["priced_in"] == 2


# ---------------------------------------------------------------------------
# cash_deployment_log (R11 #2 event_type split)
# ---------------------------------------------------------------------------


def test_cash_log_uses_event_type_split(tmp_path: Path, aid) -> None:
    """R11 #2 — trigger and follow-up rows live in the same file but are
    distinguished by event_type, never by nullable follow_up_outcome_*."""
    log = tmp_path / "cash.jsonl"
    cid = new_cash_decision_id("2026-05-24", "aggressive", 0.34)
    write_cash_critic_triggered(
        log,
        fsync=False,
        cash_decision_id=cid,
        analysis_date="2026-05-24",
        analysis_id=aid,
        risk_mode="aggressive",
        cash_ratio=0.34,
        cash_ratio_threshold=0.20,
        active_bull_scenarios=["bull_pullback", "tech_boom"],
        generated_candidates=8,
        adopted_candidates=0,
        warning_text="⚠️ aggressive mode, cash 34%, 0 buys",
        portfolio_decision_state="cash_retained",
        benchmark_basket=["VT", "AGG"],
        benchmark_weights=[0.6, 0.4],
        opus_no_buy_reason="VIX 28",
    )
    write_cash_follow_up_outcome(
        log,
        fsync=False,
        cash_decision_id=cid,
        horizon_days=1,
        benchmark_return_pct=0.012,
        opportunity_cost_bps=12,
    )
    write_cash_follow_up_outcome(
        log,
        fsync=False,
        cash_decision_id=cid,
        horizon_days=5,
        benchmark_return_pct=0.028,
        opportunity_cost_bps=28,
    )
    rows = _read_rows(log)
    assert len(rows) == 3
    types = [r["event_type"] for r in rows]
    assert types == ["critic_triggered", "follow_up_outcome", "follow_up_outcome"]
    # All three share the same cash_decision_id — the join key.
    assert {r["cash_decision_id"] for r in rows} == {cid}
    # No nullable follow_up_outcome_Nd field on the trigger row.
    trigger = rows[0]
    for k in trigger:
        assert not k.startswith("follow_up_outcome_"), (
            f"R11 #2 regression: nullable {k!r} on trigger row"
        )
    # Trigger row carries Japanese warning text without escaping.
    assert "⚠️" in trigger["warning_text"]


# ---------------------------------------------------------------------------
# belief_adjustments
# ---------------------------------------------------------------------------


def test_belief_adjustment_is_append_only(tmp_path: Path) -> None:
    log = tmp_path / "belief_adjustments.jsonl"
    write_belief_adjustment(
        log,
        fsync=False,
        belief_id="NVDA-2026-05-15-earnings_revision_pullback",
        ticker="NVDA",
        delta=-15,
        reason="invalidation:ma20_break",
        rule_version="invalidation_rules:v1.0",
        evidence={"price": 118.0, "ma20": 124.5, "rsi_14": 38},
    )
    write_belief_adjustment(
        log,
        fsync=False,
        belief_id="NVDA-2026-05-15-earnings_revision_pullback",
        ticker="NVDA",
        delta=-10,
        reason="invalidation:rsi_overheat",
        rule_version="invalidation_rules:v1.0",
    )
    rows = _read_rows(log)
    assert len(rows) == 2
    # adjusted_conviction = base + Σdelta is computed at synthesis time;
    # the log stores deltas only, never the running total.
    assert rows[0]["delta"] == -15
    assert rows[1]["delta"] == -10
    # adjustment_id == row_id alias so §6.3 consumers stay backward compatible.
    assert rows[0]["adjustment_id"] == rows[0]["row_id"]


def test_belief_adjustment_rejects_float_delta(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="delta must be int"):
        write_belief_adjustment(
            tmp_path / "log.jsonl",
            fsync=False,
            belief_id="x",
            ticker="x",
            delta=-1.5,  # type: ignore[arg-type]
            reason="x",
            rule_version="x",
        )


# ---------------------------------------------------------------------------
# Append-only discipline meta-tests
# ---------------------------------------------------------------------------


def test_logs_module_exposes_only_write_functions() -> None:
    """No update/delete/mutate API exists. The whole point of R9-3."""
    import almanac.observability.logs as logs_mod

    public_writers = {
        name for name in logs_mod.__all__
        if not name.startswith("_")
    }
    # Every public symbol must be a write_* function.
    for name in public_writers:
        assert name.startswith("write_"), (
            f"{name!r} violates append-only discipline; only write_* allowed"
        )
    # And there must be no update_/delete_/mutate_ helper anywhere.
    for name in dir(logs_mod):
        if name.startswith("_"):
            continue
        assert not name.startswith(("update_", "delete_", "mutate_", "patch_")), (
            f"{name!r} suggests mutation; R9-3 forbids non-append APIs"
        )
