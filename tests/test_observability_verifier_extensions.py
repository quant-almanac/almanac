"""Tests for almanac.observability.verifier_extensions.

The sidecar reads append-only logs and reduces them to the EV / hit-rate
/ payoff-ratio rollups plan §5 step 4 requires. Coverage pins:

- ``compute_group_stats`` math (EV, payoff ratio, hit rate, std).
- Empty / single-sample edge cases return ``None`` instead of crashing.
- ``latest_candidate_status`` picks the most recent event per
  ``hypothesis_id`` and survives missing ``event_at`` timestamps.
- ``aggregate_by_dimensions`` joins outcomes ↔ hypothesis events via
  ``hypothesis_id``, buckets unknown hypotheses safely, and supports
  multiple grouping schemes.
- ``summarize`` returns ``[]`` groups on missing-file inputs (MVP
  start-of-life) without raising.
- Malformed JSONL rows are skipped silently — an audit-log reader must
  never blow up the analyzer.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.status import CandidateStatus  # noqa: E402
from almanac.observability.verifier_extensions import (  # noqa: E402
    aggregate_by_dimensions,
    compute_group_stats,
    latest_candidate_status,
    read_hypothesis_events,
    read_outcomes,
    summarize,
)


# ---------------------------------------------------------------------------
# compute_group_stats — math
# ---------------------------------------------------------------------------


def test_empty_outcomes_returns_all_none() -> None:
    s = compute_group_stats([])
    assert s["n"] == 0
    for key in ("ev_bps", "median_bps", "std_bps", "hit_rate",
                "payoff_ratio", "max_gain_bps", "max_loss_bps"):
        assert s[key] is None, key
    assert s["win_count"] == 0
    assert s["loss_count"] == 0


def test_ev_is_mean_of_excess_return_bps() -> None:
    s = compute_group_stats([
        {"excess_return_bps": 250},
        {"excess_return_bps": -80},
        {"excess_return_bps": 150},
    ])
    assert s["n"] == 3
    assert s["ev_bps"] == pytest.approx((250 - 80 + 150) / 3)


def test_hit_rate_is_secondary_metric() -> None:
    s = compute_group_stats([
        {"excess_return_bps": 100},
        {"excess_return_bps": -50},
        {"excess_return_bps": 200},
    ])
    assert s["hit_rate"] == pytest.approx(2 / 3)
    assert s["win_count"] == 2
    assert s["loss_count"] == 1


def test_payoff_ratio_avg_win_over_avg_loss() -> None:
    s = compute_group_stats([
        {"excess_return_bps": 200},
        {"excess_return_bps": 200},
        {"excess_return_bps": -50},
        {"excess_return_bps": -150},
    ])
    # avg win = 200; avg loss = 100; ratio = 2.0
    assert s["payoff_ratio"] == pytest.approx(2.0)


def test_payoff_ratio_is_none_when_no_losses() -> None:
    """The metric is meaningless without both wins and losses."""
    s = compute_group_stats([{"excess_return_bps": 100}, {"excess_return_bps": 200}])
    assert s["payoff_ratio"] is None


def test_payoff_ratio_is_none_when_no_wins() -> None:
    s = compute_group_stats([{"excess_return_bps": -50}, {"excess_return_bps": -150}])
    assert s["payoff_ratio"] is None


def test_falls_back_to_return_pct_when_excess_missing() -> None:
    """Legacy outcome rows have only return_pct; we scale to bps."""
    s = compute_group_stats([{"return_pct": 0.05}, {"return_pct": -0.02}])
    assert s["n"] == 2
    assert s["ev_bps"] == pytest.approx((500 - 200) / 2)


def test_drops_non_finite_returns() -> None:
    """NaN / inf would corrupt every downstream metric."""
    s = compute_group_stats([
        {"excess_return_bps": float("nan")},
        {"excess_return_bps": float("inf")},
        {"excess_return_bps": 100},
    ])
    assert s["n"] == 1
    assert s["ev_bps"] == pytest.approx(100.0)


def test_drops_non_numeric_values() -> None:
    s = compute_group_stats([
        {"excess_return_bps": "not a number"},
        {"excess_return_bps": None},
        {"excess_return_bps": 50},
    ])
    assert s["n"] == 1


def test_max_gain_and_loss_track_extremes() -> None:
    s = compute_group_stats([
        {"excess_return_bps": 50},
        {"excess_return_bps": 750},
        {"excess_return_bps": -200},
    ])
    assert s["max_gain_bps"] == 750
    assert s["max_loss_bps"] == -200


def test_std_requires_two_samples() -> None:
    assert compute_group_stats([{"excess_return_bps": 100}])["std_bps"] is None
    assert compute_group_stats(
        [{"excess_return_bps": 100}, {"excess_return_bps": -100}]
    )["std_bps"] is not None


def test_median_handles_even_and_odd_counts() -> None:
    assert compute_group_stats(
        [{"excess_return_bps": v} for v in (1, 2, 3, 4)]
    )["median_bps"] == 2.5
    assert compute_group_stats(
        [{"excess_return_bps": v} for v in (1, 2, 3, 4, 5)]
    )["median_bps"] == 3


# ---------------------------------------------------------------------------
# latest_candidate_status
# ---------------------------------------------------------------------------


def test_latest_status_picks_most_recent_event() -> None:
    events = [
        {"hypothesis_id": "h1", "event_at": "2026-05-24T10:00:00",
         "candidate_status": "generated", "ticker": "NVDA"},
        {"hypothesis_id": "h1", "event_at": "2026-05-24T18:00:00",
         "candidate_status": "adopted", "ticker": "NVDA"},
        {"hypothesis_id": "h1", "event_at": "2026-05-24T15:00:00",
         "candidate_status": "injected", "ticker": "NVDA"},
    ]
    latest = latest_candidate_status(events)
    assert latest["h1"]["candidate_status"] == "adopted"


def test_latest_status_falls_back_to_generated_at_then_analysis_date() -> None:
    """status_transition rows have event_at; generated rows have generated_at."""
    events = [
        {"hypothesis_id": "h1", "generated_at": "2026-05-24T10:00:00",
         "candidate_status": "generated"},
        {"hypothesis_id": "h1", "event_at": "2026-05-25T09:00:00",
         "candidate_status": "adopted"},
    ]
    latest = latest_candidate_status(events)
    assert latest["h1"]["candidate_status"] == "adopted"


def test_latest_status_skips_events_without_hypothesis_id() -> None:
    """Some logs (portfolio_decision_log) have no hypothesis_id."""
    events = [
        {"event_at": "2026-05-24T10:00:00", "candidate_status": "generated"},
        {"hypothesis_id": "h1", "event_at": "2026-05-24T10:00:00",
         "candidate_status": "generated"},
    ]
    latest = latest_candidate_status(events)
    assert set(latest.keys()) == {"h1"}


def test_latest_status_returns_first_when_no_timestamps() -> None:
    """Two events with no timestamps — implementation is allowed to
    pick either, but it must not crash."""
    events = [
        {"hypothesis_id": "h1", "candidate_status": "generated"},
        {"hypothesis_id": "h1", "candidate_status": "adopted"},
    ]
    latest = latest_candidate_status(events)
    assert "h1" in latest


# ---------------------------------------------------------------------------
# aggregate_by_dimensions — join + bucketing
# ---------------------------------------------------------------------------


def test_aggregate_buckets_by_default_dimensions() -> None:
    events = [
        {"hypothesis_id": "h1", "event_at": "2026-05-24T18:00:00",
         "hypothesis_type": "earnings_revision_pullback",
         "candidate_status": "adopted", "ticker": "NVDA"},
        {"hypothesis_id": "h2", "event_at": "2026-05-24T18:00:00",
         "hypothesis_type": "earnings_revision_pullback",
         "candidate_status": "injected_rejected", "ticker": "AVGO"},
    ]
    outcomes = [
        {"hypothesis_id": "h1", "horizon_days": 5, "excess_return_bps": 200},
        {"hypothesis_id": "h1", "horizon_days": 10, "excess_return_bps": 350},
        {"hypothesis_id": "h2", "horizon_days": 5, "excess_return_bps": 100},
    ]
    agg = aggregate_by_dimensions(events, outcomes)
    assert ("earnings_revision_pullback", "adopted", 5) in agg
    assert ("earnings_revision_pullback", "adopted", 10) in agg
    assert ("earnings_revision_pullback", "injected_rejected", 5) in agg
    # Adopted+5d aggregation has one row, EV = 200.
    bucket = agg[("earnings_revision_pullback", "adopted", 5)]
    assert bucket["n"] == 1
    assert bucket["ev_bps"] == 200


def test_aggregate_supports_custom_dimensions() -> None:
    events = [
        {"hypothesis_id": "h1", "event_at": "2026-05-24T18:00:00",
         "hypothesis_type": "ipo_proxy", "candidate_status": "adopted",
         "ticker": "9984.T"},
    ]
    outcomes = [
        {"hypothesis_id": "h1", "horizon_days": 20, "excess_return_bps": 500},
    ]
    agg = aggregate_by_dimensions(
        events, outcomes,
        dimensions=("ticker", "horizon_days"),
    )
    assert ("9984.T", 20) in agg


def test_aggregate_uses_unknown_bucket_for_outcomes_without_event() -> None:
    """Round 9 #3 invariant — never silently drop outcome rows."""
    agg = aggregate_by_dimensions(
        hypothesis_events=[],
        outcomes=[{"hypothesis_id": "ghost", "horizon_days": 5,
                   "excess_return_bps": 100}],
    )
    expected_key = ("unknown", CandidateStatus.legacy.value, 5)
    assert expected_key in agg
    assert agg[expected_key]["n"] == 1


