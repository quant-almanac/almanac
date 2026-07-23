"""actions.py ledger safety regression tests."""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import action_stage_log
import action_state_tracker
import event_ledger
import margin_manager
from api.routes import actions


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def isolated_actions(tmp_path, monkeypatch):
    holdings = tmp_path / "holdings.json"
    account = tmp_path / "account.json"
    executions = tmp_path / "action_executions.json"
    history = tmp_path / "trade_history.csv"
    ledger_db = tmp_path / "ledger.db"
    margin_positions = tmp_path / "margin_positions.json"
    stage_log = tmp_path / "action_stage_log.jsonl"
    action_state = tmp_path / "action_state.json"
    analysis = tmp_path / "ai_portfolio_analysis.json"

    _write_json(holdings, {})
    _write_json(account, {
        "balance": 1000.0,
        "usd_balance": 0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 1000,
    })
    _write_json(executions, {"executions": []})
    _write_json(margin_positions, {
        "cash_collateral": 0,
        "securities_collateral": 0,
        "sec_haircut": 0.80,
        "positions": [],
        "updated": "",
    })
    history.write_text("", encoding="utf-8")
    _write_json(action_state, {"actions": {}})
    _write_json(analysis, {})

    monkeypatch.setattr(actions, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(actions, "ACCOUNT_FILE", account)
    monkeypatch.setattr(actions, "EXEC_FILE", executions)
    monkeypatch.setattr(actions, "HISTORY_FILE", history)
    monkeypatch.setattr(actions, "BASE_DIR", tmp_path)
    monkeypatch.setattr(actions, "ANALYSIS_FILE", analysis)
    monkeypatch.setattr(event_ledger, "DB_PATH", ledger_db)
    monkeypatch.setattr(margin_manager, "MARGIN_POS_FILE", margin_positions)
    monkeypatch.setattr(action_stage_log, "LOG_PATH", stage_log)
    # save_execution()/patch_execution() call _sync_action_state_for_execution(),
    # which reads/writes action_state_tracker's real STATE_FILE if not patched —
    # a real leak discovered 2026-07-13 (see feedback_financial_ledger_confirmation
    # memory): tests using ticker="7203.T" silently marked unrelated production
    # action_state.json entries as "filled".
    monkeypatch.setattr(action_state_tracker, "STATE_FILE", action_state)

    return {
        "holdings": holdings,
        "account": account,
        "executions": executions,
        "history": history,
        "ledger_db": ledger_db,
        "margin_positions": margin_positions,
        "stage_log": stage_log,
        "action_state": action_state,
        "analysis": analysis,
    }


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_insufficient_cash_buy_leaves_all_files_unchanged(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {})
    _write_json(files["account"], {
        "balance": 50.0,
        "usd_balance": 0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 50,
    })

    before_h = _read(files["holdings"])
    before_a = _read(files["account"])

    with pytest.raises(actions.PortfolioApplicationPending) as exc:
        actions._apply_event_to_ledger(
            event_id="exec_cash_guard",
            ticker="7203.T",
            direction="buy",
            quantity=1,
            price=100,
            currency="JPY",
            account="特定",
            investment_type="medium",
            status="executed",
            sell_all=False,
            name="Toyota",
            execution_owner="husband",
            execution_broker="rakuten",
        )

    assert exc.value.code == "cash_balance_insufficient"
    assert _read(files["holdings"]) == before_h
    assert _read(files["account"]) == before_a
    assert files["history"].read_text(encoding="utf-8") == ""
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []


def test_isolated_actions_routes_stage_log_to_tmp_path(isolated_actions):
    assert action_stage_log.LOG_PATH == isolated_actions["stage_log"]


def test_executed_sell_without_price_leaves_all_files_unchanged(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {
        "GLD": {
            "ticker": "GLD",
            "entry_price": 300.0,
            "shares": 26.0,
            "currency": "USD",
            "account": "特定",
        },
        "CASH_USD": {"ticker": "CASH_USD", "shares": 1000.0, "currency": "USD"},
    })
    _write_json(files["account"], {
        "balance": 1000.0,
        "usd_balance": 1000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 151000,
    })

    before_h = _read(files["holdings"])
    before_a = _read(files["account"])

    with pytest.raises(HTTPException) as exc:
        actions._apply_event_to_ledger(
            event_id="exec_gld_missing_price",
            ticker="GLD",
            direction="sell",
            quantity=2,
            price=None,
            currency="USD",
            account="特定",
            investment_type="medium",
            status="executed",
            sell_all=False,
            name=None,
        )

    assert exc.value.status_code == 422
    assert "price" in exc.value.detail
    assert _read(files["holdings"]) == before_h
    assert _read(files["account"]) == before_a
    assert files["history"].read_text(encoding="utf-8") == ""
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []


def test_patch_existing_executed_legacy_record_does_not_reapply(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {
        "7203.T": {
            "ticker": "7203.T",
            "entry_price": 100.0,
            "shares": 1.0,
            "currency": "JPY",
            "account": "特定",
        }
    })
    _write_json(files["executions"], {
        "executions": [{
            "id": "legacy_executed",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "executed",
            "price": 100.0,
            "quantity": 1.0,
            "currency": "JPY",
            "portfolio_updated": True,
        }]
    })

    result = asyncio.run(actions.patch_execution(
        "legacy_executed",
        actions.ExecutionPatchRequest(note="memo only"),
    ))

    assert result["portfolio"] is None
    assert _read(files["holdings"])["7203.T"]["shares"] == 1.0
    assert _read(files["account"])["balance"] == 1000.0
    record = _read(files["executions"])["executions"][0]
    assert record["portfolio_applied"] is True
    assert record["note"] == "memo only"
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []


def test_ordered_to_executed_applies_once_with_stable_event_id(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {
        "7203.T": {
            "ticker": "7203.T",
            "entry_price": 100.0,
            "shares": 1.0,
            "currency": "JPY",
            "account": "特定",
        }
    })
    _write_json(files["executions"], {
        "executions": [{
            "id": "order_1",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "ordered",
            "price": 100.0,
            "quantity": 1.0,
            "currency": "JPY",
            "investment_type": "medium",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_position_keys": ["7203.T"],
        }]
    })

    asyncio.run(actions.patch_execution(
        "order_1",
        actions.ExecutionPatchRequest(status=actions.Status.executed),
    ))
    asyncio.run(actions.patch_execution(
        "order_1",
        actions.ExecutionPatchRequest(status=actions.Status.executed, note="retry"),
    ))

    assert _read(files["holdings"])["7203.T"]["shares"] == 2.0
    assert _read(files["account"])["balance"] == 900.0
    events = event_ledger.query_events(db_path=files["ledger_db"])
    assert len(events) == 1
    assert events[0]["event_id"] == "exec_order_1"
    record = _read(files["executions"])["executions"][0]
    assert record["portfolio_applied"] is True
    assert record["event_id"] == "exec_order_1"


def test_ordered_to_executed_preserves_analysis_id_in_stage_log(isolated_actions, monkeypatch):
    files = isolated_actions
    logged = []
    monkeypatch.setattr(action_stage_log, "append_entries", lambda entries, path=None: logged.extend(entries))
    _write_json(files["holdings"], {
        "7203.T": {
            "ticker": "7203.T",
            "entry_price": 100.0,
            "shares": 1.0,
            "currency": "JPY",
            "account": "特定",
        }
    })
    _write_json(files["executions"], {
        "executions": [{
            "id": "order_with_analysis",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "ordered",
            "price": 100.0,
            "quantity": 1.0,
            "currency": "JPY",
            "investment_type": "medium",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_position_keys": ["7203.T"],
            "analysis_id": "analysis-run-456",
        }]
    })

    asyncio.run(actions.patch_execution(
        "order_with_analysis",
        actions.ExecutionPatchRequest(status=actions.Status.executed),
    ))

    assert len(logged) == 1
    assert logged[0]["analysis_id"] == "analysis-run-456"
    record = _read(files["executions"])["executions"][0]
    assert record["analysis_id"] == "analysis-run-456"


def test_ordered_to_executed_accepts_fill_and_audits_latest_blocked_readiness(isolated_actions):
    files = isolated_actions
    action_id = "linked-review"
    _write_json(files["holdings"], {})
    _write_json(files["account"], {
        "balance": 10_000.0, "usd_balance": 0.0,
        "fx_rate_usdjpy": 150.0, "total_cash": 10_000.0,
    })
    _write_json(files["action_state"], {"actions": {action_id: {
        "id": action_id, "ticker": "7203.T", "action_type": "buy", "status": "placed",
        "execution_readiness": "blocked",
        "execution_block_reasons": [{"code": "macro_event_blackout", "message": "指標発表直前"}],
    }}})
    _write_json(files["executions"], {"executions": [{
        "id": "legacy_blocked_order", "ticker": "7203.T", "direction": "buy",
        "status": "ordered", "price": 100.0, "quantity": 1.0, "currency": "JPY",
        "investment_type": "medium", "analysis_id": "analysis-old",
        "action_state_id": action_id,
    }]})

    asyncio.run(actions.patch_execution(
        "legacy_blocked_order",
        actions.ExecutionPatchRequest(status=actions.Status.executed),
    ))

    record = _read(files["executions"])["executions"][0]
    assert record["status"] == "executed"
    assert record["executed_despite_readiness"] is True
    assert record["readiness_at_execution"] == "blocked"
    assert record["execution_block_reasons_at_execution"][0]["code"] == "macro_event_blackout"


def test_ordered_to_executed_recomputes_shortfall_bps_from_fill_price(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {
        "7203.T": {
            "ticker": "7203.T",
            "entry_price": 100.0,
            "shares": 1.0,
            "currency": "JPY",
            "account": "特定",
        }
    })
    _write_json(files["executions"], {
        "executions": [{
            "id": "order_shortfall",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "ordered",
            "price": None,
            "quantity": 1.0,
            "currency": "JPY",
            "investment_type": "medium",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_position_keys": ["7203.T"],
            "decision_price": 100.0,
            "shortfall_bps": None,
        }]
    })

    asyncio.run(actions.patch_execution(
        "order_shortfall",
        actions.ExecutionPatchRequest(
            status=actions.Status.executed,
            price=110.0,
        ),
    ))

    record = _read(files["executions"])["executions"][0]
    assert record["shortfall_bps"] == pytest.approx(1000.0)


def test_ordered_to_executed_without_price_is_rejected_atomically(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {
        "GLD": {
            "ticker": "GLD",
            "entry_price": 300.0,
            "shares": 26.0,
            "currency": "USD",
            "account": "特定",
        },
        "CASH_USD": {"ticker": "CASH_USD", "shares": 1000.0, "currency": "USD"},
    })
    _write_json(files["account"], {
        "balance": 1000.0,
        "usd_balance": 1000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 151000,
    })
    _write_json(files["executions"], {
        "executions": [{
            "id": "gld_order",
            "ticker": "GLD",
            "direction": "sell",
            "status": "ordered",
            "price": None,
            "quantity": 2.0,
            "currency": "USD",
            "investment_type": "medium",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_position_keys": ["7203.T"],
        }]
    })

    before_h = _read(files["holdings"])
    before_a = _read(files["account"])
    before_e = _read(files["executions"])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.patch_execution(
            "gld_order",
            actions.ExecutionPatchRequest(status=actions.Status.executed),
        ))

    assert exc.value.status_code == 422
    assert "price" in exc.value.detail
    assert _read(files["holdings"]) == before_h
    assert _read(files["account"]) == before_a
    assert _read(files["executions"]) == before_e
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []


def test_ordered_short_to_executed_creates_margin_position_once(isolated_actions):
    files = isolated_actions
    _write_json(files["executions"], {
        "executions": [{
            "id": "short_order",
            "ticker": "7203.T",
            "direction": "short",
            "status": "ordered",
            "price": 1000.0,
            "quantity": 3.0,
            "currency": "JPY",
            "account": "信用",
            "investment_type": "swing",
        }]
    })

    asyncio.run(actions.patch_execution(
        "short_order",
        actions.ExecutionPatchRequest(status=actions.Status.executed),
    ))
    asyncio.run(actions.patch_execution(
        "short_order",
        actions.ExecutionPatchRequest(status=actions.Status.executed, note="retry"),
    ))

    margin = _read(files["margin_positions"])
    assert len(margin["positions"]) == 1
    assert margin["positions"][0]["side"] == "short"
    assert margin["positions"][0]["shares"] == 3.0
    events = event_ledger.query_events(db_path=files["ledger_db"])
    assert len(events) == 1
    assert events[0]["event_id"] == "exec_short_order"
    record = _read(files["executions"])["executions"][0]
    assert record["portfolio_applied"] is True
    assert record["margin_side"] == "short"


def test_ordered_short_without_price_is_rejected_atomically(isolated_actions):
    files = isolated_actions
    _write_json(files["executions"], {
        "executions": [{
            "id": "short_missing_price",
            "ticker": "7203.T",
            "direction": "short",
            "status": "ordered",
            "price": None,
            "quantity": 3.0,
            "currency": "JPY",
            "account": "信用",
            "investment_type": "swing",
        }]
    })
    before_m = _read(files["margin_positions"])
    before_e = _read(files["executions"])

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.patch_execution(
            "short_missing_price",
            actions.ExecutionPatchRequest(status=actions.Status.executed),
        ))

    assert exc.value.status_code == 422
    assert "price" in exc.value.detail
    assert _read(files["margin_positions"]) == before_m
    assert _read(files["executions"]) == before_e
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []


