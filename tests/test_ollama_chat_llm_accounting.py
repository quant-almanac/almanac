import sys
import types

import ollama_chat


def test_ollama_chat_claude_fallback_logs_stream_usage(monkeypatch):
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    rows: list[dict] = []

    class FakeStream:
        text_stream = ["hello", " world"]

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, exc_tb):
            return None

        def get_final_message(self):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=90, output_tokens=12),
            )

    class FakeMessages:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(ollama_chat, "anthropic", fake_anthropic)
    monkeypatch.setattr(ollama_chat, "build_portfolio_context", lambda: "portfolio context")
    monkeypatch.setattr(ollama_chat, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    chunks = list(ollama_chat.chat_stream_claude([{"role": "user", "content": "hi"}]))

    assert chunks == ["hello", " world"]
    assert rows, "Claude fallback from ollama_chat should log final Anthropic usage"
    row = rows[-1]
    assert row["role"] == "ollama_chat_claude_fallback"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["message_count"] == 1
    assert row["input_tokens"] == 90
    assert row["output_tokens"] == 12
