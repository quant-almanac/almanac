from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import execution_plan_engine as epe
from action_state_tracker import dedup_key, dedup_key_for_action


def _plan_item(
    *,
    plan_item_id: str = "2026-07-w28-usd-core-001",
    budget: int = 200_000,
    ticker: str = "META",
    action_type: str = "buy",
    account_bucket: str = "default",
) -> dict:
    return {
        "plan_item_id": plan_item_id,
        "horizon": "weekly",
        "objective": "add_usd_core",
        "priority": 1,
        "status": "active",
        "normal_budget_jpy": budget,
        "consumed_jpy": 0,
        "open_order_consumed_jpy": 0,
        "filled_consumed_jpy": 0,
        "remaining_jpy": budget,
        "budget_bucket": "normal",
        "allowed_action_types": ["buy", "add"],
        "preferred_tickers": [ticker],
        "dedup_keys": [dedup_key(ticker, action_type, account_bucket)],
        "constraints": {},
        "consumed_by": [],
        "source_reasons": [],
        "today_decision": {"decision": "unreviewed", "reason": ""},
    }


def test_open_order_consumes_plan_budget() -> None:
    items, summary = epe.compute_consumption(
        [_plan_item(budget=200_000, ticker="META")],
        action_state={
            "actions": {
                "state-meta": {
                    "id": "state-meta",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "placed",
                    "estimated_notional_jpy": 120_000,
                }
            }
        },
        executions={"executions": []},
        fx_rate=150,
    )

    item = items[0]
    assert item["open_order_consumed_jpy"] == 120_000
    assert item["filled_consumed_jpy"] == 0
    assert item["consumed_jpy"] == 120_000
    assert item["remaining_jpy"] == 80_000
    assert item["consumed_by"][0]["source"] == "action_state"
    assert item["consumed_by"][0]["consumption_type"] == "open"
    assert summary["open_order_consumed_jpy"] == 120_000
    assert summary["remaining_normal_jpy"] == 80_000


def test_pending_recommendation_does_not_consume_plan_budget() -> None:
    items, summary = epe.compute_consumption(
        [_plan_item(budget=200_000, ticker="META")],
        action_state={
            "actions": {
                "pending-meta": {
                    "id": "pending-meta",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "pending",
                    "estimated_notional_jpy": 120_000,
                }
            }
        },
        executions={"executions": []},
    )

    assert items[0]["consumed_jpy"] == 0
    assert items[0]["remaining_jpy"] == 200_000
    assert summary["pending_recommendation_count"] == 1
    assert summary["pending_recommendation_notional_jpy"] == 120_000
    assert summary["open_order_consumed_jpy"] == 0


def test_filled_execution_reduces_remaining_budget() -> None:
    items, summary = epe.compute_consumption(
        [_plan_item(budget=180_000, ticker="NEM")],
        action_state={"actions": {}},
        executions={
            "executions": [
                {
                    "id": "NEM_buy_exec",
                    "ticker": "NEM",
                    "direction": "buy",
                    "status": "executed",
                    "quantity": 3,
                    "price": 100,
                    "currency": "USD",
                }
            ]
        },
        fx_rate=150,
    )

    item = items[0]
    assert item["filled_consumed_jpy"] == 45_000
    assert item["open_order_consumed_jpy"] == 0
    assert item["remaining_jpy"] == 135_000
    assert item["consumed_by"][0]["source"] == "action_executions"
    assert item["consumed_by"][0]["consumption_type"] == "filled"
    assert summary["filled_consumed_jpy"] == 45_000


def test_terminal_execution_consumes_when_linked_action_state_is_cancelled() -> None:
    items, summary = epe.compute_consumption(
        [_plan_item(budget=200_000, ticker="META")],
        action_state={
            "actions": {
                "state-meta": {
                    "id": "state-meta",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "cancelled",
                    "estimated_notional_jpy": 120_000,
                }
            }
        },
        executions={
            "executions": [{
                "id": "exec-meta",
                "action_state_id": "state-meta",
                "ticker": "META",
                "direction": "buy",
                "status": "executed",
                "executed_amount_jpy": 120_000,
            }]
        },
    )

    assert items[0]["filled_consumed_jpy"] == 120_000
    assert items[0]["open_order_consumed_jpy"] == 0
    assert items[0]["consumed_by"] == [{
        "source": "action_executions",
        "id": "exec-meta",
        "ticker": "META",
        "status": "executed",
        "notional_jpy": 120_000,
        "consumption_type": "filled",
        "dedup_key": "META|buy|default",
    }]
    assert summary["filled_consumed_jpy"] == 120_000


