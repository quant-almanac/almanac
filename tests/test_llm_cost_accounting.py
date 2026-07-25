"""llm_cost_accounting: 価格表 prefix 順序・Sonnet 5 intro 価格の回帰テスト"""
from datetime import datetime

import llm_cost_accounting as lca


def test_sonnet_5_intro_price_before_expiry():
    price = lca._price_for_model("claude-sonnet-5", as_of=datetime(2026, 8, 1))
    assert price == lca.SONNET_5_INTRO_PRICE


def test_sonnet_5_standard_price_after_expiry():
    price = lca._price_for_model("claude-sonnet-5", as_of=datetime(2026, 9, 1))
    assert price == lca.DEFAULT_PRICES_PER_MILLION["claude-sonnet-5"]
    assert price == {"input": 3.0, "output": 15.0}


def test_sonnet_5_does_not_fall_through_to_sonnet_4():
    price = lca._price_for_model("claude-sonnet-5", as_of=datetime(2026, 9, 1))
    assert price != lca.DEFAULT_PRICES_PER_MILLION["claude-sonnet-4"] or price == {
        "input": 3.0, "output": 15.0,
    }


def test_opus_4_8_does_not_fall_through_to_generic_opus_4():
    # claude-opus-4 (旧世代 $15/$75) の prefix が claude-opus-4-8 に誤マッチしないこと
    price = lca._price_for_model("claude-opus-4-8")
    assert price == {"input": 5.0, "output": 25.0}


def test_opus_4_generic_still_maps_to_legacy_price():
    price = lca._price_for_model("claude-opus-4-20250514")
    assert price == {"input": 15.0, "output": 75.0}


def test_specific_keys_precede_generic_prefix_in_insertion_order():
    keys = list(lca.DEFAULT_PRICES_PER_MILLION.keys())
    assert keys.index("claude-opus-4-8") < keys.index("claude-opus-4")
    assert keys.index("claude-opus-4-7") < keys.index("claude-opus-4")
    assert keys.index("claude-opus-4-6") < keys.index("claude-opus-4")


# --- 外部モデル (無料枠) の 0 円計上 -----------------------------------------
# 未登録だと cost_usd=None となり「不明」と「無料」が区別できないため 0 を明示。


def test_free_tier_external_models_cost_zero_not_none():
    for model in (
        "llama-3.3-70b-versatile",
        "qwen/qwen3-235b-a22b-2507",
        "qwen/qwen-2.5-72b-instruct",
        "gemini-flash-latest",
    ):
        cost = lca.estimate_cost_usd(model, 10_000, 5_000)
        assert cost == 0.0, f"{model} -> {cost!r} (None は「不明」を意味してしまう)"


def test_unknown_external_model_stays_none_rather_than_silently_free():
    # 版が上がった未登録IDは 0 円ではなく欠落として表面化させる。
    assert lca.estimate_cost_usd("qwen/qwen4-next-9999", 10_000, 5_000) is None
    assert lca.estimate_cost_usd("llama-9.9-999b-versatile", 10_000, 5_000) is None


def test_free_tier_entries_do_not_shadow_paid_models():
    # "qwen" 等の汎用マッチで Anthropic/DeepSeek の価格が壊れないこと。
    assert lca.estimate_cost_usd("claude-opus-5", 1_000_000, 0) == 5.0
    assert lca._price_for_model("deepseek-v4-pro") == {"input": 0.27, "output": 1.10}
