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
