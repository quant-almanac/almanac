import asyncio
import sys
import types

from api.routes import agent


def test_agent_sdk_result_logs_cost_accounting(monkeypatch):
    rows: list[dict] = []

    class TextBlock:
        def __init__(self, text: str):
            self.text = text

    class ToolUseBlock:
        def __init__(self, name: str, input):
            self.name = name
            self.input = input

    class AssistantMessage:
        def __init__(self):
            self.content = [TextBlock("analysis"), ToolUseBlock("Read", {"file": "holdings.json"})]

    class ResultMessage:
        subtype = "success"
        result = "done"
        total_cost_usd = 0.0123

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    async def fake_query(prompt, options):
        yield AssistantMessage()
        yield ResultMessage()

    fake_sdk = types.SimpleNamespace(
        query=fake_query,
        ClaudeAgentOptions=ClaudeAgentOptions,
        ResultMessage=ResultMessage,
        AssistantMessage=AssistantMessage,
    )
    fake_types = types.SimpleNamespace(TextBlock=TextBlock, ToolUseBlock=ToolUseBlock)

    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk.types", fake_types)
    monkeypatch.setattr(agent, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    chunks = asyncio.run(_collect_agent_chunks("default"))

    assert any("event: done" in chunk for chunk in chunks)
    assert rows, "Agent SDK runs should log ResultMessage cost for spend accounting"
    row = rows[-1]
    assert row["role"] == "agent_sdk_run"
    assert row["mode"] == "default"
    assert row["status"] == "success"
    assert row["cost_usd"] == 0.0123
    assert row["max_turns"] == 10


async def _collect_agent_chunks(mode: str) -> list[str]:
    return [chunk async for chunk in agent._run_agent(mode)]
