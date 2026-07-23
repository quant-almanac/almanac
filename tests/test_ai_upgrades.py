"""Part E 統合: analyst/__init__.py の alpha_context 連携。"""
from __future__ import annotations

import inspect


def test_synthesize_accepts_alpha_context():
    from analyst import _synthesize  # type: ignore
    sig = inspect.signature(_synthesize)
    assert "alpha_context" in sig.parameters
    assert "dca_context" in sig.parameters
    assert "news_topic_context" in sig.parameters
    assert "social_topic_context" in sig.parameters


def test_alpha_context_default_empty():
    from analyst import _synthesize  # type: ignore
    sig = inspect.signature(_synthesize)
    assert sig.parameters["alpha_context"].default == ""


def test_telegram_order_instruction_includes_limit_details():
    from analyst import _format_order_instruction  # type: ignore

    text = _format_order_instruction({
        "ticker": "META",
        "order_type": "limit",
        "limit_price": 605,
        "expiry_minutes": 240,
        "decision_price": 608.5,
    })

    assert "指値" in text
    assert "$605.00" in text
    assert "有効240分" in text
    assert "判断値$608.50" in text
