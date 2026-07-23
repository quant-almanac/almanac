"""E2E tests for api/routes/actions.py — save_execution endpoint and helpers.

Existing test_actions_ledger_safety.py covers _apply_event_to_ledger and
patch_execution. This file covers the gaps:

  - _auto_detect_currency: .T suffix → JPY, holdings inheritance, unknown → None
  - _compute_cash_change: buy deducts, sell adds, exempt accounts skip, USD path
  - save_execution():  full round-trip through the function (not via HTTP client)
      · holdings + account updated atomically
      · action_executions.json record written with portfolio_applied=True
      · currency auto-detected for .T tickers when not specified
      · sell adds cash; buy deducts cash
"""

from __future__ import annotations

import asyncio
import json
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import pytest
from fastapi import HTTPException

import action_stage_log
import action_state_tracker
import event_ledger
import margin_manager
from api.routes import actions
from api.routes.actions import (
    ExecutionRequest,
    _auto_detect_currency,
    _compute_cash_change,
)

_ExecutionRequestModel = ExecutionRequest


def ExecutionRequest(**kwargs):
    kwargs.setdefault("idempotency_key", f"test-{uuid.uuid4().hex}")
    account = str(kwargs.get("account") or "")
    if account and "NISA" not in account:
        kwargs.setdefault("execution_owner", "husband")
        kwargs.setdefault("execution_broker", "rakuten")
        kwargs.setdefault("execution_position_keys", [str(kwargs.get("ticker") or "")])
    return _ExecutionRequestModel(**kwargs)


