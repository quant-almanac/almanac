from execution_explanation import normalize_execution_explanation


def test_formula_is_replaced_by_persisted_order_contract():
    result = normalize_execution_explanation({
        "ticker": "1489.T",
        "limit_price": 3382,
        "decision_price": 3405,
        "execution_reason": (
            "limit=min(3405,3256.7)−46.37×0.5=3233→sup3244上を尊重し3382に設定。"
            "RSI69過熱のため戻り待ちで指値。"
        ),
    })

    assert result["execution_reason"].startswith("実注文は指値¥3,382（判断値¥3,405）。")
    assert "limit=min" not in result["execution_reason"]
    assert result["execution_reason_normalized"] is True


def test_mislabeled_bid_is_changed_to_decision_value():
    result = normalize_execution_explanation({
        "ticker": "AVGO",
        "limit_price": 401,
        "decision_price": 392.47,
        "quote_bid": 390.61,
        "execution_reason": "bid392.47近辺の戻りで指値。",
    })

    assert result["execution_reason"] == "判断値392.47近辺の戻りで指値。"


def test_formula_sentence_is_replaced_even_after_a_spread_warning():
    result = normalize_execution_explanation({
        "ticker": "RTX",
        "limit_price": 211,
        "decision_price": 209.16,
        "execution_reason": (
            "spread103bps広く成行不可。"
            "limit=max(209.16,191.98)+4.52×0.8=212.8を過熱考慮し211に。"
            "少額整理。"
        ),
    })

    assert result["execution_reason"] == (
        "spread103bps広く成行不可。"
        "実注文は指値$211.00（判断値$209.16）。"
        "少額整理。"
    )


def test_real_bid_label_is_preserved():
    action = {
        "ticker": "AVGO",
        "quote_bid": 390.61,
        "execution_reason": "bid390.61近辺を確認。",
    }

    assert normalize_execution_explanation(action) is action