def test_terminal_execution_has_precedence_over_linked_placed_state() -> None:
    items, summary = epe.compute_consumption(
        [_plan_item(budget=200_000, ticker="META")],
        action_state={
            "actions": {
                "state-meta": {
                    "id": "state-meta",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "placed",
                    "estimated_notional_jpy": 120_000,
                }
            }
        },
        executions={
            "executions": [{
                "id": "exec-meta",
                "action_state_id": "state-meta",
                "ticker": "META",
                "direction": "buy",
                "status": "partial",
                "executed_amount_jpy": 75_000,
            }]
        },
    )

    assert items[0]["filled_consumed_jpy"] == 75_000
    assert items[0]["open_order_consumed_jpy"] == 0
    assert items[0]["consumed_by"][0]["source"] == "action_executions"
    assert items[0]["consumed_by"][0]["consumption_type"] == "filled"
    assert summary["filled_consumed_jpy"] == 75_000


def test_monthly_consumption_uses_explicit_attribution_and_terminal_execution() -> None:
    summary = epe.compute_monthly_consumption(
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        action_state={
            "actions": {
                "cancelled-state": {
                    "id": "cancelled-state",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "cancelled",
                    "monthly_objective_id": "2026-07:normal:add-currency-usd",
                },
                "open-state": {
                    "id": "open-state",
                    "ticker": "NEM",
                    "action_type": "buy",
                    "status": "placed",
                    "estimated_notional_jpy": 40_000,
                    "monthly_objective_id": "2026-07:normal:add-currency-usd",
                },
                "prior-month": {
                    "id": "prior-month",
                    "ticker": "V",
                    "action_type": "buy",
                    "status": "filled",
                    "filled_at": "2026-07-03T10:00:00",
                    "estimated_notional_jpy": 99_000,
                    "monthly_objective_id": "2026-06:normal:add-currency-usd",
                },
            }
        },
        executions={
            "executions": [{
                "id": "meta-exec",
                "action_state_id": "cancelled-state",
                "ticker": "META",
                "direction": "buy",
                "status": "executed",
                "saved_at": "2026-07-02T10:00:00",
                "executed_amount_jpy": 80_000,
            }]
        },
    )

    assert summary["monthly_open_order_consumed_jpy"] == 40_000
    assert summary["monthly_filled_consumed_jpy"] == 80_000
    assert summary["monthly_consumed_jpy"] == 120_000
    assert summary["monthly_consumption_record_count"] == 2
    assert {r["source"] for r in summary["monthly_consumed_by"]} == {"action_state", "action_executions"}


def test_monthly_consumption_reports_unattributed_legacy_activity_separately() -> None:
    summary = epe.compute_monthly_consumption(
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        action_state={
            "actions": {
                "legacy-open": {
                    "id": "legacy-open",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "placed",
                    "estimated_notional_jpy": 50_000,
                },
                "legacy-filled": {
                    "id": "legacy-filled",
                    "ticker": "NEM",
                    "action_type": "buy",
                    "status": "filled",
                    "filled_at": "2026-07-03T10:00:00",
                    "estimated_notional_jpy": 80_000,
                },
                "attributed-filled": {
                    "id": "attributed-filled",
                    "ticker": "V",
                    "action_type": "buy",
                    "status": "filled",
                    "filled_at": "2026-07-04T10:00:00",
                    "estimated_notional_jpy": 30_000,
                    "monthly_objective_id": "2026-07:normal:add-currency-usd",
                },
                "prior-month": {
                    "id": "prior-month",
                    "ticker": "OLD",
                    "action_type": "buy",
                    "status": "filled",
                    "filled_at": "2026-06-30T10:00:00",
                    "estimated_notional_jpy": 90_000,
                },
            }
        },
    )

    assert summary["monthly_consumed_jpy"] == 30_000
    assert summary["unattributed_monthly_open_order_count"] == 1
    assert summary["unattributed_monthly_open_order_notional_jpy"] == 50_000
    assert summary["unattributed_monthly_filled_count"] == 1
    assert summary["unattributed_monthly_filled_notional_jpy"] == 80_000
    assert summary["unattributed_monthly_total_count"] == 2
    assert summary["unattributed_monthly_total_notional_jpy"] == 130_000
    assert {row["ticker"] for row in summary["unattributed_monthly_examples"]} == {"META", "NEM"}


