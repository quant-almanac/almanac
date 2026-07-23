import sys
import types

import behavioral_guard as bg


def _blocked_state() -> dict:
    return {
        "daily_pnl_pct": -0.035,
        "daily_pnl_jpy": -350_000,
        "monthly_pnl_pct": -0.02,
        "monthly_pnl_jpy": -200_000,
        "active_trades": 3,
        "short_positions": 1,
    }


def test_guardrail_suggestion_logs_haiku_usage_without_telegram(monkeypatch):
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(text="1. 新規停止\n2. 損失確認\n3. サイズ縮小")],
                stop_reason="end_turn",
                usage=types.SimpleNamespace(input_tokens=210, output_tokens=35),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(sys.modules, "anthropic", types.SimpleNamespace(Anthropic=FakeAnthropicClient))
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(bg, "_append_llm_call_log", lambda row: rows.append(row), raising=False)

    bg._send_guardrail_suggestion(_blocked_state(), trading_stopped=True)

    assert rows, "guardrail AI suggestion should be included in llm spend accounting"
    row = rows[-1]
    assert row["role"] == "guardrail_suggestion"
    assert row["model"] == "claude-haiku-4-5-20251001"
    assert row["status"] == "ok"
    assert row["trading_stopped"] is True
    assert row["input_tokens"] == 210
    assert row["output_tokens"] == 35


def test_leverage_health_uses_tunable_vix_margin_buy_block(monkeypatch):
    values = {
        "max_portfolio_leverage": 1.2,
        "vix_margin_buy_block": 35.0,
        "vix_leverage_cap_15": 1.2,
        "vix_leverage_cap_20": 1.1,
        "vix_leverage_cap_25": 1.0,
        "vix_leverage_cap_30": 0.8,
    }
    monkeypatch.setattr(bg, "_tp_bg", lambda key, default=None: values.get(key, default))

    allowed = bg.evaluate_leverage_health(current_leverage=0.5, vix=25.0)
    blocked = bg.evaluate_leverage_health(current_leverage=0.5, vix=35.0)

    assert allowed["margin_buy_block_vix"] == 35.0
    assert allowed["margin_buy_allowed"] is True
    assert blocked["margin_buy_allowed"] is False
