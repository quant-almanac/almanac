"""Claude Opus 5 migration guards.

Covers the three failure modes that a plain model-ID swap would introduce
silently:

1. ``temperature`` sent to Opus 5 -> 400 on every call.
2. ``claude-opus-5`` missing from the price table -> cost recorded as ``None``
   (``"claude-opus-4"`` is *not* a substring of ``"claude-opus-5"``).
3. A ``stop_reason == "max_tokens"`` response accepted as a real result even
   though its ``tool_use.input`` is truncated.

Plus the ``effort`` plumbing, which must reach forced-tool calls but must never
reach Haiku 4.5 (which errors on the parameter).
"""
from __future__ import annotations

import sys
import types

import pytest


# ---------------------------------------------------------------------------
# model_router / price table
# ---------------------------------------------------------------------------


def test_opus_role_resolves_to_opus_5_exactly():
    """Exact match. ``"opus" in model_id`` would still pass on Opus 4.8."""
    import model_router as mr

    assert mr.MODEL_REGISTRY["opus"] == "claude-opus-5"
    assert mr.get_model("final_synthesis") == "claude-opus-5"


def test_opus_5_has_its_own_price_entry():
    import llm_cost_accounting as lca

    assert lca._price_for_model("claude-opus-5") == {"input": 5.0, "output": 25.0}


def test_opus_5_cost_is_numeric_not_none():
    """Regression: a missing price key makes estimate_cost_usd return None."""
    import llm_cost_accounting as lca

    assert lca.estimate_cost_usd("claude-opus-5", 1000, 1000) is not None


def test_opus_5_does_not_fall_through_to_legacy_opus_4_pricing():
    import llm_cost_accounting as lca

    assert lca._price_for_model("claude-opus-5") != {"input": 15.0, "output": 75.0}
    # ...and the legacy generation still maps to the legacy price.
    assert lca._price_for_model("claude-opus-4-20250514") == {"input": 15.0, "output": 75.0}


def test_specific_opus_keys_precede_generic_prefix():
    import llm_cost_accounting as lca

    keys = list(lca.DEFAULT_PRICES_PER_MILLION.keys())
    for specific in ("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6"):
        assert keys.index(specific) < keys.index("claude-opus-4"), specific


# ---------------------------------------------------------------------------
# sampling params / effort gating
# ---------------------------------------------------------------------------


def test_opus_5_rejects_sampling_params():
    from analyst import llm_client

    assert llm_client._model_rejects_sampling_params("claude-opus-5")


def test_opus_5_not_forced_to_disable_thinking():
    """Opus 5 must keep adaptive thinking.

    Disabling it can make the model emit a tool call as visible text instead of
    a structured ``tool_use`` block, which silently yields an empty analysis.
    """
    from analyst import llm_client

    assert not llm_client._model_defaults_to_adaptive_thinking("claude-opus-5")


@pytest.mark.parametrize("model", ["claude-opus-5", "claude-sonnet-5", "claude-opus-4-8"])
def test_effort_is_sent_for_effort_capable_models(model):
    from analyst.llm_client import anthropic_compat_kwargs

    assert anthropic_compat_kwargs(model) == {"output_config": {"effort": "low"}}


@pytest.mark.parametrize("model", ["claude-haiku-4-5", "claude-haiku-4-5-20251001"])
def test_effort_is_never_sent_to_haiku(model):
    """Haiku 4.5 errors on ``effort``; eco mode downgrades sonnet -> haiku."""
    from analyst.llm_client import anthropic_compat_kwargs

    assert anthropic_compat_kwargs(model) == {}


# ---------------------------------------------------------------------------
# usage extraction / thinking_fired three-valued
# ---------------------------------------------------------------------------


def _resp(*, stop_reason="tool_use", thinking_tokens="__absent__"):
    details = None
    if thinking_tokens != "__absent__":
        details = types.SimpleNamespace(thinking_tokens=thinking_tokens)
    return types.SimpleNamespace(
        stop_reason=stop_reason,
        content=[],
        usage=types.SimpleNamespace(
            input_tokens=1, output_tokens=2, output_tokens_details=details
        ),
    )


def test_thinking_fired_is_none_when_unavailable():
    """Recording "unavailable" as False would assert the model did not think."""
    from analyst.llm_client import usage_fields

    assert usage_fields(_resp())["thinking_fired"] is None


def test_thinking_fired_is_false_on_zero_tokens():
    from analyst.llm_client import usage_fields

    assert usage_fields(_resp(thinking_tokens=0))["thinking_fired"] is False


def test_thinking_fired_is_true_on_positive_tokens():
    from analyst.llm_client import usage_fields

    fields = usage_fields(_resp(thinking_tokens=512))
    assert fields["thinking_fired"] is True
    assert fields["thinking_tokens"] == 512


def test_response_hit_max_tokens_detector():
    from analyst.llm_client import response_hit_max_tokens

    assert response_hit_max_tokens(_resp(stop_reason="max_tokens")) is True
    assert response_hit_max_tokens(_resp(stop_reason="tool_use")) is False