def test_monthly_consumption_prefers_attributed_execution_over_unattributed_state() -> None:
    summary = epe.compute_monthly_consumption(
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        action_state={
            "actions": {
                "state-1": {
                    "id": "state-1",
                    "ticker": "META",
                    "action_type": "buy",
                    "status": "placed",
                    "estimated_notional_jpy": 50_000,
                },
            }
        },
        executions={
            "executions": [{
                "id": "execution-1",
                "action_state_id": "state-1",
                "ticker": "META",
                "direction": "buy",
                "status": "ordered",
                "estimated_notional_jpy": 50_000,
                "monthly_objective_id": "2026-07:normal:add-currency-usd",
            }]
        },
    )

    assert summary["monthly_open_order_consumed_jpy"] == 50_000
    assert summary["unattributed_monthly_total_count"] == 0


def test_monthly_remaining_clips_weekly_plan_allocation() -> None:
    objectives = [
        {
            "objective": "add_currency_usd",
            "priority": 1,
            "requested_jpy": 100_000,
            "source_reasons": ["test"],
        },
        {
            "objective": "drawdown_dca_active_tranche",
            "priority": 1,
            "requested_jpy": 50_000,
            "budget_bucket": "opportunity",
            "source_reasons": ["test"],
        },
    ]
    items = epe.allocate_plan_items(
        objectives=objectives,
        budgets={
            "weekly_normal_jpy": 100_000,
            "weekly_opportunity_reserve_jpy": 50_000,
            "max_single_normal_action_jpy": 100_000,
            "max_single_opportunity_action_jpy": 50_000,
        },
        horizon={"month": "2026-07", "iso_week": 28},
        monthly_remaining_jpy=80_000,
    )

    assert sum(item["normal_budget_jpy"] for item in items) == 80_000
    assert items[0]["monthly_objective_id"] == "2026-07:normal:add-currency-usd"


def test_trusted_sector_catalog_uses_curated_holdings_and_fresh_cache(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "long_term_meta.json").write_text(json.dumps({
        "JPM": {"sector": "Financial Services"},
    }), encoding="utf-8")
    (tmp_path / "holdings.json").write_text(json.dumps({
        "7203.T": {"ticker": "7203.T", "sector": "Consumer Cyclical"},
    }), encoding="utf-8")
    (tmp_path / "data" / "sector_cache.json").write_text(json.dumps({
        "META": {"sector": "Communication Services", "cached_at": "2026-07-09T10:00:00"},
        "OLD": {"sector": "Technology", "cached_at": "2026-06-01T10:00:00"},
    }), encoding="utf-8")

    catalog, summary, warnings = epe.load_trusted_sector_catalog(
        base_dir=tmp_path,
        now=datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone(timedelta(hours=9))),
        cache_ttl_days=8,
    )

    assert {ticker: info["sector"] for ticker, info in catalog.items()} == {
        "JPM": "Financial Services",
        "7203.T": "Consumer Cyclical",
        "META": "Communication Services",
    }
    assert summary["source_counts"] == {"long_term_meta": 1, "holdings": 1, "sector_cache": 1}
    assert warnings == ["sector_cache_stale_rows_skipped: 1"]


def test_xlf_manual_sector_override_beats_generic_etf_cache(tmp_path) -> None:
    (tmp_path / "data").mkdir()
    (tmp_path / "holdings.json").write_text(json.dumps({
        "XLF_NISA": {"ticker": "XLF", "sector": "ETF"},
    }), encoding="utf-8")
    (tmp_path / "data" / "sector_cache.json").write_text(json.dumps({
        "XLF": {"sector": "ETF", "cached_at": "2026-07-15T10:00:00"},
    }), encoding="utf-8")

    catalog, summary, warnings = epe.load_trusted_sector_catalog(
        base_dir=tmp_path,
        now=datetime(2026, 7, 16, 10, 0, 0, tzinfo=timezone(timedelta(hours=9))),
    )

    assert catalog["XLF"]["sector"] == "Financial Services"
    assert catalog["XLF"]["source"] == "manual_sector_override"
    assert summary["source_counts"]["manual_sector_override"] == 1
    assert warnings == []


def test_diversified_1489_is_not_a_financial_sector_plan_candidate(tmp_path) -> None:
    (tmp_path / "holdings.json").write_text(json.dumps({
        "1489": {"ticker": "1489.T", "shares": 100},
    }), encoding="utf-8")
    (tmp_path / "long_term_meta.json").write_text("{}", encoding="utf-8")

    catalog, _, _ = epe.load_trusted_sector_catalog(base_dir=tmp_path)

    assert "1489.T" not in catalog


