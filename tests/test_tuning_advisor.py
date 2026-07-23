from datetime import datetime

import action_stage_log as asl
import tuning_advisor as ta
import portfolio_manager


def test_recent_post_filter_stats_uses_action_stage_log(tmp_path, monkeypatch):
    log_path = tmp_path / "action_stage_log.jsonl"
    monkeypatch.setattr(asl, "LOG_PATH", log_path)
    now = datetime.now().isoformat(timespec="seconds")
    asl.append_entries([
        {
            "analysis_id": "r1",
            "as_of": now,
            "stage": "post_filter_rejected",
            "ticker": "A",
            "filter_rule": "too_small",
        },
        {
            "analysis_id": "r1",
            "as_of": now,
            "stage": "post_filter_rejected",
            "ticker": "B",
            "filter_rule": "already_executed",
        },
        {
            "analysis_id": "r1",
            "as_of": now,
            "stage": "post_filter_deferred",
            "ticker": "C",
            "order_intent_decision": "amend_existing_order",
        },
        {
            "analysis_id": "r1",
            "as_of": now,
            "stage": "post_filter_rejected",
            "ticker": "D",
            "filter_rule": "tax_loss_harvest_conflict",
        },
    ])

    stats = ta._recent_post_filter_stats(days=30)

    assert stats["recent_too_small_count"] == 1
    assert stats["recent_already_executed_count"] == 1
    assert stats["recent_deferred_count"] == 1
    assert stats["recent_tax_loss_conflict_count"] == 1


def test_market_context_uses_total_cash_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ta, "BASE_DIR", tmp_path)
    monkeypatch.setattr(portfolio_manager, "build_portfolio_snapshot", lambda: {
        "total_jpy": 1_000,
        "cash_total_jpy": 300,
        "cash_jpy": 300,
        "cash_usd_native": 1,
        "fx_rate": 150,
        "sector_breakdown": {"Cash": {"ratio": 0.3}},
    })
    context = ta._load_market_context()
    assert context["cash_ratio_pct"] == 30.0
