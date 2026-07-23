import json
from datetime import datetime

from order_intent_resolver import resolve_recent_order_intents


def test_terminal_recommendation_allows_reproposal_but_keeps_execution_conflict(tmp_path):
    state_path = tmp_path / "action_state.json"
    state_path.write_text(json.dumps({"actions": {
        "lly": {
            "id": "lly",
            "ticker": "LLY",
            "action_type": "trim",
            "status": "expired",
            "expire_reason": "manual_reset_backlog",
        }
    }}), encoding="utf-8")
    rows = [{
        "id": "lly-order",
        "saved_at": "2026-07-07T23:08:42",
        "ticker": "LLY",
        "direction": "sell",
        "status": "ordered",
        "quantity": 2,
        "action_state_id": "lly",
    }]

    effective, conflicts = resolve_recent_order_intents(
        rows,
        action_state_path=state_path,
        days=7,
        now=datetime(2026, 7, 13, 6, 8),
    )

    assert effective == []
    assert len(conflicts) == 1
    assert conflicts[0]["recommendation_status"] == "expired"
    assert conflicts[0]["resolution_required"] == "confirm_broker_order_status"


def test_later_fill_supersedes_ordered_row(tmp_path):
    state_path = tmp_path / "action_state.json"
    state_path.write_text(json.dumps({"actions": {}}), encoding="utf-8")
    rows = [
        {
            "id": "order",
            "saved_at": "2026-07-08T08:00:00",
            "ticker": "1306.T",
            "direction": "buy",
            "status": "ordered",
            "action_state_id": "state-1",
        },
        {
            "id": "fill",
            "saved_at": "2026-07-08T09:00:00",
            "ticker": "1306.T",
            "direction": "buy",
            "status": "executed",
            "action_state_id": "state-1",
        },
    ]

    effective, conflicts = resolve_recent_order_intents(
        rows,
        action_state_path=state_path,
        days=7,
        now=datetime(2026, 7, 9, 6, 0),
    )

    assert [row["id"] for row in effective] == ["fill"]
    assert conflicts == []
