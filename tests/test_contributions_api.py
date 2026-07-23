from __future__ import annotations

import asyncio
import json
from pathlib import Path

from api.routes import contributions


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_approve_bonus_defaults_to_four_month_release_without_mutating_cash(
    tmp_path: Path, monkeypatch,
) -> None:
    (tmp_path / "action_executions.json").write_text('{"executions": []}', encoding="utf-8")
    # Plan refresh is derived state and is independently tested by the engine.
    monkeypatch.setattr(contributions, "BASE_DIR", tmp_path)
    monkeypatch.setattr(contributions, "_refresh_plan", lambda: None)

    response = asyncio.run(contributions.approve_contribution(
        contributions.ContributionApprovalRequest(
            source="bonus",
            amount_jpy=400_000,
            owner="wife",
            broker="sbi",
            idempotency_key="bonus-approval-001",
        )
    ))

    assert response["ok"] is True
    assert response["contribution"]["release_months"] == 4
    assert response["contribution"]["owner"] == "wife"
    assert response["contribution"]["broker"] == "sbi"
    ledger = _read(tmp_path / "contribution_ledger.json")
    assert ledger["contributions"] == [response["contribution"]]
    assert not (tmp_path / "account.json").exists()
    assert response["summary"]["available_normal_jpy"] > 0

    replay = asyncio.run(contributions.approve_contribution(
        contributions.ContributionApprovalRequest(
            source="bonus", amount_jpy=400_000, owner="wife", broker="sbi",
            idempotency_key="bonus-approval-001",
        )
    ))
    assert replay["idempotent_replay"] is True
    assert len(_read(tmp_path / "contribution_ledger.json")["contributions"]) == 1
