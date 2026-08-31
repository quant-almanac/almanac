"""Privacy tests for the anonymized external Red Team legs (analyst/__init__.py).

Plan 🔴 #2: the external Red Team models (DeepSeek / Groq / Gemini / Qwen) must
receive PUBLIC market context only — never the book (holdings / sizes / P&L /
beliefs). These tests pin:

- The book-aware Claude Haiku leg is blocked before transport under
  ``strict_local`` and allowed only under an explicit Anthropic-capable mode.
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
import almanac.llm_safety as llm_safety  # noqa: E402
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
        "scenario": {
            "key": "BULL",
            "name": "弱い強気",
            "actions": [],
            "high_return_opportunities": [],
            "short_allowed": False,
            "short_product_enabled": {"US": True, "JP": True},
        },
        "regime": {"spy_above": True, "nk_above": False, "macro_score": 5},
        "news_sentiment_summary": {"positive": 5, "negative": 2, "neutral": 3,
                                   "total": 10, "as_of": "2026-06-04"},
        # book content — MUST be excluded from the external context:
        "risk": {"var_95": 0.021, "cvar_95": 0.033, "current_dd": -0.05},
        "positions": [{"ticker": "9999.T", "value_jpy": 12000000}],
        "guard_state": {"entry_allowed": False, "n_positions": 9},
    }


def test_haiku_redteam_is_blocked_before_transport_in_strict_local(monkeypatch) -> None:
    calls: list[dict] = []
    audits: list[dict] = []

    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "strict_local")
    monkeypatch.setattr(analyst, "call_claude", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        llm_safety,
        "log_book_aware_call",
        lambda **kwargs: audits.append(kwargs),
    )

    result = analyst._analyze_redteam(
        _data_with_book(),
        shared_ctx="book-derived-risk-context",
        beliefs=[{"ticker": "9999.T", "theme": "private-belief"}],
        tier_hints={"long": "private-tier-hint"},
    )

    assert result["attacks"] == []
    assert result["underutilized"] == []
    assert result["transport_status"] == "blocked"
    assert calls == []
    assert len(audits) == 1
    assert audits[0]["role"] == "red_team_haiku"
    assert audits[0]["status"] == "blocked"


def test_haiku_redteam_runs_and_audits_when_anthropic_book_aware(monkeypatch) -> None:
    calls: list[dict] = []
    audits: list[dict] = []

    def _fake_call_claude(**kwargs):
        calls.append(kwargs)
        return {
            "attacks": [{
                "ticker": "NVDA", "action": "add", "expected_return_pct": 20,
                "rationale": "momentum", "risk_note": "valuation",
            }],
            "underutilized": [],
        }

    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    monkeypatch.setattr(analyst, "call_claude", _fake_call_claude)
    monkeypatch.setattr(
        llm_safety,
        "log_book_aware_call",
        lambda **kwargs: audits.append(kwargs),
    )

    book = _data_with_book()
    result = analyst._analyze_redteam(book, shared_ctx="private-context")

    assert result["attacks"]
    assert result["transport_status"] == "ok"
    assert len(calls) == 1
    assert book["positions"][0]["ticker"] in calls[0]["user"]
    assert len(audits) == 1
    assert audits[0]["role"] == "red_team_haiku"
    assert audits[0]["status"] == "ok"


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


def test_routed_redteam_treats_schema_valid_zero_as_success(monkeypatch) -> None:
    from types import SimpleNamespace
    import llm_adapters
    import model_router
    import almanac.llm_safety as safety

    calls = []
    monkeypatch.setattr(model_router, "get_model", lambda role: f"model-for-{role}")
    monkeypatch.setattr(
        llm_adapters,
        "call_by_role",
        lambda role, *_args, **_kwargs: calls.append(role) or {
            "content": '{"attacks":[],"underutilized":[]}',
            "usage": {"prompt_tokens": 1, "completion_tokens": 2},
        },
    )

    def fake_external(payload, **kwargs):
        content, _usage = kwargs["transport"](
            system=payload.system,
            user=payload.user,
            max_tokens=kwargs["max_tokens"],
            temperature=kwargs["temperature"],
        )
        return SimpleNamespace(content=content)

    monkeypatch.setattr(safety, "call_external_llm", fake_external)

    result = analyst._call_routed_redteam("red_team_3", "system", "public market")
    assert result["transport_status"] == "ok"
    assert result["attacks"] == []
    assert result["model_id"] == "model-for-red_team_3"
    assert calls == ["red_team_3"]


def test_routed_redteam_logs_schema_failure_as_error(monkeypatch) -> None:
    import llm_adapters
    import almanac.llm_safety as safety
    from analyst import llm_client

    audit = []
    monkeypatch.setattr(
        llm_adapters,
        "call_by_role",
        lambda *_args, **_kwargs: {"content": '{"attacks":"not-a-list"}', "usage": {}},
    )
    monkeypatch.setattr(
        safety,
        "call_external_llm",
        lambda payload, **kwargs: kwargs["transport"](
            system=payload.system,
            user=payload.user,
            max_tokens=kwargs["max_tokens"],
            temperature=kwargs["temperature"],
        ),
    )
    monkeypatch.setattr(llm_client, "_append_llm_call_log", lambda row: audit.append(row) or True)

    result = analyst._call_routed_redteam("red_team_1", "system", "public")
    assert result["transport_status"] == "error"
    assert "schema_invalid" in result["error"]
    assert len(audit) == 1
    assert audit[0]["status"] == "error"
    assert audit[0]["role"] == "red_team_1"


def test_redteam_multi_calls_each_router_role_once_without_empty_fallback(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr(
        analyst,
        "_analyze_redteam",
        lambda *_args, **_kwargs: {
            "attacks": [], "underutilized": [], "transport_status": "ok",
        },
    )
    monkeypatch.setattr(
        analyst,
        "_call_routed_redteam",
        lambda role, *_args: calls.append(role) or {
            "attacks": [], "underutilized": [], "transport_status": "ok",
            "model_id": role,
        },
    )

    result = analyst._analyze_redteam_multi(_data_with_book(), shared_ctx="public")
    assert sorted(calls) == ["red_team_1", "red_team_2", "red_team_3", "red_team_4"]
    assert result["attacks"] == []
    assert all(row["status"] == "ok" for row in result["provider_status"].values())


def test_redteam_schema_rejects_partial_or_nonfinite_attacks() -> None:
    assert analyst._validate_redteam_payload({
        "attacks": [{"ticker": "AAPL", "action": "buy"}],
        "underutilized": [],
    }) is None
    assert analyst._validate_redteam_payload({
        "attacks": [{
            "ticker": "AAPL", "action": "buy", "rationale": "x",
            "risk_note": "y", "expected_return_pct": float("nan"),
        }],
        "underutilized": [],
    }) is None


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


def test_r1_judge_transport_requests_json_output(monkeypatch) -> None:
    import sys
    from types import SimpleNamespace

    captured: dict = {}

    class _Completions:
        def create(self, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(
                    message=SimpleNamespace(
                        content='{"contradictions":[]}',
                        reasoning_content="private reasoning",
                    ),
                )],
                usage=SimpleNamespace(prompt_tokens=12, completion_tokens=34),
            )

    class _Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs
            self.chat = SimpleNamespace(completions=_Completions())

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_Client))

    raw, usage = analyst._r1_judge_transport(
        base_url="https://example.invalid",
        api_key="test",
        model_id="deepseek-reasoner",
        system="Return JSON.",
        user='{"request":"judge"}',
        max_tokens=4096,
        temperature=0.0,
    )

    assert raw == '{"contradictions":[]}'
    assert usage == {"input_tokens": 12, "output_tokens": 34}
    assert captured["max_tokens"] == 4096
    assert captured["response_format"] == {"type": "json_object"}


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
    assert "商品・口座機能: US=True / JP=True" in ctx
    assert "相場レジーム上の広範な方向性ショート推奨: False" in ctx
    assert "後者がFalseでも商品機能OFFを意味しない" in ctx


def test_external_redteam_user_is_public_only() -> None:
    user = _build_anonymized_market_gap_user(
        _build_public_market_context(_data_with_book()))
    assert scan_text_for_pii(user) == []
    for leak in ("推定損失", "var_95", "12000000", "リスク指標"):
        assert leak not in user