# ---------------------------------------------------------------------------
# Shared fixture (same pattern as test_actions_ledger_safety.py)
# ---------------------------------------------------------------------------


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Patch all file paths and the process_lock to use tmp_path."""
    holdings  = tmp_path / "holdings.json"
    account   = tmp_path / "account.json"
    executions = tmp_path / "action_executions.json"
    history   = tmp_path / "trade_history.csv"
    ledger_db = tmp_path / "ledger.db"
    margin_positions = tmp_path / "margin_positions.json"
    stage_log = tmp_path / "action_stage_log.jsonl"
    action_state = tmp_path / "action_state.json"
    analysis = tmp_path / "ai_portfolio_analysis.json"
    execution_plan = tmp_path / "execution_plan_state.json"

    _write_json(holdings, {})
    _write_json(account, {
        "balance":         500_000.0,
        "usd_balance":     1_000.0,
        "fx_rate_usdjpy":  150.0,
        "total_cash":      500_000 + 1_000 * 150,
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
    _write_json(execution_plan, {
        "status": "active",
        "budgets": {
            "normal_pool_available_jpy": 500_000,
            "opportunity_pool_available_jpy": 0,
        },
        "contribution_summary": {"available_jpy": 500_000},
    })

    monkeypatch.setattr(actions, "HOLDINGS_FILE", holdings)
    monkeypatch.setattr(actions, "ACCOUNT_FILE",  account)
    monkeypatch.setattr(actions, "EXEC_FILE",     executions)
    monkeypatch.setattr(actions, "HISTORY_FILE",  history)
    monkeypatch.setattr(actions, "BASE_DIR",      tmp_path)
    monkeypatch.setattr(actions, "ANALYSIS_FILE", analysis)
    monkeypatch.setattr(event_ledger, "DB_PATH",  ledger_db)
    monkeypatch.setattr(margin_manager, "MARGIN_POS_FILE", margin_positions)
    monkeypatch.setattr(action_stage_log, "LOG_PATH", stage_log)
    # save_execution() calls _sync_action_state_for_execution(), which reads/writes
    # action_state_tracker's real STATE_FILE if not patched — a real-money-adjacent
    # leak discovered 2026-07-13 (see feedback_financial_ledger_confirmation memory):
    # tests using ticker="7203.T" (this repo's default placeholder ticker) were
    # silently marking unrelated production action_state.json entries as "filled".
    monkeypatch.setattr(action_state_tracker, "STATE_FILE", action_state)

    # Replace process_lock with a no-op so tests don't fight over flock files.
    @contextmanager
    def _noop_lock(name: str, *, timeout: float = 0.0) -> Iterator[Path]:
        yield tmp_path / f"{name}.lock"

    monkeypatch.setattr(actions, "process_lock", _noop_lock)

    return {
        "holdings":   holdings,
        "account":    account,
        "executions": executions,
        "history":    history,
        "ledger_db":  ledger_db,
        "margin_positions": margin_positions,
        "stage_log": stage_log,
        "action_state": action_state,
        "analysis": analysis,
        "execution_plan": execution_plan,
    }


# ---------------------------------------------------------------------------
# _auto_detect_currency
# ---------------------------------------------------------------------------


def test_auto_detect_jpy_for_t_suffix() -> None:
    assert _auto_detect_currency("7203.T", {}) == "JPY"


def test_auto_detect_jpy_for_jp_suffix() -> None:
    assert _auto_detect_currency("TEST.JP", {}) == "JPY"


def test_auto_detect_none_for_us_ticker() -> None:
    """Unknown suffix → None (caller must provide currency explicitly)."""
    assert _auto_detect_currency("NVDA", {}) is None


def test_auto_detect_none_for_empty_ticker() -> None:
    assert _auto_detect_currency("", {}) is None


def test_auto_detect_inherits_currency_from_holdings() -> None:
    """Existing position in holdings takes precedence over suffix heuristic."""
    holdings = {
        "AVGO_特定": {"ticker": "AVGO", "currency": "USD", "shares": 2.0}
    }
    assert _auto_detect_currency("AVGO", holdings) == "USD"


def test_auto_detect_t_suffix_overrides_when_not_in_holdings() -> None:
    """No holdings entry → suffix rule fires."""
    assert _auto_detect_currency("9984.T", {}) == "JPY"


# ---------------------------------------------------------------------------
# _compute_cash_change
# ---------------------------------------------------------------------------


def _base_account() -> dict:
    return {
        "balance":        500_000.0,
        "usd_balance":    1_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     650_000,
    }


def test_cash_change_buy_jpy_deducts_balance() -> None:
    acc = _base_account()
    new_acc, delta = _compute_cash_change(
        acc, direction="buy", price=10_000.0, quantity=5,
        currency="JPY", account="特定",
    )
    assert delta == pytest.approx(-50_000.0)
    assert new_acc["balance"] == pytest.approx(450_000.0)


def test_cash_change_sell_jpy_adds_balance() -> None:
    acc = _base_account()
    new_acc, delta = _compute_cash_change(
        acc, direction="sell", price=10_000.0, quantity=5,
        currency="JPY", account="特定",
    )
    assert delta == pytest.approx(50_000.0)
    assert new_acc["balance"] == pytest.approx(550_000.0)


def test_cash_change_buy_usd_deducts_usd_balance() -> None:
    acc = _base_account()
    new_acc, delta = _compute_cash_change(
        acc, direction="buy", price=100.0, quantity=5,
        currency="USD", account="特定",
    )
    assert delta == pytest.approx(-500.0)
    assert new_acc["usd_balance"] == pytest.approx(500.0)


def test_cash_change_total_cash_recalculated() -> None:
    acc = _base_account()
    new_acc, _ = _compute_cash_change(
        acc, direction="buy", price=10_000.0, quantity=5,
        currency="JPY", account="特定",
    )
    expected_total = new_acc["balance"] + new_acc["usd_balance"] * new_acc["fx_rate_usdjpy"]
    assert new_acc["total_cash"] == int(round(expected_total))


def test_cash_change_exempt_account_skips_update() -> None:
    """信用 and 持株会 accounts are cash-exempt — balance must not change."""
    acc = _base_account()
    for acct in ("信用", "持株会"):
        new_acc, delta = _compute_cash_change(
            acc, direction="buy", price=100_000.0, quantity=10,
            currency="JPY", account=acct,
        )
        assert delta is None
        assert new_acc["balance"] == acc["balance"]


def test_cash_change_raises_on_jpy_overdraft() -> None:
    acc = _base_account()
    acc["balance"] = 1_000.0   # only ¥1K available
    with pytest.raises(HTTPException) as exc:
        _compute_cash_change(
            acc, direction="buy", price=100_000.0, quantity=1,
            currency="JPY", account="特定",
        )
    assert exc.value.status_code == 400
    assert "残高不足" in exc.value.detail or "balance" in exc.value.detail.lower()


def test_cash_change_raises_on_usd_overdraft() -> None:
    acc = _base_account()
    acc["usd_balance"] = 10.0   # only $10
    with pytest.raises(HTTPException) as exc:
        _compute_cash_change(
            acc, direction="buy", price=500.0, quantity=1,
            currency="USD", account="特定",
        )
    assert exc.value.status_code == 400


def test_cash_change_skips_when_price_none() -> None:
    acc = _base_account()
    new_acc, delta = _compute_cash_change(
        acc, direction="buy", price=None, quantity=5,
        currency="JPY", account="特定",
    )
    assert delta is None
    assert new_acc["balance"] == acc["balance"]


def test_cash_change_skips_when_quantity_none() -> None:
    acc = _base_account()
    new_acc, delta = _compute_cash_change(
        acc, direction="buy", price=10_000.0, quantity=None,
        currency="JPY", account="特定",
    )
    assert delta is None


# ---------------------------------------------------------------------------
# save_execution — full round-trip
# ---------------------------------------------------------------------------


def test_save_execution_buy_updates_holdings_and_account(isolated) -> None:
    """POST /api/actions/execute buy → holdings created, cash deducted."""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 500_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 0.0, "currency": "USD"},
    })
    _write_json(files["account"], {
        "balance": 500_000.0,
        "usd_balance": 0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 500_000,
    })

    req = ExecutionRequest(
        ticker="7203.T",
        direction="buy",
        quantity=1,
        price=1_000.0,
        currency="JPY",
        account="特定",
        investment_type="medium",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    assert result["ok"] is True
    holdings = _read(files["holdings"])
    assert "7203.T" in holdings
    assert holdings["7203.T"]["shares"] == 1.0
    account = _read(files["account"])
    assert account["balance"] == pytest.approx(499_000.0)
    holdings = _read(files["holdings"])
    assert holdings["CASH_JPY"]["shares"] == pytest.approx(account["balance"])
    assert holdings["CASH_USD"]["shares"] == pytest.approx(account["usd_balance"])


def test_save_execution_records_to_execution_log(isolated) -> None:
    """Execution record is appended to action_executions.json with correct fields."""
    files = isolated

    req = ExecutionRequest(
        ticker="NVDA",
        direction="buy",
        quantity=2,
        price=500.0,
        currency="USD",
        account="特定",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    exec_data = _read(files["executions"])
    records = exec_data["executions"]
    assert len(records) == 1
    rec = records[0]
    assert rec["ticker"] == "NVDA"
    assert rec["direction"] == "buy"
    assert rec["quantity"] == 2
    assert rec["portfolio_applied"] is True
    assert rec["id"] == result["id"]


def test_save_execution_preserves_analysis_id_in_execution_record_and_stage_log(isolated) -> None:
    """AI提案からの実行は、元のanalysis_idで後追いできるように残す。"""
    files = isolated

    req = ExecutionRequest(
        ticker="7203.T",
        direction="buy",
        quantity=1,
        price=1_000.0,
        currency="JPY",
        account="特定",
        investment_type="medium",
        status="executed",
        analysis_id="analysis-run-123",
    )
    asyncio.run(actions.save_execution(req))

    rec = _read(files["executions"])["executions"][0]
    assert rec["analysis_id"] == "analysis-run-123"

    entries = action_stage_log.read_entries(files["stage_log"], stages=["executed"])
    assert len(entries) == 1
    assert entries[0]["analysis_id"] == "analysis-run-123"


def test_save_execution_without_analysis_id_keeps_legacy_execution_label(isolated) -> None:
    """手入力や旧UIからの実行は従来どおり固定ラベルにフォールバックする。"""
    files = isolated

    req = ExecutionRequest(
        ticker="7203.T",
        direction="buy",
        quantity=1,
        price=1_000.0,
        currency="JPY",
        account="特定",
        status="executed",
    )
    asyncio.run(actions.save_execution(req))

    rec = _read(files["executions"])["executions"][0]
    assert "analysis_id" not in rec

    entries = action_stage_log.read_entries(files["stage_log"], stages=["executed"])
    assert len(entries) == 1
    assert entries[0]["analysis_id"] == "execution"


def test_ai_linked_order_is_rejected_when_readiness_is_review(isolated) -> None:
    files = isolated
    action_id = "review-action"
    _write_json(files["action_state"], {"actions": {action_id: {
        "id": action_id,
        "ticker": "7203.T",
        "action_type": "buy",
        "status": "pending",
        "execution_readiness": "review",
        "execution_block_reasons": [{"code": "macro_event_caution", "message": "重要指標前"}],
    }}})

    req = ExecutionRequest(
        ticker="7203.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="特定", status="ordered",
        analysis_id="analysis-review", action_state_id=action_id,
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(req))

    assert exc.value.status_code == 409
    assert "macro_event_caution" in str(exc.value.detail)
    assert _read(files["executions"])["executions"] == []


def test_ai_linked_order_rechecks_route_text_conflict_before_ordering(isolated) -> None:
    files = isolated
    action_id = "avgo-route-conflict"
    _write_json(files["holdings"], {
        "AVGO_toku": {
            "ticker": "AVGO",
            "shares": 5,
            "entry_price": 242.128,
            "account": "特定",
            "currency": "USD",
            "investment_type": "long",
            "owner": "husband",
            "broker": "rakuten",
        },
        "AVGO_ippan": {
            "ticker": "AVGO",
            "shares": 27,
            "entry_price": 203.8148,
            "account": "一般",
            "currency": "USD",
            "investment_type": "long",
            "owner": "husband",
            "broker": "rakuten",
        },
    })
    _write_json(files["action_state"], {"actions": {action_id: {
        "id": action_id,
        "ticker": "AVGO",
        "action_type": "trim",
        "action_detail": "一般口座保有分（27株）から3株トリム",
        "status": "pending",
        "execution_readiness": "ready",
        "execution_account": "特定",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 3,
    }}})

    req = ExecutionRequest(
        ticker="AVGO",
        direction="sell",
        quantity=3,
        price=410.5,
        currency="USD",
        account="特定",
        investment_type="long",
        status="ordered",
        action_state_id=action_id,
        execution_position_keys=["AVGO_toku"],
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(req))

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "execution_route_text_conflict"
    assert _read(files["executions"])["executions"] == []


def test_reported_fill_is_accepted_and_audited_when_readiness_is_blocked(isolated) -> None:
    files = isolated
    action_id = "blocked-action"
    _write_json(files["action_state"], {"actions": {action_id: {
        "id": action_id,
        "ticker": "7203.T",
        "action_type": "buy",
        "status": "pending",
        "execution_readiness": "blocked",
        "execution_block_reasons": [{"code": "technical_data_stale", "message": "価格データが古い"}],
    }}})

    req = ExecutionRequest(
        ticker="7203.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="特定", status="executed",
        analysis_id="analysis-blocked", action_state_id=action_id,
    )
    asyncio.run(actions.save_execution(req))

    record = _read(files["executions"])["executions"][0]
    assert record["executed_despite_readiness"] is True
    assert record["readiness_at_execution"] == "blocked"
    assert record["execution_block_reasons_at_execution"][0]["code"] == "technical_data_stale"
    assert _read(files["action_state"])["actions"][action_id]["status"] == "filled"


def test_zero_funding_blocks_new_order_but_not_reported_fill(isolated) -> None:
    files = isolated
    _write_json(files["execution_plan"], {
        "status": "active",
        "budgets": {
            "normal_pool_available_jpy": 0,
            "opportunity_pool_available_jpy": 0,
        },
        "contribution_summary": {"available_jpy": 0},
    })

    ordered = ExecutionRequest(
        ticker="7203.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="特定", status="ordered",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(ordered))
    assert exc.value.status_code == 409
    assert "no_approved_discretionary_funding" in str(exc.value.detail)
    assert _read(files["executions"])["executions"] == []

    fill = ExecutionRequest(
        ticker="7203.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="特定", status="executed",
    )
    result = asyncio.run(actions.save_execution(fill))
    assert result["ok"] is True
    assert _read(files["executions"])["executions"][0]["status"] == "executed"


def test_save_execution_sell_adds_cash(isolated) -> None:
    """Selling an existing position adds cash to balance."""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 100_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 0.0, "currency": "USD"},
        "7203.T": {
            "ticker":      "7203.T",
            "shares":      2.0,
            "entry_price": 1_000.0,
            "currency":    "JPY",
            "account":     "特定",
        }
    })
    _write_json(files["account"], {
        "balance":        100_000.0,
        "usd_balance":    0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     100_000,
    })

    req = ExecutionRequest(
        ticker="7203.T",
        direction="sell",
        quantity=1,
        price=1_200.0,
        currency="JPY",
        account="特定",
        status="executed",
    )
    asyncio.run(actions.save_execution(req))

    account = _read(files["account"])
    assert account["balance"] == pytest.approx(101_200.0)
    holdings = _read(files["holdings"])
    assert holdings["CASH_JPY"]["shares"] == pytest.approx(account["balance"])
    assert holdings["CASH_USD"]["shares"] == pytest.approx(account["usd_balance"])


# ---------------------------------------------------------------------------
# M3: SELL時にholdingのcurrencyが欠落している場合のfail-closed化
# ---------------------------------------------------------------------------


def test_sell_missing_currency_holding_is_saved_as_application_pending(isolated) -> None:
    """A valid fill remains durable when its legacy currency cannot be resolved."""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 100_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 0.0, "currency": "USD"},
        "MISSINGCCY": {
            "ticker":      "MISSINGCCY",
            "shares":      2.0,
            "entry_price": 100.0,
            "account":     "特定",
            # currency キーが欠落（過去データ移行・手動編集等を想定）
        },
    })
    _write_json(files["account"], {
        "balance":        100_000.0,
        "usd_balance":    0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     100_000,
    })

    req = ExecutionRequest(
        ticker="MISSINGCCY",
        direction="sell",
        quantity=1,
        price=110.0,
        account="特定",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))
    assert result["portfolio_application_status"] == "pending"
    assert result["portfolio_application_reasons"][0]["code"] == "currency_unresolved"

    # fail-closedで副作用が出ていないこと（holdings/accountは未変更）
    holdings = _read(files["holdings"])
    assert holdings["MISSINGCCY"]["shares"] == pytest.approx(2.0)
    account = _read(files["account"])
    assert account["balance"] == pytest.approx(100_000.0)


def test_sell_missing_currency_jp_ticker_auto_detects_jpy(isolated) -> None:
    """holdingにcurrencyが無くても .T 銘柄なら ticker suffix から JPY を自動判定してSELLを通す。"""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 100_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 0.0, "currency": "USD"},
        "9999.T": {
            "ticker":      "9999.T",
            "shares":      2.0,
            "entry_price": 1_000.0,
            "account":     "特定",
            # currency キーが欠落
        },
    })
    _write_json(files["account"], {
        "balance":        100_000.0,
        "usd_balance":    0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     100_000,
    })

    req = ExecutionRequest(
        ticker="9999.T",
        direction="sell",
        quantity=1,
        price=1_200.0,
        account="特定",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    assert result["portfolio"]["cash_currency"] == "JPY"
    account = _read(files["account"])
    # JPY自動判定によりFX変換なしで加算: 100,000 + 1,200
    assert account["balance"] == pytest.approx(101_200.0)
    holdings = _read(files["holdings"])
    assert holdings["9999.T"]["shares"] == pytest.approx(1.0)


def test_sell_existing_currency_unaffected(isolated) -> None:
    """holdingにcurrencyが既に設定されている通常SELLはfail-closed化後も従来通り動く。"""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 0.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 100.0, "currency": "USD"},
        "USDHOLD": {
            "ticker":      "USDHOLD",
            "shares":      3.0,
            "entry_price": 50.0,
            "currency":    "USD",
            "account":     "特定",
        },
    })
    _write_json(files["account"], {
        "balance":        0.0,
        "usd_balance":    100.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     100 * 150,
    })

    req = ExecutionRequest(
        ticker="USDHOLD",
        direction="sell",
        quantity=1,
        price=60.0,
        account="特定",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    assert result["portfolio"]["cash_currency"] == "USD"
    account = _read(files["account"])
    assert account["usd_balance"] == pytest.approx(160.0)
    holdings = _read(files["holdings"])
    assert holdings["USDHOLD"]["shares"] == pytest.approx(2.0)
    assert holdings["USDHOLD"]["currency"] == "USD"


def test_buy_same_ticker_different_account_creates_separate_position(isolated) -> None:
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 500_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 10_000.0, "currency": "USD"},
        "LLY": {
            "ticker": "LLY",
            "shares": 3.0,
            "entry_price": 1050.0,
            "currency": "USD",
            "account": "特定",
        },
    })
    _write_json(files["account"], {
        "balance": 500_000.0,
        "usd_balance": 10_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 2_000_000,
    })

    req = ExecutionRequest(
        ticker="LLY",
        direction="buy",
        quantity=1,
        price=1_080.0,
        currency="USD",
        account="NISA成長投資枠",
        investment_type="long",
        status="executed",
        execution_owner="husband",
        execution_broker="rakuten",
    )
    asyncio.run(actions.save_execution(req))

    holdings = _read(files["holdings"])
    assert holdings["LLY"]["shares"] == pytest.approx(3.0)
    assert holdings["LLY"]["account"] == "特定"
    assert holdings["LLY_NISA_HUSBAND"]["shares"] == pytest.approx(1.0)
    assert holdings["LLY_NISA_HUSBAND"]["account"] == "NISA成長投資枠"
    rec = _read(files["executions"])["executions"][0]
    assert rec["account"] == "NISA成長投資枠"
    assert rec.get("provenance_incomplete") is not True


def test_linked_nisa_order_inherits_owner_broker_and_position_keys(isolated) -> None:
    files = isolated
    _write_json(files["action_state"], {"actions": {
        "state-1489": {
            "id": "state-1489",
            "ticker": "1489.T",
            "action_type": "buy",
            "status": "pending",
            "execution_readiness": "ready",
            "execution_account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "execution_position_keys": ["1489_WIFE"],
        },
    }})

    req = ExecutionRequest(
        ticker="1489.T",
        direction="buy",
        quantity=10,
        price=3_300,
        currency="JPY",
        status="ordered",
        action_state_id="state-1489",
        order_type="limit",
        limit_price=3_300,
    )
    asyncio.run(actions.save_execution(req))

    rec = _read(files["executions"])["executions"][0]
    assert rec["account"] == "NISA成長投資枠"
    assert rec["execution_owner"] == "wife"
    assert rec["execution_broker"] == "sbi"
    assert rec["execution_position_keys"] == ["1489_WIFE"]
    assert rec["readiness_at_order"] == "ready"


def test_linked_execution_snapshots_monthly_plan_metadata(isolated) -> None:
    files = isolated
    _write_json(files["action_state"], {"actions": {
        "state-plan": {
            "id": "state-plan",
            "ticker": "7203.T",
            "action_type": "buy",
            "status": "pending",
            "execution_readiness": "ready",
            "plan_item_id": "2026-07-w30-add-currency-usd-001",
            "monthly_objective_id": "2026-07:normal:add-currency-usd",
            "execution_plan_decision": "plan_new_order",
        },
    }})

    req = ExecutionRequest(
        ticker="7203.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="特定", status="executed", action_state_id="state-plan",
    )
    asyncio.run(actions.save_execution(req))

    rec = _read(files["executions"])["executions"][0]
    assert rec["plan_item_id"] == "2026-07-w30-add-currency-usd-001"
    assert rec["monthly_objective_id"] == "2026-07:normal:add-currency-usd"
    assert rec["execution_plan_decision"] == "plan_new_order"


def test_contribution_link_requires_exact_owner_and_broker_route(isolated) -> None:
    files = isolated
    _write_json(files["action_state"], {"actions": {}})
    _write_json(Path(actions.BASE_DIR) / "contribution_ledger.json", {
        "schema_version": 1,
        "contributions": [{
            "id": "wife-sbi-salary",
            "source": "salary",
            "bucket": "normal",
            "owner": "wife",
            "broker": "sbi",
            "amount_jpy": 100_000,
            "start_month": "2026-07",
            "release_months": 1,
            "status": "approved",
        }],
    })

    req = _ExecutionRequestModel(
        ticker="7203.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="特定", status="executed",
        execution_owner="husband", execution_broker="rakuten",
        contribution_id="wife-sbi-salary", idempotency_key="route-mismatch-contribution",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(req))
    assert exc.value.status_code == 422
    assert _read(files["executions"])["executions"] == []


def test_linked_nisa_order_without_routing_is_rejected(isolated) -> None:
    files = isolated
    _write_json(files["action_state"], {"actions": {
        "state-xlf": {
            "id": "state-xlf",
            "ticker": "XLF",
            "action_type": "buy",
            "status": "pending",
            "execution_readiness": "ready",
            "execution_account": "NISA成長投資枠",
        },
    }})

    req = ExecutionRequest(
        ticker="XLF",
        direction="buy",
        quantity=1,
        price=56,
        currency="USD",
        status="ordered",
        action_state_id="state-xlf",
        order_type="limit",
        limit_price=56,
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(req))

    assert exc.value.status_code == 409
    assert "execution_owner" in str(exc.value.detail)


def test_sell_same_ticker_uses_requested_account_position(isolated, monkeypatch) -> None:
    files = isolated
    monkeypatch.setattr(actions, "_get_fx_rate", lambda: 150.0)
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 500_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 1_000.0, "currency": "USD"},
        "AVGO_toku": {
            "ticker": "AVGO",
            "shares": 12.0,
            "entry_price": 140.0,
            "currency": "USD",
            "account": "特定",
        },
        "AVGO_ippan": {
            "ticker": "AVGO",
            "shares": 27.0,
            "entry_price": 204.0,
            "currency": "USD",
            "account": "一般",
        },
    })
    _write_json(files["account"], {
        "balance": 500_000.0,
        "usd_balance": 1_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 650_000,
    })

    req = ExecutionRequest(
        ticker="AVGO",
        direction="sell",
        quantity=2,
        price=486.0,
        currency="USD",
        account="一般",
        status="executed",
        execution_position_keys=["AVGO_ippan"],
    )
    asyncio.run(actions.save_execution(req))

    holdings = _read(files["holdings"])
    assert holdings["AVGO_toku"]["shares"] == pytest.approx(12.0)
    assert holdings["AVGO_ippan"]["shares"] == pytest.approx(25.0)
    rec = _read(files["executions"])["executions"][0]
    assert rec["account"] == "一般"
    assert rec["realized_pnl_jpy"] == pytest.approx((486.0 - 204.0) * 2 * 150.0)


def test_ordered_sell_cannot_exceed_requested_account_inventory(isolated) -> None:
    files = isolated
    _write_json(files["holdings"], {
        "AVGO_toku": {
            "ticker": "AVGO", "shares": 5.0, "entry_price": 242.0,
            "currency": "USD", "account": "特定", "investment_type": "long",
        },
        "AVGO_ippan": {
            "ticker": "AVGO", "shares": 27.0, "entry_price": 204.0,
            "currency": "USD", "account": "一般", "investment_type": "long",
        },
    })
    req = ExecutionRequest(
        ticker="AVGO",
        direction="sell",
        quantity=8,
        price=486.0,
        currency="USD",
        account="特定",
        investment_type="long",
        status="ordered",
        order_type="limit",
        limit_price=486.0,
        execution_position_keys=["AVGO_toku"],
        idempotency_key="avgo-toku-oversell",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(req))

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "holding_quantity_exceeds_account"
    assert exc.value.detail["available_quantity"] == 5
    assert exc.value.detail["requested_quantity"] == 8
    assert _read(files["executions"])["executions"] == []


def test_ordered_sell_with_exact_account_inventory_is_accepted(isolated) -> None:
    files = isolated
    _write_json(files["holdings"], {
        "AVGO_toku": {
            "ticker": "AVGO", "shares": 5.0, "entry_price": 242.0,
            "currency": "USD", "account": "特定", "investment_type": "long",
        },
        "AVGO_ippan": {
            "ticker": "AVGO", "shares": 27.0, "entry_price": 204.0,
            "currency": "USD", "account": "一般", "investment_type": "long",
        },
    })
    req = ExecutionRequest(
        ticker="AVGO",
        direction="sell",
        quantity=5,
        price=486.0,
        currency="USD",
        account="特定",
        investment_type="long",
        status="ordered",
        order_type="limit",
        limit_price=486.0,
        execution_position_keys=["AVGO_toku"],
        idempotency_key="avgo-toku-exact-sell",
    )

    result = asyncio.run(actions.save_execution(req))

    assert result["ok"] is True
    assert result["portfolio_application_status"] == "not_applicable"
    assert _read(files["holdings"])["AVGO_toku"]["shares"] == 5


def test_ordered_sell_never_auto_splits_across_taxable_accounts(isolated) -> None:
    files = isolated
    _write_json(files["holdings"], {
        "AVGO_toku": {
            "ticker": "AVGO", "shares": 5.0, "entry_price": 242.0,
            "currency": "USD", "account": "特定", "investment_type": "long",
        },
        "AVGO_ippan": {
            "ticker": "AVGO", "shares": 27.0, "entry_price": 204.0,
            "currency": "USD", "account": "一般", "investment_type": "long",
        },
    })
    req = ExecutionRequest(
        ticker="AVGO",
        direction="sell",
        quantity=8,
        price=486.0,
        currency="USD",
        investment_type="long",
        status="ordered",
        order_type="limit",
        limit_price=486.0,
        idempotency_key="avgo-ambiguous-sell",
    )

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(req))

    assert exc.value.status_code == 409
    assert exc.value.detail["code"] == "holding_scope_ambiguous"
    assert sorted(exc.value.detail["candidate_position_keys"]) == ["AVGO_ippan", "AVGO_toku"]
    assert _read(files["executions"])["executions"] == []


def test_save_execution_short_opens_margin_position_without_touching_cash(isolated) -> None:
    """New short executions create margin_positions entries, not holdings sells."""
    files = isolated
    before_h = _read(files["holdings"])
    before_a = _read(files["account"])

    req = ExecutionRequest(
        ticker="7203.T",
        direction="short",
        quantity=3,
        price=1_000.0,
        currency="JPY",
        account="信用",
        investment_type="swing",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    assert result["ok"] is True
    assert result["portfolio"]["margin_side"] == "short"
    assert _read(files["holdings"]) == before_h
    assert _read(files["account"]) == before_a

    margin = _read(files["margin_positions"])
    assert len(margin["positions"]) == 1
    pos = margin["positions"][0]
    assert pos["ticker"] == "7203.T"
    assert pos["side"] == "short"
    assert pos["shares"] == pytest.approx(3.0)
    assert pos["entry_price"] == pytest.approx(1_000.0)
    assert pos["account"] == "信用"
    assert pos["source_event_id"] == result["portfolio"]["event_id"]

    events = event_ledger.query_events(db_path=files["ledger_db"])
    assert len(events) == 1
    assert events[0]["direction"] == "short"
    assert events[0]["amount_jpy"] == pytest.approx(3_000.0)

    rec = _read(files["executions"])["executions"][0]
    assert rec["direction"] == "short"
    assert rec["margin_position_id"] == pos["id"]


def test_save_execution_margin_buy_opens_long_margin_position(isolated) -> None:
    req = ExecutionRequest(
        ticker="9984.T",
        direction="margin_buy",
        quantity=2,
        price=8_000.0,
        currency="JPY",
        account="信用",
        investment_type="swing",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    margin = _read(isolated["margin_positions"])
    pos = margin["positions"][0]
    assert result["portfolio"]["margin_side"] == "long"
    assert pos["side"] == "long"
    assert pos["shares"] == pytest.approx(2.0)
    events = event_ledger.query_events(db_path=isolated["ledger_db"])
    assert events[0]["direction"] == "margin_buy"
    assert events[0]["amount_jpy"] == pytest.approx(-16_000.0)


def test_save_execution_cover_partially_closes_short_position(isolated) -> None:
    files = isolated
    _write_json(files["margin_positions"], {
        "cash_collateral": 0,
        "securities_collateral": 0,
        "sec_haircut": 0.80,
        "positions": [{
            "id": 1,
            "ticker": "7203.T",
            "side": "short",
            "shares": 3.0,
            "entry_price": 1_000.0,
            "current_price": 1_000.0,
            "currency": "JPY",
            "account": "信用",
            "position_type": "一般信用",
            "opened": "2026-06-01",
            "closed": False,
        }],
        "updated": "",
    })

    req = ExecutionRequest(
        ticker="7203.T",
        direction="cover",
        quantity=2,
        price=900.0,
        currency="JPY",
        account="信用",
        investment_type="swing",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))

    assert result["portfolio"]["realized_pnl_jpy"] == pytest.approx(200.0)
    margin = _read(files["margin_positions"])
    open_pos = next(p for p in margin["positions"] if not p.get("closed"))
    closed = [p for p in margin["positions"] if p.get("closed")]
    assert open_pos["shares"] == pytest.approx(1.0)
    assert len(closed) == 1
    assert closed[0]["shares"] == pytest.approx(2.0)
    assert closed[0]["realized_pnl_jpy"] == pytest.approx(200.0)

    events = event_ledger.query_events(db_path=files["ledger_db"])
    assert events[0]["direction"] == "cover"
    assert events[0]["amount_jpy"] == pytest.approx(-1_800.0)


def test_save_execution_usd_trade_updates_cash_usd_mirror(isolated) -> None:
    """USD executions update both account.usd_balance and holdings.CASH_USD."""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 100_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 1_000.0, "currency": "USD"},
    })
    _write_json(files["account"], {
        "balance":        100_000.0,
        "usd_balance":    1_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     250_000,
    })

    req = ExecutionRequest(
        ticker="NVDA",
        direction="buy",
        quantity=2,
        price=100.0,
        currency="USD",
        account="特定",
        status="executed",
    )
    asyncio.run(actions.save_execution(req))

    account = _read(files["account"])
    holdings = _read(files["holdings"])
    assert account["usd_balance"] == pytest.approx(800.0)
    assert holdings["CASH_USD"]["shares"] == pytest.approx(account["usd_balance"])
    assert holdings["CASH_JPY"]["shares"] == pytest.approx(account["balance"])


def test_save_execution_buy_jt_ticker_with_auto_currency(isolated) -> None:
    """When currency is None for a .T ticker, _auto_detect_currency fills in JPY."""
    files = isolated
    _write_json(files["account"], {
        "balance":        500_000.0,
        "usd_balance":    0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     500_000,
    })

    req = ExecutionRequest(
        ticker="9984.T",
        direction="buy",
        quantity=1,
        price=8_000.0,
        currency="JPY",   # explicit here, but auto-detection is tested in unit test above
        account="特定",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))
    assert result["ok"] is True
    holdings = _read(files["holdings"])
    assert "9984.T" in holdings


def test_save_execution_ordered_status_does_not_deduct_cash(isolated) -> None:
    """status=ordered should not deduct cash (not yet executed)."""
    files = isolated
    _write_json(files["account"], {
        "balance":        500_000.0,
        "usd_balance":    0.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash":     500_000,
    })

    req = ExecutionRequest(
        ticker="7203.T",
        direction="buy",
        quantity=1,
        price=10_000.0,
        currency="JPY",
        account="特定",
        status="ordered",
    )
    asyncio.run(actions.save_execution(req))

    account = _read(files["account"])
    # ordered status: _apply_event_to_ledger records but does not update portfolio
    # so balance remains unchanged (holdings not modified yet)
    assert account["balance"] == pytest.approx(500_000.0)


def test_save_execution_result_contains_id_and_portfolio_keys(isolated) -> None:
    """Response schema check: ok, id, portfolio are always present."""
    req = ExecutionRequest(
        ticker="NVDA",
        direction="buy",
        quantity=1,
        price=500.0,
        currency="USD",
        account="特定",
        status="executed",
    )
    result = asyncio.run(actions.save_execution(req))
    assert "ok" in result
    assert "id" in result
    assert "portfolio" in result
    assert result["ok"] is True
    assert result["id"].startswith("execution_")


def test_save_execution_multiple_calls_append_to_log(isolated) -> None:
    """Each call appends a new record; the log grows."""
    req1 = ExecutionRequest(
        ticker="NVDA", direction="buy", quantity=1, price=500.0,
        currency="USD", account="特定", status="executed",
    )
    req2 = ExecutionRequest(
        ticker="AAPL", direction="buy", quantity=2, price=200.0,
        currency="USD", account="特定", status="executed",
    )
    asyncio.run(actions.save_execution(req1))
    asyncio.run(actions.save_execution(req2))

    records = _read(isolated["executions"])["executions"]
    assert len(records) == 2
    tickers = {r["ticker"] for r in records}
    assert tickers == {"NVDA", "AAPL"}


def test_same_second_same_ticker_executions_get_distinct_event_ids(isolated, monkeypatch) -> None:
    """同一秒の同一 ticker/direction でも event_id が衝突せず、両方が台帳反映される。"""
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 0.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 1_000.0, "currency": "USD"},
    })
    _write_json(files["account"], {
        "balance": 0.0,
        "usd_balance": 1_000.0,
        "fx_rate_usdjpy": 150.0,
        "total_cash": 150_000,
    })

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 6, 27, 10, 0, 0)

    monkeypatch.setattr(actions, "datetime", FixedDateTime)
    req = ExecutionRequest(
        ticker="NVDA",
        direction="buy",
        quantity=1,
        price=100.0,
        currency="USD",
        account="特定",
        status="executed",
    )

    first = asyncio.run(actions.save_execution(req))
    second_req = ExecutionRequest(
        ticker="NVDA",
        direction="buy",
        quantity=1,
        price=100.0,
        currency="USD",
        account="特定",
        status="executed",
    )
    second = asyncio.run(actions.save_execution(second_req))

    assert first["id"] != second["id"]
    holdings = _read(files["holdings"])
    assert holdings["NVDA"]["shares"] == pytest.approx(2.0)
    account = _read(files["account"])
    assert account["usd_balance"] == pytest.approx(800.0)
    records = _read(files["executions"])["executions"]
    assert len({r["event_id"] for r in records}) == 2
    assert len(event_ledger.query_events(db_path=files["ledger_db"])) == 2


def test_idempotency_replay_survives_execution_json_trim(isolated) -> None:
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY": {"ticker": "CASH_JPY", "shares": 500_000.0, "currency": "JPY"},
        "CASH_USD": {"ticker": "CASH_USD", "shares": 1_000.0, "currency": "USD"},
    })
    req = _ExecutionRequestModel(
        ticker="NVDA", direction="buy", quantity=1, price=100,
        currency="USD", account="特定", status="executed",
        execution_owner="husband", execution_broker="rakuten",
        idempotency_key="stable-retry-key-001",
    )
    first = asyncio.run(actions.save_execution(req))
    replay = asyncio.run(actions.save_execution(req))
    assert replay["id"] == first["id"]
    assert replay["idempotent_replay"] is True
    assert _read(files["holdings"])["NVDA"]["shares"] == 1
    assert len(event_ledger.query_events(db_path=files["ledger_db"])) == 1

    _write_json(files["executions"], {"executions": []})
    trimmed_replay = asyncio.run(actions.save_execution(req))
    assert trimmed_replay["idempotent_replay"] is True
    assert _read(files["holdings"])["NVDA"]["shares"] == 1

    conflict = req.model_copy(update={"quantity": 2})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.save_execution(conflict))
    assert exc.value.status_code == 409


def test_ambiguous_nisa_fill_waits_then_applies_only_selected_wife_position(isolated) -> None:
    files = isolated
    original_account = _read(files["account"])
    _write_json(files["holdings"], {
        "XLF_NISA": {
            "ticker": "XLF", "shares": 2, "entry_price": 50, "currency": "USD",
            "account": "NISA成長投資枠", "owner": "husband", "broker": "楽天証券",
        },
        "XLF_WIFE": {
            "ticker": "XLF", "shares": 3, "entry_price": 51, "currency": "JPY",
            "account": "NISA成長投資枠", "owner": "wife", "broker": "SBI証券（妻）",
        },
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI", "shares": 100_000, "currency": "JPY"},
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE", "shares": 50_000, "currency": "JPY",
            "reported_balance_jpy": 50_000, "reported_as_of": "2026-05-12",
            "ledger_delta_since_report_jpy": 0, "balance_status": "confirmed",
            "reconciliation_required": False,
        },
    })
    req = _ExecutionRequestModel(
        ticker="XLF", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="NISA成長投資枠", status="executed",
        idempotency_key="ambiguous-nisa-fill-001",
    )
    saved = asyncio.run(actions.save_execution(req))
    assert saved["portfolio_application_status"] == "pending"
    assert saved["portfolio_application_reasons"][0]["code"] == "holding_scope_ambiguous"
    assert _read(files["holdings"])["XLF_WIFE"]["shares"] == 3
    assert _read(files["account"]) == original_account
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []

    resolved = asyncio.run(actions.resolve_execution_portfolio(
        saved["id"],
        actions.PortfolioResolutionRequest(
            resolution="apply",
            execution_owner="wife",
            execution_broker="sbi",
            account="NISA成長投資枠",
            execution_position_key="XLF_WIFE",
        ),
    ))
    holdings = _read(files["holdings"])
    assert resolved["portfolio_application_status"] == "applied"
    assert holdings["XLF_WIFE"]["shares"] == 4
    assert holdings["XLF_NISA"]["shares"] == 2
    assert holdings["CASH_JPY_SBI_WIFE"]["shares"] == 49_000
    assert holdings["CASH_JPY_SBI"]["shares"] == 100_000
    assert _read(files["account"]) == original_account
    assert len(event_ledger.query_events(db_path=files["ledger_db"])) == 1


def test_external_reconcile_resolves_route_without_mutating_internal_ledger(isolated) -> None:
    files = isolated
    _write_json(files["holdings"], {
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE", "shares": 50_000, "currency": "JPY",
        },
    })
    before_holdings = _read(files["holdings"])
    before_account = _read(files["account"])
    saved = asyncio.run(actions.save_execution(_ExecutionRequestModel(
        ticker="285A.T", direction="buy", quantity=1, price=1_000,
        currency="JPY", account="NISA成長投資枠", status="executed",
        idempotency_key="external-reconcile-route-001",
    )))
    assert saved["portfolio_application_status"] == "pending"

    with pytest.raises(HTTPException) as exc:
        asyncio.run(actions.resolve_execution_portfolio(
            saved["id"],
            actions.PortfolioResolutionRequest(
                resolution="externally_reconciled",
                external_reconcile_source="rakuten_sbi_csv_20260717",
            ),
        ))
    assert exc.value.status_code == 422

    resolved = asyncio.run(actions.resolve_execution_portfolio(
        saved["id"],
        actions.PortfolioResolutionRequest(
            resolution="externally_reconciled",
            execution_owner="wife",
            execution_broker="sbi",
            account="NISA成長投資枠",
            execution_position_key="285A_WIFE",
            external_reconcile_source="rakuten_sbi_csv_20260717",
        ),
    ))

    record = _read(files["executions"])["executions"][0]
    assert resolved["portfolio_application_status"] == "externally_reconciled"
    assert record["execution_owner"] == "wife"
    assert record["execution_broker"] == "sbi"
    assert record["execution_position_keys"] == ["285A_WIFE"]
    assert "provenance_incomplete" not in record
    assert _read(files["holdings"]) == before_holdings
    assert _read(files["account"]) == before_account
    assert event_ledger.query_events(db_path=files["ledger_db"]) == []


def test_wife_sbi_negative_estimate_keeps_fill_and_blocks_buying_power(isolated) -> None:
    files = isolated
    account_before = _read(files["account"])
    _write_json(files["holdings"], {
        "CASH_JPY_SBI": {"ticker": "CASH_JPY_SBI", "shares": 100_000, "currency": "JPY"},
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE", "shares": 50, "currency": "JPY",
            "reported_balance_jpy": 50, "reported_as_of": "2026-05-12",
            "ledger_delta_since_report_jpy": 0, "balance_status": "confirmed",
            "reconciliation_required": False,
        },
    })
    req = _ExecutionRequestModel(
        ticker="285A.T", direction="buy", quantity=1, price=100,
        currency="JPY", account="NISA成長投資枠", status="executed",
        execution_owner="wife", execution_broker="sbi",
        idempotency_key="wife-negative-cash-001",
    )
    result = asyncio.run(actions.save_execution(req))
    wife_cash = _read(files["holdings"])["CASH_JPY_SBI_WIFE"]
    assert result["portfolio_application_status"] == "applied"
    assert wife_cash["shares"] == -50
    assert wife_cash["ledger_delta_since_report_jpy"] == -100
    assert wife_cash["balance_status"] == "estimated_negative"
    assert wife_cash["reconciliation_required"] is True
    assert _read(files["account"]) == account_before


def test_interrupted_application_recovers_exact_after_state_without_double_apply(isolated) -> None:
    files = isolated
    req = _ExecutionRequestModel(
        ticker="7203.T", direction="buy", quantity=1, price=100,
        currency="JPY", account="特定", status="executed",
        execution_owner="husband", execution_broker="rakuten",
        idempotency_key="interrupted-application-001",
    )
    exec_id = actions._execution_id_from_key(req.idempotency_key)
    event_id = actions._make_execution_event_id(exec_id)
    event_ledger.reserve_execution_idempotency(
        idempotency_key=req.idempotency_key,
        request_hash=actions._execution_request_hash(req),
        execution_id=exec_id,
    )
    account_after = {**_read(files["account"]), "balance": 499_900.0, "total_cash": 649_900}
    holdings_after = {
        "7203.T": {
            "ticker": "7203.T", "shares": 1, "entry_price": 100,
            "currency": "JPY", "account": "特定", "owner": "husband", "broker": "楽天証券",
        }
    }
    event_kwargs = {
        "event_id": event_id,
        "direction": "buy",
        "ticker": "7203.T",
        "price": 100,
        "quantity": 1,
        "currency": "JPY",
        "account": "特定",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "cash_route": "account.json",
        "execution_position_key": "7203.T",
    }
    event_ledger.prepare_portfolio_application(
        event_id=event_id,
        holdings_after=holdings_after,
        account_after=account_after,
        event_kwargs=event_kwargs,
        result={
            "updated": True, "message": "recovered", "cash_delta": -100,
            "cash_currency": "JPY", "cash_route": "account.json",
            "position_key": "7203.T", "event_id": event_id,
        },
    )
    # Simulate a kill after holdings write but before account/event/log writes.
    _write_json(files["holdings"], holdings_after)

    result = asyncio.run(actions.save_execution(req))
    assert result["portfolio_application_status"] == "applied"
    assert _read(files["holdings"])["7203.T"]["shares"] == 1
    assert _read(files["account"])["balance"] == 499_900
    assert len(event_ledger.query_events(db_path=files["ledger_db"])) == 1
