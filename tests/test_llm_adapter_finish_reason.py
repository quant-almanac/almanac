from types import SimpleNamespace
import sys

import llm_adapters


def test_openai_compatible_length_finish_is_not_a_success(monkeypatch):
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"risk_warnings": ['),
            finish_reason="length",
        )],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=200, total_tokens=300),
        model="deepseek-test",
    )
    completions = SimpleNamespace(create=lambda **_kwargs: response)
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **_kwargs: client),
    )

    result = llm_adapters._retry_openai_compat(
        "https://example.invalid",
        "key",
        "deepseek-test",
        "system",
        "user",
        200,
        0.2,
        True,
        "deepseek",
    )

    assert result["content"] == ""
    assert "stop_reason=max_tokens" in result["error"]
    assert result["usage"]["completion_tokens"] == 200


def test_deepseek_thinking_mode_is_sent_as_top_level_extension(monkeypatch):
    captured = {}
    response = SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content='{"analyses": []}'),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30),
        model="deepseek-test",
    )

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return response

    client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
    monkeypatch.setitem(
        sys.modules,
        "openai",
        SimpleNamespace(OpenAI=lambda **_kwargs: client),
    )

    result = llm_adapters._retry_openai_compat(
        "https://example.invalid",
        "key",
        "deepseek-test",
        "system",
        "user",
        4000,
        0.2,
        True,
        "deepseek",
        thinking_mode="disabled",
    )

    assert "error" not in result
    assert captured["extra_body"] == {"thinking": {"type": "disabled"}}
