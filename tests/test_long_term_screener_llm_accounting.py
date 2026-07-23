import json
import sys
import types
from pathlib import Path

import long_term_screener as lts


def _candidate(ticker: str = "AAPL") -> dict:
    return {
        "ticker": ticker,
        "name": "Apple",
        "sector": "Technology",
        "score": 120,
        "roe": 0.25,
        "eps_growth": 0.18,
        "rev_growth": 0.12,
        "gross_margin": 0.42,
        "fcf_yield": 0.04,
        "peg_ratio": 1.8,
    }


def test_long_term_deepseek_predebate_logs_llm_usage(monkeypatch):
    rows: list[dict] = []

    def fake_call_deepseek(system, user, **kwargs):
        return {
            "content": json.dumps(
                {
                    "perspectives": [
                        {
                            "ticker": "AAPL",
                            "growth_view": "AI growth",
                            "risk_view": "valuation",
                            "macro_view": "rates",
                        }
                    ]
                }
            ),
            "usage": {"prompt_tokens": 144, "completion_tokens": 36, "total_tokens": 180},
            "model": "deepseek-v4-flash",
            "adapter": "deepseek",
        }

    fake_adapters = types.SimpleNamespace(call_deepseek=fake_call_deepseek)
    monkeypatch.setitem(sys.modules, "llm_adapters", fake_adapters)
    monkeypatch.setattr(lts, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = lts._call_deepseek_predebate([_candidate()])

    assert result[0]["ticker"] == "AAPL"
    assert rows, "long-term DeepSeek predebate should be included in llm spend accounting"
    row = rows[-1]
    assert row["role"] == "long_term_predebate_deepseek"
    assert row["model"] == "deepseek-v4-flash"
    assert row["adapter"] == "deepseek"
    assert row["status"] == "ok"
    assert row["candidate_count"] == 1
    assert row["input_tokens"] == 144
    assert row["output_tokens"] == 36


def test_long_term_debate_synthesis_logs_llm_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        input={
                            "theses": [
                                {
                                    "ticker": "AAPL",
                                    "thesis": "AI ecosystem growth. Valuation risk remains.",
                                    "bull_point": "AI services",
                                    "bear_point": "valuation",
                                }
                            ]
                        },
                    )
                ],
                stop_reason="tool_use",
                usage=types.SimpleNamespace(input_tokens=777, output_tokens=88),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(
        lts,
        "_call_deepseek_predebate",
        lambda candidates: [
            {
                "ticker": "AAPL",
                "growth_view": "AI growth",
                "risk_view": "valuation",
                "macro_view": "rates",
            }
        ],
    )
    monkeypatch.setattr(lts, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = lts._generate_debate_thesis([_candidate()])

    assert result["AAPL"]["thesis"]
    assert rows, "long-term Sonnet synthesis should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "long_term_thesis_synthesis"
    assert row["model"] == "claude-sonnet-5"
    assert row["status"] == "ok"
    assert row["candidate_count"] == 1
    assert row["input_tokens"] == 777
    assert row["output_tokens"] == 88


def test_long_term_haiku_fallback_logs_llm_usage(tmp_path: Path, monkeypatch):
    rows: list[dict] = []
    results_path = tmp_path / "long_term_screen_results.json"
    results_path.write_text(
        json.dumps({"passed": [_candidate("MSFT")]}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="Cloud growth. Valuation risk.")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=55, output_tokens=12),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(lts, "_append_llm_call_log", lambda row: rows.append(row), raising=False)
    monkeypatch.setattr(lts.time, "sleep", lambda seconds: None)

    lts._generate_thesis_sync(results_path)

    saved = json.loads(results_path.read_text(encoding="utf-8"))
    assert saved["passed"][0]["ai_thesis"] == "Cloud growth. Valuation risk."
    assert rows, "long-term Haiku fallback should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "long_term_thesis_haiku"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["ticker"] == "MSFT"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 55
    assert row["output_tokens"] == 12


def test_long_term_batch_submit_logs_request_metadata(tmp_path: Path, monkeypatch):
    rows: list[dict] = []
    state_path = tmp_path / "long_term_batch_state.json"

    class FakeBatches:
        def create(self, *, requests):
            self.requests = requests
            return types.SimpleNamespace(id="batch_123", processing_status="in_progress")

    fake_batches = FakeBatches()

    class FakeMessages:
        def __init__(self):
            self.batches = fake_batches

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(
        Anthropic=FakeAnthropicClient,
        types=types.SimpleNamespace(
            message_create_params=types.SimpleNamespace(
                MessageCreateParamsNonStreaming=lambda **kwargs: types.SimpleNamespace(**kwargs)
            )
        ),
    )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(lts, "_BATCH_STATE_FILE", state_path)
    monkeypatch.setattr(lts, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    batch_id = lts.submit_ai_batch([_candidate("AAPL"), _candidate("7203.T")])

    assert batch_id == "batch_123"
    assert len(fake_batches.requests) == 2
    assert rows, "Batch submit should leave auditable request metadata in llm call logs"
    row = rows[-1]
    assert row["role"] == "long_term_thesis_batch_submit"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "submitted"
    assert row["batch"] is True
    assert row["batch_id"] == "batch_123"
    assert row["request_count"] == 2
    assert row["tickers"] == ["AAPL", "7203.T"]
    assert row["cost_usd"] == 0.0


def test_long_term_batch_poll_logs_result_usage(tmp_path: Path, monkeypatch):
    rows: list[dict] = []
    state_path = tmp_path / "long_term_batch_state.json"
    results_path = tmp_path / "long_term_screen_results.json"
    state_path.write_text(
        json.dumps({"batch_id": "batch_123", "tickers": ["AAPL"], "status": "in_progress"}),
        encoding="utf-8",
    )
    results_path.write_text(
        json.dumps({"passed": [_candidate("AAPL")], "rejected": []}, ensure_ascii=False),
        encoding="utf-8",
    )

    class FakeBatches:
        def retrieve(self, batch_id):
            assert batch_id == "batch_123"
            return types.SimpleNamespace(processing_status="ended")

        def results(self, batch_id):
            assert batch_id == "batch_123"
            yield types.SimpleNamespace(
                custom_id="AAPL",
                result=types.SimpleNamespace(
                    type="succeeded",
                    message=types.SimpleNamespace(
                        content=[types.SimpleNamespace(text="Cloud growth. Valuation risk.")],
                        stop_reason="end_turn",
                        usage=types.SimpleNamespace(input_tokens=60, output_tokens=10),
                    ),
                ),
            )

    class FakeMessages:
        def __init__(self):
            self.batches = FakeBatches()

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(Anthropic=FakeAnthropicClient)

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(lts, "_BATCH_STATE_FILE", state_path)
    monkeypatch.setattr(lts, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    assert lts.poll_ai_batch(results_path=results_path) is True

    saved = json.loads(results_path.read_text(encoding="utf-8"))
    assert saved["passed"][0]["ai_thesis"] == "Cloud growth. Valuation risk."
    assert rows, "Batch results should log token usage for cost accounting"
    row = rows[-1]
    assert row["role"] == "long_term_thesis_batch_result"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["batch"] is True
    assert row["batch_id"] == "batch_123"
    assert row["custom_id"] == "AAPL"
    assert row["input_tokens"] == 60
    assert row["output_tokens"] == 10