def test_sector_objective_requires_deterministic_ticker_mapping() -> None:
    items = epe.build_plan_items(
        rebalance_report={
            "buy_candidates": {
                "sectors": [
                    {"sector": "Financial Services", "gap_jpy": 200_000},
                    {"sector": "Unmapped Theme", "gap_jpy": 200_000},
                ]
            }
        },
        bottom_fishing={},
        nisa={},
        budgets={
            "weekly_normal_jpy": 200_000,
            "weekly_opportunity_reserve_jpy": 0,
            "max_single_normal_action_jpy": 200_000,
            "max_single_opportunity_action_jpy": 0,
        },
        horizon={"month": "2026-07", "iso_week": 28},
        trusted_sector_catalog={
            "JPM": {"sector": "Financial Services", "source": "long_term_meta"},
            "META": {"sector": "Communication Services", "source": "sector_cache"},
        },
    )

    assert len(items) == 1
    assert items[0]["objective"] == "add_sector_financial-services"
    assert items[0]["preferred_tickers"] == ["JPM"]
    assert items[0]["dedup_keys"] == ["JPM|buy|default"]


def test_currency_only_objective_is_advisory_not_plan_consumption_or_enforcement() -> None:
    item = {
        **_plan_item(budget=100_000, ticker="DUMMY"),
        "preferred_tickers": [],
        "dedup_keys": [],
        "constraints": {"currency": "USD"},
    }
    action = {
        "ticker": "NEM",
        "type": "buy",
        "currency": "USD",
        "estimated_notional_jpy": 50_000,
        "confidence_pct": 40,
        "rank": 9,
        "urgency": "low",
    }

    items, summary = epe.compute_consumption(
        [item],
        action_state={"actions": {"state": {**action, "id": "state", "action_type": "buy", "status": "placed"}}},
        executions={"executions": []},
    )
    decision = epe.classify_candidate_against_plan(
        action,
        {"items": [item], "consumption_summary": {"remaining_opportunity_jpy": 0}},
    )

    assert items[0]["consumed_jpy"] == 0
    assert summary["normal_consumed_jpy"] == 0
    assert decision == {
        "execution_plan_decision": "plan_advisory_match",
        "execution_plan_advisory_item_ids": [item["plan_item_id"]],
        "execution_plan_match_kind": "advisory",
        "reason": "broad account/currency intent cannot authorize an order",
        "executable": False,
    }


def test_short_is_not_a_defensive_plan_override() -> None:
    decision = epe.classify_candidate_against_plan(
        {
            "ticker": "TSLA",
            "type": "short",
            "estimated_notional_jpy": 80_000,
            "confidence_pct": 40,
            "rank": 9,
            "urgency": "low",
        },
        {"items": [_plan_item(ticker="META")], "consumption_summary": {"remaining_opportunity_jpy": 0}},
    )

    assert decision == {
        "execution_plan_decision": "plan_unmatched_no_override",
        "executable": False,
    }


def test_monthly_cap_blocks_exact_candidate_even_when_weekly_item_has_room() -> None:
    item = _plan_item(budget=100_000, ticker="META")
    decision = epe.classify_candidate_against_plan(
        {
            "ticker": "META",
            "type": "buy",
            "estimated_notional_jpy": 80_000,
            "confidence_pct": 75,
            "rank": 2,
            "urgency": "medium",
        },
        {
            "items": [item],
            "consumption_summary": {
                "remaining_opportunity_jpy": 0,
                "monthly_remaining_jpy": 50_000,
            },
        },
    )

    assert decision["execution_plan_decision"] == "plan_monthly_cap_reached"
    assert decision["monthly_remaining_jpy"] == 50_000
    assert decision["executable"] is False


def test_bare_jpx_plan_ticker_matches_canonical_action_ticker() -> None:
    item = _plan_item(budget=400_000, ticker="1489")
    decision = epe.classify_candidate_against_plan(
        {
            "ticker": "1489.T",
            "type": "buy",
            "estimated_notional_jpy": 300_000,
            "confidence_pct": 80,
            "rank": 1,
            "urgency": "high",
        },
        {"items": [item], "consumption_summary": {"remaining_opportunity_jpy": 0}},
    )

    assert decision["execution_plan_decision"] == "plan_new_order"
    assert decision["execution_plan_match_kind"] == "preferred_ticker"


