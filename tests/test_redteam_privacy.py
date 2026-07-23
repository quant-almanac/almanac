"""Privacy tests for the anonymized external Red Team legs (analyst/__init__.py).

Plan 🔴 #2: the external Red Team models (DeepSeek / Groq / Gemini / Qwen) must
receive PUBLIC market context only — never the book (holdings / sizes / P&L /
beliefs). These tests pin:

- ``_build_anonymized_market_gap_user`` produces a prompt the PII scanner deems
  clean and that carries no book-data tokens, while public market context
  passes through.
- ``_call_openai_compat_redteam`` fail-closes (returns empty, makes no network
  call) when handed a book-laden payload, because ``call_external_llm`` validates
  before the transport runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import analyst  # noqa: E402
from analyst import (  # noqa: E402
    _build_anonymized_market_gap_user,
    _build_public_market_context,
    _call_openai_compat_redteam,
)
from almanac.llm_safety import scan_text_for_pii  # noqa: E402


def _data_with_book() -> dict:
    """Market data that ALSO carries book-derived fields that must never leak."""
    return {
        "market_meta": {"vix": 18.2, "vix_level": "低位",
                        "us10y_yield": {"value": 4.2, "change_pct": 0.1}},
        "scenario": {"key": "neutral", "name": "中立",
                     "actions": [], "high_return_opportunities": []},
        "regime": {"spy_above": True, "nk_above": False, "macro_score": 5},
        "news_sentiment_summary": {"positive": 5, "negative": 2, "neutral": 3,
                                   "total": 10, "as_of": "2026-06-04"},
        # book content — MUST be excluded from the external context:
        "risk": {"var_95": 0.021, "cvar_95": 0.033, "current_dd": -0.05},
        "positions": [{"ticker": "9999.T", "value_jpy": 12000000}],
        "guard_state": {"entry_allowed": False, "n_positions": 9},
    }


def test_anonymized_prompt_is_pii_clean() -> None:
    txt = _build_anonymized_market_gap_user("VIX 18.2, US10Y 4.2%, risk-on")
    assert scan_text_for_pii(txt) == []


def test_anonymized_prompt_has_no_book_data_tokens() -> None:
    txt = _build_anonymized_market_gap_user("market ctx")
    for tok in ("value_jpy", "unrealized_pct", "ポジション概要", "pos_summary"):
        assert tok not in txt


def test_anonymized_prompt_passes_public_context_through() -> None:
    txt = _build_anonymized_market_gap_user("VIX 18.2 special-marker-xyz")
    assert "special-marker-xyz" in txt


def test_external_leg_fail_closes_on_book_payload() -> None:
    """A book-laden user must be blocked before any network call (dummy key)."""
    res = _call_openai_compat_redteam(
        base_url="https://api.deepseek.com",
        api_key="dummy-never-used",
        model_id="deepseek-chat",
        system="x",
        user='{"value_jpy": 2500000, "unrealized_pct": 15.3}',
    )
    assert res == {"attacks": [], "underutilized": []}


# ---------------------------------------------------------------------------
# Judge cross-validation — tickers pseudonymized, free-text dropped, restored
# ---------------------------------------------------------------------------


def test_judge_pseudonymizes_tickers_and_restores(monkeypatch) -> None:
    """The DeepSeek-R1 Judge must send only labels (T1/T2…) — no real tickers,
    no reason free-text — and restore real tickers in its local report."""
    captured: dict[str, str] = {}

    def fake_transport(*, system, user, **kwargs):
        captured["user"] = user
        judge = {
            "contradictions": ["T1: LongはbuyだがMediumはtrim"],
            "consensus_ranking": [
                {"ticker": "T1", "direction": "buy", "agreeing_tiers": 2, "avg_confidence": 80}
            ],
            "judge_summary": "T1 は要注意",
        }
        return json.dumps(judge), {"input_tokens": 10, "output_tokens": 5}

    monkeypatch.setattr(analyst, "_r1_judge_transport", fake_transport)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")

    long_a = {"priority_actions": [
        {"ticker": "NVDA", "type": "buy", "urgency": "high",
         "confidence_pct": 80, "reason": "secret-strong-thesis"}]}
    medium_a = {"priority_actions": [
        {"ticker": "NVDA", "type": "trim", "urgency": "medium",
         "confidence_pct": 70, "reason": "secret-rich-valuation"}]}

    report = analyst._judge_sonnet_outputs(long_a, medium_a, {}, {}, {}, {"attacks": []})

    sent = captured["user"]
    # No real ticker and no free-text reason may leave the process.
    assert "NVDA" not in sent
    assert "secret-strong-thesis" not in sent
    assert "secret-rich-valuation" not in sent
    assert "T1" in sent
    # The local report restores the real ticker for the human reader.
    assert "NVDA" in report
    assert "T1" not in report


# ---------------------------------------------------------------------------
# Public market context excludes book (R-round P1: shared_ctx stress/risk leak)
# ---------------------------------------------------------------------------


def test_public_market_context_excludes_book() -> None:
    ctx = _build_public_market_context(_data_with_book())
    assert scan_text_for_pii(ctx) == []
    for leak in ("推定損失", "var_95", "cvar_95", "0.021", "ストレステスト",
                 "リスク指標", "12000000"):
        assert leak not in ctx, f"book leaked into public context: {leak}"
    assert "VIX" in ctx and "18.2" in ctx          # public data still present


def test_external_redteam_user_is_public_only() -> None:
    user = _build_anonymized_market_gap_user(
        _build_public_market_context(_data_with_book()))
    assert scan_text_for_pii(user) == []
    for leak in ("推定損失", "var_95", "12000000", "リスク指標"):
        assert leak not in user
