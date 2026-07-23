"""Typed writers for every observability log defined in the plan.

This module gives the rest of the codebase a single, schema-validating
entry point per log file. The writers do three jobs:

1. **Schema enforcement** — required fields are checked at call time so a
   missing ``hypothesis_id`` or ``analysis_id`` fails fast at the call
   site rather than silently producing a partial row.
2. **Status taxonomy hygiene** — :class:`CandidateStatus` /
   :class:`ExecutionState` / :class:`PortfolioDecisionState` are accepted
   either as enum members or their string values, but values are
   validated against the canonical enums so a typo cannot reach disk.
3. **Append-only discipline** — every writer ultimately calls
   :func:`append_jsonl_safe`. There is no mutate/update path; status
   transitions and outcomes enter as additional rows joined by stable
   IDs (``hypothesis_id``, ``cash_decision_id``, ``sell_decision_id``).

Each log has a one-to-one mapping with a plan section. Cross-references in
docstrings point back to that section for traceability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .append_only_log import MeasurementQuality, append_jsonl_safe
from .ids import new_row_id
from .status import CandidateStatus, ExecutionState, PortfolioDecisionState

__all__ = [
    # catalyst_hypothesis_log (§6.6) — strict append-only, three event types
    "write_catalyst_hypothesis_generated",
    "write_catalyst_hypothesis_status_transition",
    "write_catalyst_hypothesis_filtered",
    # catalyst_outcome_log (§6.14) — append-only outcome events
    "write_catalyst_outcome",
    # sell_decision_log (§6.8) + sell_outcome_log (R9-3 symmetric split)
    "write_sell_decision",
    "write_sell_outcome",
    # agent_attribution_log (§6.10, R11-1 flat-row)
    "write_agent_attribution",
    # portfolio_decision_log (§6.11) — daily portfolio-level decision
    "write_portfolio_decision",
    # cash_deployment_log (§6.12, R11-2 event_type split)
    "write_cash_critic_triggered",
    "write_cash_follow_up_outcome",
    # belief_adjustments (§6.3) — invalidation_rules sink
    "write_belief_adjustment",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> str:
    """ISO-8601 timestamp for log rows. UTC keeps tz arithmetic boring."""
    return datetime.now(timezone.utc).isoformat()


def _coerce_status(value: Any, expected: type) -> str:
    """Accept enum member or string; validate against canonical enum.

    Returns the canonical string value. Callers can then pass either
    ``CandidateStatus.adopted`` or ``"adopted"`` without ambiguity.

    Raises
    ------
    ValueError
        If ``value`` is a string but not a valid member of ``expected``.
    TypeError
        If ``value`` is neither a string nor a member of ``expected``.
    """
    if isinstance(value, expected):
        return value.value
    if isinstance(value, str):
        # Triggers ValueError if the string is not a valid enum value.
        return expected(value).value
    raise TypeError(
        f"Expected {expected.__name__} or str, got {type(value).__name__}"
    )


def _require(row: Mapping[str, Any], fields: Iterable[str], schema_name: str) -> None:
    """Raise if any required field is missing or None."""
    missing = [f for f in fields if row.get(f) is None]
    if missing:
        raise ValueError(
            f"{schema_name}: required fields missing or None: {missing}"
        )


def _validate_benchmark(
    basket: list[str],
    weights: list[float],
    currency_normalized_to: str,
) -> None:
    """Round 9 #6 invariants for benchmark_basket fields.

    A benchmark basket without a declared normalization currency or with
    mismatched basket/weight lengths is a measurement bug, so we surface
    it at the writer rather than letting it propagate into the outcome
    log.
    """
    if not basket:
        raise ValueError("benchmark_basket must not be empty")
    if len(basket) != len(weights):
        raise ValueError(
            f"benchmark_basket / benchmark_weights length mismatch: "
            f"{len(basket)} vs {len(weights)}"
        )
    if currency_normalized_to not in {"JPY", "USD"}:
        raise ValueError(
            f"benchmark_currency_normalized_to must be 'JPY' or 'USD', "
            f"got {currency_normalized_to!r}"
        )
    # Allow weight sums slightly off 1.0 for partial baskets, but a clear
    # arithmetic mistake (e.g. someone passed [50, 30, 20] instead of
    # [0.5, 0.3, 0.2]) is worth blocking.
    if not (0.0 < sum(weights) <= 1.001):
        raise ValueError(
            f"benchmark_weights must sum to (0, 1], got {sum(weights)}"
        )


# ---------------------------------------------------------------------------
# catalyst_hypothesis_log.jsonl (plan §6.6) — strict append-only
# ---------------------------------------------------------------------------


def write_catalyst_hypothesis_generated(
    path: Path | str,
    *,
    hypothesis_id: str,
    analysis_id: str,
    analysis_date: str,
    hypothesis_type: str,
    primary_ticker: str,
    catalyst_score: float,
    scenario_readiness: float,
    priced_in_penalty: float,
    surprise_score: float,
    gross_expected_return_bps: float,
    conviction_at_generation: int,
    price_at_event: float | None,
    benchmark_basket: list[str],
    benchmark_weights: list[float],
    benchmark_currency_normalized_to: str,
    benchmark_price_at_event: dict[str, float | None],
    usdjpy_at_event: float,
    # Optional / contextual fields
    proxy_tickers: list[str] | None = None,
    non_listed_entity: str | None = None,
    observe_only: bool = False,
    source_event_id: str | None = None,
    primary_source_agent: str | None = None,
    action_type: str | None = None,
    horizon_days: int | None = None,
    first_seen_at: str | None = None,
    prior_mentions_count: int | None = None,
    event_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a ``event_type=generated`` row.

    Returns the ``row_id`` so callers can correlate the write with later
    attribution writes inside the same ``analyzer.py`` run.
    """
    _validate_benchmark(benchmark_basket, benchmark_weights, benchmark_currency_normalized_to)
    row_id = new_row_id()
    row: dict[str, Any] = {
        "row_id": row_id,
        "event_type": "generated",
        "hypothesis_id": hypothesis_id,
        "analysis_id": analysis_id,
        "analysis_date": analysis_date,
        "event_at": event_at or _now_utc(),
        "hypothesis_type": hypothesis_type,
        "primary_ticker": primary_ticker,
        "proxy_tickers": proxy_tickers or [],
        "non_listed_entity": non_listed_entity,
        "observe_only": observe_only,
        "source_event_id": source_event_id,
        "primary_source_agent": primary_source_agent,
        "action_type": action_type,
        "horizon_days": horizon_days,
        "catalyst_score": catalyst_score,
        "scenario_readiness": scenario_readiness,
        "priced_in_penalty": priced_in_penalty,
        "surprise_score": surprise_score,
        "gross_expected_return_bps": gross_expected_return_bps,
        "first_seen_at": first_seen_at,
        "prior_mentions_count": prior_mentions_count,
        "conviction_at_generation": conviction_at_generation,
        "candidate_status": CandidateStatus.generated.value,
        "price_at_event": price_at_event,
        "benchmark_basket": benchmark_basket,
        "benchmark_weights": benchmark_weights,
        "benchmark_currency_normalized_to": benchmark_currency_normalized_to,
        "benchmark_price_at_event": benchmark_price_at_event,
        "usdjpy_at_event": usdjpy_at_event,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


def write_catalyst_hypothesis_status_transition(
    path: Path | str,
    *,
    hypothesis_id: str,
    analysis_id: str,
    analysis_date: str,
    candidate_status: CandidateStatus | str,
    previous_status: CandidateStatus | str,
    reason: str,
    price_at_event: float,
    event_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a ``event_type=status_transition`` row.

    Transitions go ``generated → injected → adopted | injected_rejected``,
    or ``generated → not_injected``, or ``adopted → policy_filtered``.
    """
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "event_type": "status_transition",
        "hypothesis_id": hypothesis_id,
        "analysis_id": analysis_id,
        "analysis_date": analysis_date,
        "event_at": event_at or _now_utc(),
        "candidate_status": _coerce_status(candidate_status, CandidateStatus),
        "previous_status": _coerce_status(previous_status, CandidateStatus),
        "reason": reason,
        "price_at_event": price_at_event,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


def write_catalyst_hypothesis_filtered(
    path: Path | str,
    *,
    hypothesis_id: str,
    analysis_id: str,
    analysis_date: str,
    filter_reason: str,
    missing_fields: list[str],
    filter_rule_version: str = "esg:v1.0",
    filtered_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append an Evidence Sufficiency Gate filter row (Round 7 C7-4)."""
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "event_type": "filtered",
        "hypothesis_id": hypothesis_id,
        "analysis_id": analysis_id,
        "analysis_date": analysis_date,
        "candidate_status": CandidateStatus.not_injected.value,
        "filter_reason": filter_reason,
        "missing_fields": list(missing_fields),
        "filtered_at": filtered_at or _now_utc(),
        "filter_rule_version": filter_rule_version,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


# ---------------------------------------------------------------------------
# catalyst_outcome_log.jsonl (plan §6.14)
# ---------------------------------------------------------------------------


def write_catalyst_outcome(
    path: Path | str,
    *,
    hypothesis_id: str,
    horizon_days: int,
    reference_event_at: str,
    price_at_event: float,
    price_at_measure: float,
    benchmark_basket: list[str],
    benchmark_weights: list[float],
    benchmark_currency_normalized_to: str,
    benchmark_return_pct: float,
    primary_ticker_currency: str,
    usdjpy_at_event: float,
    usdjpy_at_measure: float,
    measurement_quality: str = MeasurementQuality.OK,
    data_source: str = "yfinance",
    after_cost_excess_return_bps: float | None = None,
    measured_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append an outcome measurement row.

    ``return_pct`` and ``excess_return_bps`` are computed from the inputs
    so callers cannot accidentally pre-compute them inconsistently. After-
    cost figures stay ``None`` until a live, wired cost model is introduced.
    """
    _validate_benchmark(benchmark_basket, benchmark_weights, benchmark_currency_normalized_to)
    if price_at_event == 0:
        raise ValueError("price_at_event must be non-zero for return calc")
    return_pct = (price_at_measure - price_at_event) / price_at_event
    excess_return_bps = (return_pct - benchmark_return_pct) * 10_000
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "hypothesis_id": hypothesis_id,
        "horizon_days": horizon_days,
        "measured_at": measured_at or _now_utc(),
        "reference_event_at": reference_event_at,
        "price_at_event": price_at_event,
        "price_at_measure": price_at_measure,
        "return_pct": return_pct,
        "benchmark_basket": benchmark_basket,
        "benchmark_weights": benchmark_weights,
        "benchmark_currency_normalized_to": benchmark_currency_normalized_to,
        "benchmark_return_pct": benchmark_return_pct,
        "excess_return_bps": excess_return_bps,
        "primary_ticker_currency": primary_ticker_currency,
        "usdjpy_at_event": usdjpy_at_event,
        "usdjpy_at_measure": usdjpy_at_measure,
        "after_cost_excess_return_bps": after_cost_excess_return_bps,
        "measurement_quality": measurement_quality,
        "data_source": data_source,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


# ---------------------------------------------------------------------------
# sell_decision_log.jsonl + sell_outcome_log.jsonl (plan §6.8, R8-6/R9-3)
# ---------------------------------------------------------------------------


def write_sell_decision(
    path: Path | str,
    *,
    sell_decision_id: str,
    ticker: str,
    action_type: str,  # "sell" | "trim"
    shares_recommended: int,
    price_at_recommend: float,
    reason: str,
    conviction_at_sell: int,
    benchmark_basket: list[str],
    benchmark_weights: list[float],
    hypothesis_id: str | None = None,
    shares_executed: int | None = None,
    price_at_execute: float | None = None,
    recommended_at: str | None = None,
    ordered_at: str | None = None,
    executed_at: str | None = None,
    cancelled_at: str | None = None,
    execution_id: str | None = None,
    execution_state: ExecutionState | str = ExecutionState.not_ordered,
    context_blocks: dict | None = None,
    narrative_context_present: bool | None = None,
    fsync: bool = True,
) -> str:
    """Append a sell/trim recommendation event.

    R8-6 mandates strict separation of ``recommended_at`` / ``ordered_at``
    / ``executed_at`` / ``cancelled_at``. The writer accepts all four so a
    later append in a subsequent run can record the execution lifecycle by
    re-emitting a row with later timestamps populated.
    """
    if action_type not in {"sell", "trim"}:
        raise ValueError(
            f"sell_decision_log accepts action_type in {{sell, trim}}, "
            f"got {action_type!r}"
        )
    # Validate basket without currency normalization (sell_decision_log is
    # decision-only; the outcome log will validate the normalization).
    if not benchmark_basket or len(benchmark_basket) != len(benchmark_weights):
        raise ValueError("benchmark_basket / benchmark_weights mismatch")
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "sell_decision_id": sell_decision_id,
        "hypothesis_id": hypothesis_id,
        "ticker": ticker,
        "action_type": action_type,
        "shares_recommended": shares_recommended,
        "shares_executed": shares_executed,
        "price_at_recommend": price_at_recommend,
        "price_at_execute": price_at_execute,
        "reason": reason,
        "conviction_at_sell": conviction_at_sell,
        "recommended_at": recommended_at or _now_utc(),
        "ordered_at": ordered_at,
        "executed_at": executed_at,
        "cancelled_at": cancelled_at,
        "execution_id": execution_id,
        "execution_state": _coerce_status(execution_state, ExecutionState),
        "benchmark_basket": benchmark_basket,
        "benchmark_weights": benchmark_weights,
        "context_blocks": context_blocks or {},
        "narrative_context_present": narrative_context_present,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


def write_sell_outcome(
    path: Path | str,
    *,
    sell_decision_id: str,
    horizon_days: int,
    price_at_recommend: float,
    counterfactual_price: float,
    benchmark_return_pct: float,
    benchmark_basket: list[str],
    benchmark_weights: list[float],
    benchmark_currency_normalized_to: str,
    primary_ticker_currency: str,
    usdjpy_at_recommend: float,
    usdjpy_at_measure: float,
    measurement_quality: str = MeasurementQuality.OK,
    data_source: str = "yfinance",
    measured_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a sell counterfactual outcome.

    ``missed_gain_pct`` and ``missed_excess_return_bps`` are computed so
    "positive missed_excess_return = opportunity cost" is the consistent
    convention everywhere downstream.
    """
    _validate_benchmark(benchmark_basket, benchmark_weights, benchmark_currency_normalized_to)
    if price_at_recommend == 0:
        raise ValueError("price_at_recommend must be non-zero")
    missed_gain_pct = (counterfactual_price - price_at_recommend) / price_at_recommend
    missed_excess_return_bps = (missed_gain_pct - benchmark_return_pct) * 10_000
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "sell_decision_id": sell_decision_id,
        "horizon_days": horizon_days,
        "measured_at": measured_at or _now_utc(),
        "price_at_recommend": price_at_recommend,
        "counterfactual_price": counterfactual_price,
        "missed_gain_pct": missed_gain_pct,
        "benchmark_basket": benchmark_basket,
        "benchmark_weights": benchmark_weights,
        "benchmark_currency_normalized_to": benchmark_currency_normalized_to,
        "benchmark_return_pct": benchmark_return_pct,
        "missed_excess_return_bps": missed_excess_return_bps,
        "primary_ticker_currency": primary_ticker_currency,
        "usdjpy_at_recommend": usdjpy_at_recommend,
        "usdjpy_at_measure": usdjpy_at_measure,
        "measurement_quality": measurement_quality,
        "data_source": data_source,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


# ---------------------------------------------------------------------------
# agent_attribution_log.jsonl (plan §6.10, R11-1 flat-row)
# ---------------------------------------------------------------------------

_VALID_ROLES = {"originator", "specialist", "critic", "final_decider"}
_VALID_STANCES = {"support", "oppose", "veto", "reject", "neutral"}


def write_agent_attribution(
    path: Path | str,
    *,
    hypothesis_id: str,
    analysis_id: str,
    analysis_date: str,
    ticker: str,
    hypothesis_type: str,
    time_horizon_days: int,
    agent: str,
    role: str,
    stance: str,
    confidence_pct: int | None = None,
    evidence_ids: list[str] | None = None,
    reason: str | None = None,
    severity: str | None = None,
    issue_type: str | None = None,
    final_candidate_status: CandidateStatus | str | None = None,
    recorded_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a single agent's attribution event (R11 #1: 1 agent = 1 row).

    The Round 6/11 attribution design uses append-event-per-agent, so each
    agent (catalyst_layer / Sonnet tiers / DeepSeek / Red Team / Opus) calls
    this independently. Reports rebuild the full ``agents`` list by
    group-by on ``hypothesis_id × analysis_id``.
    """
    if role not in _VALID_ROLES:
        raise ValueError(f"role must be in {_VALID_ROLES}, got {role!r}")
    if stance not in _VALID_STANCES:
        raise ValueError(f"stance must be in {_VALID_STANCES}, got {stance!r}")
    row_id = new_row_id()
    row: dict[str, Any] = {
        "row_id": row_id,
        "hypothesis_id": hypothesis_id,
        "analysis_id": analysis_id,
        "analysis_date": analysis_date,
        "ticker": ticker,
        "hypothesis_type": hypothesis_type,
        "time_horizon_days": time_horizon_days,
        "agent": agent,
        "role": role,
        "stance": stance,
        "confidence_pct": confidence_pct,
        "evidence_ids": evidence_ids,
        "reason": reason,
        "severity": severity,
        "issue_type": issue_type,
        "recorded_at": recorded_at or _now_utc(),
    }
    if final_candidate_status is not None:
        row["final_candidate_status"] = _coerce_status(
            final_candidate_status, CandidateStatus
        )
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


# ---------------------------------------------------------------------------
# portfolio_decision_log.jsonl (plan §6.11)
# ---------------------------------------------------------------------------


def write_portfolio_decision(
    path: Path | str,
    *,
    analysis_date: str,
    analysis_id: str,
    portfolio_decision_state: PortfolioDecisionState | str,
    risk_mode: str,
    cash_ratio: float,
    total_assets_jpy: float,
    active_scenarios: list[dict[str, Any]],
    generated_candidates: int,
    injected_candidates: int,
    adopted_candidates: int,
    rejected_count_by_reason: dict[str, int],
    cash_critic_triggered: bool,
    benchmark_return_today: float,
    portfolio_return_today: float,
    opportunity_cost_today_bps: float,
    opus_no_buy_reason: str | None = None,
    fsync: bool = True,
) -> str:
    """Append the daily portfolio-level decision row.

    There is exactly one such row per ``analyzer.py`` run; that is the
    "why didn't we buy today" anchor (Round 7 C7-3).
    """
    if risk_mode not in {"aggressive", "defensive", "neutral"}:
        raise ValueError(f"risk_mode invalid: {risk_mode!r}")
    if not (0.0 <= cash_ratio <= 1.0):
        raise ValueError(f"cash_ratio must be in [0, 1], got {cash_ratio}")
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "analysis_date": analysis_date,
        "analysis_id": analysis_id,
        "portfolio_decision_state": _coerce_status(
            portfolio_decision_state, PortfolioDecisionState
        ),
        "risk_mode": risk_mode,
        "cash_ratio": cash_ratio,
        "total_assets_jpy": total_assets_jpy,
        "active_scenarios": active_scenarios,
        "generated_candidates": generated_candidates,
        "injected_candidates": injected_candidates,
        "adopted_candidates": adopted_candidates,
        "rejected_count_by_reason": rejected_count_by_reason,
        "opus_no_buy_reason": opus_no_buy_reason,
        "cash_critic_triggered": cash_critic_triggered,
        "benchmark_return_today": benchmark_return_today,
        "portfolio_return_today": portfolio_return_today,
        "opportunity_cost_today_bps": opportunity_cost_today_bps,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


# ---------------------------------------------------------------------------
# cash_deployment_log.jsonl (plan §6.12, R11-2 event_type split)
# ---------------------------------------------------------------------------


def write_cash_critic_triggered(
    path: Path | str,
    *,
    cash_decision_id: str,
    analysis_date: str,
    analysis_id: str,
    risk_mode: str,
    cash_ratio: float,
    cash_ratio_threshold: float,
    active_bull_scenarios: list[str],
    generated_candidates: int,
    adopted_candidates: int,
    warning_text: str,
    portfolio_decision_state: PortfolioDecisionState | str,
    benchmark_basket: list[str],
    benchmark_weights: list[float],
    opus_response_acknowledged: bool = False,
    opus_no_buy_reason: str | None = None,
    recorded_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a ``event_type=critic_triggered`` row.

    Paired with subsequent ``event_type=follow_up_outcome`` rows by
    ``cash_decision_id``. Per R11-2 the schema is event-typed rather than
    using nullable ``follow_up_outcome_*`` columns.
    """
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "event_type": "critic_triggered",
        "cash_decision_id": cash_decision_id,
        "analysis_date": analysis_date,
        "analysis_id": analysis_id,
        "critic_triggered": True,
        "risk_mode": risk_mode,
        "cash_ratio": cash_ratio,
        "cash_ratio_threshold": cash_ratio_threshold,
        "active_bull_scenarios": active_bull_scenarios,
        "generated_candidates": generated_candidates,
        "adopted_candidates": adopted_candidates,
        "warning_text": warning_text,
        "opus_response_acknowledged": opus_response_acknowledged,
        "opus_no_buy_reason": opus_no_buy_reason,
        "portfolio_decision_state": _coerce_status(
            portfolio_decision_state, PortfolioDecisionState
        ),
        "recorded_at": recorded_at or _now_utc(),
        "benchmark_basket": benchmark_basket,
        "benchmark_weights": benchmark_weights,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


def write_cash_follow_up_outcome(
    path: Path | str,
    *,
    cash_decision_id: str,
    horizon_days: int,
    benchmark_return_pct: float,
    opportunity_cost_bps: float,
    measurement_quality: str = MeasurementQuality.OK,
    data_source: str = "yfinance",
    measured_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a ``event_type=follow_up_outcome`` row.

    ``opportunity_cost_bps > 0`` means the cash position underperformed the
    benchmark — confirming the cash_deployment_critic was right to warn.
    Negative ``opportunity_cost_bps`` means cash was the correct call.
    """
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "event_type": "follow_up_outcome",
        "cash_decision_id": cash_decision_id,
        "horizon_days": horizon_days,
        "measured_at": measured_at or _now_utc(),
        "benchmark_return_pct": benchmark_return_pct,
        "opportunity_cost_bps": opportunity_cost_bps,
        "measurement_quality": measurement_quality,
        "data_source": data_source,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id


# ---------------------------------------------------------------------------
# belief_adjustments.jsonl (plan §6.3)
# ---------------------------------------------------------------------------


def write_belief_adjustment(
    path: Path | str,
    *,
    belief_id: str,
    ticker: str,
    delta: int,
    reason: str,
    rule_version: str,
    evidence: dict[str, Any] | None = None,
    applied_at: str | None = None,
    fsync: bool = True,
) -> str:
    """Append a single belief-conviction adjustment event.

    ``invalidation_rules.py`` writes here; ``analyst/__init__.py`` reads
    the log and computes ``adjusted_conviction = base_conviction +
    Σdelta`` per belief at synthesis time. This keeps the agent_beliefs
    file mutation-free (Round 3 belief_adjustments separation).
    """
    if not isinstance(delta, int):
        raise TypeError(f"delta must be int, got {type(delta).__name__}")
    row_id = new_row_id()
    row = {
        "row_id": row_id,
        "adjustment_id": row_id,  # alias for §6.3 consumers
        "belief_id": belief_id,
        "ticker": ticker,
        "delta": delta,
        "reason": reason,
        "evidence": evidence or {},
        "applied_at": applied_at or _now_utc(),
        "rule_version": rule_version,
    }
    append_jsonl_safe(path, row, fsync=fsync)
    return row_id
