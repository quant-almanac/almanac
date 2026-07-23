from __future__ import annotations

import asyncio
import json

from api.routes import agent


def _write(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_default_result_returns_newer_main_analysis(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent, "BASE_DIR", tmp_path)
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "as_of": "2026-07-17T06:09:00",
        "synthesis": {
            "morning_brief_headline": "main-new",
            "overall_stance": "neutral",
            "priority_actions": [],
            "risk_warnings": [],
        },
    })
    _write(tmp_path / "agent_briefing.json", {
        "as_of": "2026-07-17T05:00:00",
        "headline": "agent-old",
    })

    result = asyncio.run(agent.get_agent_result("default"))

    assert result["headline"] == "main-new"
    assert result["result_source"] == "main_analysis"


def test_default_result_returns_newer_on_demand_agent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(agent, "BASE_DIR", tmp_path)
    _write(tmp_path / "ai_portfolio_analysis.json", {
        "as_of": "2026-07-17T06:09:00",
        "synthesis": {"morning_brief_headline": "main-old"},
    })
    _write(tmp_path / "agent_briefing.json", {
        "as_of": "2026-07-17T10:30:00",
        "headline": "agent-new",
    })

    result = asyncio.run(agent.get_agent_result("default"))

    assert result["headline"] == "agent-new"
    assert result["result_source"] == "agent_briefing"
