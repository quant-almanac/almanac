import json
from pathlib import Path

import pytest

from almanac.observability.scenario_promotion import (
    aggregate_scenario_promotion,
    snapshot_to_file,
)


def _event(hid: str, scenario_id: str, *, observe_only: bool = True) -> dict:
    return {
        "event_type": "generated",
        "hypothesis_id": hid,
        "event_at": "2026-06-01T00:00:00+00:00",
        "hypothesis_type": f"scenario_{scenario_id}",
        "source_event_id": f"scenario:{scenario_id}",
        "primary_source_agent": f"scenario:{scenario_id}",
        "primary_ticker": "1306.T",
        "observe_only": observe_only,
    }


def _outcome(
    hid: str, bps: float, *, horizon_days: int = 20, after_cost_bps: float | None = None,
) -> dict:
    row = {
        "hypothesis_id": hid,
        "horizon_days": horizon_days,
        "measured_at": "2026-07-01T00:00:00+00:00",
        "excess_return_bps": bps,
    }
    if after_cost_bps is not None:
        row["after_cost_excess_return_bps"] = after_cost_bps
    return row


def test_aggregate_scenario_promotion_ready_from_catalyst_outcomes() -> None:
    events = [_event(f"h{i}", "japan_standalone_bull") for i in range(5)]
    outcomes = [
        _outcome("h0", 120.0),
        _outcome("h1", 80.0),
        _outcome("h2", -20.0),
        _outcome("h3", 40.0),
        _outcome("h4", 10.0),
    ]

    stats = aggregate_scenario_promotion(events, outcomes)
    row = stats["japan_standalone_bull"]

    assert row.observe_only_hypotheses == 5
    assert row.measured_episodes == 5
    assert row.hit_rate == pytest.approx(0.8)
    assert row.mean_excess_return_bps == pytest.approx(46.0)
    assert row.promotion_ready is True
    assert row.auto_decision_stage == "full_decision"
    # None of these outcomes carried after_cost_excess_return_bps: the mean
    # above is entirely gross. cost_adjusted_episodes must say so explicitly
    # rather than let mean_excess_return_bps silently pass as cost-adjusted
    # (2026-09 review, P3 -- after_cost_excess_return_bps was 0% populated in
    # production; a mean this module computed was always gross-only, unlabeled).
    assert row.cost_adjusted_episodes == 0


def test_aggregate_scenario_promotion_tracks_partial_cost_adjustment() -> None:
    events = [_event(f"h{i}", "japan_standalone_bull") for i in range(5)]
    outcomes = [
        _outcome("h0", 120.0, after_cost_bps=90.0),
        _outcome("h1", 80.0, after_cost_bps=60.0),
        _outcome("h2", -20.0),
        _outcome("h3", 40.0),
        _outcome("h4", 10.0),
    ]

    stats = aggregate_scenario_promotion(events, outcomes)
    row = stats["japan_standalone_bull"]

    assert row.measured_episodes == 5
    assert row.cost_adjusted_episodes == 2


def test_aggregate_scenario_promotion_requires_observe_only_scope() -> None:
    events = [_event(f"h{i}", "war_end", observe_only=False) for i in range(5)]
    outcomes = [_outcome(f"h{i}", 100.0) for i in range(5)]

    stats = aggregate_scenario_promotion(events, outcomes)

    assert stats["war_end"].promotion_ready is False
    assert stats["war_end"].observe_only_hypotheses == 0
    assert stats["war_end"].auto_decision_stage == "not_observe_only"


def test_aggregate_scenario_promotion_not_ready_below_episode_threshold() -> None:
    events = [_event(f"h{i}", "japan_standalone_bull") for i in range(4)]
    outcomes = [_outcome(f"h{i}", 100.0) for i in range(4)]

    stats = aggregate_scenario_promotion(events, outcomes)

    assert stats["japan_standalone_bull"].measured_episodes == 4
    assert stats["japan_standalone_bull"].promotion_ready is False
    assert stats["japan_standalone_bull"].auto_decision_stage == "capped_provisional"


def test_snapshot_to_file_writes_derived_artifact(tmp_path: Path) -> None:
    hlog = tmp_path / "catalyst_hypothesis_log.jsonl"
    olog = tmp_path / "catalyst_outcome_log.jsonl"
    output = tmp_path / "scenario_promotion_summary.json"
    hlog.write_text(
        "\n".join(json.dumps(_event(f"h{i}", "japan_standalone_bull")) for i in range(5))
        + "\n",
        encoding="utf-8",
    )
    olog.write_text(
        "\n".join(json.dumps(_outcome(f"h{i}", 100.0)) for i in range(5))
        + "\n",
        encoding="utf-8",
    )

    payload = snapshot_to_file(hlog, olog, output)

    assert output.exists()
    assert payload["by_scenario"]["japan_standalone_bull"]["promotion_ready"] is True
    assert payload["by_scenario"]["japan_standalone_bull"]["auto_decision_stage"] == "full_decision"
    assert payload["by_scenario"]["japan_standalone_bull"]["promotion_review_required"] is False
    assert payload["by_scenario"]["japan_standalone_bull"]["cost_adjusted_episodes"] == 0
    assert "operator review" not in payload["note"]
    assert json.loads(output.read_text(encoding="utf-8")) == payload
