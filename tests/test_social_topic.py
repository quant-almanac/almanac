"""Part C: social_topic_analyzer."""
from __future__ import annotations

import json
import importlib


def test_module_imports():
    m = importlib.import_module("social_topic_analyzer")
    assert callable(getattr(m, "format_for_prompt", None))
    # 熱狂しきい値の存在
    assert getattr(m, "MSG_THRESHOLD", None) is not None
    assert getattr(m, "BULLISH_THRESHOLD", None) is not None


def test_format_empty_when_no_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from social_topic_analyzer import format_for_prompt
    assert format_for_prompt() == ""


def test_analyze_logs_llm_usage_for_deepdive(tmp_path, monkeypatch):
    import social_topic_analyzer as sta

    rows: list[dict] = []
    social_path = tmp_path / "social_sentiment.json"
    news_path = tmp_path / "news_signal_candidates.json"
    social_path.write_text(
        json.dumps(
            {
                "stocktwits": {
                    "TSLA": {
                        "message_count": 280,
                        "bullish_pct": 82.5,
                        "is_trending": True,
                        "watchlist_count": 12000,
                        "sentiment": "bullish",
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    news_path.write_text(
        json.dumps(
            {"candidates": [{"ticker": "TSLA", "top_headlines": ["Tesla delivery beat"]}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_call_by_role(role, system, user, **kwargs):
        assert role == "social_topic_deepdive"
        return {
            "content": json.dumps(
                {
                    "evaluations": [
                        {
                            "ticker": "TSLA",
                            "category": "earnings_driven",
                            "confidence_pct": 76,
                            "action_bias": "hold",
                            "one_liner": "熱狂に材料が伴う",
                        }
                    ]
                },
                ensure_ascii=False,
            ),
            "usage": {"prompt_tokens": 88, "completion_tokens": 21, "total_tokens": 109},
            "model": "deepseek-v4-flash",
            "adapter": "deepseek",
        }

    monkeypatch.setattr(sta, "SOCIAL_FILE", social_path)
    monkeypatch.setattr(sta, "NEWS_FILE", news_path)
    monkeypatch.setattr(sta, "OUTPUT_FILE", tmp_path / "social_topic_analysis.json")
    monkeypatch.setattr(sta, "call_by_role", fake_call_by_role)
    monkeypatch.setattr(sta, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    out = sta.analyze(dry_run=True)

    assert out["evaluations"][0]["ticker"] == "TSLA"
    assert rows, "social topic LLM calls should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "social_topic_deepdive"
    assert row["model"] == "deepseek-v4-flash"
    assert row["adapter"] == "deepseek"
    assert row["status"] == "ok"
    assert row["candidate_count"] == 1
    assert row["input_tokens"] == 88
    assert row["output_tokens"] == 21
