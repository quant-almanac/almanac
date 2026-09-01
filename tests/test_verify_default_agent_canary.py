from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import scripts.verify_default_agent_canary as canary


JST = ZoneInfo("Asia/Tokyo")


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _fixtures(root: Path, *, status: str = "success", duplicate: bool = False) -> None:
    _write(root / "ai_portfolio_analysis.json", {"as_of": "2026-09-02 06:25"})
    _write(root / "agent_briefing.json", {
        "schema_version": canary.SCHEMA_VERSION,
        "commentary_is_non_actionable": True,
        "mode": "default",
        "evaluation_as_of": "2026-09-01T21:35:05+00:00",
        "projection_sha256": "projection-digest",
        "headline": "検証済み所見",
        "overall_stance": "neutral",
        "actions": [{
            "rank": 1,
            "candidate_id": "candidate-1",
            "ticker": "SYNTH",
            "action_type": "watch",
            "actionability": "review",
            "reason": "scope内",
        }],
        "risk_warnings": [],
        "as_of": "2026-09-01T21:35:20+00:00",
    })
    row = {
        "ts": "2026-09-02T06:35:20",
        "role": "agent_sdk_run",
        "model": "test-model",
        "use_tool": False,
        "max_turns": 2,
        "structured_output_transport_seen": True,
        "forbidden_tool_use_seen": False,
        "mode": "default",
        "status": status,
        "cost_usd": 0.04,
    }
    rows = [row, dict(row)] if duplicate else [row]
    path = root / "logs" / "llm_calls.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in rows), encoding="utf-8")


def _stub_projection(monkeypatch) -> None:
    projection = {
        "schema_version": canary.SCHEMA_VERSION,
        "mode": "default",
        "evaluation_as_of": "2026-09-01T21:35:05+00:00",
        "portfolio_context": {"max_overall_stance": "neutral"},
        "action_scope": [{
            "candidate_id": "candidate-1",
            "canonical_instrument_id": "SYNTH",
            "allowed_actions": ["watch"],
            "max_actionability": "review",
        }],
    }
    monkeypatch.setattr(canary, "build_agent_projection", lambda *a, **k: projection)
    monkeypatch.setattr(canary, "projection_sha256", lambda payload: "projection-digest")
    monkeypatch.setattr(canary, "resolve_agent_model", lambda: "test-model")


def test_reports_pending_before_formal_analysis(tmp_path):
    _write(tmp_path / "ai_portfolio_analysis.json", {"as_of": "2026-09-01 06:25"})

    report = canary.verify_canary(
        base_dir=tmp_path,
        since=datetime(2026, 9, 2, tzinfo=JST),
    )

    assert report["status"] == "pending"
    assert report["reason"] == "formal_analysis_not_yet_available"


def test_passes_one_scoped_accounted_tool_free_run(tmp_path, monkeypatch):
    _fixtures(tmp_path)
    _stub_projection(monkeypatch)

    report = canary.verify_canary(
        base_dir=tmp_path,
        since=datetime(2026, 9, 2, tzinfo=JST),
    )

    assert report["status"] == "pass"
    assert report["accounting_rows"] == 1
    assert all(item["ok"] for item in report["checks"])


def test_duplicate_or_failed_agent_run_is_not_hidden(tmp_path, monkeypatch):
    _fixtures(tmp_path, status="error_max_turns", duplicate=True)
    _stub_projection(monkeypatch)

    report = canary.verify_canary(
        base_dir=tmp_path,
        since=datetime(2026, 9, 2, tzinfo=JST),
    )
    failed = {item["name"] for item in report["checks"] if not item["ok"]}

    assert report["status"] == "fail"
    assert {"exactly_one_agent_run", "agent_status"} <= failed
