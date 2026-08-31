import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from execution_readiness import (
    annotate_reason_scopes,
    classify_execution_readiness,
    evaluate_cash_buying_power,
    portfolio_snapshot_health,
    resolve_cash_buying_capacity,
)


JST = ZoneInfo("Asia/Tokyo")


def _active_execution_plan(
    now: datetime,
    *,
    normal_jpy: int = 100_000,
    opportunity_jpy: int = 0,
    contribution_jpy: int = 100_000,
) -> dict:
    week_start = now.date() - timedelta(days=now.date().weekday())
    return {
        "schema_version": 2,
        "as_of": now.isoformat(),
        "horizon": {
            "month": f"{now.year:04d}-{now.month:02d}",
            "week_start": week_start.isoformat(),
            "week_end": (week_start + timedelta(days=6)).isoformat(),
        },
        "status": "active",
        "budgets": {
            "normal_pool_available_jpy": normal_jpy,
            "opportunity_pool_available_jpy": opportunity_jpy,
        },
        "contribution_summary": {"available_jpy": contribution_jpy},
    }


def _write_base(tmp_path, now, *, snapshot_hours=1, ticker="XLF", tech_status="fresh"):
    stamp = (now - timedelta(hours=snapshot_hours)).isoformat()
    (tmp_path / "account.json").write_text(json.dumps({"last_updated": stamp}), encoding="utf-8")
    (tmp_path / "holdings.json").write_text(json.dumps({"last_updated": stamp}), encoding="utf-8")
    (tmp_path / "broker_position_snapshot_fixture.json").write_text(json.dumps({
        "complete": True,
        "source_as_of": stamp,
        "positions": [],
    }), encoding="utf-8")
    # 本番の technical_state.json は全行が data_quality_status と
    # freshness_status を持つ (実測 72/72)。品質契約が fail-closed に
    # なったので、共有 fixture も本番形に合わせる —— 省略すると
    # data_quality_unknown として正しく blocked され、この fixture を使う
    # 無関係なテストまで巻き込む (Codex レビュー round 10)。
    (tmp_path / "technical_state.json").write_text(json.dumps({
        "tickers": {ticker: {"freshness_status": tech_status,
                             "data_quality_status": "ok",
                             "data_as_of": "2026-07-13"}}
    }), encoding="utf-8")
    (tmp_path / "macro_event_state.json").write_text(json.dumps({
        "status": "ok", "refreshed_at": now.isoformat(), "events": []
    }), encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(
        json.dumps(_active_execution_plan(now)), encoding="utf-8"
    )


def test_zero_discretionary_funding_blocks_buy_independent_of_plan_gate(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "execution_plan_state.json").write_text(json.dumps(
        _active_execution_plan(now, normal_jpy=0, contribution_jpy=0)
    ), encoding="utf-8")

    result = classify_execution_readiness({
        "ticker": "XLF",
        "type": "buy",
        "order_type": "limit",
        "limit_price": 55,
        "execution_plan_gate_mode": "off",
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert "no_approved_discretionary_funding" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_missing_discretionary_funding_state_fails_closed_for_buy(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "execution_plan_state.json").unlink()

    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit", "limit_price": 55,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert "discretionary_funding_unresolved" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_zero_discretionary_funding_does_not_block_sell(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "execution_plan_state.json").write_text(json.dumps(
        _active_execution_plan(now, normal_jpy=0, contribution_jpy=0)
    ), encoding="utf-8")

    result = classify_execution_readiness({
        "ticker": "XLF", "type": "sell", "order_type": "limit", "limit_price": 55,
    }, base_dir=tmp_path, now=now)

    assert "no_approved_discretionary_funding" not in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_exit_sizing_review_cannot_be_promoted_to_ready(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    result = classify_execution_readiness({
        "ticker": "XLF",
        "type": "sell",
        "execution_owner": "husband",
        "execution_broker": "rakuten",
        "execution_account": "特定",
        "holding_shares_before": 80,
        "quantity": 20,
        "exit_sizing_status": "review",
        "exit_sizing_reason": "取得原価が不明",
        "exit_cost_basis_status": "review",
        "exit_cost_basis_reason": "broker_total_cost_basis_missing",
    }, base_dir=tmp_path, now=now)
    assert result["execution_readiness"] != "ready"
    assert "exit_sizing_requires_review" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_wife_sbi_estimated_cash_is_not_buying_power(tmp_path):
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 492_606,
            "currency": "JPY",
            "reported_as_of": "2026-05-12",
            "balance_status": "estimated",
            "reconciliation_required": True,
        },
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T",
        "type": "add",
        "amount_hint": "20口",
        "limit_price": 3_382,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path)

    assert result["readiness"] == "blocked"
    assert result["reasons"][0]["code"] == "cash_balance_unconfirmed"
    assert result["reasons"][0]["cash_route"] == "CASH_JPY_SBI_WIFE"


def test_configured_broker_cash_schedule_reduces_effective_wallet_once(tmp_path, monkeypatch):
    import contribution_schedule

    monkeypatch.setattr(contribution_schedule, "CONTRIBUTIONS", [{
        "id": "example_weekly_broker_cash",
        "label": "Example weekly broker-cash contribution",
        "amount": 10_000,
        "currency": "JPY",
        "cadence": "weekly",
        "weekday": 0,
        "calendar_rule": "weekday",
        "owner": "wife",
        "broker": "sbi",
        "funding_source": "broker_cash",
        "cash_route": "CASH_JPY_SBI_WIFE",
    }])
    now = datetime(2026, 8, 19, 16, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 100_000,
            "available_to_trade_jpy": 100_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-08-07T00:00:00+09:00",
        },
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T",
        "type": "buy",
        "quantity": 20,
        "limit_price": 3_500,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "blocked"
    capacity = result["reasons"][0]["cash_capacity_observation"]
    assert capacity["scheduled_outflows"]["amount"] == 40_000
    assert capacity["effective_cash"] == 60_000


def test_confirmed_wife_sbi_cash_must_cover_requested_notional(tmp_path):
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 100_000,
            "available_to_trade_jpy": 50_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
        },
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T",
        "type": "buy",
        "quantity": 20,
        "limit_price": 3_382,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path)

    assert result["readiness"] == "blocked"
    assert result["reasons"][0]["code"] == "cash_balance_insufficient"
    assert result["reasons"][0]["requested_cash"] == 67_640
    assert result["reasons"][0]["available_cash"] == 50_000


def test_cash_buy_requires_complete_account_resource_identity(tmp_path):
    result = evaluate_cash_buying_power({
        "ticker": "1489.T",
        "type": "buy",
        "quantity": 1,
        "limit_price": 3_000,
        "execution_owner": "wife",
        "execution_broker": "sbi",
    }, base_dir=tmp_path)

    assert result["readiness"] == "blocked"
    assert result["reasons"][0]["code"] == "cash_resource_identity_missing"


def test_cash_buy_requires_confirmed_identity_scoped_balance_without_time_expiry(tmp_path):
    now = datetime(2026, 7, 28, 9, 0, tzinfo=JST)
    action = {
        "ticker": "1489.T",
        "type": "buy",
        "quantity": 1,
        "limit_price": 3_000,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }
    row = {
        "ticker": "CASH_JPY_SBI_WIFE",
        "shares": 50_000,
        "currency": "JPY",
        "balance_status": "confirmed",
        "reconciliation_required": False,
    }
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": row,
    }), encoding="utf-8")

    unknown = evaluate_cash_buying_power(action, base_dir=tmp_path, now=now)
    assert unknown["readiness"] == "blocked"
    assert unknown["reasons"][0]["code"] == "cash_resource_freshness_unknown"

    row["broker_reconciled_at"] = "2026-07-24T09:00:00+09:00"
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": row,
    }), encoding="utf-8")
    old_but_valid = evaluate_cash_buying_power(action, base_dir=tmp_path, now=now)
    assert old_but_valid["readiness"] == "ready"
    assert old_but_valid["cash_resource_validation_mode"] == "event_based"

    row["broker_reconciled_at"] = "2026-07-28T08:00:00+09:00"
    row["source_as_of"] = "2026-07-24T09:00:00+09:00"
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": row,
    }), encoding="utf-8")
    source_is_authority = evaluate_cash_buying_power(action, base_dir=tmp_path, now=now)
    assert source_is_authority["readiness"] == "ready"
    assert source_is_authority["cash_resource_as_of"].startswith("2026-07-24")

    row["source_as_of"] = "2026-07-28T08:00:00+09:00"
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": row,
    }), encoding="utf-8")
    fresh = evaluate_cash_buying_power(action, base_dir=tmp_path, now=now)
    assert fresh["readiness"] == "ready"
    assert (
        fresh["account_resource_identity"]
        == "wife|sbi|nisa_growth|JPY|cash"
    )


