import types

import decision_support as ds


def test_decision_support_sonnet_analysis_logs_llm_usage(monkeypatch):
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="状況サマリー")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=321, output_tokens=45),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(ds.anthropic, "Anthropic", FakeAnthropicClient)
    monkeypatch.setattr(ds, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = ds.analyze_with_sonnet("A", "context", "question")

    assert result == "状況サマリー"
    assert rows, "decision_support Sonnet analysis should be included in llm spend accounting"
    row = rows[-1]
    assert row["role"] == "decision_support_sonnet_analysis"
    assert row["model"] == ds.SONNET_MODEL
    assert row["case"] == "A"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 321
    assert row["output_tokens"] == 45


def test_decision_support_final_judgment_logs_llm_usage(monkeypatch):
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="最終判断")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=456, output_tokens=67),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setattr(ds.anthropic, "Anthropic", FakeAnthropicClient)
    monkeypatch.setattr(ds, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = ds.final_judgment_with_opus("E", "context", "analysis", "preference")

    assert result == "最終判断"
    assert rows, "decision_support final judgment should be included in llm spend accounting"
    row = rows[-1]
    assert row["role"] == "decision_support_final_judgment"
    assert row["model"] == ds.OPUS_MODEL
    assert row["case"] == "E"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 456
    assert row["output_tokens"] == 67
