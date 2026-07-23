"""Part C: news_topic_analyzer の JSON 構造 & format_for_prompt 耐性。"""
from __future__ import annotations

import json
import importlib


def test_module_imports():
    m = importlib.import_module("news_topic_analyzer")
    assert callable(getattr(m, "format_for_prompt", None))


def test_format_for_prompt_returns_string():
    from news_topic_analyzer import format_for_prompt
    s = format_for_prompt()
    assert isinstance(s, str)


def test_format_for_prompt_survives_missing_file(tmp_path, monkeypatch):
    import news_topic_analyzer as nta
    monkeypatch.setattr(nta, "OUTPUT_FILE", tmp_path / "missing_news_topic_analysis.json")
    assert nta.format_for_prompt() == ""


def test_analyze_logs_llm_usage_for_deepdive(tmp_path, monkeypatch):
    import news_topic_analyzer as nta

    rows: list[dict] = []
    candidates_path = tmp_path / "news_signal_candidates.json"
    candidates_path.write_text(
        json.dumps(
            {
                "candidates": [
                    {
                        "ticker": "AAPL",
                        "name": "Apple",
                        "sentiment_score": 45,
                        "signal": "positive",
                        "top_headlines": ["Apple unveils durable AI product cycle"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_call_by_role(role, system, user, **kwargs):
        assert role == "news_topic_deepdive"
        return {
            "content": json.dumps(
                {
                    "analyses": [
                        {
                            "ticker": "AAPL",
                            "catalyst_type": "product",
                            "durability": "medium",
                            "impact_magnitude": 72,
                            "ripple_tickers": ["MSFT"],
                            "hold_horizon_days": 45,
                            "one_liner": "製品サイクルが追い風",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            "usage": {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168},
            "model": "deepseek-v4-flash",
            "adapter": "deepseek",
        }

    monkeypatch.setattr(nta, "CANDIDATES_FILE", candidates_path)
    monkeypatch.setattr(nta, "OUTPUT_FILE", tmp_path / "news_topic_analysis.json")
    monkeypatch.setattr(nta, "call_by_role", fake_call_by_role)
    monkeypatch.setattr(nta, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    out = nta.analyze(dry_run=True)

    assert out["analyses"][0]["ticker"] == "AAPL"
    assert rows, "news topic LLM calls should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "news_topic_deepdive"
    assert row["model"] == "deepseek-v4-flash"
    assert row["adapter"] == "deepseek"
    assert row["status"] == "ok"
    assert row["candidate_count"] == 1
    assert row["input_tokens"] == 123
    assert row["output_tokens"] == 45
