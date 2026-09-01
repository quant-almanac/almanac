"""portfolio_agent.py (CLI) の会計ログ。

api/routes/agent.py と同じロジックのはずが、以前は CLI 側の
AgentProtocolViolation / query() 例外パスに _log_agent_result 呼び出しが
無く、「何も記録されない run」が発生していた
(Codex レビュー round 17 で指摘)。
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

import portfolio_agent as cli


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _ToolUseBlock:
    def __init__(self, name: str, input):
        self.name = name
        self.input = input


def _install_fake_sdk(monkeypatch, *, blocks, raise_query_error=None):
    class AssistantMessage:
        def __init__(self):
            self.content = blocks

    async def fake_query(prompt, options):
        if raise_query_error is not None:
            raise raise_query_error
        yield AssistantMessage()

    fake_sdk = types.SimpleNamespace(
        query=fake_query,
        ClaudeAgentOptions=lambda **kw: types.SimpleNamespace(**kw),
        ResultMessage=type("ResultMessage", (), {}),
        AssistantMessage=AssistantMessage,
    )
    fake_types = types.SimpleNamespace(TextBlock=_TextBlock, ToolUseBlock=_ToolUseBlock)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    def _write(name, payload):
        (tmp_path / name).write_text(json.dumps(payload), encoding="utf-8")

    from datetime import datetime, timezone

    _write("technical_state.json", {"tickers": {
        "VT": {"price": 160.0, "rsi": 57.0, "data_quality_status": "ok",
               "freshness_status": "fresh", "data_as_of": "2026-08-24"}}})
    _write("holdings.json", {"VT_row": {"ticker": "VT", "shares": 10.0}})
    _write("ai_portfolio_analysis.json", {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "synthesis": {"overall_stance": "neutral", "priority_actions": [
            {"ticker": "VT", "type": "buy", "execution_readiness": "review"}]}})
    monkeypatch.setattr(cli, "BASE_DIR", tmp_path)
    return tmp_path


def test_a_protocol_violation_still_logs_a_row(monkeypatch, sandbox):
    rows: list[dict] = []
    _install_fake_sdk(monkeypatch,
                      blocks=[_TextBlock("x"), _ToolUseBlock("Read", {})])
    monkeypatch.setattr(cli, "_log_agent_result", lambda **kw: rows.append(kw))

    code = asyncio.run(cli._run_locked("default"))

    assert code == 1
    assert rows, "プロトコル違反が会計ログに1行も残らなかった"
    assert rows[-1]["status"] == "protocol_violation"
    assert rows[-1]["forbidden_tool_use_seen"] is True
    assert rows[-1]["structured_output_transport_seen"] is False


def test_a_query_exception_still_logs_a_row(monkeypatch, sandbox):
    rows: list[dict] = []
    _install_fake_sdk(monkeypatch, blocks=[], raise_query_error=RuntimeError("boom"))
    monkeypatch.setattr(cli, "_log_agent_result", lambda **kw: rows.append(kw))

    code = asyncio.run(cli._run_locked("default"))

    assert code == 1
    assert rows, "query() の例外が会計ログに1行も残らなかった"
    assert rows[-1]["status"] == "error"


def test_a_persistence_failure_after_a_known_cost_still_logs_a_row(monkeypatch, sandbox):
    """保存失敗後も既知のコストが会計ログに残る (API と同じ理由)。"""
    rows: list[dict] = []

    class ResultMessage:
        subtype = "success"
        total_cost_usd = 0.0789

    class AssistantMessage:
        def __init__(self):
            self.content = [_ToolUseBlock("StructuredOutput", {})]

    async def fake_query(prompt, options):
        yield AssistantMessage()
        yield ResultMessage()

    fake_sdk = types.SimpleNamespace(
        query=fake_query, ClaudeAgentOptions=lambda **kw: types.SimpleNamespace(**kw),
        ResultMessage=ResultMessage, AssistantMessage=AssistantMessage)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types",
                        types.SimpleNamespace(TextBlock=_TextBlock, ToolUseBlock=_ToolUseBlock))

    import agent_projection as ap
    projection = ap.build_agent_projection("default", base_dir=sandbox)
    scope = projection["action_scope"][0]
    ResultMessage.result = json.dumps({
        "headline": "h", "overall_stance": "neutral", "risk_warnings": [],
        "actions": [{"rank": 1, "candidate_id": scope["candidate_id"],
                     "action_type": scope["allowed_actions"][0],
                     "actionability": "watch_only", "reason": "ok"}]})
    ResultMessage.structured_output = json.loads(ResultMessage.result)

    monkeypatch.setattr(cli, "_log_agent_result", lambda **kw: rows.append(kw))
    monkeypatch.setattr(
        cli, "save_verified_result",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    code = asyncio.run(cli._run_locked("default"))

    assert code == 1
    assert rows, "保存失敗が会計ログに1行も残らなかった"
    row = rows[-1]
    assert row["status"] == "persistence_error"
    assert row["cost_usd"] == 0.0789
    assert row["structured_output_transport_seen"] is True
    assert row["forbidden_tool_use_seen"] is False
