import json
import sys
import types

import screener


def _candidate(ticker: str = "AAPL", *, score: int = 90) -> dict:
    return {
        "ticker": ticker,
        "strategy": "モメンタム",
        "score": score,
        "rsi": 58,
        "mom_5d": 4.2,
        "mom_1m": 8.5,
        "ma50_dev": 3.1,
        "volume_ratio": 1.7,
        "atr_pct": 2.4,
        "new_52w_high": True,
        "reason": "出来高を伴う上昇",
    }


def test_screener_deepseek_multiperspective_logs_usage(monkeypatch):
    rows: list[dict] = []

    def fake_call_deepseek(system, user, **kwargs):
        return {
            "content": json.dumps(
                {
                    "signals": [
                        {
                            "ticker": "AAPL",
                            "signal": "BUY",
                            "confidence": 77,
                            "reason": "勢い継続",
                            "bull_view": "出来高増",
                            "bear_view": "過熱注意",
                            "macro_view": "地合い良好",
                        }
                    ]
                }
            ),
            "usage": {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133},
            "model": "deepseek-v4-flash",
            "adapter": "deepseek",
        }

    fake_adapters = types.SimpleNamespace(call_deepseek=fake_call_deepseek)
    monkeypatch.setitem(sys.modules, "llm_adapters", fake_adapters)
    monkeypatch.setattr(screener, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = screener._call_deepseek_multiperspective(
        [_candidate()],
        {"sp500": "上", "nikkei": "上"},
        {"fed_rate": 4.5},
    )

    assert result[0]["ticker"] == "AAPL"
    assert rows, "screener DeepSeek calls should be included in llm spend accounting"
    row = rows[-1]
    assert row["role"] == "screener_deepseek_multiperspective"
    assert row["model"] == "deepseek-v4-flash"
    assert row["adapter"] == "deepseek"
    assert row["status"] == "ok"
    assert row["candidate_count"] == 1
    assert row["input_tokens"] == 111
    assert row["output_tokens"] == 22


def test_screener_sonnet_second_opinion_logs_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        name="submit_final_signals",
                        input={
                            "signals": [
                                {
                                    "ticker": "AAPL",
                                    "signal": "WATCH",
                                    "confidence": 61,
                                    "reason": "短期過熱",
                                }
                            ]
                        },
                    )
                ],
                stop_reason="tool_use",
                usage=types.SimpleNamespace(input_tokens=222, output_tokens=33),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropicClient))
    monkeypatch.setattr(screener, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = screener._call_sonnet_second_opinion(
        [_candidate()],
        {"sp500": "上", "nikkei": "上"},
        None,
    )

    assert result["AAPL"]["signal"] == "WATCH"
    assert rows, "screener Sonnet second opinion should log token usage"
    row = rows[-1]
    assert row["role"] == "screener_sonnet_second_opinion"
    assert row["status"] == "ok"
    assert row["candidate_count"] == 1
    assert row["input_tokens"] == 222
    assert row["output_tokens"] == 33


def test_screener_haiku_fallback_logs_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text='{"signal":"BUY","confidence":70,"reason":"反発期待"}')],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=44, output_tokens=11),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropicClient))
    monkeypatch.setattr(screener, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = screener._call_fallback_signal("AAPLの短期判定")

    assert result["signal"] == "BUY"
    assert rows, "screener Haiku fallback should log token usage"
    row = rows[-1]
    assert row["role"] == "screener_haiku_fallback"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 44
    assert row["output_tokens"] == 11


def test_screener_legacy_debate_logs_view_and_final_usage(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            tool_name = kwargs["tools"][0]["name"]
            if tool_name == "submit_views":
                return types.SimpleNamespace(
                    content=[
                        types.SimpleNamespace(
                            type="tool_use",
                            input={
                                "views": [
                                    {
                                        "ticker": "AAPL",
                                        "view": "BULLISH",
                                        "reason": "出来高増",
                                    }
                                ]
                            },
                        )
                    ],
                    stop_reason="tool_use",
                    usage=types.SimpleNamespace(input_tokens=100, output_tokens=10),
                )
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        input={
                            "signals": [
                                {
                                    "ticker": "AAPL",
                                    "signal": "BUY",
                                    "confidence": 76,
                                    "reason": "三視点一致",
                                }
                            ]
                        },
                    )
                ],
                stop_reason="tool_use",
                usage=types.SimpleNamespace(input_tokens=200, output_tokens=20),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropicClient))
    monkeypatch.setattr(screener, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    result = screener._call_debate_signals(
        [_candidate()],
        {"sp500": "上", "nikkei": "上"},
        None,
    )

    assert result[0]["ai_signal"] == "BUY"
    roles = [row["role"] for row in rows]
    assert roles.count("screener_legacy_debate_view") == 3
    assert "screener_legacy_final_signal" in roles
    assert {row["perspective"] for row in rows if row["role"] == "screener_legacy_debate_view"} == {
        "bull",
        "bear",
        "macro",
    }
