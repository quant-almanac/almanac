from __future__ import annotations

import json
from pathlib import Path

import pytest

import extraction_audit_sampler as sampler


def _feature(idx: int, **overrides) -> dict:
    row = {
        "feature_id": f"feature-{idx}",
        "source_event_id": f"tdnet:7203:{idx}",
        "ticker": "7203.T",
        "source": "tdnet",
        "disclosure_type": "earnings",
        "publish_time": f"2026-06-{idx % 28 + 1:02d}T06:00:00+00:00",
        "model_id": "deepseek-chat",
        "prompt_version": "p1",
        "feature_schema_version": "0.4.0",
        "directional_score": 0.4,
        "directional_confidence": 0.8,
        "catalyst_specificity": 0.7,
        "contradiction_count": 0,
        "summary": f"summary {idx}",
        "evidence": [{"quote": f"evidence {idx}", "field": "guidance"}],
    }
    row.update(overrides)
    return row


def test_monthly_audit_sample_is_deterministic_and_capped() -> None:
    features = [_feature(i) for i in range(30)]

    first = sampler.sample_extraction_audit(features, month="2026-06", n=20)
    second = sampler.sample_extraction_audit(features, month="2026-06", n=20)

    assert len(first) == 20
    assert [item["audit_id"] for item in first] == [item["audit_id"] for item in second]
    assert len({item["audit_id"] for item in first}) == 20


def test_sample_excludes_items_already_reviewed_in_human_feedback_log() -> None:
    features = [_feature(i) for i in range(6)]
    reviewed = sampler.build_audit_item(features[0], month="2026-06")

    sample = sampler.sample_extraction_audit(
        features,
        month="2026-06",
        n=6,
        feedback_rows=[
            {
                "subject_type": "extraction_audit",
                "subject_id": reviewed["feedback_subject_id"],
                "source_event_id": features[1]["source_event_id"],
                "verdict": "bad",
            },
        ],
    )

    sample_event_ids = {item["source_event_id"] for item in sample}
    assert features[0]["source_event_id"] not in sample_event_ids
    assert features[1]["source_event_id"] not in sample_event_ids
    assert len(sample) == 4


def test_sample_uses_only_rows_from_requested_month() -> None:
    features = [
        _feature(1, publish_time="2026-06-01T06:00:00+00:00"),
        _feature(2, publish_time="2026-05-31T23:59:00+00:00"),
        _feature(3, publish_time="2026-07-01T00:00:00+00:00"),
    ]

    sample = sampler.sample_extraction_audit(features, month="2026-06", n=20)

    assert [item["source_event_id"] for item in sample] == [features[0]["source_event_id"]]


def test_audit_item_contains_review_context_and_feedback_identity() -> None:
    row = _feature(1, guidance_revision_pct=0.12, placebo_hash_score=0.42)

    item = sampler.build_audit_item(row, month="2026-06")

    assert item["subject_type"] == "extraction_audit"
    assert item["feedback_subject_id"] == item["audit_id"]
    assert item["ticker"] == "7203.T"
    assert item["features_to_review"]["directional_score"] == 0.4
    assert item["features_to_review"]["guidance_revision_pct"] == 0.12
    assert "placebo_hash_score" not in item["features_to_review"]
    assert item["summary"] == "summary 1"
    assert item["evidence"] == [{"quote": "evidence 1", "field": "guidance"}]
    assert "human_feedback_log.py append" in item["feedback_command"]


def test_cli_samples_feature_store_and_prints_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    feature_path = tmp_path / "disclosure_features.jsonl"
    feedback_path = tmp_path / "human_feedback_log.jsonl"
    feature_path.write_text(
        "\n".join(json.dumps(_feature(i), sort_keys=True) for i in range(3)) + "\n",
        encoding="utf-8",
    )

    rc = sampler.main(
        [
            "sample",
            "--month",
            "2026-06",
            "--n",
            "2",
            "--features-path",
            str(feature_path),
            "--feedback-path",
            str(feedback_path),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["month"] == "2026-06"
    assert payload["requested_n"] == 2
    assert payload["available_unreviewed"] == 3
    assert len(payload["items"]) == 2


def test_cli_rejects_negative_sample_size(tmp_path: Path) -> None:
    feature_path = tmp_path / "disclosure_features.jsonl"
    feature_path.write_text(json.dumps(_feature(1)) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="n must be non-negative"):
        sampler.main(
            [
                "sample",
                "--month",
                "2026-06",
                "--n",
                "-1",
                "--features-path",
                str(feature_path),
                "--feedback-path",
                str(tmp_path / "missing.jsonl"),
            ]
        )
