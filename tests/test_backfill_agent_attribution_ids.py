import json
from pathlib import Path

from scripts.backfill_agent_attribution_ids import backfill


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _attr(hypothesis_id: str, *, ticker: str = "NVDA") -> dict:
    return {
        "row_id": "attr-row",
        "hypothesis_id": hypothesis_id,
        "analysis_id": "analysis-1",
        "analysis_date": "2026-07-02",
        "ticker": ticker,
        "hypothesis_type": "legacy",
        "time_horizon_days": 20,
        "agent": "opus_final",
        "role": "final_decider",
        "stance": "support",
        "final_candidate_status": "adopted",
    }


def _hyp(hypothesis_id: str, *, ticker: str = "NVDA") -> dict:
    return {
        "row_id": "hyp-row",
        "event_type": "generated",
        "hypothesis_id": hypothesis_id,
        "analysis_id": "cli-catalyst-2026-07-02",
        "analysis_date": "2026-07-02",
        "hypothesis_type": "legacy",
        "primary_ticker": ticker,
        "primary_source_agent": "legacy_producer:opus_final",
        "horizon_days": 20,
        "candidate_status": "generated",
    }


def test_backfill_dry_run_reports_join_improvement_without_mutating(tmp_path: Path) -> None:
    attr = tmp_path / "agent_attribution_log.jsonl"
    hyp = tmp_path / "catalyst_hypothesis_log.jsonl"
    out = tmp_path / "catalyst_outcome_log.jsonl"

    _write_jsonl(attr, [_attr("old-id"), {"agent": "other", "hypothesis_id": "keep"}])
    _write_jsonl(hyp, [_hyp("new-id"), _hyp("new-id")])
    _write_jsonl(out, [{"hypothesis_id": "new-id", "horizon_days": 10}])

    summary = backfill(
        attribution_log_path=attr,
        hypothesis_log_path=hyp,
        outcome_log_path=out,
        apply=False,
    )

    assert summary.target_rows == 1
    assert summary.converted_rows == 1
    assert summary.old_outcome_matches == 0
    assert summary.new_outcome_matches == 1
    assert json.loads(attr.read_text(encoding="utf-8").splitlines()[0])["hypothesis_id"] == "old-id"


def test_backfill_apply_replaces_only_unambiguous_rows_and_writes_backup(tmp_path: Path) -> None:
    attr = tmp_path / "agent_attribution_log.jsonl"
    hyp = tmp_path / "catalyst_hypothesis_log.jsonl"

    _write_jsonl(attr, [_attr("old-id"), _attr("ambiguous-old", ticker="MSFT")])
    _write_jsonl(hyp, [_hyp("new-id"), _hyp("msft-1", ticker="MSFT"), _hyp("msft-2", ticker="MSFT")])

    summary = backfill(
        attribution_log_path=attr,
        hypothesis_log_path=hyp,
        apply=True,
    )

    rows = [json.loads(line) for line in attr.read_text(encoding="utf-8").splitlines()]
    assert rows[0]["hypothesis_id"] == "new-id"
    assert rows[1]["hypothesis_id"] == "ambiguous-old"
    assert summary.converted_rows == 1
    assert summary.ambiguous_match_rows == 1
    assert summary.backup_path is not None
    assert Path(summary.backup_path).exists()