def test_attested_playbook_action_has_dedicated_bounded_override() -> None:
    action = {
        "ticker": "1489.T",
        "type": "buy",
        "source": "scenario_playbook",
        "scenario_id": "japan_standalone_bull",
        "playbook_injected": True,
        "estimated_notional_jpy": 100_000,
        "playbook_gate": {
            "version": 1,
            "attested": True,
            "scenario_status": "active",
            "entry_cap_jpy": 100_000,
            "run_cap_jpy": 1_500_000,
            "run_used_after_jpy": 100_000,
            "jp_target_check_applicable": True,
            "jp_target_check_passed": True,
        },
    }
    plan = {
        "horizon": {"month": "2026-07"},
        "items": [_plan_item(ticker="META")],
        "consumption_summary": {
            "remaining_opportunity_jpy": 0,
            "monthly_remaining_jpy": 150_000,
        },
    }

    decision = epe.classify_candidate_against_plan(action, plan)

    # Scenario playbooks need explicit opportunity capital; ordinary monthly
    # capacity is not an automatic exception.
    plan["consumption_summary"]["remaining_opportunity_jpy"] = 150_000
    decision = epe.classify_candidate_against_plan(action, plan)
    assert decision["execution_plan_decision"] == "scenario_playbook_bounded"
    assert decision["execution_plan_override"] == "scenario_playbook"
    assert decision["monthly_objective_id"] == "2026-07:scenario:japan-standalone-bull"
    assert decision["opportunity_remaining_after_jpy"] == 50_000
    assert decision["executable"] is True


def test_unattested_playbook_action_fails_closed() -> None:
    decision = epe.classify_candidate_against_plan(
        {
            "ticker": "1489.T",
            "type": "buy",
            "source": "scenario_playbook",
            "playbook_injected": True,
            "estimated_notional_jpy": 100_000,
        },
        {
            "horizon": {"month": "2026-07"},
            "items": [_plan_item(ticker="META")],
            "consumption_summary": {"monthly_remaining_jpy": 150_000},
        },
    )

    assert decision == {
        "execution_plan_decision": "scenario_playbook_unattested",
        "executable": False,
    }


def test_explicit_plan_id_with_wrong_account_fails_as_metadata_mismatch() -> None:
    item = {
        **_plan_item(ticker="NEM", account_bucket="wife_nisa"),
        "constraints": {"account_hint": "wife_nisa_growth"},
    }
    decision = epe.classify_candidate_against_plan(
        {
            "ticker": "NEM",
            "type": "buy",
            "account": "husband_nisa",
            "plan_item_id": item["plan_item_id"],
            "estimated_notional_jpy": 50_000,
        },
        {"items": [item], "consumption_summary": {"remaining_opportunity_jpy": 0}},
    )

    assert decision == {
        "execution_plan_decision": "plan_metadata_mismatch",
        "plan_item_id": item["plan_item_id"],
        "metadata_mismatch": "direction_account_currency_or_ticker",
        "executable": False,
    }


def test_batch_allocation_is_deterministic_and_does_not_oversubscribe_item() -> None:
    item = _plan_item(budget=250_000, ticker="META")
    actions = [
        {
            "ticker": "META",
            "type": "buy",
            "rank": 2,
            "confidence_pct": 90,
            "urgency": "high",
            "estimated_notional_jpy": 150_000,
            "execution_plan_decision": "plan_new_order",
            "plan_item_id": item["plan_item_id"],
        },
        {
            "ticker": "META",
            "type": "buy",
            "rank": 1,
            "confidence_pct": 80,
            "urgency": "medium",
            "estimated_notional_jpy": 150_000,
            "execution_plan_decision": "plan_new_order",
            "plan_item_id": item["plan_item_id"],
        },
    ]

    allocated = epe.allocate_candidate_batch_against_plan(
        actions,
        {"items": [item], "consumption_summary": {"remaining_opportunity_jpy": 0}},
    )

    assert allocated[0]["execution_plan_decision"] == "plan_over_budget"
    assert allocated[0]["executable"] is False
    assert allocated[1]["execution_plan_decision"] == "plan_new_order"
    assert allocated[1]["executable"] is True
    assert allocated[1]["plan_remaining_before_jpy"] == 250_000
    assert allocated[1]["plan_remaining_after_jpy"] == 100_000


