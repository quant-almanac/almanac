"""Status enums for the catalyst observability layer (3-axis taxonomy).

The 11-round dialectic (Codex × Claude) converged on a 3-axis orthogonal
status model where each axis captures a distinct dimension of a candidate's
lifecycle (plan Round 8 #2 / Round 9 #2):

1. :class:`CandidateStatus` — the *decision* axis. Tracks how a hypothesis
   flowed through the catalyst_layer + Opus deliberation.
2. :class:`ExecutionState` — the *execution* axis. Tracks whether the
   recommended action was actually ordered and filled.
3. :class:`PortfolioDecisionState` — the *portfolio* axis. Tracks the daily
   portfolio-level decision (one row per ``analyzer.py`` run).

Crucially, ``user_not_executed`` lives on the execution axis, not the
candidate axis — a candidate can be ``adopted`` (decision OK) yet
``user_not_executed`` (operator skipped the trade). Mixing these axes was
the bug Codex caught in Round 8 #2 and Round 9 #2.

A ``legacy`` value exists on each enum so historical entries written before
the schema migration continue to parse without raising.
"""

from __future__ import annotations

from enum import Enum
from typing import Final

__all__ = [
    "CandidateStatus",
    "ExecutionState",
    "PortfolioDecisionState",
    "LEGACY_CANDIDATE_STATUS",
    "LEGACY_EXECUTION_STATE",
]


class CandidateStatus(str, Enum):
    """How a hypothesis fared in the catalyst_layer + Opus pipeline."""

    #: Catalyst layer produced the candidate but has not yet decided to inject.
    generated = "generated"
    #: Catalyst layer chose to inject this candidate into the Opus prompt.
    injected = "injected"
    #: Opus took action (it appears in ``priority_actions``).
    adopted = "adopted"
    #: Opus saw the candidate but explicitly rejected it in ``rejection_notes``.
    injected_rejected = "injected_rejected"
    #: Catalyst layer filtered the candidate (score / evidence / etc.) before
    #: Opus ever saw it. Distinct from ``injected_rejected`` because the
    #: improvement lever is the *catalyst layer threshold*, not Opus.
    not_injected = "not_injected"
    #: Adopted by Opus but blocked downstream by policy_engine (VaR / DD /
    #: leverage / freshness gate).
    policy_filtered = "policy_filtered"
    #: Backward-compat marker for rows written before the migration.
    legacy = "legacy"


class ExecutionState(str, Enum):
    """Whether the recommended action was ordered and filled."""

    #: No order has been placed yet (the default after ``adopted``).
    not_ordered = "not_ordered"
    #: Order placed at the broker but not yet filled.
    ordered = "ordered"
    #: Order filled.
    executed = "executed"
    #: Order cancelled before fill (by the system).
    cancelled = "cancelled"
    #: The operator chose not to execute despite the AI recommendation.
    #: Moved here from CandidateStatus in Round 8 #2 — this is an execution
    #: concern, not a decision concern.
    user_not_executed = "user_not_executed"
    #: The recommendation expired (horizon elapsed) without being executed.
    expired = "expired"
    #: Backward-compat marker.
    legacy = "legacy"


class PortfolioDecisionState(str, Enum):
    """Daily portfolio-level decision (one row per analyzer.py run)."""

    #: One or more priority actions were adopted.
    action_taken = "action_taken"
    #: Candidates existed but Opus chose to hold cash — the prompt for the
    #: ``cash_deployment_critic``.
    cash_retained = "cash_retained"
    #: Candidate set was empty; signal to revisit universe / screening.
    no_valid_candidates = "no_valid_candidates"
    #: All candidates were blocked by the policy engine.
    risk_blocked = "risk_blocked"
    #: User-initiated pause (``ALMANAC_ENABLE_CATALYST`` unset, manual halt).
    user_paused = "user_paused"


#: Convenience constants for legacy backfill so call sites can read clearly.
LEGACY_CANDIDATE_STATUS: Final[CandidateStatus] = CandidateStatus.legacy
LEGACY_EXECUTION_STATE: Final[ExecutionState] = ExecutionState.legacy
