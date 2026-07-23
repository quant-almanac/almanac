import sys
import types

from api.routes import chat


def test_chat_stream_logs_final_message_usage(monkeypatch):
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
                usage=types.SimpleNamespace(input_tokens=123, output_tokens=17),
            )

    class FakeMessages:
        def stream(self, **kwargs):
            return FakeStream()

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropicClient))
    monkeypatch.setattr(chat, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    chunks = list(chat._stream_claude("system prompt", [{"role": "user", "content": "hi"}]))

    assert chunks[-1] == "data: [DONE]\n\n"
    assert rows, "chat streaming should log final Anthropic usage for spend accounting"
    row = rows[-1]
    assert row["role"] == "chat_stream"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["message_count"] == 1
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 17