def test_batch_allocation_applies_monthly_pool_to_playbook_actions() -> None:
    actions = [
        {
            "ticker": "1489.T", "type": "buy", "rank": 2,
            "estimated_notional_jpy": 70_000,
            "execution_plan_decision": "scenario_playbook_bounded",
        },
        {
            "ticker": "1306.T", "type": "buy", "rank": 1,
            "estimated_notional_jpy": 70_000,
            "execution_plan_decision": "scenario_playbook_bounded",
        },
    ]

    allocated = epe.allocate_candidate_batch_against_plan(
        actions,
        {
            "items": [_plan_item(ticker="META")],
            "consumption_summary": {
                "remaining_opportunity_jpy": 100_000,
                "monthly_remaining_jpy": 100_000,
            },
        },
    )

    assert allocated[0]["execution_plan_decision"] == "plan_over_budget"
    assert allocated[1]["execution_plan_decision"] == "scenario_playbook_bounded"
    assert allocated[1]["opportunity_remaining_before_jpy"] == 100_000
    assert allocated[1]["opportunity_remaining_after_jpy"] == 30_000


def test_account_hint_without_exact_key_is_advisory_and_does_not_consume() -> None:
    item = {
        **_plan_item(budget=100_000, ticker="DUMMY"),
        "objective": "wife_nisa_growth_capacity",
        "preferred_tickers": [],
        "dedup_keys": [],
        "constraints": {"account_hint": "wife_nisa_growth"},
    }

    items, summary = epe.compute_consumption(
        [item],
        action_state={
            "actions": {
                "state-nem": {
                    "id": "state-nem",
                    "ticker": "NEM",
                    "action_type": "buy",
                    "status": "placed",
                    "action_detail": "妻NISA成長投資枠でNEM 6株を指値購入",
                    "amount_hint": "6株",
                    "limit_price": 95.5,
                    "currency": "USD",
                }
            }
        },
        executions={"executions": []},
        fx_rate=150,
    )

    assert items[0]["open_order_consumed_jpy"] == 0
    assert items[0]["remaining_jpy"] == 100_000
    assert summary["open_order_consumed_jpy"] == 0


def test_old_execution_outside_week_does_not_consume_current_plan() -> None:
    items, summary = epe.compute_consumption(
        [_plan_item(budget=100_000, ticker="META")],
        action_state={"actions": {}},
        executions={
            "executions": [
                {
                    "id": "old-meta",
                    "saved_at": "2026-06-01T10:00:00",
                    "ticker": "META",
                    "direction": "buy",
                    "status": "executed",
                    "quantity": 10,
                    "price": 600,
                    "currency": "USD",
                }
            ]
        },
        fx_rate=150,
        period_start=date(2026, 7, 6),
        period_end=date(2026, 7, 12),
    )

    assert items[0]["consumed_jpy"] == 0
    assert items[0]["remaining_jpy"] == 100_000
    assert summary["filled_consumed_jpy"] == 0


def test_plan_classifier_keeps_existing_guard_precedence() -> None:
    plan = {
        "items": [
            {
                **_plan_item(budget=200_000, ticker="META"),
                "remaining_jpy": 200_000,
            }
        ]
    }

    decision = epe.classify_candidate_against_plan(
        {
            "ticker": "META",
            "type": "buy",
            "estimated_notional_jpy": 100_000,
        },
        plan,
        done_keys={("META", "buy")},
    )

    assert decision == {
        "execution_plan_decision": "blocked_by_existing_guard",
        "existing_guard": "done_list",
        "executable": False,
    }


def test_budget_derivation_fail_closed_when_cash_missing() -> None:
    params = {
        "default_monthly_budget_jpy": 300_000,
        "cash_deploy_pct": 0.05,
        "max_monthly_budget_jpy": 700_000,
        "weekly_normal_budget_pct": 0.70,
        "opportunity_reserve_pct": 0.25,
        "max_single_normal_jpy": 250_000,
        "max_single_opportunity_jpy": 300_000,
        "max_single_action_pct_of_portfolio": 0.05,
    }
    cash_info, cash_warnings = epe.derive_cash_info({}, now=datetime(2026, 7, 10), stale_hours=72)
    horizon = epe.horizon_for(date(2026, 7, 10))
    budgets, budget_warnings = epe.derive_budgets(
        cash_info=cash_info,
        guard={"portfolio_value": 30_000_000, "new_entry_allowed": True, "trading_allowed": True},
        params=params,
        scheduled_contributions_jpy=0,
        horizon=horizon,
    )

    assert cash_info["valid_for_budget"] is False
    assert budgets["monthly_total_jpy"] == 0
    assert budgets["budget_source"] == "explicit_policy"
    assert any("cash_info_missing" in w for w in cash_warnings)
    assert any("cash_info_stale" in w for w in budget_warnings)