def test_ordered_to_partial_applies_portfolio_once(isolated_actions):
    files = isolated_actions
    _write_json(files["holdings"], {
        "7203.T": {
            "ticker": "7203.T",
            "entry_price": 100.0,
            "shares": 1.0,
            "currency": "JPY",
            "account": "特定",
        }
    })
    _write_json(files["executions"], {
        "executions": [{
            "id": "partial_1",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "ordered",
            "price": 100.0,
            "quantity": 1.0,
            "currency": "JPY",
            "investment_type": "medium",
            "execution_owner": "husband",
            "execution_broker": "rakuten",
            "execution_position_keys": ["7203.T"],
        }]
    })

    asyncio.run(actions.patch_execution(
        "partial_1",
        actions.ExecutionPatchRequest(status=actions.Status.partial),
    ))
    asyncio.run(actions.patch_execution(
        "partial_1",
        actions.ExecutionPatchRequest(status=actions.Status.executed, note="final marker"),
    ))

    assert _read(files["holdings"])["7203.T"]["shares"] == 2.0
    assert _read(files["account"])["balance"] == 900.0
    assert len(event_ledger.query_events(db_path=files["ledger_db"])) == 1
    record = _read(files["executions"])["executions"][0]
    assert record["status"] == "executed"
    assert record["portfolio_applied"] is True


def test_delete_applied_execution_is_forbidden(isolated_actions):
    files = isolated_actions
    _write_json(files["executions"], {
        "executions": [{
            "id": "applied",
            "ticker": "7203.T",
            "direction": "buy",
            "status": "executed",
            "portfolio_updated": True,
        }]
    })

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.delete_execution("applied"))

    assert exc.value.status_code == 409
    assert len(_read(files["executions"])["executions"]) == 1
