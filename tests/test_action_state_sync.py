"""action_state と execution log の同期テスト。"""
import json
from pathlib import Path

import action_state_tracker as ast


def _write_state(path: Path, actions: dict) -> None:
    path.write_text(json.dumps({"actions": actions, "last_updated": ""}, ensure_ascii=False), encoding="utf-8")


def _read_state(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_executed_sell_marks_matching_pending_action_filled(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "txn1": {
            "id": "txn1",
            "ticker": "TXN",
            "action_type": "take_profit",
            "action_detail": "TXN 1株を成行で全数利確",
            "recommended_at": "2026-05-21T00:35:10",
            "status": "pending",
            "placed_at": None,
            "filled_at": None,
        }
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    action_id = ast.sync_execution_status(
        ticker="TXN",
        direction="sell",
        execution_status="executed",
        note="execution:TXN_sell_test status=executed",
    )

    entry = _read_state(state_path)["actions"]["txn1"]
    assert action_id == "txn1"
    assert entry["status"] == "filled"
    assert entry["placed_at"]
    assert entry["filled_at"]
    assert "TXN_sell_test" in entry["note"]


def test_ordered_buy_marks_pending_action_placed(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "buy1": {
            "id": "buy1",
            "ticker": "MSFT",
            "action_type": "add",
            "action_detail": "MSFT 1株を追加購入",
            "recommended_at": "2026-05-21T00:35:10",
            "status": "pending",
            "placed_at": None,
            "filled_at": None,
        }
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    action_id = ast.sync_execution_status(
        ticker="MSFT",
        direction="buy",
        execution_status="ordered",
    )

    entry = _read_state(state_path)["actions"]["buy1"]
    assert action_id == "buy1"
    assert entry["status"] == "placed"
    assert entry["placed_at"]
    assert entry["filled_at"] is None


def test_record_recommendations_persists_execution_plan_metadata(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {})
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    added = ast.record_recommendations([{
        "ticker": "META",
        "type": "buy",
        "action": "META 1株を購入",
        "reason": "plan test",
        "plan_item_id": "2026-07-w28-usd-001",
        "monthly_objective_id": "2026-07:normal:add-currency-usd",
        "execution_plan_decision": "plan_new_order",
        "plan_remaining_before_jpy": 250_000,
        "plan_remaining_after_jpy": 50_000,
    }])

    actions = _read_state(state_path)["actions"]
    entry = next(iter(actions.values()))
    assert added == 1
    assert entry["plan_item_id"] == "2026-07-w28-usd-001"
    assert entry["monthly_objective_id"] == "2026-07:normal:add-currency-usd"
    assert entry["execution_plan_decision"] == "plan_new_order"
    assert entry["plan_remaining_before_jpy"] == 250_000
    assert entry["plan_remaining_after_jpy"] == 50_000


def test_direction_mismatch_does_not_update_action_state(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "sell1": {
            "id": "sell1",
            "ticker": "QCOM",
            "action_type": "trim",
            "action_detail": "QCOM 1株を利確",
            "recommended_at": "2026-05-21T00:35:10",
            "status": "pending",
        }
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    action_id = ast.sync_execution_status(
        ticker="QCOM",
        direction="buy",
        execution_status="executed",
    )

    entry = _read_state(state_path)["actions"]["sell1"]
    assert action_id is None
    assert entry["status"] == "pending"


def test_sync_execution_status_prefers_explicit_action_state_id(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "older": {
            "id": "older",
            "ticker": "META",
            "action_type": "buy",
            "action_detail": "META 1株を買付",
            "recommended_at": "2026-05-20T00:00:00",
            "status": "placed",
        },
        "newer": {
            "id": "newer",
            "ticker": "META",
            "action_type": "buy",
            "action_detail": "META 1株を追加買付",
            "recommended_at": "2026-05-21T00:00:00",
            "status": "pending",
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    action_id = ast.sync_execution_status(
        ticker="META",
        direction="buy",
        execution_status="executed",
        action_state_id="older",
        note="execution:META_buy_test status=executed",
    )

    actions = _read_state(state_path)["actions"]
    assert action_id == "older"
    assert actions["older"]["status"] == "filled"
    assert actions["newer"]["status"] == "pending"


def test_short_and_cover_sync_use_normalized_action_type_before_text(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "short1": {
            "id": "short1",
            "ticker": "7203.T",
            "action_type": "short",
            "action_detail": "7203.T を空売りで新規建て",
            "recommended_at": "2026-07-10T00:00:00",
            "status": "pending",
        },
        "cover1": {
            "id": "cover1",
            "ticker": "7203.T",
            "action_type": "cover",
            "action_detail": "7203.T を返済買い",
            "recommended_at": "2026-07-10T00:00:00",
            "status": "placed",
        },
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    assert ast.sync_execution_status(
        ticker="7203.T", direction="short", execution_status="executed", action_state_id="short1"
    ) == "short1"
    assert ast.sync_execution_status(
        ticker="7203.T", direction="cover", execution_status="partial", action_state_id="cover1"
    ) == "cover1"

    actions = _read_state(state_path)["actions"]
    assert actions["short1"]["status"] == "filled"
    # API ledger applies the quantity recorded as partial, so it is terminal
    # for action_state as well.
    assert actions["cover1"]["status"] == "filled"


def test_expire_stale_ordered_executions_never_mutates_broker_order_state(tmp_path, monkeypatch):
    monkeypatch.setattr(ast, "BASE_DIR", tmp_path)
    exec_path = tmp_path / "action_executions.json"
    exec_path.write_text(json.dumps({
        "executions": [
            {
                "id": "ordered",
                "saved_at": "2026-07-08T08:34:05",
                "ticker": "1306.T",
                "direction": "buy",
                "status": "ordered",
                "action_state_id": "state-1306",
            },
            {
                "id": "executed",
                "saved_at": "2026-07-08T22:51:13",
                "ticker": "1306.T",
                "direction": "buy",
                "status": "executed",
                "action_state_id": "state-1306",
            },
        ]
    }), encoding="utf-8")

    expired = ast.expire_stale_ordered_executions(max_days=999)

    records = json.loads(exec_path.read_text(encoding="utf-8"))["executions"]
    assert expired == 0
    assert records[0]["status"] == "ordered"
    assert records[1]["status"] == "executed"


def test_explicit_fill_updates_expired_recommendation(tmp_path, monkeypatch):
    state_path = tmp_path / "action_state.json"
    _write_state(state_path, {
        "lly": {
            "id": "lly",
            "ticker": "LLY",
            "action_type": "trim",
            "recommended_at": "2026-07-07T20:14:31",
            "status": "expired",
            "expired_at": "2026-07-10T07:23:26",
        }
    })
    monkeypatch.setattr(ast, "STATE_FILE", state_path)

    action_id = ast.sync_execution_status(
        ticker="LLY",
        direction="sell",
        execution_status="executed",
        action_state_id="lly",
    )

    entry = _read_state(state_path)["actions"]["lly"]
    assert action_id == "lly"
    assert entry["status"] == "filled"
    assert entry["status_before_execution_sync"] == "expired"
    assert entry["filled_at"]