def test_budget_derivation_fails_closed_when_cash_stale() -> None:
    old = datetime(2026, 7, 1, tzinfo=timezone(timedelta(hours=9)))
    cash_info, warnings = epe.derive_cash_info(
        {
            "balance": 100_000,
            "usd_balance": 1_000,
            "fx_rate_usdjpy": 150,
            "last_updated": old.isoformat(),
        },
        now=old + timedelta(hours=100),
        stale_hours=72,
    )

    assert cash_info["valid_for_budget"] is False
    assert cash_info["source"] == "stale_account_fallback"
    assert any("cash_info_stale" in w for w in warnings)


def test_dedup_key_public_api_matches_plan_consumption() -> None:
    entry = {
        "ticker": "LLY",
        "action_type": "add",
        "account": "wife_nisa",
    }

    assert dedup_key_for_action(entry) == dedup_key("LLY", "add", "wife_nisa")
    assert dedup_key_for_action(entry) == "LLY|buy|wife_nisa"


def test_build_execution_plan_top_level_shape() -> None:
    plan = epe.build_execution_plan(
        account={
            "balance": 100_000,
            "usd_balance": 1_000,
            "fx_rate_usdjpy": 150,
            "last_updated": "2026-07-10T07:00:00",
        },
        guard={"portfolio_value": 30_000_000, "new_entry_allowed": True, "trading_allowed": True},
        rebalance_report={
            "as_of": "2026-07-09 19:05",
            "buy_candidates": {
                "currencies": [{"currency": "USD", "gap_jpy": 500_000}],
                "sectors": [{"sector": "Financial Services", "gap_jpy": 300_000}],
            },
        },
        bottom_fishing={"evaluated_at": "2026-07-10T07:25:00", "recommended_buys": []},
        nisa={"wife": {"growth_limit_annual": 2_400_000, "growth_used_this_year": 2_000_000}},
        action_state={"actions": {}},
        executions={"executions": []},
        ai_analysis={"as_of": "2026-07-10 06:12"},
        params={
            "monthly_discretionary_budget_jpy": 300_000,
            "default_monthly_budget_jpy": 300_000,
            "cash_deploy_pct": 0.05,
            "max_monthly_budget_jpy": 700_000,
            "weekly_normal_budget_pct": 0.70,
            "opportunity_reserve_pct": 0.25,
            "max_single_normal_jpy": 250_000,
            "max_single_opportunity_jpy": 300_000,
            "max_single_action_pct_of_portfolio": 0.05,
            "cash_stale_hours": 72,
        },
        now=datetime(2026, 7, 10, 7, 30),
        contribution_occurrences=[],
    )

    assert plan["schema_version"] == 2
    assert plan["horizon"]["month"] == "2026-07"
    assert set(plan) >= {
        "schema_version",
        "as_of",
        "horizon",
        "status",
        "source_versions",
        "budgets",
        "consumption_summary",
        "items",
        "no_action_rationale",
        "generated_by",
    }
    assert plan["source_versions"]["rebalance_report_as_of"] == "2026-07-09 19:05"
    assert plan["items"]


def test_zero_discretionary_policy_does_not_turn_cash_or_auto_dca_into_buying_power() -> None:
    plan = epe.build_execution_plan(
        account={
            "balance": 2_000_000,
            "usd_balance": 10_000,
            "fx_rate_usdjpy": 150,
            "last_updated": "2026-07-10T07:00:00",
        },
        guard={"portfolio_value": 30_000_000, "new_entry_allowed": True, "trading_allowed": True},
        rebalance_report={"buy_candidates": {"currencies": [{"currency": "USD", "gap_jpy": 500_000}]}},
        bottom_fishing={},
        nisa={"wife": {"growth_limit_annual": 2_400_000, "growth_used_this_year": 2_000_000}},
        action_state={"actions": {}},
        executions={"executions": []},
        params={
            "monthly_discretionary_budget_jpy": 0,
            "max_monthly_budget_jpy": 700_000,
            "weekly_normal_budget_pct": 0.70,
            "opportunity_reserve_pct": 0.25,
            "max_single_normal_jpy": 250_000,
            "max_single_opportunity_jpy": 300_000,
            "max_single_action_pct_of_portfolio": 0.05,
            "cash_stale_hours": 72,
        },
        now=datetime(2026, 7, 10, 7, 30),
        contribution_occurrences=[(date(2026, 7, 11), {"amount": 100_000, "currency": "JPY"})],
    )

    assert plan["budgets"]["monthly_total_jpy"] == 0
    assert plan["budgets"]["normal_pool_available_jpy"] == 0
    assert plan["items"] == []
    assert plan["no_action_rationale"][0]["reason_code"] == "no_approved_discretionary_funding"
    assert "scheduled_contributions_excluded_from_discretionary_budget" in plan["warnings"]


