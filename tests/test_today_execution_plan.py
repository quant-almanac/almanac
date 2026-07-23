from datetime import datetime
import os

from api.routes import today
import portfolio_manager


def test_market_sessions_include_jp_and_full_us_daylight_saving_clock():
    sessions = today._market_sessions(datetime(2026, 7, 16, 23, 0, 0))
    by_id = {row["id"]: row for row in sessions}

    assert set(by_id) == {"jpx-am", "jpx-pm", "us-pre", "us-regular", "us-after"}
    assert (by_id["us-pre"]["start"], by_id["us-pre"]["end"]) == ("17:00", "22:30")
    assert (by_id["us-regular"]["start"], by_id["us-regular"]["end"]) == ("22:30", "05:00")
    assert (by_id["us-after"]["start"], by_id["us-after"]["end"]) == ("05:00", "09:00")


def test_market_sessions_follow_us_standard_time():
    sessions = today._market_sessions(datetime(2026, 1, 15, 23, 0, 0))
    regular = next(row for row in sessions if row["id"] == "us-regular")

    assert (regular["start"], regular["end"]) == ("23:30", "06:00")


def test_market_sessions_use_exchange_calendar_for_jpx_holiday():
    sessions = today._market_sessions(datetime(2026, 7, 20, 6, 8, 0))
    jp = next(row for row in sessions if row["id"] == "jpx-am")

    assert jp["is_open_day"] is False
    assert jp["calendar_status"] == "closed"
    assert jp["next_market_open"]


def test_lifecycle_view_never_presents_elapsed_or_reprice_order_as_pending():
    expired = today._lifecycle_view({
        "id": "old", "status": "pending", "recommended_at": "2020-01-01T09:00:00",
        "expiry_minutes": 30,
    }, None)
    reprice = today._lifecycle_view({
        "id": "holiday", "status": "reprice_required", "recommended_at": "2026-07-20T06:08:00",
        "expiry_minutes": 720, "expiry_deferred_until_reprice": True,
        "market_reprice_after": "2026-07-20T13:30:00+00:00",
    }, None)

    assert expired["status"] == "expired"
    assert reprice["status"] == "reprice_required"
    assert reprice["expiry_at"] is None
    assert reprice["market_reprice_after"] == "2026-07-20T13:30:00+00:00"


def test_lifecycle_countdown_starts_at_next_market_open():
    from datetime import timedelta

    opens_at = datetime.now().astimezone() + timedelta(hours=2)
    lifecycle = today._lifecycle_view({
        "id": "morning", "status": "pending", "recommended_at": "2020-01-01T06:15:00",
        "expiry_starts_at": opens_at.isoformat(), "expiry_minutes": 30,
    }, None)

    assert lifecycle["status"] == "pending"
    assert lifecycle["expiry_starts_at"] == opens_at.isoformat()
    assert lifecycle["expiry_at"] == (opens_at + timedelta(minutes=30)).isoformat()


def test_build_benchmark_overlays_twr_and_market_returns(monkeypatch):
    monkeypatch.setattr(today, "modified_dietz_twr_series", lambda **_kwargs: {
        "points": [
            {"date": "2026-07-01", "twr_pct": 0.0},
            {"date": "2026-07-08", "twr_pct": 2.0},
            {"date": "2026-07-14", "twr_pct": 4.0},
        ],
        "confirmed": True,
        "clean_ok": True,
        "clean_since": "2026-05-25",
        "v_start_date": "2026-07-01",
        "v_end_date": "2026-07-14",
        "period_days_actual": 13,
        "net_cash_flow": 100_000.0,
        "error": None,
    })
    monkeypatch.setattr(today, "_fetch_benchmark_closes", lambda: {
        "sp500": {"2026-07-01": 100.0, "2026-07-08": 105.0, "2026-07-14": 110.0},
        "nikkei": {"2026-07-01": 200.0, "2026-07-08": 198.0, "2026-07-14": 190.0},
        "usdjpy": {"2026-07-01": 150.0, "2026-07-08": 151.0, "2026-07-14": 152.0},
    })
    guard = {"pnl_history": [
        {"date": "2026-07-01", "pnl_jpy": 500_000},
        {"date": "2026-07-02", "pnl_jpy": -1_000_000},
        {"date": "2026-07-03", "pnl_jpy": 250_000},
        {"date": "2026-07-08", "pnl_jpy": 20_000},
        {"date": "2026-07-14", "pnl_jpy": 10_000},
    ]}

    result = today._build_benchmark(guard)

    assert result["portfolio"] == [0.0, 2.0, 4.0]
    assert result["sp500"] == [0.0, 5.7, 11.467]
    assert result["nikkei"] == [0.0, -1.0, -5.0]
    assert result["outperf"] == {"sp500": -7.47, "nikkei": 9.0}
    assert result["method"] == "modified_dietz"
    assert result["confirmed"] is True
    assert result["start_date"] == "2026-07-01"


