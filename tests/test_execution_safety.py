from __future__ import annotations

from datetime import datetime
import json
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal

from execution_safety import (
    classify_recent_opposite_execution,
    enrich_action_routing,
    evaluate_nisa_capacity,
    exchange_session,
    execution_expiry_at,
    market_session_context,
)


JST = ZoneInfo("Asia/Tokyo")


def _write_nisa_base(tmp_path, *, last_updated: str = "2026-06-10") -> None:
    (tmp_path / "nisa_portfolio.json").write_text(json.dumps({
        "last_updated": last_updated,
        "husband": {
            "broker": "楽天証券",
            "growth_limit_annual": 2_400_000,
            "growth_used_this_year": 1_000_000,
            "growth_planned_this_year": 100_000,
        },
        "wife": {
            "broker": "SBI証券",
            "growth_limit_annual": 2_400_000,
            "growth_used_this_year": 1_000_000,
            "growth_planned_this_year": 100_000,
        },
    }, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "account.json").write_text(
        json.dumps({"fx_rate_usdjpy": 150}), encoding="utf-8"
    )
    (tmp_path / "action_state.json").write_text(
        json.dumps({"actions": {}}), encoding="utf-8"
    )
    (tmp_path / "action_executions.json").write_text(
        json.dumps({"executions": []}), encoding="utf-8"
    )


def _nisa_action(**overrides) -> dict:
    return {
        "ticker": "1489.T",
        "type": "buy",
        "execution_account": "NISA成長投資枠",
        "execution_owner": "wife",
        "execution_broker": "sbi",
        "amount_jpy": 100_000,
        **overrides,
    }


def test_xlf_replay_blocks_opposite_fill_in_same_nyse_local_session() -> None:
    result = classify_recent_opposite_execution(
        {"ticker": "XLF", "type": "buy"},
        [{
            "id": "XLF_sell_20260716011043",
            "ticker": "XLF",
            "direction": "sell",
            "status": "executed",
            "saved_at": "2026-07-16T01:10:43",
        }],
        now=datetime(2026, 7, 16, 6, 9, 26, tzinfo=JST),
    )

    assert result is not None
    assert result["level"] == "blocked"
    assert result["code"] == "same_session_opposite_execution"
    assert result["timestamp_source"] == "saved_at"
    assert result["execution_session_date"] == "2026-07-15"
    assert result["current_session_date"] == "2026-07-15"


def test_executed_at_time_precedes_saved_at_for_session_attribution() -> None:
    result = classify_recent_opposite_execution(
        {"ticker": "XLF", "type": "buy"},
        [{
            "id": "fill-1",
            "ticker": "XLF",
            "direction": "sell",
            "status": "filled",
            "executed_at_time": "2026-07-16T01:10:43",
            "saved_at": "2026-07-01T01:10:43",
        }],
        now=datetime(2026, 7, 16, 6, 9, 26, tzinfo=JST),
    )

    assert result is not None
    assert result["code"] == "same_session_opposite_execution"
    assert result["timestamp_source"] == "executed_at_time"


def test_opposite_fill_on_previous_nyse_session_requires_review() -> None:
    result = classify_recent_opposite_execution(
        {"ticker": "XLF", "type": "buy"},
        [{
            "id": "fill-1",
            "ticker": "XLF",
            "direction": "sell",
            "status": "done",
            "saved_at": "2026-07-16T01:10:43",
        }],
        now=datetime(2026, 7, 17, 6, 9, 26, tzinfo=JST),
    )

    assert result is not None
    assert result["level"] == "review"
    assert result["code"] == "recent_opposite_execution"
    assert result["session_age_calendar_days"] == 1


def test_same_session_opposite_fill_for_other_owner_is_review_only() -> None:
    result = classify_recent_opposite_execution(
        {
            "ticker": "1489.T",
            "type": "buy",
            "execution_owner": "wife",
        },
        [{
            "id": "fill-1",
            "ticker": "1489.T",
            "direction": "sell",
            "status": "executed",
            "execution_owner": "husband",
            "saved_at": "2026-07-16T10:00:00",
        }],
        now=datetime(2026, 7, 16, 15, 0, tzinfo=JST),
    )

    assert result is not None
    assert result["level"] == "review"
    assert result["code"] == "cross_owner_opposite_action"


def test_nyse_post_close_timestamp_stays_on_same_local_calendar_session() -> None:
    result = exchange_session("XLF", datetime(2026, 7, 16, 6, 9, tzinfo=JST))

    assert result["status"] == "resolved"
    assert result["session_date"] == "2026-07-15"


def test_calendar_exception_degrades_to_unresolved(monkeypatch) -> None:
    def _raise(_exchange):
        raise RuntimeError("calendar unavailable")

    monkeypatch.setattr(mcal, "get_calendar", _raise)

    result = exchange_session("XLF", datetime(2026, 7, 16, 6, 9, tzinfo=JST))

    assert result["status"] == "unresolved"
    assert result["reason"] == "calendar_error:RuntimeError"


def test_generic_nisa_label_does_not_infer_husband(tmp_path) -> None:
    _write_nisa_base(tmp_path)

    generic = enrich_action_routing(
        {"ticker": "XLF", "type": "buy", "action": "NISA成長投資枠で買付"},
        base_dir=tmp_path,
    )
    wife = enrich_action_routing(
        {"ticker": "1489.T", "type": "buy", "action": "妻NISA成長投資枠で買付"},
        base_dir=tmp_path,
    )

    assert generic["execution_account"] == "NISA成長投資枠"
    assert "execution_owner" not in generic
    assert "execution_broker" not in generic
    assert wife["execution_owner"] == "wife"
    assert wife["execution_broker"] == "sbi"


def test_explicit_wife_nisa_route_normalizes_conflicting_model_broker(tmp_path) -> None:
    _write_nisa_base(tmp_path)

    action = enrich_action_routing({
        "ticker": "1489.T",
        "type": "buy",
        "execution_account": "NISA成長投資枠",
        "execution_owner": "wife",
        "execution_broker": "rakuten",
    }, base_dir=tmp_path)

    assert action["execution_owner"] == "wife"
    assert action["execution_broker"] == "sbi"
    assert action["routing_normalized"] is True
    assert action["routing_model_broker"] == "rakuten"


def test_jpx_holiday_requires_next_session_reprice() -> None:
    context = market_session_context("1489.T", datetime(2026, 7, 20, 6, 8, tzinfo=JST))

    assert context["status"] == "closed"
    assert context["exchange"] == "JPX"
    assert context["local_date"] == "2026-07-20"
    assert context["next_session_date"] == "2026-07-21"


def test_jpx_lunch_break_waits_for_afternoon_session() -> None:
    context = market_session_context("1489.T", datetime(2026, 7, 21, 12, 0, tzinfo=JST))

    assert context["status"] == "trading_day"
    assert context["session_state"] == "closed"
    assert context["reason"] == "between_regular_sessions"
    assert context["next_market_open"] == "2026-07-21T03:30:00+00:00"


def test_execution_expiry_starts_at_market_open_instead_of_analysis_time() -> None:
    expiry = execution_expiry_at({
        "recommended_at": "2026-07-21T06:15:00",
        "expiry_starts_at": "2026-07-21T13:30:00+00:00",
        "expiry_minutes": 120,
    })

    assert expiry is not None
    assert expiry.isoformat() == "2026-07-21T15:30:00+00:00"


def test_execution_expiry_is_capped_at_session_close() -> None:
    expiry = execution_expiry_at({
        "recommended_at": "2026-07-21T06:15:00",
        "expiry_starts_at": "2026-07-21T00:00:00+00:00",
        "expiry_ends_at": "2026-07-21T06:30:00+00:00",
        "expiry_minutes": 720,
    })

    assert expiry is not None
    assert expiry.isoformat() == "2026-07-21T06:30:00+00:00"


def test_nisa_capacity_counts_only_activity_strictly_after_date_baseline(tmp_path) -> None:
    _write_nisa_base(tmp_path)
    (tmp_path / "action_executions.json").write_text(json.dumps({"executions": [
        {
            "id": "same-day",
            "ticker": "1489.T",
            "direction": "buy",
            "status": "executed",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "notional_jpy": 200_000,
            "saved_at": "2026-06-10T15:00:00",
        },
        {
            "id": "after-day",
            "ticker": "1489.T",
            "direction": "buy",
            "status": "executed",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "notional_jpy": 100_000,
            "saved_at": "2026-06-11T10:00:00",
        },
    ]}), encoding="utf-8")

    result = evaluate_nisa_capacity(
        _nisa_action(),
        base_dir=tmp_path,
        now=datetime(2026, 6, 20, 6, 0, tzinfo=JST),
    )

    assert result["readiness"] == "ready"
    assert result["nisa_capacity_remaining_jpy"] == 1_200_000


def test_nisa_capacity_datetime_baseline_is_strictly_after_not_at(tmp_path) -> None:
    _write_nisa_base(tmp_path, last_updated="2026-06-10T12:00:00+09:00")
    (tmp_path / "action_executions.json").write_text(json.dumps({"executions": [
        {
            "id": "at-baseline",
            "ticker": "1489.T",
            "direction": "buy",
            "status": "executed",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "notional_jpy": 200_000,
            "saved_at": "2026-06-10T12:00:00+09:00",
        },
        {
            "id": "after-baseline",
            "ticker": "1489.T",
            "direction": "buy",
            "status": "executed",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "notional_jpy": 100_000,
            "saved_at": "2026-06-10T12:00:01+09:00",
        },
    ]}), encoding="utf-8")

    result = evaluate_nisa_capacity(
        _nisa_action(),
        base_dir=tmp_path,
        now=datetime(2026, 6, 20, 6, 0, tzinfo=JST),
    )

    assert result["nisa_capacity_remaining_jpy"] == 1_200_000


def test_nisa_capacity_stale_baseline_requires_review(tmp_path) -> None:
    _write_nisa_base(tmp_path, last_updated="2026-05-01")

    result = evaluate_nisa_capacity(
        _nisa_action(),
        base_dir=tmp_path,
        now=datetime(2026, 6, 20, 6, 0, tzinfo=JST),
    )

    assert result["readiness"] == "review"
    assert any(reason["code"] == "nisa_capacity_stale" for reason in result["reasons"])


def test_nisa_capacity_insufficient_or_unattributed_activity_blocks(tmp_path) -> None:
    _write_nisa_base(tmp_path)
    (tmp_path / "action_executions.json").write_text(json.dumps({"executions": [{
        "id": "unknown-owner",
        "ticker": "XLF",
        "direction": "buy",
        "status": "executed",
        "account": "NISA成長投資枠",
        "notional_jpy": 100_000,
        "saved_at": "2026-06-11T10:00:00",
    }]}), encoding="utf-8")

    unattributed = evaluate_nisa_capacity(
        _nisa_action(),
        base_dir=tmp_path,
        now=datetime(2026, 6, 20, 6, 0, tzinfo=JST),
    )
    insufficient = evaluate_nisa_capacity(
        _nisa_action(amount_jpy=1_400_000),
        base_dir=tmp_path,
        now=datetime(2026, 6, 20, 6, 0, tzinfo=JST),
    )

    assert unattributed["readiness"] == "blocked"
    assert any(
        reason["code"] == "nisa_capacity_unattributed_activity"
        for reason in unattributed["reasons"]
    )
    assert insufficient["readiness"] == "blocked"
    assert any(
        reason["code"] == "nisa_capacity_insufficient"
        for reason in insufficient["reasons"]
    )


def test_cancelled_order_is_not_reserved_in_nisa_capacity(tmp_path) -> None:
    _write_nisa_base(tmp_path)
    (tmp_path / "action_executions.json").write_text(json.dumps({"executions": [
        {
            "id": "order-1",
            "action_state_id": "state-1",
            "ticker": "1489.T",
            "direction": "buy",
            "status": "ordered",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "notional_jpy": 500_000,
            "saved_at": "2026-06-11T10:00:00",
        },
        {
            "id": "cancel-1",
            "action_state_id": "state-1",
            "ticker": "1489.T",
            "direction": "buy",
            "status": "cancelled",
            "account": "NISA成長投資枠",
            "execution_owner": "wife",
            "execution_broker": "sbi",
            "saved_at": "2026-06-12T10:00:00",
        },
    ]}), encoding="utf-8")

    result = evaluate_nisa_capacity(
        _nisa_action(),
        base_dir=tmp_path,
        now=datetime(2026, 6, 20, 6, 0, tzinfo=JST),
    )

    assert result["readiness"] == "ready"
    assert result["nisa_capacity_remaining_jpy"] == 1_300_000