def test_wife_sbi_effective_cash_reserves_weekly_outflow_after_snapshot(tmp_path):
    now = datetime(2026, 8, 13, 6, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 192_886,
            "available_to_trade_jpy": 192_886,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-08-07T11:55:00+09:00",
            # An inferred label is deliberately not proof that the 8/10 debit
            # is included in the broker snapshot.
            "reserved_current": 23_076,
            "reserved_current_status": "inferred",
        },
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T", "type": "buy", "quantity": 52, "limit_price": 3_509,
        "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "blocked"
    reason = result["reasons"][0]
    assert reason["code"] == "cash_balance_insufficient"
    capacity = reason["cash_capacity_observation"]
    assert capacity["scheduled_outflows"]["amount"] == 92_304
    assert capacity["scheduled_outflows"]["unreflected_amount"] == 23_076
    assert capacity["scheduled_outflows"]["future_amount"] == 69_228
    assert capacity["effective_cash"] == 100_582
    assert capacity["capacity_valid_until"].startswith("2026-08-13")


def test_future_dated_cash_snapshot_never_authorizes_buy(tmp_path):
    now = datetime(2026, 8, 13, 6, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 200_000,
            "available_to_trade_jpy": 200_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": (now + timedelta(hours=2)).isoformat(),
        },
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T", "type": "buy", "quantity": 1, "limit_price": 3_500,
        "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "blocked"
    assert result["reasons"][0]["code"] == "cash_resource_future_dated"


def test_capacity_resolution_exposes_effective_wallet_without_bypassing_readiness(tmp_path):
    now = datetime(2026, 8, 13, 6, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 192_886,
            "available_to_trade_jpy": 192_886,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-08-07T11:55:00+09:00",
        },
    }), encoding="utf-8")
    action = {
        "ticker": "1489.T", "type": "buy", "quantity": 150, "limit_price": 3_509,
        "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }
    normal = evaluate_cash_buying_power(action, base_dir=tmp_path, now=now)
    capacity = resolve_cash_buying_capacity(action, base_dir=tmp_path, now=now)

    assert normal["readiness"] == "blocked"
    assert normal["reasons"][0]["code"] == "cash_balance_insufficient"
    assert capacity["readiness"] == "ready"
    assert capacity["effective_cash"] == 100_582
    assert capacity["cash_capacity_observation"]["scheduled_outflows"]["amount"] == 92_304


def test_readiness_reasons_include_scoped_fingerprint(tmp_path):
    now = datetime(2026, 8, 13, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1489.T")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE", "shares": 10_000,
            "available_to_trade_jpy": 10_000, "currency": "JPY",
            "balance_status": "confirmed", "reconciliation_required": False,
            "source_as_of": now.isoformat(),
        },
    }), encoding="utf-8")
    result = classify_execution_readiness({
        "ticker": "1489.T", "type": "buy", "quantity": 20, "limit_price": 3_500,
        "order_type": "limit", "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)
    reason = next(row for row in result["execution_block_reasons"] if row["code"] == "cash_balance_insufficient")
    assert reason["reason_scope"] == "wallet"
    assert reason["scope_key"].startswith("wallet:wife|sbi|nisa_growth|JPY")


def test_opposite_execution_reason_is_ticker_scoped():
    rows = annotate_reason_scopes(
        {"ticker": "XLF"},
        [{"code": "same_session_opposite_execution", "message": "recent opposite fill"}],
    )

    assert rows[0]["reason_scope"] == "ticker"
    assert rows[0]["scope_key"] == "ticker:XLF"


def test_claim_provenance_reason_remains_analysis_scoped():
    rows = annotate_reason_scopes(
        {"ticker": "XLF"},
        [{"code": "claim_provenance_unverified", "message": "stale parents"}],
    )

    assert rows[0]["reason_scope"] == "analysis"
    assert rows[0]["scope_key"] == "analysis:global"


def test_capacity_below_minimum_is_blocked_with_dedicated_reason(tmp_path):
    now = datetime(2026, 8, 14, 6, 15, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1306.T")
    result = classify_execution_readiness({
        "ticker": "1306.T", "type": "buy", "order_type": "limit",
        "limit_price": 3_500, "quantity": 150,
        "max_executable_quantity_below_minimum": True,
        "max_executable_quantity": 40,
        "minimum_executable_quantity": 50,
        "max_executable_notional_jpy": 140_000,
        "minimum_executable_notional_jpy": 175_000,
        "capacity_shortfall_jpy": 35_000,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "max_executable_quantity_below_minimum"
    )
    assert reason["capacity_shortfall_jpy"] == 35_000
    assert "必要余力差額" in reason["message"]


def test_live_ordered_buy_reserves_same_wallet_without_auto_release(tmp_path):
    now = datetime(2026, 8, 13, 6, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 200_000,
            "available_to_trade_jpy": 200_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-08-13T05:00:00+09:00",
        },
    }), encoding="utf-8")
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "live-buy", "status": "ordered", "ticker": "1489.T",
            "direction": "buy", "quantity": 20, "limit_price": 3_000,
            "execution_owner": "wife", "execution_broker": "sbi",
            "execution_account": "NISA成長投資枠",
            "ordered_at": "2026-07-29T09:00:00+09:00",
        }],
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T", "type": "buy", "quantity": 50, "limit_price": 3_000,
        "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "blocked"
    capacity = result["reasons"][0]["cash_capacity_observation"]
    assert capacity["active_order_reservations"]["reserved_cash"] == 60_000
    assert capacity["active_order_reservations"]["stale_reservation_count"] == 1
    assert capacity["effective_cash"] == 70_772

def test_cash_snapshot_is_invalidated_by_later_fill(tmp_path):
    now = datetime(2026, 7, 28, 9, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 50_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-07-27T09:00:00+09:00",
        },
    }), encoding="utf-8")
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "later-fill",
            "ticker": "1489.T",
            "status": "executed",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "execution_account": "NISA成長投資枠",
            "saved_at": "2026-07-27T12:00:00+09:00",
        }],
    }), encoding="utf-8")
    result = evaluate_cash_buying_power({
        "ticker": "1489.T",
        "type": "buy",
        "quantity": 1,
        "limit_price": 3_000,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "blocked"
    assert result["reasons"][0]["code"] == "cash_resource_snapshot_invalidated"


def test_complete_web_fill_advances_cash_authority_without_new_snapshot(tmp_path):
    now = datetime(2026, 7, 28, 9, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 47_000,
            "available_to_trade_jpy": 47_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-07-27T09:00:00+09:00",
        },
    }), encoding="utf-8")
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "web-fill", "ticker": "1489.T", "status": "executed",
            "direction": "buy", "account": "NISA成長投資枠",
            "execution_owner": "wife", "execution_broker": "sbi",
            "broker_confirmed_filled": True,
            "external_execution_id": "sbi-123",
            "broker_source": "web_manual_confirmation",
            "broker_reported_at": "2026-07-27T12:00:00+09:00",
            "filled_quantity": 1, "filled_price": 3_000,
            "reconciled_at": "2026-07-27T12:05:00+09:00",
            "reconciliation_snapshot_hash": "sha256:web",
            "portfolio_applied": True,
            "saved_at": "2026-07-27T12:05:00+09:00",
        }],
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T", "type": "buy", "quantity": 1,
        "limit_price": 3_000, "execution_owner": "wife",
        "execution_broker": "sbi", "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "ready"
    assert result["cash_resource_authority_source"] == "broker_confirmed_web_fill"
    assert result["cash_resource_as_of"] == "2026-07-27T12:05:00+09:00"


