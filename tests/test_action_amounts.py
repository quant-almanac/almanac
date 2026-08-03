from action_amounts import (
    canonicalize_action_amount_hint,
    render_action_amount,
    synchronize_persisted_resized_action_prose,
)


def test_quantity_hint_discards_stale_embedded_money():
    action = {
        "amount_hint": "22口（約¥104,600）",
        "estimated_notional_jpy": 76_164,
    }
    normalized = canonicalize_action_amount_hint(action)
    assert normalized["amount_hint"] == "22口"
    assert normalized["amount_hint_original"] == "22口（約¥104,600）"
    assert normalized["estimated_notional_jpy"] == 76_164


def test_response_amount_is_rendered_from_structured_notional():
    rendered = render_action_amount({
        "amount_hint": "22口",
        "estimated_notional_jpy": 76_164,
    })
    assert rendered == {
        "quantity": "22口",
        "notional_jpy": 76_164,
        "display": "22口 / 約¥76,164",
    }


def test_amount_only_hint_is_not_reinterpreted_as_quantity():
    action = {"amount_hint": "毎月¥80,000", "estimated_notional_jpy": 80_000}
    assert canonicalize_action_amount_hint(action) == action


def test_persisted_policy_resize_prose_is_repaired_without_mutating_source():
    action = {
        "ticker": "GOOGL",
        "amount_hint": "3株",
        "action": "GOOGLを3株新規購入（約¥28万相当）",
        "reason": "最低ロット規則に沿い5株。",
        "estimated_notional_jpy": 166_339,
        "policy_size_applied": {
            "amount_hint": {"from": "5株", "to": "3株"},
            "estimated_notional_jpy": {"from": 277_232, "to": 166_339},
        },
    }

    repaired = synchronize_persisted_resized_action_prose(action)

    assert action["action"].endswith("約¥28万相当）")
    assert action["reason"] == "最低ロット規則に沿い5株。"
    assert repaired["action"] == "GOOGLを3株新規購入（約¥166,339）"
    assert "5株" not in repaired["reason"]
    assert repaired["display_prose_repaired_from_policy_audit"] is True