def test_aggregate_drops_outcomes_without_hypothesis_id() -> None:
    """A row that cannot be joined to anything has no place in the report."""
    agg = aggregate_by_dimensions(
        hypothesis_events=[],
        outcomes=[
            {"horizon_days": 5, "excess_return_bps": 100},
            {"hypothesis_id": "h1", "horizon_days": 5, "excess_return_bps": 200},
        ],
    )
    total_n = sum(stats["n"] for stats in agg.values())
    assert total_n == 1


def test_aggregate_falls_back_to_primary_ticker_when_ticker_missing() -> None:
    """Codex Round 12 P2 #4 — catalyst_hypothesis_log writes
    ``primary_ticker`` (catalyst-layer convention); without a fallback,
    every catalyst entry would bucket under ``ticker=None``."""
    events = [
        {"hypothesis_id": "h1", "event_at": "2026-05-24T18:00:00",
         "hypothesis_type": "ipo_proxy",
         "candidate_status": "adopted",
         "primary_ticker": "9984.T"},   # catalyst writer uses primary_ticker
    ]
    outcomes = [
        {"hypothesis_id": "h1", "horizon_days": 20, "excess_return_bps": 400},
    ]
    agg = aggregate_by_dimensions(
        events, outcomes,
        dimensions=("ticker", "horizon_days"),
    )
    assert ("9984.T", 20) in agg
    # And the None bucket must NOT exist for this group.
    assert (None, 20) not in agg


