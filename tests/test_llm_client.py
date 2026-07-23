import sys
import types

import pytest

from analyst import llm_client


def test_call_claude_retries_empty_tool_result(monkeypatch):
    calls = {"n": 0}

    class FakeMessages:
        def create(self, **kwargs):
            calls["n"] += 1
            return types.SimpleNamespace(content=[])

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(
        Anthropic=FakeAnthropicClient,
        APIStatusError=type("APIStatusError", (Exception,), {}),
        APITimeoutError=type("APITimeoutError", (Exception,), {}),
        APIConnectionError=type("APIConnectionError", (Exception,), {}),
    )

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(llm_client.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_client, "_append_llm_call_log", lambda row: None)

    with pytest.raises(RuntimeError, match="tool_use returned no usable result"):
        llm_client.call_claude(
            "system",
            "user",
            use_tool=True,
            request_timeout=1,
        )

    assert calls["n"] == 3


def test_fetch_web_search_news_logs_llm_usage(monkeypatch):
    rows = []

    class FakeBetaMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="market news")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(
                    input_tokens=321,
                    output_tokens=45,
                    server_tool_use=types.SimpleNamespace(web_search_requests=1),
                ),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.beta = types.SimpleNamespace(
                messages=FakeBetaMessages(),
            )

    fake_anthropic = types.SimpleNamespace(
        Anthropic=FakeAnthropicClient,
    )

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(llm_client, "_append_llm_call_log", lambda row: rows.append(row))

    assert llm_client.fetch_web_search_news() == "market news"
    assert rows, "web search should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "web_search_news"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 321
    assert row["output_tokens"] == 45
    assert row["server_tool_use"] == {"web_search_requests": 1}
