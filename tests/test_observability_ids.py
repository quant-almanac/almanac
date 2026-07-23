"""Tests for almanac.observability.ids.

Verifies the 3-tier ID contract finalized in Round 8/9 of the design dialectic:

- ``hypothesis_id`` is stable across days for the same logical hypothesis.
- ``hypothesis_id`` differs when any defining attribute differs.
- ``row_id`` and ``analysis_id`` are unique per call (UUID4).
- ``cash_decision_id`` joins ``critic_triggered`` and ``follow_up_outcome``
  rows of cash_deployment_log.jsonl.
"""

from __future__ import annotations

import pytest

from almanac.observability.ids import (
    compute_hypothesis_id,
    compute_source_event_id,
    new_analysis_id,
    new_cash_decision_id,
    new_row_id,
)


# ---------------------------------------------------------------------------
# compute_hypothesis_id
# ---------------------------------------------------------------------------


def _hid(**overrides):
    """Helper: build a hypothesis_id with sensible defaults."""
    defaults = dict(
        ticker="NVDA",
        action_type="buy",
        hypothesis_type="earnings_revision_pullback",
        horizon_days=10,
        source_event_id="news:guidance_raise_2026_q1",
    )
    defaults.update(overrides)
    return compute_hypothesis_id(**defaults)


def test_hypothesis_id_is_stable_across_calls() -> None:
    """Same inputs must produce identical IDs — the multi-day join key."""
    assert _hid() == _hid()


def test_hypothesis_id_is_16_hex_chars() -> None:
    """Format contract: 16 hex chars (64-bit truncated sha256)."""
    hid = _hid()
    assert len(hid) == 16
    assert all(c in "0123456789abcdef" for c in hid)


@pytest.mark.parametrize(
    "field",
    ["ticker", "action_type", "hypothesis_type", "horizon_days", "source_event_id"],
)
def test_hypothesis_id_differs_per_field(field: str) -> None:
    """Changing any defining attribute changes the ID."""
    base = _hid()
    new_value = {
        "ticker": "AVGO",
        "action_type": "sell",
        "hypothesis_type": "bull_pullback",
        "horizon_days": 20,
        "source_event_id": "news:other_event",
    }[field]
    assert _hid(**{field: new_value}) != base


def test_hypothesis_id_independent_of_analysis_date() -> None:
    """Round 8 #1 — analysis_date must NOT affect the ID.

    The function signature deliberately excludes ``analysis_date`` so it
    cannot be passed; this test guards against a regression where someone
    sneaks it back into the hash input.
    """
    import inspect

    sig = inspect.signature(compute_hypothesis_id)
    assert "analysis_date" not in sig.parameters


def test_hypothesis_id_rejects_empty_source_event_id() -> None:
    """R19 — empty event IDs would collapse unrelated hypotheses."""
    with pytest.raises(ValueError, match="source_event_id must be non-empty"):
        _hid(source_event_id="")


def test_hypothesis_id_unicode_safe() -> None:
    """Source events with non-ASCII text (Japanese headlines) must work."""
    hid = _hid(source_event_id="news:上方修正_9999_2026")
    assert len(hid) == 16


# ---------------------------------------------------------------------------
# compute_source_event_id
# ---------------------------------------------------------------------------


def test_source_event_id_prefers_native_doc_id() -> None:
    """Native ids survive URL changes / corrected re-filings — preferred."""
    sid = compute_source_event_id(
        "edgar",
        native_doc_id="0000320193-26-000010",
        source_url="https://sec.gov/whatever",
    )
    assert sid == "edgar:0000320193-26-000010"


def test_source_event_id_namespaced_and_lowercased() -> None:
    assert compute_source_event_id("EDINET", native_doc_id="S1001ABC").startswith("edinet:")


def test_source_event_id_url_fallback_is_stable() -> None:
    a = compute_source_event_id("news", source_url="https://x.com/a")
    b = compute_source_event_id("news", source_url="https://x.com/a")
    assert a == b
    assert a.startswith("news:url:")


def test_source_event_id_requires_source() -> None:
    with pytest.raises(ValueError, match="source must be non-empty"):
        compute_source_event_id("", native_doc_id="x")


def test_source_event_id_requires_anchor() -> None:
    with pytest.raises(ValueError, match="native_doc_id or source_url"):
        compute_source_event_id("news")


# ---------------------------------------------------------------------------
# new_row_id / new_analysis_id
# ---------------------------------------------------------------------------


def test_new_row_id_is_unique() -> None:
    """Trivially-distinct UUIDs."""
    ids = {new_row_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_new_analysis_id_is_unique() -> None:
    ids = {new_analysis_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_row_id_and_analysis_id_distinct_namespaces() -> None:
    """Both are UUID4 but should never accidentally collide."""
    assert new_row_id() != new_analysis_id()


# ---------------------------------------------------------------------------
# new_cash_decision_id
# ---------------------------------------------------------------------------


def test_cash_decision_id_stable_for_same_inputs() -> None:
    """Same inputs → same ID, so follow_up_outcome rows can join."""
    a = new_cash_decision_id("2026-05-24", "aggressive", 0.3400)
    b = new_cash_decision_id("2026-05-24", "aggressive", 0.3400)
    assert a == b


def test_cash_decision_id_rounds_cash_ratio_to_4dp() -> None:
    """Tiny drift in cash_ratio between trigger and follow-up must not desync."""
    a = new_cash_decision_id("2026-05-24", "aggressive", 0.34001)
    b = new_cash_decision_id("2026-05-24", "aggressive", 0.34002)
    assert a == b, "rounding to 4 decimals should absorb sub-bp drift"


def test_cash_decision_id_differs_per_field() -> None:
    """Each input contributes — fundamental joinability sanity check."""
    base = new_cash_decision_id("2026-05-24", "aggressive", 0.34)
    assert new_cash_decision_id("2026-05-25", "aggressive", 0.34) != base
    assert new_cash_decision_id("2026-05-24", "defensive", 0.34) != base
    assert new_cash_decision_id("2026-05-24", "aggressive", 0.41) != base
