import analyst
import pytest


def test_long_self_consistency_defaults_to_single_run(monkeypatch):
    calls = {"n": 0}

    def fake_analyze_long(data, shared_ctx=""):
        calls["n"] += 1
        return {
            "health": "good",
            "priority_actions": [{
                "ticker": "7203.T",
                "type": "buy",
            }],
        }

    monkeypatch.setattr(analyst, "_analyze_long", fake_analyze_long)

    result = analyst._self_consistent_long({"positions": []}, "")

    assert calls["n"] == 1
    assert result["priority_actions"][0]["self_consistency"] == "single_run"


def test_tier_timeout_env_has_floor(monkeypatch):
    monkeypatch.setenv("ALMANAC_TIER_LLM_TIMEOUT_SECONDS", "5")

    assert analyst._tier_llm_timeout_seconds() == 30.0


def test_tier_env_helpers_ignore_retired_kairos_name(monkeypatch):
    """The old KAIROS_* env var name is fully retired: setting it has no effect."""
    monkeypatch.delenv("ALMANAC_TIER_MAX_TOKENS", raising=False)
    monkeypatch.setenv("KAIROS_TIER_MAX_TOKENS", "12000")

    assert analyst._tier_max_tokens() == 16000  # falls through to the default


def test_tier_timeout_default_is_long_enough_for_large_tool_json():
    assert analyst._tier_llm_timeout_seconds() == 300.0


def test_sonnet_tier_defaults_to_16k_output_budget():
    assert analyst._tier_max_tokens() == 16000
    assert analyst._tier_retry_max_tokens() == 24000


def test_sonnet_tier_retries_max_token_truncation(monkeypatch):
    calls = []

    def fake_call_claude(system, user, **kwargs):
        calls.append({"user": user, **kwargs})
        if len(calls) == 1:
            raise RuntimeError(
                "Claude tool_use: stop_reason=max_tokens — max_tokens=16000 が不足。"
            )
        return {"priority_actions": []}

    monkeypatch.setattr(analyst, "call_claude", fake_call_claude)

    result = analyst._call_sonnet_tier_json(
        "tier_analysis_long",
        "PROMPT",
        "SHARED",
        "Long分析",
    )

    assert result == {"priority_actions": []}
    assert [c["max_tokens"] for c in calls] == [16000, 24000]
    assert calls[0]["role"] == "tier_analysis_long"
    assert calls[1]["cached_prefix"] == "SHARED"
    assert "再出力制約" in calls[1]["user"]
    assert "priority_actions は実行可能な高優先候補を最大12件目安" in calls[1]["user"]
    assert "6件固定で圧縮しない" in calls[1]["user"]


def test_deepseek_tier_call_receives_request_timeout(monkeypatch):
    from analyst.llm_client import call_tier_analysis

    seen = {}

    def fake_call_by_role(**kwargs):
        seen.update(kwargs)
        return {"content": '{"health":"good","priority_actions":[]}'}

    monkeypatch.setattr("llm_adapters.call_by_role", fake_call_by_role)

    result = call_tier_analysis(
        "system",
        "user",
        role="tier_analysis_margin_long",
        max_tokens=123,
        request_timeout=42,
    )

    assert result["_source"] == "deepseek:deepseek-v4-pro"
    assert seen["request_timeout"] == 42


def test_call_tier_analysis_retries_anthropic_max_tokens(monkeypatch):
    from analyst import llm_client

    calls = []

    monkeypatch.setattr("model_router.resolve_adapter", lambda role: "anthropic")
    monkeypatch.setattr("model_router.get_model", lambda role: "claude-sonnet-test")

    def fake_call_claude(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            raise RuntimeError("Claude tool_use: stop_reason=max_tokens — max_tokens=6000 が不足。")
        return {"priority_actions": []}

    monkeypatch.setattr(llm_client, "call_claude", fake_call_claude)

    result = llm_client.call_tier_analysis(
        "system",
        "user prompt",
        role="tier_analysis_short",
        max_tokens=6000,
        request_timeout=42,
    )

    assert result["_retry"] == "max_tokens_compact"
    assert [c["max_tokens"] for c in calls] == [6000, 12000]
    assert "再出力制約" in calls[1]["user"]
    assert "priority_actions は実行可能な高優先候補を最大12件目安" in calls[1]["user"]
    assert "最大6件" not in calls[1]["user"]


def test_call_tier_analysis_retries_deepseek_max_tokens(monkeypatch):
    from analyst.llm_client import call_tier_analysis

    calls = []

    def fake_call_by_role(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"error": "stop_reason=max_tokens"}
        return {"content": '{"health":"good","priority_actions":[]}'}

    monkeypatch.setattr("llm_adapters.call_by_role", fake_call_by_role)

    result = call_tier_analysis(
        "system",
        "user",
        role="tier_analysis_margin_long",
        max_tokens=6000,
        request_timeout=42,
    )

    assert result["_source"] == "deepseek:deepseek-v4-pro"
    assert [c["max_tokens"] for c in calls] == [6000, 12000]
    assert "再出力制約" in calls[1]["user"]
    assert "priority_actions は実行可能な高優先候補を最大12件目安" in calls[1]["user"]
    assert "最大6件" not in calls[1]["user"]