def test_aggregate_uses_primary_ticker_from_outcome_when_no_event() -> None:
    """If only the outcome carries primary_ticker (no event row), still
    bucket correctly instead of dropping into ``None``."""
    outcomes = [
        {"hypothesis_id": "ghost", "horizon_days": 5,
         "primary_ticker": "NVDA", "excess_return_bps": 100},
    ]
    agg = aggregate_by_dimensions(
        hypothesis_events=[], outcomes=outcomes,
        dimensions=("ticker", "horizon_days"),
    )
    assert ("NVDA", 5) in agg


def test_aggregate_picks_latest_status_for_join() -> None:
    """A status_transition after generation must flow through to the
    bucket: a hypothesis that later flipped to ``injected_rejected``
    must NOT show up in the ``adopted`` bucket."""
    events = [
        {"hypothesis_id": "h1", "event_at": "2026-05-24T18:00:00",
         "hypothesis_type": "earnings_revision_pullback",
         "candidate_status": "adopted", "ticker": "NVDA"},
        {"hypothesis_id": "h1", "event_at": "2026-05-25T08:00:00",
         "hypothesis_type": "earnings_revision_pullback",
         "candidate_status": "injected_rejected", "ticker": "NVDA"},
    ]
    outcomes = [
        {"hypothesis_id": "h1", "horizon_days": 5, "excess_return_bps": 99},
    ]
    agg = aggregate_by_dimensions(events, outcomes)
    assert ("earnings_revision_pullback", "adopted", 5) not in agg
    assert ("earnings_revision_pullback", "injected_rejected", 5) in agg


# ---------------------------------------------------------------------------
# I/O — file readers
# ---------------------------------------------------------------------------


def test_read_returns_empty_for_missing_files(tmp_path: Path) -> None:
    assert read_hypothesis_events(tmp_path / "nope.jsonl") == []
    assert read_outcomes(tmp_path / "nope.jsonl") == []


def test_read_skips_malformed_lines(tmp_path: Path) -> None:
    p = tmp_path / "log.jsonl"
    p.write_text(
        "{valid json one: nope\n"
        '{"hypothesis_id":"h1","horizon_days":5,"excess_return_bps":100}\n'
        "\n"  # blank line
        '{"hypothesis_id":"h2"}\n',
    )
    rows = read_hypothesis_events(p)
    assert len(rows) == 2  # the two valid lines


