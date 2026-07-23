import json

import pytest

import backup_manager
import human_feedback_log as feedback


def test_record_feedback_appends_jsonl_without_rewriting(tmp_path) -> None:
    path = tmp_path / "human_feedback_log.jsonl"

    first = feedback.record_feedback(
        subject_type="disclosure_reading",
        subject_id="tdnet:7203:2026-06-30",
        verdict="good",
        comment="読み取りは妥当",
        ticker="7203.T",
        source_event_id="tdnet:7203:2026-06-30",
        occurred_at="2026-06-30T09:00:00+00:00",
        path=path,
        fsync=False,
    )
    second = feedback.record_feedback(
        subject_type="disclosure_reading",
        subject_id="tdnet:7203:2026-06-30",
        verdict="bad",
        comment="希薄化率の解釈が違う",
        ticker="7203.T",
        source_event_id="tdnet:7203:2026-06-30",
        occurred_at="2026-06-30T09:05:00+00:00",
        path=path,
        fsync=False,
    )

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["feedback_id"] == first["feedback_id"]
    assert json.loads(lines[1])["feedback_id"] == second["feedback_id"]
    assert first["feedback_id"] != second["feedback_id"]
    assert [row["verdict"] for row in feedback.read_feedback(path)] == ["good", "bad"]


def test_record_feedback_rejects_unknown_verdict(tmp_path) -> None:
    with pytest.raises(ValueError, match="verdict"):
        feedback.record_feedback(
            subject_type="hypothesis",
            subject_id="h1",
            verdict="great",
            comment="表記揺れは拒否",
            path=tmp_path / "human_feedback_log.jsonl",
            fsync=False,
        )


def test_human_feedback_log_is_backed_up() -> None:
    assert "human_feedback_log.jsonl" in backup_manager.TARGETS
