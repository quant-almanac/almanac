"""Tests for almanac.observability.agent_reliability.

Coverage:
- Empty inputs → returns {}
- Single agent with n=5 → weight is None
- Single agent with n=15 → weight is 1.0 (neutral)
- Single agent with n=40, positive excess → weight > 1.0 and <= 1.5
- Single agent with n=40, negative excess → weight < 1.0 and >= 0.5
- Multi-agent join: 2 originators on same hypothesis, both rated
- Outcomes without matching attribution → silently ignored
- Attribution without matching outcomes → group_stats has n=0, weight=None (n<10)
- horizon_days filter: outcomes with different horizon are excluded
- snapshot_to_file atomic write: no .tmp residue after success
- snapshot_to_file roundtrip: reads back exactly what was written
- Cross-module: build via logs.write_agent_attribution + write_catalyst_outcome,
  then snapshot end-to-end
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from almanac.observability.agent_reliability import (  # noqa: E402
    GroupStats,
    aggregate_agent_reliability,
    derive_weight,
    snapshot_to_file,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attr(
    hypothesis_id: str,
    agent: str,
    role: str = "originator",
    stance: str = "support",
    analysis_id: str = "aid1",
) -> dict:
    """Minimal attribution row."""
    return {
        "hypothesis_id": hypothesis_id,
        "analysis_id": analysis_id,
        "agent": agent,
        "role": role,
        "stance": stance,
    }


def _outcome(
    hypothesis_id: str,
    excess_return_bps: float,
    horizon_days: int = 10,
) -> dict:
    """Minimal outcome row."""
    return {
        "hypothesis_id": hypothesis_id,
        "horizon_days": horizon_days,
        "excess_return_bps": excess_return_bps,
    }


def _make_attrs(
    n: int,
    agent: str,
    role: str = "originator",
    stance: str = "support",
) -> list[dict]:
    """Create n attribution rows for distinct hypotheses h1..hN."""
    return [_attr(f"h{i}", agent, role, stance) for i in range(n)]


def _make_outcomes(
    n: int,
    excess_return_bps: float,
    horizon_days: int = 10,
) -> list[dict]:
    """Create n outcome rows matching h0..h(N-1)."""
    return [_outcome(f"h{i}", excess_return_bps, horizon_days) for i in range(n)]


# ---------------------------------------------------------------------------
# derive_weight — pure unit tests
# ---------------------------------------------------------------------------


def test_derive_weight_returns_none_for_n_lt_10() -> None:
    stats = GroupStats(n=9, win_rate=0.6, mean_excess_return_bps=100.0, payoff_ratio=1.5, weight=None)
    assert derive_weight(stats) is None


def test_derive_weight_returns_none_for_n_zero() -> None:
    stats = GroupStats(n=0, win_rate=None, mean_excess_return_bps=None, payoff_ratio=None, weight=None)
    assert derive_weight(stats) is None


def test_derive_weight_returns_1_for_10_to_29() -> None:
    for n in (10, 15, 20, 29):
        stats = GroupStats(n=n, win_rate=0.6, mean_excess_return_bps=500.0, payoff_ratio=2.0, weight=None)
        assert derive_weight(stats) == 1.0, f"n={n}"


def test_derive_weight_positive_excess_returns_above_1() -> None:
    # n=40, mean_bps=3000 → 0.5 + 2*(3000/10000) = 0.5 + 0.6 = 1.1
    stats = GroupStats(n=40, win_rate=0.7, mean_excess_return_bps=3000.0, payoff_ratio=2.0, weight=None)
    w = derive_weight(stats)
    assert w is not None
    assert w > 1.0
    assert w <= 1.5


def test_derive_weight_large_positive_excess_clips_at_1_5() -> None:
    # n=40, mean_bps=10000 → 0.5 + 2*(10000/10000) = 2.5 → clamped to 1.5
    stats = GroupStats(n=40, win_rate=1.0, mean_excess_return_bps=10000.0, payoff_ratio=None, weight=None)
    w = derive_weight(stats)
    assert w == pytest.approx(1.5)


def test_derive_weight_negative_excess_returns_below_1() -> None:
    # n=40, mean_bps=-500 → 0.5 + 2*(-500/10000) = 0.5 - 0.1 = 0.4 → clamped to 0.5
    stats = GroupStats(n=40, win_rate=0.3, mean_excess_return_bps=-500.0, payoff_ratio=0.5, weight=None)
    w = derive_weight(stats)
    assert w is not None
    assert w < 1.0
    assert w >= 0.5


def test_derive_weight_large_negative_excess_clips_at_0_5() -> None:
    # n=40, mean_bps=-10000 → 0.5 + 2*(-10000/10000) = -1.5 → clamped to 0.5
    stats = GroupStats(n=40, win_rate=0.0, mean_excess_return_bps=-10000.0, payoff_ratio=None, weight=None)
    w = derive_weight(stats)
    assert w == pytest.approx(0.5)


def test_derive_weight_none_mean_with_n_ge_30_returns_none() -> None:
    """If n>=30 but mean_bps is None, keep the group explicitly unmeasured."""
    stats = GroupStats(n=40, win_rate=None, mean_excess_return_bps=None, payoff_ratio=None, weight=None)
    assert derive_weight(stats) is None


def test_derive_weight_exact_boundary_n_10() -> None:
    """n=10 is the start of neutral band, not insufficient."""
    stats = GroupStats(n=10, win_rate=0.5, mean_excess_return_bps=200.0, payoff_ratio=1.0, weight=None)
    assert derive_weight(stats) == 1.0


def test_derive_weight_exact_boundary_n_30() -> None:
    """n=30 enters the proportional formula."""
    stats = GroupStats(n=30, win_rate=0.6, mean_excess_return_bps=250.0, payoff_ratio=1.2, weight=None)
    w = derive_weight(stats)
    expected = 0.5 + 2.0 * (250.0 / 10_000.0)
    assert w == pytest.approx(expected)


# ---------------------------------------------------------------------------
# aggregate_agent_reliability — empty inputs
# ---------------------------------------------------------------------------


def test_empty_inputs_returns_empty_dict() -> None:
    result = aggregate_agent_reliability([], [])
    assert result == {}


def test_empty_attribution_returns_empty_dict() -> None:
    outcomes = [_outcome("h1", 200)]
    result = aggregate_agent_reliability([], outcomes)
    assert result == {}


def test_empty_outcomes_still_returns_stats_per_attribution() -> None:
    """Attribution without any outcome → group recorded, but n drives weight."""
    attrs = _make_attrs(5, "agent_x")
    result = aggregate_agent_reliability(attrs, [])
    assert "agent_x" in result
    stats = result["agent_x"]["originator/support"]
    assert stats.n == 5
    assert stats.weight is None  # n < 10
    assert stats.win_rate is None
    assert stats.mean_excess_return_bps is None


# ---------------------------------------------------------------------------
# n-threshold tests via aggregate_agent_reliability
# ---------------------------------------------------------------------------


def test_n5_weight_is_none() -> None:
    attrs = _make_attrs(5, "agent_a")
    outcomes = _make_outcomes(5, excess_return_bps=200.0)
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["agent_a"]["originator/support"]
    assert stats.n == 5
    assert stats.weight is None


def test_n15_weight_is_1_neutral() -> None:
    attrs = _make_attrs(15, "agent_b")
    outcomes = _make_outcomes(15, excess_return_bps=300.0)
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["agent_b"]["originator/support"]
    assert stats.n == 15
    assert stats.weight == pytest.approx(1.0)


def test_n40_positive_excess_weight_above_1() -> None:
    excess = 3000.0  # mean_bps=3000 → 0.5 + 2*(3000/10000) = 1.1
    attrs = _make_attrs(40, "agent_c")
    outcomes = _make_outcomes(40, excess_return_bps=excess)
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["agent_c"]["originator/support"]
    assert stats.n == 40
    assert stats.weight is not None
    assert stats.weight > 1.0
    assert stats.weight <= 1.5


def test_n40_negative_excess_weight_below_1() -> None:
    excess = -200.0  # mean_bps=-200 → 0.5 + 2*(-200/10000) = 0.46 → clamped to 0.5
    attrs = _make_attrs(40, "agent_d")
    outcomes = _make_outcomes(40, excess_return_bps=excess)
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["agent_d"]["originator/support"]
    assert stats.n == 40
    assert stats.weight is not None
    assert stats.weight < 1.0
    assert stats.weight >= 0.5


def test_n40_negative_deep_weight_clamps_at_0_5() -> None:
    excess = -5000.0  # very negative → clamped to 0.5
    attrs = _make_attrs(40, "agent_e")
    outcomes = _make_outcomes(40, excess_return_bps=excess)
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["agent_e"]["originator/support"]
    assert stats.weight == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Multi-agent join
# ---------------------------------------------------------------------------


def test_two_agents_on_same_hypothesis_both_rated() -> None:
    """Two originator agents for the same hypothesis; both should get credit."""
    hypothesis_id = "h1"
    attrs = [
        _attr(hypothesis_id, "agent_alpha", role="originator", stance="support"),
        _attr(hypothesis_id, "agent_beta", role="originator", stance="support"),
    ]
    outcomes = [_outcome(hypothesis_id, excess_return_bps=150.0)]

    result = aggregate_agent_reliability(attrs, outcomes)

    assert "agent_alpha" in result
    assert "agent_beta" in result
    # Both attributed to same hypothesis → same outcome value included.
    alpha_stats = result["agent_alpha"]["originator/support"]
    beta_stats = result["agent_beta"]["originator/support"]
    assert alpha_stats.n == 1
    assert beta_stats.n == 1
    assert alpha_stats.mean_excess_return_bps == pytest.approx(150.0)
    assert beta_stats.mean_excess_return_bps == pytest.approx(150.0)


def test_multiple_roles_and_stances_keyed_separately() -> None:
    """Same agent in originator/support vs specialist/oppose → separate keys."""
    attrs = [
        _attr("h1", "the_agent", role="originator", stance="support"),
        _attr("h2", "the_agent", role="specialist", stance="oppose"),
    ]
    outcomes = [
        _outcome("h1", 100.0),
        _outcome("h2", -50.0),
    ]
    result = aggregate_agent_reliability(attrs, outcomes)
    assert "the_agent" in result
    assert "originator/support" in result["the_agent"]
    assert "specialist/oppose" in result["the_agent"]
    s1 = result["the_agent"]["originator/support"]
    s2 = result["the_agent"]["specialist/oppose"]
    assert s1.n == 1
    assert s2.n == 1
    assert s1.mean_excess_return_bps == pytest.approx(100.0)
    assert s2.mean_excess_return_bps == pytest.approx(-50.0)


# ---------------------------------------------------------------------------
# Join edge cases
# ---------------------------------------------------------------------------


def test_outcomes_without_matching_attribution_silently_ignored() -> None:
    """Outcomes for unknown hypothesis_ids are dropped — not attributed."""
    attrs = [_attr("h1", "agent_x")]
    outcomes = [
        _outcome("h1", 200.0),
        _outcome("ghost_hid", 999.0),  # no attribution row for this
    ]
    result = aggregate_agent_reliability(attrs, outcomes)
    assert "agent_x" in result
    stats = result["agent_x"]["originator/support"]
    # ghost_hid outcome should NOT be counted.
    assert stats.n == 1
    assert stats.mean_excess_return_bps == pytest.approx(200.0)


def test_attribution_without_matching_outcomes_weight_none_for_small_n() -> None:
    """Attribution rows with no corresponding outcomes → n set, metrics None."""
    attrs = [_attr(f"h{i}", "lonely_agent") for i in range(3)]
    result = aggregate_agent_reliability(attrs, [])
    stats = result["lonely_agent"]["originator/support"]
    assert stats.n == 3
    assert stats.weight is None  # n < 10
    assert stats.win_rate is None
    assert stats.mean_excess_return_bps is None
    assert stats.payoff_ratio is None


def test_weight_uses_measured_n_not_total_attribution_count() -> None:
    attrs = _make_attrs(40, "mixed_agent")
    outcomes = _make_outcomes(9, excess_return_bps=300.0)

    result = aggregate_agent_reliability(attrs, outcomes)

    stats = result["mixed_agent"]["originator/support"]
    assert stats.n == 40
    assert stats.measured_n == 9
    assert stats.mean_excess_return_bps == pytest.approx(300.0)
    assert stats.weight is None


def test_repeated_adoption_of_same_hypothesis_joins_outcome_once() -> None:
    """日付非依存 hypothesis_id の再採用が measured_n を水増ししないこと。

    同じ仮説を40回採用しても outcome は (hid, horizon) につき1行なので、
    join される測定値は1個 (採用頻度で mean を重み付けしない)。
    """
    attrs = [_attr("h0", "repeat_agent") for _ in range(40)]
    outcomes = [_outcome("h0", 300.0)]

    result = aggregate_agent_reliability(attrs, outcomes)

    stats = result["repeat_agent"]["originator/support"]
    assert stats.n == 40
    assert stats.measured_n == 1
    assert stats.mean_excess_return_bps == pytest.approx(300.0)
    assert stats.weight is None


def test_attribution_missing_hypothesis_id_skipped() -> None:
    """Rows without hypothesis_id cannot join; they must not crash."""
    attrs = [
        {"agent": "agent_x", "role": "originator", "stance": "support"},  # no hid
        _attr("h1", "agent_x"),
    ]
    outcomes = [_outcome("h1", 100.0)]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["agent_x"]["originator/support"]
    assert stats.n == 1  # only the row with hypothesis_id counts


def test_attribution_missing_agent_field_skipped() -> None:
    """Rows without required group-key fields must be skipped silently."""
    attrs = [
        {"hypothesis_id": "h1", "role": "originator", "stance": "support"},  # no agent
        _attr("h1", "agent_y"),
    ]
    outcomes = [_outcome("h1", 50.0)]
    result = aggregate_agent_reliability(attrs, outcomes)
    assert "agent_y" in result
    # The bad row without agent must not appear under None key.
    assert None not in result


# ---------------------------------------------------------------------------
# horizon_days filter (R5 #4)
# ---------------------------------------------------------------------------


def test_horizon_days_filter_excludes_wrong_horizon() -> None:
    """Outcomes with a different horizon_days must not pollute the stats."""
    attrs = _make_attrs(10, "filtered_agent")
    outcomes = [
        _outcome(f"h{i}", 500.0, horizon_days=5) for i in range(10)  # wrong horizon
    ]
    result = aggregate_agent_reliability(attrs, outcomes, horizon_days=10)
    stats = result["filtered_agent"]["originator/support"]
    assert stats.n == 10
    # No outcomes matched → metrics are None.
    assert stats.mean_excess_return_bps is None


def test_horizon_days_filter_includes_correct_horizon() -> None:
    """Outcomes with the matching horizon_days are included."""
    attrs = _make_attrs(10, "horizon_agent")
    outcomes = [
        _outcome(f"h{i}", 200.0, horizon_days=10) for i in range(10)
    ]
    result = aggregate_agent_reliability(attrs, outcomes, horizon_days=10)
    stats = result["horizon_agent"]["originator/support"]
    assert stats.mean_excess_return_bps == pytest.approx(200.0)


def test_horizon_days_mixed_only_matching_counted() -> None:
    """Mixed-horizon outcomes: only horizon=10 included for horizon_days=10."""
    attrs = _make_attrs(4, "mix_agent")
    outcomes = [
        _outcome("h0", 100.0, horizon_days=10),
        _outcome("h1", 200.0, horizon_days=10),
        _outcome("h2", 999.0, horizon_days=5),   # should be excluded
        _outcome("h3", 999.0, horizon_days=20),  # should be excluded
    ]
    result = aggregate_agent_reliability(attrs, outcomes, horizon_days=10)
    stats = result["mix_agent"]["originator/support"]
    assert stats.mean_excess_return_bps == pytest.approx((100.0 + 200.0) / 2)


# ---------------------------------------------------------------------------
# GroupStats computed fields
# ---------------------------------------------------------------------------


def test_win_rate_computed_correctly() -> None:
    attrs = [_attr(f"h{i}", "wr_agent") for i in range(4)]
    outcomes = [
        _outcome("h0", 100.0),
        _outcome("h1", 200.0),
        _outcome("h2", -50.0),
        _outcome("h3", 300.0),
    ]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["wr_agent"]["originator/support"]
    assert stats.win_rate == pytest.approx(3 / 4)


def test_payoff_ratio_computed_correctly() -> None:
    attrs = [_attr(f"h{i}", "pr_agent") for i in range(4)]
    outcomes = [
        _outcome("h0", 200.0),
        _outcome("h1", 200.0),
        _outcome("h2", -50.0),
        _outcome("h3", -150.0),
    ]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["pr_agent"]["originator/support"]
    # avg_win=200, avg_loss=100, ratio=2.0
    assert stats.payoff_ratio == pytest.approx(2.0)


def test_payoff_ratio_none_when_no_losses() -> None:
    attrs = [_attr(f"h{i}", "noloss_agent") for i in range(3)]
    outcomes = [_outcome(f"h{i}", 100.0) for i in range(3)]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["noloss_agent"]["originator/support"]
    assert stats.payoff_ratio is None


def test_payoff_ratio_none_when_no_wins() -> None:
    attrs = [_attr(f"h{i}", "nowin_agent") for i in range(3)]
    outcomes = [_outcome(f"h{i}", -100.0) for i in range(3)]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["nowin_agent"]["originator/support"]
    assert stats.payoff_ratio is None


def test_non_finite_returns_excluded() -> None:
    """NaN / inf in excess_return_bps must be dropped silently."""
    attrs = [_attr(f"h{i}", "finite_agent") for i in range(3)]
    outcomes = [
        _outcome("h0", float("nan")),
        _outcome("h1", float("inf")),
        _outcome("h2", 100.0),
    ]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["finite_agent"]["originator/support"]
    assert stats.n == 3  # 3 attribution rows
    assert stats.mean_excess_return_bps == pytest.approx(100.0)


def test_fallback_to_return_pct_when_excess_missing() -> None:
    """If excess_return_bps is absent, fall back to return_pct × 10000."""
    attrs = [_attr("h1", "legacy_agent")]
    outcomes = [{"hypothesis_id": "h1", "horizon_days": 10, "return_pct": 0.05}]
    result = aggregate_agent_reliability(attrs, outcomes)
    stats = result["legacy_agent"]["originator/support"]
    assert stats.mean_excess_return_bps == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# snapshot_to_file — I/O tests
# ---------------------------------------------------------------------------


def test_snapshot_to_file_creates_output(tmp_path: Path) -> None:
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "outcome.jsonl"
    output = tmp_path / "agent_reliability.json"

    attrs = _make_attrs(5, "snap_agent")
    for a in attrs:
        attr_log.write_text(
            "\n".join(json.dumps(a) for a in attrs) + "\n"
        )
        break  # just write once

    outcomes = _make_outcomes(5, 200.0)
    out_log.write_text("\n".join(json.dumps(o) for o in outcomes) + "\n")

    result = snapshot_to_file(attr_log, out_log, output)
    assert output.exists()
    assert "as_of" in result
    assert result["horizon_days"] == 10
    assert "snap_agent" in result["agents"]


def test_snapshot_to_file_no_tmp_residue(tmp_path: Path) -> None:
    """After a successful write, the .tmp file must be gone."""
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "outcome.jsonl"
    output = tmp_path / "agent_reliability.json"

    attr_log.write_text(json.dumps(_attr("h1", "a")) + "\n")
    out_log.write_text(json.dumps(_outcome("h1", 100.0)) + "\n")

    snapshot_to_file(attr_log, out_log, output)

    tmp_file = output.with_suffix(output.suffix + ".tmp")
    assert not tmp_file.exists(), ".tmp residue found after successful write"


def test_snapshot_to_file_roundtrip(tmp_path: Path) -> None:
    """Reading back the JSON must yield exactly what was returned."""
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "outcome.jsonl"
    output = tmp_path / "agent_reliability.json"

    attrs = _make_attrs(12, "rt_agent")
    attr_log.write_text("\n".join(json.dumps(a) for a in attrs) + "\n")
    outcomes = _make_outcomes(12, 150.0)
    out_log.write_text("\n".join(json.dumps(o) for o in outcomes) + "\n")

    returned = snapshot_to_file(attr_log, out_log, output)
    on_disk = json.loads(output.read_text(encoding="utf-8"))

    assert returned == on_disk


def test_snapshot_to_file_missing_logs_returns_empty_agents(tmp_path: Path) -> None:
    """MVP day-0: no logs yet → empty agents dict, no crash."""
    output = tmp_path / "agent_reliability.json"
    result = snapshot_to_file(
        tmp_path / "nope_attr.jsonl",
        tmp_path / "nope_outcome.jsonl",
        output,
    )
    assert result["agents"] == {}
    assert output.exists()


def test_snapshot_to_file_creates_parent_dir(tmp_path: Path) -> None:
    """Output path in a non-existent subdirectory must be created."""
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "out.jsonl"
    attr_log.write_text("")
    out_log.write_text("")
    output = tmp_path / "deep" / "nested" / "agent_reliability.json"
    snapshot_to_file(attr_log, out_log, output)
    assert output.exists()


def test_snapshot_to_file_honours_horizon_days(tmp_path: Path) -> None:
    """Custom horizon_days is reflected in output and used for filtering."""
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "out.jsonl"
    output = tmp_path / "agent_reliability.json"

    attrs = _make_attrs(10, "hz_agent")
    attr_log.write_text("\n".join(json.dumps(a) for a in attrs) + "\n")
    # Write outcomes for horizon=5 only (should be excluded with horizon=10 default).
    outcomes = _make_outcomes(10, 999.0, horizon_days=5)
    out_log.write_text("\n".join(json.dumps(o) for o in outcomes) + "\n")

    result = snapshot_to_file(attr_log, out_log, output, horizon_days=10)
    assert result["horizon_days"] == 10
    stats_dict = result["agents"]["hz_agent"]["originator/support"]
    # horizon mismatch → mean_excess_return_bps is None
    assert stats_dict["mean_excess_return_bps"] is None


def test_snapshot_output_schema(tmp_path: Path) -> None:
    """Output JSON must match the documented schema shape."""
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "out.jsonl"
    output = tmp_path / "agent_reliability.json"

    attrs = _make_attrs(42, "schema_agent")
    attr_log.write_text("\n".join(json.dumps(a) for a in attrs) + "\n")
    outcomes = _make_outcomes(42, 87.4)
    out_log.write_text("\n".join(json.dumps(o) for o in outcomes) + "\n")

    result = snapshot_to_file(attr_log, out_log, output)
    assert "as_of" in result
    assert "horizon_days" in result
    assert "agents" in result

    agent_entry = result["agents"]["schema_agent"]
    group_entry = agent_entry["originator/support"]
    for field in ("n", "win_rate", "mean_excess_return_bps", "payoff_ratio", "weight", "measured_n"):
        assert field in group_entry, f"missing field: {field}"
    assert group_entry["measured"] is True
    assert group_entry["measured_n"] == 42


def test_snapshot_marks_unmeasured_group_without_neutral_weight(tmp_path: Path) -> None:
    """No joined returns should not masquerade as a neutral reliability score."""
    attr_log = tmp_path / "attr.jsonl"
    out_log = tmp_path / "out.jsonl"
    output = tmp_path / "agent_reliability.json"

    attrs = _make_attrs(40, "unmeasured_agent")
    attr_log.write_text("\n".join(json.dumps(a) for a in attrs) + "\n")
    out_log.write_text("", encoding="utf-8")

    result = snapshot_to_file(attr_log, out_log, output)
    group = result["agents"]["unmeasured_agent"]["originator/support"]

    assert group["n"] == 40
    assert group["measured_n"] == 0
    assert group["mean_excess_return_bps"] is None
    assert group["weight"] is None
    assert group["measured"] is False


# ---------------------------------------------------------------------------
# Cross-module end-to-end (uses logs.write_agent_attribution + write_catalyst_outcome)
# ---------------------------------------------------------------------------


def test_end_to_end_via_log_writers(tmp_path: Path) -> None:
    """Build real JSONL files using the canonical writers, then snapshot."""
    from almanac.observability.logs import (  # noqa: WPS433
        write_agent_attribution,
        write_catalyst_outcome,
    )

    attr_log = tmp_path / "agent_attribution_log.jsonl"
    outcome_log = tmp_path / "catalyst_outcome_log.jsonl"
    output = tmp_path / "agent_reliability.json"

    hypothesis_id = "e2e-hyp-1"
    analysis_id = "e2e-aid-1"

    # Write 12 attribution rows for the same agent (different hypotheses)
    # so weight lands in the neutral band (10 <= n < 30 → weight = 1.0).
    for i in range(12):
        write_agent_attribution(
            attr_log,
            hypothesis_id=f"e2e-hyp-{i}",
            analysis_id=analysis_id,
            analysis_date="2026-05-24",
            ticker="NVDA",
            hypothesis_type="bull_pullback",
            time_horizon_days=10,
            agent="catalyst_layer",
            role="originator",
            stance="support",
            fsync=False,
        )

    # Write matching outcomes for each hypothesis.
    for i in range(12):
        write_catalyst_outcome(
            outcome_log,
            hypothesis_id=f"e2e-hyp-{i}",
            horizon_days=10,
            reference_event_at="2026-05-24T09:00:00+00:00",
            price_at_event=100.0,
            price_at_measure=102.0,  # +2%
            benchmark_basket=["QQQ"],
            benchmark_weights=[1.0],
            benchmark_currency_normalized_to="USD",
            benchmark_return_pct=0.005,  # 0.5% → excess ≈ 150bps
            primary_ticker_currency="USD",
            usdjpy_at_event=155.0,
            usdjpy_at_measure=155.5,
            fsync=False,
        )

    result = snapshot_to_file(attr_log, outcome_log, output)

    assert "catalyst_layer" in result["agents"]
    group = result["agents"]["catalyst_layer"]["originator/support"]
    assert group["n"] == 12
    assert group["weight"] == pytest.approx(1.0)  # neutral band
    # excess ≈ (0.02 - 0.005) * 10000 = 150 bps
    assert group["mean_excess_return_bps"] == pytest.approx(150.0, rel=1e-4)
    assert group["win_rate"] == pytest.approx(1.0)

    # Snapshot file on disk must be valid JSON.
    on_disk = json.loads(output.read_text(encoding="utf-8"))
    assert on_disk["agents"]["catalyst_layer"]["originator/support"]["n"] == 12