def test_approved_contribution_creates_one_common_pool_without_priority_wallet_splitting() -> None:
    plan = epe.build_execution_plan(
        account={"balance": 0, "usd_balance": 0, "fx_rate_usdjpy": 150, "last_updated": "2026-07-10T07:00:00"},
        guard={"portfolio_value": 30_000_000, "new_entry_allowed": True, "trading_allowed": True},
        rebalance_report={
            "buy_candidates": {
                "currencies": [{"currency": "USD", "gap_jpy": 500_000}],
                "sectors": [{"sector": "Financial Services", "gap_jpy": 300_000}],
            },
        },
        bottom_fishing={},
        nisa={"wife": {"growth_limit_annual": 2_400_000, "growth_used_this_year": 2_000_000}},
        action_state={"actions": {}},
        executions={"executions": []},
        params={
            "monthly_discretionary_budget_jpy": 0,
            "max_monthly_budget_jpy": 700_000,
            "max_single_normal_jpy": 250_000,
            "max_single_opportunity_jpy": 300_000,
            "max_single_action_pct_of_portfolio": 0.05,
            "cash_stale_hours": 72,
        },
        now=datetime(2026, 7, 10, 7, 30),
        contribution_occurrences=[],
        contribution_ledger={"contributions": [{
            "id": "salary", "source": "salary", "bucket": "normal", "owner": "husband", "broker": "rakuten",
            "amount_jpy": 200_000, "start_month": "2026-07", "release_months": 1, "status": "approved",
        }]},
        trusted_sector_catalog={"JPM": {"sector": "Financial Services"}},
    )

    assert plan["budgets"]["normal_pool_available_jpy"] == 200_000
    assert len(plan["items"]) >= 2
    assert {item["shared_pool_jpy"] for item in plan["items"] if item["budget_bucket"] == "normal"} == {200_000}
    assert sum(item["normal_budget_jpy"] for item in plan["items"] if item["budget_bucket"] == "normal") > 200_000


def test_monthly_unattributed_totals_separate_buys_sells_and_unpriced_rows() -> None:
    summary = epe.compute_monthly_consumption(
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        executions={"executions": [
            {"id": "buy", "ticker": "V", "direction": "buy", "status": "executed", "notional_jpy": 100_000, "saved_at": "2026-07-02T10:00:00"},
            {"id": "sell", "ticker": "XLF", "direction": "sell", "status": "executed", "notional_jpy": 70_000, "saved_at": "2026-07-02T10:00:00"},
            {"id": "unpriced", "ticker": "NFLX", "direction": "sell", "status": "executed", "saved_at": "2026-07-02T10:00:00"},
        ]},
    )

    assert summary["unattributed_monthly_buy_total_notional_jpy"] == 100_000
    assert summary["unattributed_monthly_sell_total_notional_jpy"] == 70_000
    assert summary["unattributed_monthly_total_notional_jpy"] == 170_000
    assert summary["unattributed_monthly_unpriced_count"] == 1


def test_unpriced_open_execution_is_not_double_counted_with_its_lifecycle_state() -> None:
    summary = epe.compute_monthly_consumption(
        month_start=date(2026, 7, 1),
        month_end=date(2026, 7, 31),
        action_state={"actions": {
            "state-unpriced": {"id": "state-unpriced", "ticker": "V", "action_type": "buy", "status": "placed"},
        }},
        executions={"executions": [{
            "id": "exec-unpriced", "action_state_id": "state-unpriced", "ticker": "V",
            "direction": "buy", "status": "ordered", "saved_at": "2026-07-10T09:00:00",
        }]},
    )

    assert summary["unattributed_monthly_unpriced_count"] == 1
    assert summary["unattributed_monthly_total_count"] == 1