def test_read_skips_non_dict_payloads(tmp_path: Path) -> None:
    """A line that parses to a list / int / string is not a row."""
    p = tmp_path / "log.jsonl"
    p.write_text('"a string"\n[1,2,3]\n42\n{"hypothesis_id":"h1"}\n')
    assert len(read_outcomes(p)) == 1


# ---------------------------------------------------------------------------
# summarize — end-to-end
# ---------------------------------------------------------------------------


def test_summarize_returns_empty_skeleton_on_missing_logs(tmp_path: Path) -> None:
    """MVP day 0: no logs yet, the report must still be well-formed."""
    out = summarize(
        hypothesis_log_path=tmp_path / "h.jsonl",
        outcome_log_path=tmp_path / "o.jsonl",
    )
    assert out["n_hypothesis_events"] == 0
    assert out["n_outcomes"] == 0
    assert out["n_groups"] == 0
    assert out["groups"] == []
    assert out["dimensions"] == (
        "hypothesis_type", "candidate_status", "horizon_days",
    )


def test_summarize_groups_have_named_key_dict(tmp_path: Path) -> None:
    h = tmp_path / "h.jsonl"
    o = tmp_path / "o.jsonl"
    h.write_text(json.dumps({
        "hypothesis_id": "h1", "event_at": "2026-05-24T18:00:00",
        "hypothesis_type": "bull_pullback",
        "candidate_status": "adopted", "ticker": "NVDA",
    }) + "\n")
    o.write_text(json.dumps({
        "hypothesis_id": "h1", "horizon_days": 10, "excess_return_bps": 300,
    }) + "\n")
    out = summarize(hypothesis_log_path=h, outcome_log_path=o)
    assert out["n_groups"] == 1
    group = out["groups"][0]
    assert group["key"] == {
        "hypothesis_type": "bull_pullback",
        "candidate_status": "adopted",
        "horizon_days": 10,
    }
    assert group["stats"]["n"] == 1
    assert group["stats"]["ev_bps"] == 300


def test_summarize_round_trip_with_realistic_machinery(tmp_path: Path) -> None:
    """Cross-module sanity: use the actual writers from logs.py to build
    the input files, then summarize them."""
    from almanac.observability.logs import (  # noqa: WPS433
        write_catalyst_hypothesis_generated,
        write_catalyst_hypothesis_status_transition,
        write_catalyst_outcome,
    )
    h = tmp_path / "catalyst_hypothesis_log.jsonl"
    o = tmp_path / "catalyst_outcome_log.jsonl"

    common = dict(
        analysis_id="aid",
        analysis_date="2026-05-24",
        hypothesis_type="bull_pullback",
        primary_ticker="NVDA",
        catalyst_score=0.78,
        scenario_readiness=0.55,
        priced_in_penalty=0.10,
        surprise_score=0.7,
        gross_expected_return_bps=200,
        conviction_at_generation=70,
        price_at_event=120.5,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_price_at_event={"QQQ": 478.3},
        usdjpy_at_event=156.2,
    )
    hypothesis_id = None
    write_catalyst_hypothesis_generated(
        h, fsync=False,
        hypothesis_id="hyp-bull-1",
        **common,
    )
    write_catalyst_hypothesis_status_transition(
        h, fsync=False,
        hypothesis_id="hyp-bull-1",
        analysis_id="aid",
        analysis_date="2026-05-24",
        candidate_status="adopted",
        previous_status="injected",
        reason="opus adopted",
        price_at_event=120.5,
    )
    write_catalyst_outcome(
        o, fsync=False,
        hypothesis_id="hyp-bull-1",
        horizon_days=10,
        reference_event_at="2026-05-24T18:30:00",
        price_at_event=120.5,
        price_at_measure=126.5,
        benchmark_basket=["QQQ"],
        benchmark_weights=[1.0],
        benchmark_currency_normalized_to="USD",
        benchmark_return_pct=0.02,
        primary_ticker_currency="USD",
        usdjpy_at_event=156.2,
        usdjpy_at_measure=157.1,
    )

    out = summarize(hypothesis_log_path=h, outcome_log_path=o)
    assert out["n_hypothesis_events"] == 2
    assert out["n_outcomes"] == 1
    assert out["n_groups"] == 1
    group = out["groups"][0]
    assert group["key"]["hypothesis_type"] == "bull_pullback"
    # 126.5/120.5 = 4.979% return; vs 2% benchmark ≈ 297.9 bps excess.
    expected_bps = ((126.5 - 120.5) / 120.5 - 0.02) * 10_000
    assert group["stats"]["ev_bps"] == pytest.approx(expected_bps, rel=1e-6)
