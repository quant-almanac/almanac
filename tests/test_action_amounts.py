from action_amounts import (
    canonicalize_action_amount_hint,
    render_action_amount,
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