# ---------------------------------------------------------------------------
# call_claude: effort must reach forced-tool calls
# ---------------------------------------------------------------------------


def _install_fake_anthropic(monkeypatch, captured, response):
    class FakeMessages:
        def create(self, **kwargs):
            captured.append(kwargs)
            return response

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(
        sys.modules,
        "anthropic",
        types.SimpleNamespace(
            Anthropic=FakeClient,
            APIStatusError=type("APIStatusError", (Exception,), {}),
            APITimeoutError=type("APITimeoutError", (Exception,), {}),
            APIConnectionError=type("APIConnectionError", (Exception,), {}),
        ),
    )


def test_call_claude_sends_effort_on_forced_tool_call(monkeypatch):
    """The effort kwarg sits outside the ``if use_tool / elif`` branch.

    Putting it inside would silently skip every forced-tool call - which is
    most of this system.
    """
    from analyst import llm_client

    captured: list[dict] = []
    response = types.SimpleNamespace(
        stop_reason="tool_use",
        content=[
            types.SimpleNamespace(
                type="tool_use", name="submit_analysis", input={"result": {"ok": True}}
            )
        ],
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1, output_tokens_details=None),
    )
    _install_fake_anthropic(monkeypatch, captured, response)
    monkeypatch.setattr(llm_client, "_append_llm_call_log", lambda row: None, raising=False)

    llm_client.call_claude("sys", "user", model="claude-opus-5", use_tool=True)

    assert captured, "no request was captured"
    kwargs = captured[-1]
    assert kwargs["output_config"] == {"effort": "low"}
    assert kwargs["tool_choice"] == {"type": "tool", "name": "submit_analysis"}
    assert "temperature" not in kwargs, "Opus 5 returns 400 on temperature"


def test_call_claude_omits_effort_for_haiku(monkeypatch):
    from analyst import llm_client

    captured: list[dict] = []
    response = types.SimpleNamespace(
        stop_reason="end_turn",
        content=[types.SimpleNamespace(type="text", text="hi")],
        usage=types.SimpleNamespace(input_tokens=1, output_tokens=1, output_tokens_details=None),
    )
    _install_fake_anthropic(monkeypatch, captured, response)
    monkeypatch.setattr(llm_client, "_append_llm_call_log", lambda row: None, raising=False)

    llm_client.call_claude("sys", "user", model="claude-haiku-4-5-20251001")

    assert captured
    assert "output_config" not in captured[-1]


# ---------------------------------------------------------------------------
# premium budget mode
# ---------------------------------------------------------------------------


def test_premium_escalates_sonnet_roles_to_opus_5(monkeypatch):
    import model_router as mr

    monkeypatch.setenv("ALMANAC_BUDGET_MODE", "premium")
    for role in ("tier_analysis_long", "screener_deepdive", "screener_second_opinion",
                 "decision_support"):
        assert mr.get_model(role) == "claude-opus-5", role


def test_long_term_thesis_is_wired_to_the_model_router(monkeypatch):
    """``long_term_thesis`` の budget mode が実装まで届くこと。

    以前は long_term_screener がモデルIDを直書きしており、MODEL_REGISTRY に
    登録済みのロールが使われず premium/eco がこの経路だけ無視されていた。

    ``SONNET_MODEL_ID`` は import 時に確定するため、import 順に依存しない
    ``_resolve_thesis_model()`` を直接検証する。
    """
    import model_router as mr
    import long_term_screener

    monkeypatch.setenv("ALMANAC_BUDGET_MODE", "premium")
    assert mr.get_model("long_term_thesis") == "claude-opus-5"
    assert long_term_screener._resolve_thesis_model() == "claude-opus-5"

    monkeypatch.setenv("ALMANAC_BUDGET_MODE", "normal")
    assert long_term_screener._resolve_thesis_model() == "claude-sonnet-5"


def test_thesis_model_resolution_falls_back_when_router_unavailable(monkeypatch):
    """model_router を import できない場合も現行世代の Sonnet に落ちること。"""
    import builtins

    import long_term_screener

    real_import = builtins.__import__

    def _blocked(name, *args, **kwargs):
        if name == "model_router":
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert long_term_screener._resolve_thesis_model() == "claude-sonnet-5"


# ---------------------------------------------------------------------------
# _synthesize: truncated responses must never be accepted
# ---------------------------------------------------------------------------