def test_usd_fill_does_not_advance_jpy_cash_authority(tmp_path):
    now = datetime(2026, 7, 28, 9, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE",
            "shares": 47_000,
            "available_to_trade_jpy": 47_000,
            "currency": "JPY",
            "balance_status": "confirmed",
            "reconciliation_required": False,
            "source_as_of": "2026-07-27T09:00:00+09:00",
        },
    }), encoding="utf-8")
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "usd-web-fill",
            "ticker": "XLF",
            "status": "executed",
            "direction": "buy",
            "currency": "USD",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "broker_confirmed_filled": True,
            "external_execution_id": "sbi-usd-123",
            "broker_source": "web_manual_confirmation",
            "broker_reported_at": "2026-07-27T12:00:00+09:00",
            "filled_quantity": 1,
            "filled_price": 20,
            "reconciled_at": "2026-07-27T12:05:00+09:00",
            "reconciliation_snapshot_hash": "sha256:web-usd",
            "portfolio_applied": True,
        }],
    }), encoding="utf-8")

    result = evaluate_cash_buying_power({
        "ticker": "1489.T",
        "type": "buy",
        "quantity": 1,
        "limit_price": 3_000,
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["readiness"] == "ready"
    assert result["cash_resource_authority_source"] == "cash_snapshot"
    assert result["cash_resource_as_of"] == "2026-07-27T09:00:00+09:00"


def test_exit_quantity_over_requested_account_inventory_is_blocked(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "amount_hint": "8株",
        "order_type": "limit",
        "limit_price": 486,
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 8,
        "holding_quantity_exceeds_account": True,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "holding_quantity_exceeds_account"
    )
    assert reason["available_quantity"] == 5
    assert reason["requested_quantity"] == 8
    assert reason["shortfall_quantity"] == 3


def test_exit_quantity_equal_to_requested_account_inventory_is_not_quantity_blocked(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "amount_hint": "5株",
        "order_type": "limit",
        "limit_price": 486,
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 5,
    }, base_dir=tmp_path, now=now)

    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert "holding_quantity_exceeds_account" not in codes
    assert "holding_quantity_unresolved" not in codes


def test_exit_route_text_conflict_blocks_current_avgo_case(tmp_path):
    now = datetime(2026, 7, 23, 6, 23, tzinfo=JST)
    _write_base(tmp_path, now, ticker="AVGO")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "last_updated": now.isoformat(),
        "AVGO_toku": {
            "ticker": "AVGO", "account": "特定", "shares": 5,
        },
        "AVGO_ippan": {
            "ticker": "AVGO", "account": "一般", "shares": 27,
        },
    }), encoding="utf-8")

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "action": "一般口座保有分（27株）から3株トリム（半導体集中是正・NISA分は保有継続）",
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 3,
        "order_type": "limit",
        "limit_price": 410.5,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "execution_route_text_conflict"
    )
    assert reason["conflict_type"] == "account"
    assert reason["execution_account"] == "特定"
    assert reason["text_account"] == "general"


def test_exit_route_text_matching_specific_account_stays_ready(tmp_path):
    now = datetime(2026, 7, 23, 6, 23, tzinfo=JST)
    _write_base(tmp_path, now, ticker="AVGO")

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "action": "特定口座から3株トリム",
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 3,
        "order_type": "limit",
        "limit_price": 410.5,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    assert any(row["code"] == "position_identity_unknown" for row in result["execution_block_reasons"])
    assert "execution_route_text_conflict" not in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_exit_route_without_account_words_does_not_false_positive(tmp_path):
    now = datetime(2026, 7, 23, 6, 23, tzinfo=JST)
    _write_base(tmp_path, now, ticker="AVGO")

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "action": "AVGOを3株トリム",
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 3,
        "order_type": "limit",
        "limit_price": 410.5,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    assert any(row["code"] == "position_identity_unknown" for row in result["execution_block_reasons"])


def test_nonexistent_nisa_holding_claim_is_blocked(tmp_path):
    now = datetime(2026, 7, 23, 6, 23, tzinfo=JST)
    _write_base(tmp_path, now, ticker="AVGO")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "last_updated": now.isoformat(),
        "AVGO_ippan": {
            "ticker": "AVGO", "account": "一般", "shares": 27,
        },
    }), encoding="utf-8")

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "action": "一般口座保有分（27株）から3株トリム。NISA分は保有継続",
        "execution_account": "一般",
        "execution_position_keys": ["AVGO_ippan"],
        "holding_shares_before": 27,
        "requested_sell_quantity": 3,
        "order_type": "limit",
        "limit_price": 410.5,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "execution_route_text_conflict"
    )
    assert reason["conflict_type"] == "nonexistent_nisa_holding"


def test_stated_holding_quantity_must_match_bound_account(tmp_path):
    now = datetime(2026, 7, 23, 6, 23, tzinfo=JST)
    _write_base(tmp_path, now, ticker="AVGO")

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "trim",
        "action": "特定口座保有分（27株）から3株トリム",
        "execution_account": "特定",
        "execution_position_keys": ["AVGO_toku"],
        "holding_shares_before": 5,
        "requested_sell_quantity": 3,
        "order_type": "limit",
        "limit_price": 410.5,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "execution_route_text_conflict"
    )
    assert reason["conflict_type"] == "holding_quantity"


def test_exit_without_resolved_inventory_fails_closed(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "sell",
        "amount_hint": "1株",
        "order_type": "limit",
        "limit_price": 486,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert any(
        row["code"] == "holding_quantity_unresolved"
        for row in result["execution_block_reasons"]
    )


@pytest.mark.parametrize(
    "text",
    [
        "残り50株のうち10株を売却",
        "1株ずつ計5株売却",
    ],
)
def test_exit_quantity_is_not_guessed_from_natural_language(tmp_path, text):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, ticker="AVGO")

    result = classify_execution_readiness({
        "ticker": "AVGO",
        "type": "sell",
        "action": text,
        "execution_account": "特定",
        "holding_shares_before": 3,
        "order_type": "limit",
        "limit_price": 486,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "holding_quantity_unresolved"
    )
    assert reason["requested_quantity"] is None


def test_unadjusted_price_series_blocks_buy(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1306.T")
    tech = json.loads((tmp_path / "technical_state.json").read_text(encoding="utf-8"))
    tech["tickers"]["1306.T"].update({
        "data_quality_status": "blocked",
        "data_quality_reasons": [{
            "code": "unadjusted_price_discontinuity",
            "date": "2026-03-30",
            "daily_change_pct": -90.16,
        }],
    })
    (tmp_path / "technical_state.json").write_text(json.dumps(tech), encoding="utf-8")

    result = classify_execution_readiness({
        "ticker": "1306.T", "type": "buy", "order_type": "limit", "limit_price": 418,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert "technical_data_degraded" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_execution_plan_would_filter_is_advisory_in_observe_mode(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "account.json").write_text(json.dumps({
        "last_updated": now.isoformat(), "usd_balance": 10_000,
        "fx_rate_usdjpy": 150,
    }), encoding="utf-8")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "XLF_fixture": {
            "ticker": "XLF", "owner": "husband", "broker": "楽天証券",
            "account": "一般", "note": "楽天CSV保有同期 2026-07-14",
        },
    }), encoding="utf-8")
    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit", "limit_price": 55,
        "quantity": 1, "execution_owner": "husband",
        "execution_broker": "rakuten", "execution_account": "一般",
        "execution_plan_would_filter": True,
    }, base_dir=tmp_path, now=now)
    assert result["execution_readiness"] == "ready"
    assert not any(row["code"] == "execution_plan_observe_conflict" for row in result["execution_block_reasons"])
    assert any(row["code"] == "execution_plan_observe_conflict" for row in result["execution_advisories"])