def test_build_today_includes_scenario_summary(monkeypatch):
    state = {
        "scenarios": {
            "active": {"status": "active"},
            "partial": {"status": "partial"},
            "watching": {"status": "watching"},
        },
        "overall_alert_level": "high",
        "evaluated_at": "2026-07-11T09:00:00+09:00",
    }
    monkeypatch.setattr(today, "_load", lambda name: state if name == "scenario_state.json" else {})

    result = today._build_today()

    assert result["scenario_summary"] == {
        "active": 1,
        "partial": 1,
        "watching": 1,
        "alert_level": "high",
        "evaluated_at": "2026-07-11T09:00:00+09:00",
    }


def test_build_today_uses_missing_scenario_state_fallback(monkeypatch):
    monkeypatch.setattr(today, "_load", lambda _name: {})

    result = today._build_today()

    assert result["scenario_summary"] == {
        "active": 0,
        "partial": 0,
        "watching": 0,
        "alert_level": None,
        "evaluated_at": None,
    }


def test_build_today_separates_ready_orders_from_review_candidates(monkeypatch):
    analysis = {
        "as_of": "2026-07-14 07:00",
        "portfolio_total": 10_000_000,
        "synthesis": {
            "analysis_id": "analysis-0714",
            "priority_actions": [
                {
                    "rank": 1, "ticker": "1489.T", "type": "buy",
                    "execution_readiness": "ready", "estimated_notional_jpy": 120_000,
                },
                {
                    "rank": 2, "ticker": "ROBO", "type": "buy",
                    "execution_readiness": "blocked",
                    "execution_block_reasons": [{"code": "market_spread_too_wide", "message": "spread 408bps"}],
                },
            ],
            "order_intent_deferred_actions": [{
                "rank": 3, "ticker": "4063.T", "type": "buy",
                "order_intent_decision": "near_minimum_notional",
                "non_executable_reason": "最小発注額に未達",
            }],
            "decision_summary": {
                "candidate_count": 3, "executable_count": 1, "review_count": 2,
                "filtered_count": 0, "deferred_count": 1,
                "no_action_classification": None, "reason_counts": {},
                "count_conservation_ok": True,
            },
        },
    }
    action_state = {"actions": {
        "ready-id": {"id": "ready-id", "ticker": "1489.T", "action_type": "buy", "status": "pending", "recommended_at": "2026-07-14T07:00:00"},
        "blocked-id": {"id": "blocked-id", "ticker": "ROBO", "action_type": "buy", "status": "pending", "recommended_at": "2026-07-14T07:00:00"},
    }}

    def load(name):
        if name == "ai_portfolio_analysis.json":
            return analysis
        if name == "action_state.json":
            return action_state
        return {}

    monkeypatch.setattr(today, "_load", load)
    monkeypatch.setattr(portfolio_manager, "build_portfolio_snapshot", lambda: {"positions": [], "total_jpy": 0})
    # This is a board-separation contract, not a parquet/benchmark integration
    # test. Keep it independent of the local market-data cache.
    monkeypatch.setattr(today, "_ticker_closes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(today, "_build_benchmark", lambda _guard: {})
    result = today._build_today()

    assert [row["ticker"] for row in result["board"]] == ["1489.T"]
    assert [row["ticker"] for row in result["review_board"]] == ["ROBO", "4063.T"]
    assert result["board"][0]["analysis_id"] == "analysis-0714"
    assert result["board"][0]["action_state_id"] == "ready-id"
    assert result["review_board"][0]["execution_block_reasons"][0]["code"] == "market_spread_too_wide"
    assert result["review_board"][1]["execution_block_reasons"][0]["code"] == "near_minimum_notional"
    assert result["execution_plan"]["summary"]["board_count"] == 1
    assert result["decision_summary"]["count_conservation_ok"] is True


def test_historical_non_ready_candidate_never_returns_as_executable_backlog(monkeypatch):
    state = {"actions": {"old-blocked": {
        "id": "old-blocked", "ticker": "ROBO", "action_type": "buy",
        "status": "pending", "recommended_at": "2000-01-01T00:00:00",
        "execution_readiness": "blocked",
        "execution_block_reasons": [{"code": "market_order_spread_too_wide", "message": "spread too wide"}],
    }}}
    monkeypatch.setattr(
        today,
        "_load",
        lambda name: state if name == "action_state.json" else {},
    )
    monkeypatch.setattr(portfolio_manager, "build_portfolio_snapshot", lambda: {"positions": [], "total_jpy": 0})
    monkeypatch.setattr(today, "_ticker_closes", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(today, "_build_benchmark", lambda _guard: {})

    result = today._build_today()

    assert [row["ticker"] for row in result["backlog"]] == ["ROBO"]
    assert result["backlog"][0]["execution_readiness"] == "review"
    assert result["backlog"][0]["historical_backlog"] is True
    assert result["review_board"] == []


def test_ticker_closes_reuses_only_unchanged_parquet_mtime(monkeypatch, tmp_path):
    class FakeClose:
        def __init__(self, rows):
            self.rows = rows

        def dropna(self):
            return self

        def items(self):
            return iter(self.rows)

    class FakeFrame:
        def __init__(self, rows):
            self.close = FakeClose(rows)

        def __getitem__(self, key):
            assert key == "Close"
            return self.close

    parquet = tmp_path / "data" / "ohlcv" / "TEST.parquet"
    parquet.parent.mkdir(parents=True)
    parquet.write_bytes(b"placeholder")
    rows = [
        (datetime(2026, 7, 7), 100.0),
        (datetime(2026, 7, 8), 101.0),
        (datetime(2026, 7, 9), 102.0),
    ]
    calls = []

    import pandas as pd

    def read_parquet(path, columns=None):
        calls.append((path, columns))
        return FakeFrame(rows)

    monkeypatch.setattr(today, "BASE_DIR", tmp_path)
    monkeypatch.setattr(pd, "read_parquet", read_parquet)
    today._ohlcv_cache.clear()

    assert today._ticker_closes("TEST", days=3) == [
        {"d": "07/07", "c": 100.0}, {"d": "07/08", "c": 101.0}, {"d": "07/09", "c": 102.0},
    ]
    assert today._ticker_closes("TEST", days=2) == [
        {"d": "07/08", "c": 101.0}, {"d": "07/09", "c": 102.0},
    ]
    assert len(calls) == 1

    stat = parquet.stat()
    os.utime(parquet, (stat.st_atime, stat.st_mtime + 2))
    assert today._ticker_closes("TEST", days=2)[-1] == {"d": "07/09", "c": 102.0}
    assert len(calls) == 2


def test_build_execution_plan_view_explains_open_order_consumption():
    plan = {
        "status": "active",
        "as_of": "2026-07-10T06:00:00+09:00",
        "horizon": {"month": "2026-07", "week_start": "2026-07-06", "week_end": "2026-07-12"},
        "budgets": {
            "monthly_total_jpy": 600000,
            "weekly_normal_jpy": 100000,
            "weekly_opportunity_reserve_jpy": 30000,
            "budget_source": "cash_derived",
        },
        "consumption_summary": {
            "normal_consumed_jpy": 120000,
            "open_order_consumed_jpy": 90000,
            "filled_consumed_jpy": 30000,
            "remaining_normal_jpy": 0,
            "remaining_opportunity_jpy": 30000,
            "unattributed_monthly_total_count": 2,
            "unattributed_monthly_total_notional_jpy": 130000,
        },
        "no_action_rationale": [
            {
                "reason_code": "covered_by_open_orders",
                "message": "Current plan items are already covered by open orders or fills.",
            },
            "Legacy rationale text.",
        ],
        "items": [
            {
                "plan_item_id": "2026-07-w28-usd-001",
                "objective": "add_currency_usd",
                "status": "covered",
                "normal_budget_jpy": 100000,
                "consumed_jpy": 120000,
                "remaining_jpy": 0,
                "consumed_by": [{"id": "abc", "status": "placed"}],
            }
        ],
    }
    synthesis = {
        "_filtered_actions": [
            {
                "ticker": "META",
                "type": "buy",
                "confidence_pct": 72,
                "estimated_notional_jpy": 180000,
                "execution_plan_decision": "plan_consumed_by_open_order",
                "filtered_reason": "plan_consumed_by_open_order: covered",
            }
        ],
        "order_intent_deferred_actions": [{
            "ticker": "NEM",
            "type": "buy",
            "action": "NEM の既存指値を維持",
            "order_intent_decision": "keep_existing_order",
            "non_executable_reason": "existing order already covers intent",
            "existing_order_id": "nem-order-1",
            "existing_order_status": "ordered",
            "existing_order_notional_jpy": 150000,
            "recommended_notional_jpy": 100000,
            "incremental_notional_jpy": 0,
            "non_executable": True,
        }],
        "post_filter": {
            "execution_plan_gate": {
                "mode": "observe",
                "would_filter_count": 2,
                "batch_allocation": {"applied": True, "accepted_count": 1, "over_budget_count": 1},
                "readiness": {
                    "ready_for_enforce": False,
                    "trading_day_count": 2,
                    "classification_count": 8,
                    "blockers": ["observation_sample_short"],
                },
            }
        },
    }

    view = today._build_execution_plan_view(
        plan,
        board=[],
        synthesis=synthesis,
        now=datetime(2026, 7, 10, 7, 0, 0),
    )

    assert view["today_decision"]["code"] == "wait_open_order"
    assert view["summary"] == {
        "items_total": 1,
        "active_items": 0,
        "covered_items": 1,
        "board_count": 0,
        "plan_filtered_count": 1,
    }
    assert view["items"][0]["label"] == "USD不足の補正"
    assert view["items"][0]["consumed_by_count"] == 1
    assert view["consumption"]["unattributed_monthly_total_count"] == 2
    assert view["consumption"]["unattributed_monthly_total_notional_jpy"] == 130000
    assert view["consumption"]["normal_plan_budget_consumed_jpy"] == 100000
    assert view["consumption"]["normal_plan_budget_consumed_pct"] == 100.0
    assert view["consumption"]["normal_matched_notional_jpy"] == 120000
    assert view["consumption"]["normal_open_order_matched_notional_jpy"] == 0
    assert view["consumption"]["normal_filled_matched_notional_jpy"] == 0
    assert view["consumption"]["opportunity_matched_notional_jpy"] == 0
    assert view["consumption"]["monthly_attribution_incomplete"] is True
    assert view["filtered_summary"] == {"plan_consumed_by_open_order": 1}
    assert view["filtered_examples"] == [{
        "ticker": "META",
        "type": "buy",
        "code": "plan_consumed_by_open_order",
        "reason": "plan_consumed_by_open_order: covered",
        "plan_item_id": None,
        "confidence_pct": 72,
        "estimated_notional_jpy": 180000,
    }]
    assert view["order_intent_review"] == {
        "count": 1,
        "summary": {"keep_existing_order": 1},
        "items": [{
            "ticker": "NEM",
            "type": "buy",
            "action": "NEM の既存指値を維持",
            "decision": "keep_existing_order",
            "label": "既存注文を維持",
            "reason": "existing order already covers intent",
            "existing_order_id": "nem-order-1",
            "existing_order_status": "ordered",
            "existing_order_notional_jpy": 150000,
            "recommended_notional_jpy": 100000,
            "incremental_notional_jpy": 0,
            "material_change": False,
            "non_executable": True,
        }],
    }
    assert view["gate_observation"] == {
        "mode": "observe",
        "would_filter_count": 2,
        "batch_allocation": {"applied": True, "accepted_count": 1, "over_budget_count": 1},
        "readiness": {
            "ready_for_enforce": False,
            "trading_day_count": 2,
            "classification_count": 8,
            "blockers": ["observation_sample_short"],
        },
    }
    assert view["no_action_rationale"] == [
        {
            "reason_code": "covered_by_open_orders",
            "message": "Current plan items are already covered by open orders or fills.",
        },
        {"reason_code": "legacy", "message": "Legacy rationale text."},
    ]


def test_build_execution_plan_view_reports_missing_state():
    view = today._build_execution_plan_view({}, board=[], synthesis={}, now=datetime(2026, 7, 10, 7, 0, 0))

    assert view["status"] == "missing"
    assert view["today_decision"]["code"] == "missing"
    assert view["items"] == []
    assert view["consumption"] == {
        "normal_plan_budget_consumed_jpy": None,
        "normal_plan_budget_consumed_pct": None,
        "normal_matched_notional_jpy": None,
        "normal_open_order_matched_notional_jpy": None,
        "normal_filled_matched_notional_jpy": None,
        "opportunity_matched_notional_jpy": None,
        "monthly_attribution_incomplete": False,
        "unattributed_monthly_buy_total_notional_jpy": None,
        "unattributed_monthly_sell_total_notional_jpy": None,
        "unattributed_monthly_unpriced_count": 0,
    }
    assert view["order_intent_review"] == {"count": 0, "summary": {}, "items": []}
    assert view["gate_observation"] == {}


def test_build_execution_plan_view_keeps_opportunity_out_of_normal_contract():
    plan = {
        "status": "active",
        "as_of": "2026-07-10T06:00:00+09:00",
        "budgets": {"weekly_normal_jpy": 52500},
        "consumption_summary": {
            "normal_consumed_jpy": 387055,
            "unattributed_monthly_total_count": 0,
        },
        "items": [
            {
                "budget_bucket": "normal",
                "normal_budget_jpy": 5748,
                "consumed_jpy": 294600,
                "open_order_consumed_jpy": 0,
                "filled_consumed_jpy": 294600,
            },
            {
                "budget_bucket": "normal",
                "normal_budget_jpy": 4599,
                "consumed_jpy": 92455,
                "open_order_consumed_jpy": 92455,
                "filled_consumed_jpy": 0,
            },
            {
                "budget_bucket": "opportunity",
                "normal_budget_jpy": 18750,
                "consumed_jpy": 50000,
                "open_order_consumed_jpy": 50000,
                "filled_consumed_jpy": 0,
            },
        ],
    }

    view = today._build_execution_plan_view(
        plan,
        board=[],
        synthesis={},
        now=datetime(2026, 7, 10, 7, 0, 0),
    )

    consumption = view["consumption"]
    assert consumption["normal_plan_budget_consumed_jpy"] == 10347
    assert consumption["normal_plan_budget_consumed_pct"] == 19.7
    assert consumption["normal_matched_notional_jpy"] == 387055
    assert consumption["normal_open_order_matched_notional_jpy"] == 92455
    assert consumption["normal_filled_matched_notional_jpy"] == 294600
    assert consumption["opportunity_matched_notional_jpy"] == 50000
    assert consumption["monthly_attribution_incomplete"] is False


def test_build_execution_plan_view_handles_missing_weekly_budget_and_unattributed_activity():
    plan = {
        "status": "active",
        "consumption_summary": {"unattributed_monthly_total_count": 1},
        "items": [{"normal_budget_jpy": 1000, "consumed_jpy": 500}],
    }

    view = today._build_execution_plan_view(
        plan,
        board=[],
        synthesis={},
        now=datetime(2026, 7, 10, 7, 0, 0),
    )

    consumption = view["consumption"]
    assert consumption["normal_plan_budget_consumed_jpy"] == 500
    assert consumption["normal_plan_budget_consumed_pct"] is None
    assert consumption["monthly_attribution_incomplete"] is True


def test_build_execution_plan_view_counts_shared_pool_fill_once() -> None:
    plan = {
        "status": "active",
        "budgets": {"normal_pool_available_jpy": 150_000, "weekly_normal_jpy": 50_000},
        "consumption_summary": {
            "monthly_consumed_by": [{
                "monthly_objective_id": "2026-07:normal:add-currency-usd",
                "notional_jpy": 80_000,
                "consumption_type": "filled",
            }],
            "unattributed_monthly_total_count": 0,
        },
        "items": [
            {
                "budget_bucket": "normal",
                "monthly_objective_id": "2026-07:normal:add-currency-usd",
                "normal_budget_jpy": 150_000,
                "consumed_jpy": 80_000,
            },
            {
                # A shared objective can describe the same fill, but is not a
                # second wallet and must not double the displayed consumption.
                "budget_bucket": "normal",
                "monthly_objective_id": "2026-07:normal:add-sector-financial-services",
                "normal_budget_jpy": 150_000,
                "consumed_jpy": 80_000,
            },
        ],
    }

    view = today._build_execution_plan_view(plan, board=[], synthesis={}, now=datetime(2026, 7, 10, 7, 0, 0))

    assert view["consumption"]["normal_plan_budget_consumed_jpy"] == 80_000
    assert view["consumption"]["normal_filled_matched_notional_jpy"] == 80_000
    assert view["consumption"]["normal_plan_budget_consumed_pct"] is None
