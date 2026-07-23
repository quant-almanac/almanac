"""Tests for almanac.observability.status.

The 3-axis status taxonomy is the linchpin of the entire observability layer.
These tests pin down the exact enum membership so a future refactor cannot
silently re-introduce mixing (the bug Codex caught in Round 8 #2 / Round 9 #2).
"""

from __future__ import annotations

import json
from enum import Enum

import pytest

from almanac.observability.status import (
    LEGACY_CANDIDATE_STATUS,
    LEGACY_EXECUTION_STATE,
    CandidateStatus,
    ExecutionState,
    PortfolioDecisionState,
)


# ---------------------------------------------------------------------------
# Enum membership (locked by spec — change here = change in plan)
# ---------------------------------------------------------------------------


def test_candidate_status_has_exactly_seven_values() -> None:
    """6 production states + 1 legacy marker."""
    expected = {
        "generated",
        "injected",
        "adopted",
        "injected_rejected",
        "not_injected",
        "policy_filtered",
        "legacy",
    }
    assert {s.value for s in CandidateStatus} == expected


def test_execution_state_has_exactly_seven_values() -> None:
    """6 production states + 1 legacy marker."""
    expected = {
        "not_ordered",
        "ordered",
        "executed",
        "cancelled",
        "user_not_executed",
        "expired",
        "legacy",
    }
    assert {s.value for s in ExecutionState} == expected


def test_portfolio_decision_state_has_exactly_five_values() -> None:
    """No legacy on this axis — it was new in Round 7 C7-3."""
    expected = {
        "action_taken",
        "cash_retained",
        "no_valid_candidates",
        "risk_blocked",
        "user_paused",
    }
    assert {s.value for s in PortfolioDecisionState} == expected


# ---------------------------------------------------------------------------
# Cross-axis hygiene
# ---------------------------------------------------------------------------


def test_user_not_executed_lives_on_execution_axis_only() -> None:
    """Round 8 #2 / Round 9 #2 — the bug fix locked in by enum design."""
    candidate_values = {s.value for s in CandidateStatus}
    execution_values = {s.value for s in ExecutionState}
    assert "user_not_executed" in execution_values
    assert "user_not_executed" not in candidate_values


def test_no_action_does_not_live_on_candidate_axis() -> None:
    """Round 11 #4 — candidate enum must not contain no_action."""
    assert "no_action" not in {s.value for s in CandidateStatus}


def test_no_action_does_not_live_on_execution_axis() -> None:
    """Belt and suspenders: no_action belongs to portfolio_decision_state."""
    assert "no_action" not in {s.value for s in ExecutionState}


def test_no_overlap_between_candidate_and_portfolio_axes() -> None:
    """The three axes are orthogonal; values must not collide."""
    candidate = {s.value for s in CandidateStatus}
    portfolio = {s.value for s in PortfolioDecisionState}
    assert candidate.isdisjoint(portfolio)


# ---------------------------------------------------------------------------
# Serialization (str enums → JSON strings without .value access)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("enum_cls", [CandidateStatus, ExecutionState, PortfolioDecisionState])
def test_enums_are_str_enums(enum_cls: type[Enum]) -> None:
    """str subclassing lets json.dumps emit bare strings — needed for
    append_only_log.append_jsonl_safe."""
    member = next(iter(enum_cls))
    assert isinstance(member, str)


def test_enums_round_trip_through_json() -> None:
    row = {
        "candidate_status": CandidateStatus.injected_rejected,
        "execution_state": ExecutionState.user_not_executed,
        "portfolio_decision_state": PortfolioDecisionState.cash_retained,
    }
    encoded = json.dumps(row)
    decoded = json.loads(encoded)
    assert decoded["candidate_status"] == "injected_rejected"
    assert decoded["execution_state"] == "user_not_executed"
    assert decoded["portfolio_decision_state"] == "cash_retained"


# ---------------------------------------------------------------------------
# Legacy markers
# ---------------------------------------------------------------------------


def test_legacy_markers_are_correct() -> None:
    assert LEGACY_CANDIDATE_STATUS is CandidateStatus.legacy
    assert LEGACY_EXECUTION_STATE is ExecutionState.legacy


def test_legacy_values_are_plain_string_legacy() -> None:
    """recommendation_verifier wildcards on this exact value."""
    assert CandidateStatus.legacy.value == "legacy"
    assert ExecutionState.legacy.value == "legacy"