def test_derived_usd_notional_cannot_exceed_discretionary_budget(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "account.json").write_text(json.dumps({
        "last_updated": now.isoformat(), "usd_balance": 10_000,
        "fx_rate_usdjpy": 150,
    }), encoding="utf-8")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "XLF_fixture": {
            "ticker": "XLF", "owner": "husband", "broker": "楽天証券",
            "account": "一般", "note": "楽天CSV保有同期 2026-07-14",
        },
    }), encoding="utf-8")

    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit",
        "limit_price": 55, "quantity": 20,
        "execution_owner": "husband", "execution_broker": "rakuten",
        "execution_account": "一般",
    }, base_dir=tmp_path, now=now)

    funding_reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "approved_discretionary_funding_exceeded"
    )
    assert funding_reason["requested_notional_jpy"] == 165_000
    assert funding_reason["available_jpy"] == 100_000


def test_derived_notional_does_not_coerce_string_quantity(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit",
        "limit_price": 55, "quantity": "1",
    }, base_dir=tmp_path, now=now)

    assert "discretionary_funding_notional_unresolved" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_scheduled_contribution_flag_cannot_exempt_plain_buy(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "execution_plan_state.json").unlink()

    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit",
        "limit_price": 55, "quantity": 1, "scheduled_contribution": True,
    }, base_dir=tmp_path, now=now)

    assert "discretionary_funding_unresolved" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_exit_with_opposite_active_plan_is_review_not_blocked(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, ticker="XLF")
    result = classify_execution_readiness({
        "ticker": "XLF",
        "type": "trim",
        "amount_hint": "1株",
        "holding_shares_before": 80,
        "requested_sell_quantity": 1,
        "order_type": "limit",
        "limit_price": 55,
        "execution_plan_direction_conflict": True,
        "execution_plan_conflict_item_ids": ["2026-08-w32-add-financials-003"],
        "execution_plan_conflict_reason": "XLFの買付計画と売却候補が併存",
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "execution_plan_direction_conflict"
    )
    assert reason["plan_item_ids"] == ["2026-08-w32-add-financials-003"]


def test_same_session_opposite_execution_is_blocked(tmp_path):
    now = datetime(2026, 7, 16, 6, 9, tzinfo=JST)
    _write_base(tmp_path, now)
    result = classify_execution_readiness({
        "ticker": "XLF",
        "type": "buy",
        "order_type": "limit",
        "limit_price": 56,
        "recent_opposite_execution_guard": {
            "level": "blocked",
            "code": "same_session_opposite_execution",
            "message": "同一NYSEセッションに売却約定あり",
            "execution_id": "XLF_sell_20260716011043",
        },
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert any(
        row["code"] == "same_session_opposite_execution"
        for row in result["execution_block_reasons"]
    )


def test_raw_opposite_intent_and_cross_scope_never_return_ready(tmp_path):
    now = datetime(2026, 7, 16, 6, 9, tzinfo=JST)
    _write_base(tmp_path, now)
    result = classify_execution_readiness({
        "ticker": "XLF",
        "type": "sell",
        "amount_hint": "1株",
        "holding_shares_before": 80,
        "order_type": "limit",
        "limit_price": 56,
        "opposite_intent_conflict": True,
        "cross_scope_opposite_action": True,
        "requested_sell_quantity": 1,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert {"opposite_intent_conflict", "cross_scope_opposite_action"} <= codes


def test_risk_increasing_side_of_same_analysis_conflict_is_blocked(tmp_path):
    now = datetime(2026, 7, 16, 6, 9, tzinfo=JST)
    _write_base(tmp_path, now)

    result = classify_execution_readiness({
        "ticker": "XLF",
        "type": "buy",
        "order_type": "limit",
        "limit_price": 56,
        "opposite_intent_conflict": True,
        "cross_scope_opposite_action": True,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "opposite_intent_conflict"
    )
    assert reason["message"] == "同一分析に反対方向の売買意図が併存"


def test_generic_nisa_buy_is_blocked_when_route_is_unknown(tmp_path):
    now = datetime(2026, 7, 16, 6, 9, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1489.T")
    (tmp_path / "nisa_portfolio.json").write_text(json.dumps({
        "last_updated": "2026-07-15",
        "husband": {"broker": "楽天証券", "growth_limit_annual": 2_400_000},
        "wife": {"broker": "SBI証券", "growth_limit_annual": 2_400_000},
    }), encoding="utf-8")
    result = classify_execution_readiness({
        "ticker": "1489.T",
        "type": "buy",
        "amount_jpy": 100_000,
        "order_type": "limit",
        "limit_price": 3_300,
        "execution_account": "NISA成長投資枠",
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert any(
        row["code"] == "nisa_route_missing"
        for row in result["execution_block_reasons"]
    )


def test_old_portfolio_snapshot_does_not_expire_by_time_alone(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, snapshot_hours=80)
    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit", "limit_price": 55,
    }, base_dir=tmp_path, now=now)
    assert not any(
        row["code"].startswith("portfolio_snapshot_")
        for row in result["execution_block_reasons"]
    )


def test_legacy_ledgers_without_broker_snapshot_are_not_called_fresh(tmp_path):
    now = datetime(2026, 7, 17, 6, 0, tzinfo=JST)
    holdings_at = now - timedelta(hours=30)
    _write_base(tmp_path, now, snapshot_hours=30)
    (tmp_path / "broker_position_snapshot_fixture.json").unlink()
    (tmp_path / "account.json").write_text(
        json.dumps({"last_updated": (now - timedelta(hours=1)).isoformat()}),
        encoding="utf-8",
    )
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "XLF_sell",
            "status": "executed",
            "portfolio_applied": True,
            "portfolio_applied_at": (holdings_at + timedelta(seconds=1)).isoformat(),
        }],
    }), encoding="utf-8")

    health = portfolio_snapshot_health(tmp_path, now=now)

    assert health["status"] == "legacy_unverified"
    assert health["validation_mode"] == "event_based"
    assert health["legacy_ledgers_only"] is True


def test_aggregate_health_does_not_use_unscoped_execution_timestamp(tmp_path):
    now = datetime(2026, 7, 17, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, snapshot_hours=30)
    (tmp_path / "broker_position_snapshot_fixture.json").unlink()
    (tmp_path / "account.json").write_text(
        json.dumps({"last_updated": (now - timedelta(hours=1)).isoformat()}),
        encoding="utf-8",
    )
    (tmp_path / "action_executions.json").write_text(json.dumps({
        "executions": [{
            "id": "late_fill",
            "status": "executed",
            "portfolio_applied": True,
            "portfolio_applied_at": (now - timedelta(hours=2)).isoformat(),
        }],
    }), encoding="utf-8")

    health = portfolio_snapshot_health(tmp_path, now=now)

    assert health["status"] == "legacy_unverified"
    assert health["validation_mode"] == "event_based"


def test_date_only_snapshot_timestamp_uses_file_mtime(tmp_path):
    # NYSE regular session: avoid combining this freshness-only assertion with
    # the separate after-close reprice gate.
    now = datetime(2026, 7, 14, 23, 10, tzinfo=JST)
    account = tmp_path / "account.json"
    holdings = tmp_path / "holdings.json"
    account.write_text(json.dumps({"last_updated": "2026-07-14"}), encoding="utf-8")
    holdings.write_text(json.dumps({"last_updated": "2026-07-14"}), encoding="utf-8")
    (tmp_path / "broker_position_snapshot_fixture.json").write_text(json.dumps({
        "complete": True,
        "source_as_of": "2026-07-14",
        "positions": [],
    }), encoding="utf-8")
    imported_at = (now - timedelta(hours=7)).timestamp()
    os.utime(account, (imported_at, imported_at))
    os.utime(holdings, (imported_at, imported_at))
    (tmp_path / "technical_state.json").write_text(json.dumps({
        "tickers": {"XLF": {"freshness_status": "fresh", "data_as_of": "2026-07-14"}}
    }), encoding="utf-8")
    (tmp_path / "macro_event_state.json").write_text(json.dumps({
        "status": "ok", "refreshed_at": now.isoformat(), "events": []
    }), encoding="utf-8")
    (tmp_path / "execution_plan_state.json").write_text(
        json.dumps(_active_execution_plan(now)), encoding="utf-8"
    )

    result = classify_execution_readiness({
        "ticker": "XLF", "type": "buy", "order_type": "limit", "limit_price": 56,
    }, base_dir=tmp_path, now=now)

    assert not any(
        row["code"].startswith("portfolio_snapshot_")
        for row in result["execution_block_reasons"]
    )


def test_ambiguous_holding_scope_blocks_execution(tmp_path):
    now = datetime(2026, 7, 15, 6, 10, tzinfo=JST)
    _write_base(tmp_path, now)
    result = classify_execution_readiness({
        "ticker": "XLF", "type": "sell", "order_type": "limit", "limit_price": 56,
        "holding_scope_ambiguous": True,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert any(row["code"] == "holding_scope_ambiguous" for row in result["execution_block_reasons"])


def test_low_urgency_market_order_is_blocked(tmp_path):
    # spread判定は米国場中でのみ意味を持つ (場外は session_closed で spread不明)
    now = datetime(2026, 7, 14, 23, 30, tzinfo=JST)
    _write_base(tmp_path, now, ticker="ROBO")
    result = classify_execution_readiness({
        "ticker": "ROBO", "type": "sell", "urgency": "low", "order_type": "market",
        "decision_price": 82.96, "spread_bps": 408,
        "quote_bid": 81.30, "quote_ask": 84.70, "quote_as_of": now.isoformat(),
    }, base_dir=tmp_path, now=now)
    assert result["execution_readiness"] == "blocked"
    codes = {row["code"] for row in result["execution_block_reasons"]}
    assert "market_order_low_urgency" in codes
    assert "market_order_spread_too_wide" in codes


def test_limit_order_with_wide_spread_requires_review(tmp_path):
    # 同上: 広いspreadを「実際のコスト」として扱えるのは場中だけ
    now = datetime(2026, 7, 14, 23, 30, tzinfo=JST)
    _write_base(tmp_path, now, ticker="ROBO")
    result = classify_execution_readiness({
        "ticker": "ROBO", "type": "sell", "urgency": "low", "order_type": "limit",
        "limit_price": 82.96, "decision_price": 82.96, "spread_bps": 408,
        "quote_bid": 81.30, "quote_ask": 84.70, "quote_as_of": now.isoformat(),
        "amount_hint": "1株", "holding_shares_before": 10, "requested_sell_quantity": 1,
    }, base_dir=tmp_path, now=now)
    assert result["execution_readiness"] == "review"
    assert any(row["code"] == "limit_order_wide_spread_review" for row in result["execution_block_reasons"])


def test_nyse_sunday_morning_plan_waits_for_same_jst_day_open_without_blocking(tmp_path):
    # 2026-07-20 06:08 JST is Sunday afternoon in New York.
    now = datetime(2026, 7, 20, 6, 8, tzinfo=JST)
    _write_base(tmp_path, now, ticker="ROBO")

    result = classify_execution_readiness({
        "ticker": "ROBO", "type": "sell", "urgency": "low", "order_type": "limit",
        "limit_price": 80.0, "decision_price": 77.89,
        "amount_hint": "1株", "holding_shares_before": 10, "requested_sell_quantity": 1,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    assert any(row["code"] == "position_identity_unknown" for row in result["execution_block_reasons"])
    assert result["market_quote_confirmation_required"] is True
    assert result["expiry_starts_at"] == "2026-07-20T13:30:00+00:00"
    assert result["market_session"]["next_session_date"] == "2026-07-20"
    assert any(
        row["code"] == "market_quote_confirmation_required"
        for row in result["execution_advisories"]
    )


def test_nyse_after_close_plan_remains_ready_and_ttl_starts_at_next_open(tmp_path):
    # 2026-07-21 06:08 JST is 17:08 EDT, after the 7/20 NYSE close.
    now = datetime(2026, 7, 21, 6, 8, tzinfo=JST)
    _write_base(tmp_path, now, ticker="ROBO")

    result = classify_execution_readiness({
        "ticker": "ROBO", "type": "sell", "order_type": "limit", "limit_price": 80.0,
        "amount_hint": "1株", "holding_shares_before": 10, "requested_sell_quantity": 1,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    assert any(row["code"] == "position_identity_unknown" for row in result["execution_block_reasons"])
    assert result["market_session"]["status"] == "trading_day"
    assert result["market_session"]["reason"] == "after_regular_session"
    assert result["market_session"]["next_session_date"] == "2026-07-21"
    assert result["expiry_starts_at"] == "2026-07-21T13:30:00+00:00"
    assert result["market_quote_confirmation_required"] is True


def test_jpx_preopen_plan_is_ready_for_commute_order(tmp_path):
    now = datetime(2026, 7, 21, 6, 15, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1489.T")

    result = classify_execution_readiness({
        "ticker": "1489.T", "type": "sell", "order_type": "limit", "limit_price": 2_950,
        "amount_hint": "1口", "holding_shares_before": 10, "requested_sell_quantity": 1,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    assert any(row["code"] == "position_identity_unknown" for row in result["execution_block_reasons"])
    assert result["market_order_window"] == "before_regular_session"
    assert result["expiry_starts_at"] == "2026-07-21T00:00:00+00:00"
    assert result["expiry_ends_at"] == "2026-07-21T06:30:00+00:00"


def test_jpx_holiday_more_than_24h_before_open_requires_next_morning_analysis(tmp_path):
    now = datetime(2026, 7, 20, 6, 15, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1489.T")

    result = classify_execution_readiness({
        "ticker": "1489.T", "type": "sell", "order_type": "limit", "limit_price": 2_950,
        "amount_hint": "1口", "holding_shares_before": 10, "requested_sell_quantity": 1,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "review"
    assert result["market_reprice_required"] is True
    assert result["expiry_deferred_until_reprice"] is True


def test_fund_market_order_is_exempt_from_equity_spread_rule(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now, ticker="SLIM_SP500")
    (tmp_path / "execution_plan_state.json").unlink()
    result = classify_execution_readiness({
        "ticker": "SLIM_SP500", "type": "dca", "urgency": "low", "order_type": "market",
        "scheduled_contribution": True,
    }, base_dir=tmp_path, now=now)
    assert result["execution_readiness"] == "ready"
    assert not any(
        str(row.get("code") or "").startswith("discretionary_funding")
        for row in result["execution_block_reasons"]
    )


def test_observe_plan_conflict_is_advisory_while_other_guards_still_block(tmp_path):
    now = datetime(2026, 7, 14, 6, 8, tzinfo=JST)
    _write_base(tmp_path, now, ticker="4063.T")
    raw = json.loads((tmp_path / "technical_state.json").read_text(encoding="utf-8"))
    raw["tickers"]["ROBO"] = {"freshness_status": "fresh", "data_as_of": "2026-07-13"}
    (tmp_path / "technical_state.json").write_text(json.dumps(raw), encoding="utf-8")

    rows = [
        classify_execution_readiness({
            "ticker": "4063.T", "type": "buy", "urgency": "medium",
            "order_type": "market", "decision_price": 7199.0,
            "execution_plan_would_filter": True,
        }, base_dir=tmp_path, now=now),
        classify_execution_readiness({
            "ticker": "ROBO", "type": "sell", "urgency": "low",
            "order_type": "market", "decision_price": 82.96, "spread_bps": 408,
        }, base_dir=tmp_path, now=now),
    ]

    assert [row["execution_readiness"] for row in rows] == ["blocked", "blocked"]
    codes_4063 = {reason["code"] for reason in rows[0]["execution_block_reasons"]}
    codes_robo = {reason["code"] for reason in rows[1]["execution_block_reasons"]}
    assert "execution_plan_observe_conflict" not in codes_4063
    assert "execution_plan_observe_conflict" in {
        row["code"] for row in rows[0]["execution_advisories"]
    }
    assert "market_order_low_urgency" in codes_4063
    # ROBO は bid/ask を伴わない spread_bps だけを持つ。新しいクオート契約では
    # spread の裏付けが取れないので spread_too_wide では止めない。板から外す
    # 本質は低urgencyの成行禁止で、そこは変わらない。
    assert "market_order_low_urgency" in codes_robo
    assert "market_order_spread_too_wide" not in codes_robo
    assert sum(row["execution_readiness"] == "ready" for row in rows) == 0


def test_unverified_claim_provenance_is_advisory(tmp_path):
    now = datetime(2026, 7, 21, 6, 15, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1489.T")
    result = classify_execution_readiness({
        "ticker": "1489.T",
        "type": "sell",
        "order_type": "limit",
        "limit_price": 2_950,
        "amount_hint": "1口",
        "holding_shares_before": 10,
        "requested_sell_quantity": 1,
        "confidence_evidence_verified": False,
        "claim_ids": ["snapshot:action:1"],
        "unverified_numeric_claims": ["利上げ確率35.8%"],
    }, base_dir=tmp_path, now=now)
    assert "claim_provenance_unverified" not in {
        row["code"] for row in result["execution_block_reasons"]
    }
    reason = next(
        row for row in result["execution_advisories"]
        if row["code"] == "claim_provenance_unverified"
    )
    assert reason["claim_ids"] == ["snapshot:action:1"]


def test_capacity_resolution_reserves_explicit_wallet_schedule(tmp_path, monkeypatch):
    import contribution_schedule as schedule

    monkeypatch.setattr(schedule, "CONTRIBUTIONS", [{
        "id": "broker_cash_example", "label": "Example broker cash contribution",
        "amount": 25_000, "currency": "JPY", "cadence": "weekly",
        "weekday": 0, "owner": "wife", "broker": "sbi",
        "funding_source": "broker_cash", "cash_route": "CASH_JPY_SBI_WIFE",
    }])
    now = datetime(2026, 8, 13, 6, 0, tzinfo=JST)
    (tmp_path / "holdings.json").write_text(json.dumps({
        "CASH_JPY_SBI_WIFE": {
            "ticker": "CASH_JPY_SBI_WIFE", "shares": 200_000,
            "available_to_trade_jpy": 200_000, "currency": "JPY",
            "balance_status": "confirmed", "reconciliation_required": False,
            "source_as_of": "2026-08-07T11:55:00+09:00",
        },
    }), encoding="utf-8")
    action = {
        "ticker": "1306.T", "type": "buy", "quantity": 150, "limit_price": 3_500,
        "execution_owner": "wife", "execution_broker": "sbi",
        "execution_account": "NISA成長投資枠",
    }

    normal = evaluate_cash_buying_power(action, base_dir=tmp_path, now=now)
    capacity = resolve_cash_buying_capacity(action, base_dir=tmp_path, now=now)

    assert normal["readiness"] == "blocked"
    assert normal["reasons"][0]["code"] == "cash_balance_insufficient"
    assert capacity["readiness"] == "ready"
    assert capacity["effective_cash"] == 100_000
    assert capacity["cash_capacity_observation"]["scheduled_outflows"]["amount"] == 100_000


def test_reason_scope_distinguishes_candidate_and_analysis_failures():
    candidate = annotate_reason_scopes(
        {"ticker": "SPY"},
        [{"code": "same_session_opposite_execution", "message": "opposite fill"}],
    )[0]
    global_reason = annotate_reason_scopes(
        {"ticker": "SPY"},
        [{"code": "claim_provenance_unverified", "message": "stale parents"}],
    )[0]

    assert (candidate["reason_scope"], candidate["scope_key"]) == ("ticker", "ticker:SPY")
    assert (global_reason["reason_scope"], global_reason["scope_key"]) == (
        "analysis", "analysis:global",
    )


def test_capacity_below_minimum_is_blocked_with_dedicated_reason(tmp_path):
    now = datetime(2026, 8, 14, 6, 15, tzinfo=JST)
    _write_base(tmp_path, now, ticker="1306.T")
    result = classify_execution_readiness({
        "ticker": "1306.T", "type": "buy", "order_type": "limit",
        "limit_price": 3_500, "quantity": 150,
        "max_executable_quantity_below_minimum": True,
        "max_executable_quantity": 40,
        "minimum_executable_quantity": 50,
        "max_executable_notional_jpy": 140_000,
        "minimum_executable_notional_jpy": 175_000,
        "capacity_shortfall_jpy": 35_000,
    }, base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    reason = next(
        row for row in result["execution_block_reasons"]
        if row["code"] == "max_executable_quantity_below_minimum"
    )
    assert reason["capacity_shortfall_jpy"] == 35_000
    assert "必要余力差額" in reason["message"]


# ---------------------------------------------------------------------------
# 実行内テクニカル補完 (technical_signals.ensure_technical_coverage) が
# 書いた行に対するゲートの振る舞い。
#
# 補完の目的は「取得の網羅漏れによる誤検知」だけを消すことで、ゲートの強度は
# 一切下げない。以下は下げていないことの検証。
# ---------------------------------------------------------------------------

def _write_technical(tmp_path, ticker, **fields):
    row = {
        "price": 210.0,
        "data_as_of": "2026-07-13",
        "freshness_status": "fresh",
        "data_quality_status": "ok",
    }
    row.update(fields)
    (tmp_path / "technical_state.json").write_text(
        json.dumps({"tickers": {ticker: row}}), encoding="utf-8")


def _technical_codes(result):
    return {row["code"] for row in result["execution_block_reasons"]
            if str(row.get("code", "")).startswith("technical_")}


def _buy(ticker="JPM"):
    return {"ticker": ticker, "type": "buy", "order_type": "limit", "limit_price": 210}


def test_a_topped_up_row_clears_technical_data_missing(tmp_path):
    """補完が入る前は必ず blocked、入った後は技術的理由が消えること。"""
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)  # XLF の行だけがある = JPM は行なし

    before = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
    assert "technical_data_missing" in _technical_codes(before)

    _write_technical(tmp_path, "JPM", coverage_source="topup",
                     coverage_added_at=now.isoformat())
    after = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    assert _technical_codes(after) == set()


def test_a_topped_up_row_with_blocked_quality_is_still_blocked(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "JPM", coverage_source="topup",
                     data_quality_status="blocked",
                     data_quality_reasons=[{"code": "unadjusted_price_discontinuity"}])

    result = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert "technical_data_degraded" in _technical_codes(result)


def test_a_topped_up_row_that_is_stale_is_still_blocked(tmp_path):
    """古い parquet しか無い銘柄は補完しても blocked のまま。"""
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "JPM", coverage_source="topup", freshness_status="stale")

    result = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    assert result["execution_readiness"] == "blocked"
    assert "technical_data_stale" in _technical_codes(result)


def test_a_carried_forward_row_the_rebuild_could_not_refetch_is_not_ready(tmp_path):
    """引き継がれた行の freshness_status は補完時点の値で凍結されている。

    全再計算がその銘柄を取得できなくても、行が引き継がれる限り
    freshness_status="fresh" のまま残るので、それだけを見ると ready へ
    進んでしまう (Codex レビュー round 6 で再現した fail-open)。
    rebuild_unresolved が付いた行は、取得できていない事実そのものを理由に
    最低でも review へ落ちること。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    # 印が無ければ (通常の補完行) 技術的理由は出ない = 対照群。
    _write_technical(tmp_path, "JPM", coverage_source="topup")
    clean = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
    assert _technical_codes(clean) == set()

    # 同じ "fresh" な行に印だけを足すと review へ落ちる。
    _write_technical(tmp_path, "JPM", coverage_source="topup",
                     rebuild_unresolved=True,
                     rebuild_unresolved_at=now.isoformat())
    result = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    assert result["execution_readiness"] != "ready", (
        "直近の全再計算で取得できていない行が ready になった"
    )
    assert "technical_rebuild_unresolved" in {
        row["code"] for row in result["execution_block_reasons"]
    }


def test_the_unresolved_level_rule_maps_lag_to_review_or_blocked():
    """review/blocked の対応そのものを純関数で固定する。

    以前は classify_execution_readiness の総合判定で検証しようとしていたが、
    最小 fixture は position/cash の identity を満たさず総合が常に blocked に
    なるため、review と blocked を逆転させてもテストが通ってしまった
    (Codex レビュー round 9)。判定関数を直接見る。
    """
    from execution_readiness import unresolved_row_level

    assert unresolved_row_level("fresh") == "review"
    assert unresolved_row_level("degraded") == "review"
    assert unresolved_row_level("stale") == "blocked"
    assert unresolved_row_level("unknown") == "blocked"

    # ⚠️ 既知4値だけでは不足。旧実装は `in {"stale","unknown"}` の否定形で、
    # この4値に対しては同じ答えを返しつつ、None・空文字・未知の値・将来
    # 増える値をすべて review へ fail-open していた (Codex レビュー round 10)。
    # 許可リストであることを、それらの値で固定する。
    for unexpected in (None, "", "weird", "FRESH", "degraded ", 0, [], {}):
        assert unresolved_row_level(unexpected) == "blocked", (
            f"未知の鮮度値 {unexpected!r} が review へ fail-open した"
        )


def test_an_unresolved_row_is_judged_on_recomputed_lag_not_the_frozen_label(tmp_path):
    """保存済みの freshness_status を信じず、data_as_of からラグを引き直す
    こと (Codex レビュー round 7)。上の純関数テストがレベル対応を固定して
    いるので、ここではラグの引き直しが実際に行われているかだけを見る。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    def _recomputed(data_as_of):
        _write_technical(tmp_path, "JPM", coverage_source="topup",
                         data_as_of=data_as_of, freshness_status="fresh",
                         rebuild_unresolved=True)
        result = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
        rows = [r for r in result["execution_block_reasons"]
                if r["code"] == "technical_rebuild_unresolved"]
        assert rows, f"data_as_of={data_as_of} で未解決の理由が出ていない"
        return rows[0]["recomputed_freshness"]

    # 保存済みラベルは3ケースとも "fresh"。差は data_as_of だけ。
    assert _recomputed("2026-07-13") == "fresh"
    assert _recomputed("2026-07-10") == "degraded"
    assert _recomputed("2026-06-01") == "stale", (
        "数週間前の行が、保存済みラベル fresh のまま素通りした"
    )


def test_a_row_with_missing_or_unknown_quality_is_blocked(tmp_path):
    """品質フィールドの欠損・未知値を "ok" へ昇格させないこと。

    以前は `str(tech.get("data_quality_status") or "ok")` としており、
    共通契約が unusable とする行をテクニカル安全ゲートが素通ししていた
    (Codex レビュー round 10 で「missing_quality -> technical理由なし」を再現)。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    # 対照群: ok + fresh なら技術的理由は出ない。
    _write_technical(tmp_path, "JPM", data_quality_status="ok",
                     freshness_status="fresh")
    assert _technical_codes(
        classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)) == set()

    # 品質フィールドが無い行。
    row = {"price": 210.0, "data_as_of": "2026-07-13", "freshness_status": "fresh"}
    (tmp_path / "technical_state.json").write_text(
        json.dumps({"tickers": {"JPM": row}}), encoding="utf-8")
    missing = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
    assert "technical_data_quality_unknown" in _technical_codes(missing)

    # 未知値も同じ。
    _write_technical(tmp_path, "JPM", data_quality_status="weird",
                     freshness_status="fresh")
    weird = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
    assert "technical_data_quality_unknown" in _technical_codes(weird)


def test_a_row_with_missing_freshness_is_blocked(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    row = {"price": 210.0, "data_as_of": "2026-07-13", "data_quality_status": "ok"}
    (tmp_path / "technical_state.json").write_text(
        json.dumps({"tickers": {"JPM": row}}), encoding="utf-8")

    result = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    assert "technical_data_stale" in _technical_codes(result)


def test_the_caller_actually_applies_the_unresolved_level_rule(monkeypatch, tmp_path):
    """純関数を固定するだけでは、caller がその戻り値を使っているかまでは
    保証されない (Codex レビュー round 10)。関数を差し替えて、readiness に
    その値が反映されることを確認する。
    """
    import execution_readiness as er

    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    # ラグ的には fresh (通常なら review) な引き継ぎ行。
    _write_technical(tmp_path, "JPM", coverage_source="topup",
                     data_as_of="2026-07-13", freshness_status="fresh",
                     rebuild_unresolved=True)

    monkeypatch.setattr(er, "unresolved_row_level", lambda _f: "blocked")
    forced = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
    assert forced["execution_readiness"] == "blocked"

    # 逆向きにも効くことを見る: この fixture 由来の blocked 理由が無い
    # 状態を作れないので、理由行が付くこと自体で適用を確認する。
    monkeypatch.setattr(er, "unresolved_row_level", lambda _f: "review")
    relaxed = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)
    assert "technical_rebuild_unresolved" in {
        r["code"] for r in relaxed["execution_block_reasons"]
    }


def _technical_levels(result):
    """technical_* 理由が実際に付与されたレベル。

    総合 readiness だけでは、他の理由が先に blocked を立てていると個別
    理由の寄与レベルが観測できず、レベル取り違えのバグをテストが素通しする
    (Codex レビュー round 11)。add() が行へ残す level を直接見る。
    """
    return {row["code"]: row.get("level")
            for row in result["execution_block_reasons"]
            if str(row.get("code", "")).startswith("technical_")}


def _worst(levels):
    if "blocked" in levels.values():
        return "blocked"
    return "review" if "review" in levels.values() else "ready"


def test_quality_and_freshness_axes_are_evaluated_independently(tmp_path):
    """rebuild_unresolved が独立した品質 block を無効化しないこと。

    「corporate action で blocked → 次の再計算が取得失敗 → その行が
    carry-forward」は実際に起きうる並びで、以前は品質軸ごとスキップして
    いたため review だけになっていた (Codex レビュー round 11 で再現)。
    引き継ぎ行で無視してよいのは保存済みの freshness_status だけ。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)

    def _judge(**fields):
        _write_base(tmp_path, now)
        row = {"price": 210.0,
               "data_as_of": fields.pop("data_as_of", "2026-07-13"), **fields}
        (tmp_path / "technical_state.json").write_text(
            json.dumps({"tickers": {"JPM": row}}), encoding="utf-8")
        return _technical_levels(
            classify_execution_readiness(_buy(), base_dir=tmp_path, now=now))

    # blocked + unresolved/fresh -> blocked (品質軸が生きている)
    levels = _judge(data_quality_status="blocked", freshness_status="fresh",
                    rebuild_unresolved=True)
    assert levels.get("technical_data_degraded") == "blocked"
    assert _worst(levels) == "blocked"

    # unknown + unresolved/fresh -> blocked
    levels = _judge(freshness_status="fresh", rebuild_unresolved=True)
    assert levels.get("technical_data_quality_unknown") == "blocked"
    assert _worst(levels) == "blocked"

    # ok + unresolved/fresh -> review (引き継ぎのみ)
    levels = _judge(data_quality_status="ok", freshness_status="fresh",
                    rebuild_unresolved=True)
    assert levels == {"technical_rebuild_unresolved": "review"}

    # ok + unresolved/stale -> blocked (現在ラグで引き直した結果)
    levels = _judge(data_quality_status="ok", freshness_status="fresh",
                    rebuild_unresolved=True, data_as_of="2026-06-01")
    assert levels.get("technical_rebuild_unresolved") == "blocked"


def test_the_unresolved_reason_carries_the_level_the_rule_returned(monkeypatch, tmp_path):
    """caller が unresolved_row_level の戻り値を実際に適用していること。

    round 10 で書いたテストは総合 readiness と理由コードの存在しか見て
    おらず、本体を `_level = "review"` に固定するミューテーションが全テストを
    通過した (Codex レビュー round 11)。理由行に残る level を直接見る。
    """
    import execution_readiness as er

    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "JPM", coverage_source="topup",
                     data_as_of="2026-07-13", freshness_status="fresh",
                     data_quality_status="ok", rebuild_unresolved=True)

    # 素の実装ではラグ 0 = fresh なので review。
    baseline = _technical_levels(
        classify_execution_readiness(_buy(), base_dir=tmp_path, now=now))
    assert baseline["technical_rebuild_unresolved"] == "review"

    # 判定関数を差し替えると、理由行のレベルが追従すること。
    monkeypatch.setattr(er, "unresolved_row_level", lambda _f: "blocked")
    forced = _technical_levels(
        classify_execution_readiness(_buy(), base_dir=tmp_path, now=now))
    assert forced["technical_rebuild_unresolved"] == "blocked", (
        "caller が unresolved_row_level の戻り値を無視している"
    )


def test_the_coverage_marker_does_not_change_the_verdict(tmp_path):
    """AI 提案であること自体にペナルティも優遇も設けない (方針①)。

    マーカーは監査専用で、判定は朝の再計算が作った行と1ビットも変わらない。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    _write_technical(tmp_path, "JPM")
    without = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    _write_technical(tmp_path, "JPM", coverage_source="topup",
                     coverage_added_at=now.isoformat())
    with_marker = classify_execution_readiness(_buy(), base_dir=tmp_path, now=now)

    assert with_marker == without


# ---------------------------------------------------------------------------
# first_time_symbol: 初回銘柄の非ブロック助言。
#
# ready から発注板までの経路に流動性・出来高・売買単位の検証は存在しない。
# 一度も保有せずスクリーナーも通っていない銘柄は、単元も板の厚みも
# 本システムが検証していないまま ready に到達しうる (2026-08-24 に VT が
# 実際にそうなった)。readiness は変えずに、発注する人間へ出すだけにする。
# ---------------------------------------------------------------------------

def _advisory_codes(result):
    return {row["code"] for row in result["execution_advisories"]}


def test_a_symbol_with_no_holding_and_no_screener_pass_is_flagged(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "MDB")

    result = classify_execution_readiness(
        {"ticker": "MDB", "type": "buy", "order_type": "limit", "limit_price": 210},
        base_dir=tmp_path, now=now,
    )

    assert "first_time_symbol" in _advisory_codes(result)
    row = next(r for r in result["execution_advisories"] if r["code"] == "first_time_symbol")
    assert row["trading_unit_assumed"] == 1


def test_a_held_symbol_is_not_flagged(tmp_path):
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    (tmp_path / "holdings.json").write_text(
        json.dumps({"XLF_NISA": {"ticker": "XLF", "shares": 10}}), encoding="utf-8")

    result = classify_execution_readiness(
        {"ticker": "XLF", "type": "buy", "order_type": "limit", "limit_price": 55},
        base_dir=tmp_path, now=now,
    )

    assert "first_time_symbol" not in _advisory_codes(result)


def test_a_screener_candidate_is_not_flagged(tmp_path):
    """screener.py の ADV・価格フィルタを通っていれば流動性の裏付けがある。"""
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "MRNA")
    (tmp_path / "screen_results.json").write_text(
        json.dumps({"candidates": [{"ticker": "MRNA"}]}), encoding="utf-8")

    result = classify_execution_readiness(
        {"ticker": "MRNA", "type": "buy", "order_type": "limit", "limit_price": 210},
        base_dir=tmp_path, now=now,
    )

    assert "first_time_symbol" not in _advisory_codes(result)


def test_the_proposal_registry_is_not_liquidity_evidence(tmp_path):
    """本題: レジストリ登録を裏付けとして数えないこと。

    proposed_ticker_candidates.json は「テクニカルを取りに行った銘柄」の
    記録であって、ADV や板の厚みを一度も見ていない。これを裏付けに数えると
    first_time_symbol が狙った銘柄でだけ黙る (補完が動いた翌日から
    永久に警告が出なくなる)。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "VT")
    (tmp_path / "proposed_ticker_candidates.json").write_text(
        json.dumps({"version": 1, "candidates": [{"ticker": "VT"}]}), encoding="utf-8")

    result = classify_execution_readiness(
        {"ticker": "VT", "type": "buy", "order_type": "limit", "limit_price": 160.77},
        base_dir=tmp_path, now=now,
    )

    assert "first_time_symbol" in _advisory_codes(result)


def test_the_advisory_never_changes_readiness(tmp_path):
    """advisories は reasons と別リストで _merge を通らない。

    「AI提案であること自体にペナルティを設けない」方針の実行可能な形。
    """
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "MDB")
    action = {"ticker": "MDB", "type": "buy", "order_type": "limit", "limit_price": 210}

    flagged = classify_execution_readiness(action, base_dir=tmp_path, now=now)
    assert "first_time_symbol" in _advisory_codes(flagged)

    # 同じ銘柄をスクリーナー通過済みにすると助言だけが消え、判定は不変。
    (tmp_path / "screen_results.json").write_text(
        json.dumps({"candidates": [{"ticker": "MDB"}]}), encoding="utf-8")
    unflagged = classify_execution_readiness(action, base_dir=tmp_path, now=now)

    assert "first_time_symbol" not in _advisory_codes(unflagged)
    assert flagged["execution_readiness"] == unflagged["execution_readiness"]
    assert flagged["execution_block_reasons"] == unflagged["execution_block_reasons"]


def test_a_sell_is_not_flagged(tmp_path):
    """リスク非増加の売りに初回警告は要らない。"""
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)

    result = classify_execution_readiness(
        {"ticker": "MDB", "type": "sell", "order_type": "limit", "limit_price": 210},
        base_dir=tmp_path, now=now,
    )

    assert "first_time_symbol" not in _advisory_codes(result)


def test_an_unlisted_jpx_symbol_surfaces_the_assumed_unit(tmp_path):
    """未登録の .T は 100 が既定で返る。1489.T=1 / 1306.T=10 なので誤りうる。"""
    now = datetime(2026, 7, 14, 6, 0, tzinfo=JST)
    _write_base(tmp_path, now)
    _write_technical(tmp_path, "7203.T")

    result = classify_execution_readiness(
        {"ticker": "7203.T", "type": "buy", "order_type": "limit", "limit_price": 3100},
        base_dir=tmp_path, now=now,
    )

    row = next(r for r in result["execution_advisories"] if r["code"] == "first_time_symbol")
    assert row["trading_unit_assumed"] == 100


def test_liquidity_evidence_files_stay_aligned_with_the_universe_sources():
    """名前を複製している以上、乖離を検出できるようにしておく。

    technical_signals 側に screener レーンが増えたのにこちらへ追随しないと、
    その銘柄が永久に初回扱いになる。逆にレジストリを取り込むと警告が黙る。
    """
    import execution_readiness as er
    import technical_signals as ts

    universe = set(ts.CANDIDATE_UNIVERSE_FILES)
    evidence = set(er._LIQUIDITY_EVIDENCE_FILES)

    assert evidence <= universe
    # 意図的に除外しているのはレジストリだけ。
    assert universe - evidence == {"proposed_ticker_candidates.json"}
