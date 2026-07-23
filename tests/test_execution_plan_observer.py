from __future__ import annotations

import json
from pathlib import Path

import execution_plan_observer as epo


def _synthesis(*, decisions: dict[str, int], would_filter: int = 0, batch_error: str = "") -> dict:
    return {
        "post_filter": {
            "execution_plan_gate": {
                "mode": "observe",
                "observed_decisions": decisions,
                "would_filter_count": would_filter,
                "batch_allocation": {
                    "applied": True,
                    "accepted_count": 1,
                    "over_budget_count": 0,
                    "error": batch_error,
                },
                "monthly_attribution": {
                    "available": True,
                    "unattributed_count": 0,
                    "unattributed_notional_jpy": 0,
                },
            }
        }
    }


def test_record_observation_is_deduplicated_and_persists_jsonl(tmp_path: Path) -> None:
    path = tmp_path / "observations.jsonl"
    synthesis = _synthesis(decisions={"plan_new_order": 2}, would_filter=0)

    first = epo.record_observation(
        synthesis,
        analysis_id="run-1",
        as_of="2026-07-10T09:00:00+09:00",
        path=path,
        fsync=False,
    )
    second = epo.record_observation(
        synthesis,
        analysis_id="run-1",
        as_of="2026-07-10T09:00:00+09:00",
        path=path,
        fsync=False,
    )

    assert first["recorded"] is True
    assert second["recorded"] is False
    assert second["duplicate"] is True
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["classification_count"] == 2
    assert rows[0]["trading_date"] == "2026-07-10"


def test_readiness_accepts_twenty_clean_classifications() -> None:
    observations = [
        {
            "mode": "observe",
            "trading_date": "2026-07-10",
            "classification_count": 20,
            "classification_error_count": 0,
            "metadata_mismatch_count": 0,
            "monthly_attribution": {"available": True, "unattributed_count": 0, "unattributed_notional_jpy": 0},
        }
    ]

    readiness = epo.evaluate_enforce_readiness(observations)

    assert readiness["ready_for_enforce"] is True
    assert readiness["classification_count"] == 20
    assert readiness["trading_day_count"] == 1
    assert readiness["blockers"] == []


def test_readiness_accepts_ten_clean_trading_days() -> None:
    weekdays = [1, 2, 3, 6, 7, 8, 9, 10, 13, 14]
    observations = [
        {
            "mode": "observe",
            "trading_date": f"2026-07-{day:02d}",
            "classification_count": 1,
            "classification_error_count": 0,
            "metadata_mismatch_count": 0,
            "monthly_attribution": {"available": True, "unattributed_count": 0, "unattributed_notional_jpy": 0},
        }
        for day in weekdays
    ]

    readiness = epo.evaluate_enforce_readiness(observations)

    assert readiness["ready_for_enforce"] is True
    assert readiness["trading_day_count"] == 10


def test_readiness_does_not_count_weekends_as_trading_days() -> None:
    observations = [
        {
            "mode": "observe",
            "trading_date": f"2026-07-{day:02d}",
            "classification_count": 1,
            "classification_error_count": 0,
            "metadata_mismatch_count": 0,
            "monthly_attribution": {"available": True, "unattributed_count": 0, "unattributed_notional_jpy": 0},
        }
        for day in range(4, 14)  # 6 weekdays + 4 weekend rows
    ]

    readiness = epo.evaluate_enforce_readiness(observations)

    assert readiness["ready_for_enforce"] is False
    assert readiness["trading_day_count"] == 6
    assert "observation_sample_short" in readiness["blockers"][0]


def test_readiness_blocks_errors_and_metadata_mismatches_even_with_sample() -> None:
    observations = [
        {
            "mode": "observe",
            "trading_date": "2026-07-10",
            "classification_count": 25,
            "classification_error_count": 1,
            "metadata_mismatch_count": 2,
            "monthly_attribution": {"available": True, "unattributed_count": 0, "unattributed_notional_jpy": 0},
        }
    ]

    readiness = epo.evaluate_enforce_readiness(observations)

    assert readiness["ready_for_enforce"] is False
    assert readiness["blockers"] == [
        "classification_errors_present: 1",
        "plan_metadata_mismatches_present: 2",
    ]


def test_build_observation_counts_batch_error_and_mismatch() -> None:
    observation = epo.build_observation(
        _synthesis(
            decisions={"plan_metadata_mismatch": 2, "execution_plan_error": 1},
            would_filter=3,
            batch_error="synthetic",
        ),
        analysis_id="run-2",
        as_of="2026-07-10T10:00:00+09:00",
    )

    assert observation is not None
    assert observation["classification_count"] == 3
    assert observation["classification_error_count"] == 2
    assert observation["metadata_mismatch_count"] == 2
    assert observation["monthly_attribution"] == {
        "available": True,
        "unattributed_count": 0,
        "unattributed_notional_jpy": 0,
    }


def test_readiness_blocks_unattributed_monthly_activity_even_with_sample() -> None:
    readiness = epo.evaluate_enforce_readiness([{
        "mode": "observe",
        "trading_date": "2026-07-10",
        "classification_count": 20,
        "classification_error_count": 0,
        "metadata_mismatch_count": 0,
        "monthly_attribution": {
            "available": True,
            "unattributed_count": 2,
            "unattributed_notional_jpy": 130_000,
        },
    }])

    assert readiness["ready_for_enforce"] is False
    assert readiness["monthly_attribution"] == {
        "available": True,
        "unattributed_count": 2,
        "unattributed_notional_jpy": 130_000,
    }
    assert readiness["blockers"] == [
        "legacy_monthly_attribution_incomplete: count=2, notional_jpy=130000",
    ]