def _stub_synthesize_deps(monkeypatch, rows):
    """Neutralize _synthesize's context builders so only the API path is tested."""
    import analyst

    monkeypatch.setattr(analyst, "_append_llm_call_log", lambda row: rows.append(row), raising=False)
    for name, ret in [
        ("load_history_context", ""),
        ("_load_bl_views_for_opus", ""),
        ("_load_execution_quality_summary", None), ("_fmt_tunable_limits_context", ""),
    ]:
        monkeypatch.setattr(analyst, name, lambda *a, _r=ret, **k: _r)
    for name in (
        "fetch_web_search_news",
        "_load_catalyst_context_for_opus",
        "_load_beliefs",
    ):
        monkeypatch.setattr(
            analyst,
            name,
            lambda *a, _name=name, **k: (_ for _ in ()).throw(
                AssertionError(f"{_name} must not be called after decision snapshot freeze")
            ),
        )
    for name in [
        "fmt_news_section", "fmt_earnings_section",
        "_format_beliefs_context", "_format_execution_quality_for_prompt",
        "_format_agent_reliability_for_prompt", "_format_recent_own_recs_for_prompt",
        "_format_earnings_blackout_for_prompt", "_format_done_list_for_prompt",
        "_build_consolidated_rebalance_context", "_fmt_scenario_monitoring",
        "_extract_tax_urgent_actions",
    ]:
        monkeypatch.setattr(analyst, name, lambda *a, **k: "")
    monkeypatch.setattr(analyst, "_gather_chart_context", None, raising=False)
    monkeypatch.setattr(analyst, "_format_chart_for_prompt", None, raising=False)

    import behavioral_guard

    monkeypatch.setattr(
        behavioral_guard, "evaluate_leverage_health",
        lambda portfolio_total_jpy=0: {
            "current_leverage": 1.0, "leverage_cap": 1.2, "max_leverage_setting": 1.2,
            "status": "safe", "action": "ok", "new_buy_allowed": True, "margin_buy_allowed": True,
        },
    )


def _run_synthesize():
    import analyst

    tier = {"health": "good", "priority_actions": []}
    return analyst._synthesize(
        tier, tier, tier, tier, tier,
        portfolio_total=1_000_000,
        scenario={"key": "base", "name": "Base", "cash_ratio_target": 0},
        risk={},
        market_meta={"vix": 15, "us10y_yield": {}, "us2y_yield": {}},
        news=[], earnings={},
        cash_info={"total_cash_jpy": 0, "fx_rate_usdjpy": 150},
    )


def _tool_block(payload):
    return types.SimpleNamespace(type="tool_use", name="submit_analysis", input={"result": payload})


def _synth_resp(stop_reason, payload):
    return types.SimpleNamespace(
        stop_reason=stop_reason,
        content=[_tool_block(payload)],
        usage=types.SimpleNamespace(input_tokens=10, output_tokens=20, output_tokens_details=None),
        model="claude-opus-5",
    )


# A truncated response whose tool input is PARTIAL BUT NON-EMPTY. The old
# empty-result guard does not catch this, so it would be accepted as the day's
# final investment decision.
_TRUNCATED_PARTIAL = {"priority_actions": [{"ticker": "AAPL", "type": "buy"}]}
_COMPLETE = {
    "priority_actions": [{"ticker": "AAPL", "type": "buy"}],
    "overall_stance": "neutral",
    "hold_notes": ["ok"],
}


def test_truncated_non_empty_result_is_not_accepted_and_is_retried(monkeypatch):
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    rows: list[dict] = []
    seen_max_tokens: list[int] = []

    class FakeMessages:
        def __init__(self):
            self.n = 0

        def create(self, **kwargs):
            self.n += 1
            seen_max_tokens.append(kwargs["max_tokens"])
            if self.n == 1:
                return _synth_resp("max_tokens", _TRUNCATED_PARTIAL)
            return _synth_resp("tool_use", _COMPLETE)

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(
        sys.modules, "anthropic",
        types.SimpleNamespace(
            Anthropic=FakeClient,
            APIStatusError=type("APIStatusError", (Exception,), {}),
            APITimeoutError=type("APITimeoutError", (Exception,), {}),
            APIConnectionError=type("APIConnectionError", (Exception,), {}),
        ),
    )
    _stub_synthesize_deps(monkeypatch, rows)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    result = _run_synthesize()

    # The truncated attempt was retried, not returned.
    assert len(seen_max_tokens) >= 2, seen_max_tokens
    assert seen_max_tokens[1] > seen_max_tokens[0], "retry must raise max_tokens"
    # The accepted result is the complete one.
    assert result.get("overall_stance") == "neutral"
    # The truncated attempt is recorded as such, not as "ok".
    assert any(r.get("status") == "max_tokens" for r in rows), [r.get("status") for r in rows]


def test_persistently_truncated_synthesis_returns_error_not_partial(monkeypatch):
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    rows: list[dict] = []

    class FakeMessages:
        def create(self, **kwargs):
            return _synth_resp("max_tokens", _TRUNCATED_PARTIAL)

    class FakeClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    monkeypatch.setitem(
        sys.modules, "anthropic",
        types.SimpleNamespace(
            Anthropic=FakeClient,
            APIStatusError=type("APIStatusError", (Exception,), {}),
            APITimeoutError=type("APITimeoutError", (Exception,), {}),
            APIConnectionError=type("APIConnectionError", (Exception,), {}),
        ),
    )
    _stub_synthesize_deps(monkeypatch, rows)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    result = _run_synthesize()

    assert "max_tokens_truncated" in (result.get("error") or ""), result.get("error")
    # Crucially: the partial actions must NOT be surfaced as real decisions.
    assert result.get("priority_actions") == []
