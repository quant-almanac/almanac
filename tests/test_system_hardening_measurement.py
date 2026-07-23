from __future__ import annotations

import inspect
import json
from pathlib import Path
from datetime import datetime

import analyzer
import follow_rate_analyzer
import screener
from almanac.observability.catalyst_layer import run
from almanac.observability.lane_registry import load_lane_registry, validate_lane_registry
from almanac.observability.screener_hypotheses import extract_screener_packets
from llm_cost_accounting import estimate_cost_usd, normalize_usage_row, summarize_month


def test_screener_packets_are_deterministic_observe_only_and_top_three() -> None:
    payloads = {
        "margin_long": {
            "candidates": [
                {"ticker": f"T{i}", "score": 80 - i, "reason": "rule"}
                for i in range(5)
            ]
        },
        "pair": {
            "candidates": [
                {"pair": "AAA/BBB", "long": "AAA", "short": "BBB", "z_score": 2.4}
            ]
        },
    }
    first = extract_screener_packets(payloads, analysis_date="2026-06-12")
    second = extract_screener_packets(payloads, analysis_date="2026-06-12")

    assert first == second
    assert len([p for p in first if p["hypothesis_type"] == "screener_margin_long"]) == 3
    assert all(packet["observe_only"] for packet in first)
    assert {p["action_type"] for p in first if p["hypothesis_type"] == "screener_pair"} == {
        "buy",
        "short_sell",
    }


def test_catalyst_never_promotes_screener_hypotheses_to_top() -> None:
    output = run(
        screener_payloads={
            "short": {
                "candidates": [
                    {"ticker": "XYZ", "composite_score": 95, "reason": "short rule"}
                ]
            }
        },
        analysis_id="test-analysis",
        analysis_date="2026-06-12",
        write_log=False,
    )

    assert output.n_hypotheses_total == 1
    assert output.n_hypotheses_top == 0
    assert output.top == []
    assert output.all_hypotheses[0].observe_only is True


def test_lane_registry_declares_required_lanes() -> None:
    assert validate_lane_registry("lane_registry.json") == []


def test_legacy_signal_tracker_is_retired_and_unscheduled() -> None:
    lanes = {lane["name"]: lane for lane in load_lane_registry("lane_registry.json")}

    assert lanes["signal_tracker"]["status"] == "retired"
    cron = Path("crontab.proposed")
    if cron.exists():
        assert "signal_tracker.py" not in cron.read_text(encoding="utf-8")


def test_llm_usage_schema_and_monthly_summary() -> None:
    row = normalize_usage_row(
        {
            "ts": "2026-06-12T09:00:00+00:00",
            "role": "final_synthesis",
            "model": "claude-sonnet-4-20250514",
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
        }
    )
    assert row["lane"] == "final_synthesis"
    assert row["provider"] == "anthropic"
    assert row["cost_usd"] == 4.5
    summary = summarize_month([row], month="2026-06")
    assert summary["calls"] == 1
    assert summary["cost_usd"] == 4.5


def test_deepseek_v4_alias_usage_is_priced() -> None:
    assert estimate_cost_usd("deepseek-v4-flash", 100, 50) is not None
    assert estimate_cost_usd("deepseek-v4-pro", 100, 50) is not None
    row = normalize_usage_row(
        {
            "ts": "2026-06-12T09:00:00+00:00",
            "role": "disclosure_extractor",
            "model": "deepseek-v4-flash",
            "input_tokens": 100,
            "output_tokens": 50,
        }
    )
    assert row["cost_usd"] == estimate_cost_usd("deepseek-v4-flash", 100, 50)


def test_claude_haiku_45_usage_is_priced() -> None:
    row = normalize_usage_row(
        {
            "ts": "2026-06-30T09:00:00+00:00",
            "role": "web_search_news",
            "model": "claude-haiku-4-5-20251001",
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
        }
    )
    assert row["provider"] == "anthropic"
    assert row["lane"] == "web_search_news"
    assert row["cost_usd"] == 1.5


def test_batch_api_usage_gets_half_price_discount() -> None:
    row = normalize_usage_row(
        {
            "ts": "2026-06-30T09:00:00+00:00",
            "role": "long_term_thesis_batch_result",
            "model": "claude-haiku-4-5-20251001",
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "batch": True,
        }
    )
    assert row["cost_usd"] == 0.75
    assert row["batch_discount_factor"] == 0.5


def test_web_search_server_tool_usage_is_priced() -> None:
    row = normalize_usage_row(
        {
            "ts": "2026-06-30T09:00:00+00:00",
            "role": "web_search_news",
            "model": "claude-haiku-4-5-20251001",
            "input_tokens": 0,
            "output_tokens": 0,
            "server_tool_use": {"web_search_requests": 2},
        }
    )
    assert row["cost_usd"] == 0.02


def test_follow_rate_reconstructs_sell_proposals(tmp_path) -> None:
    rec_path = tmp_path / "recs.json"
    sell_path = tmp_path / "sell.jsonl"
    rec_path.write_text("[]", encoding="utf-8")
    sell_path.write_text(
        json.dumps(
            {
                "sell_decision_id": "s1",
                "ticker": "AAA",
                "action_type": "trim",
                "recommended_at": "2026-06-10T09:00:00",
                "price_at_recommend": 100,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    recs = follow_rate_analyzer.load_recommendations(rec_path, sell_path)
    result = follow_rate_analyzer.match_recommendations(
        recs=recs,
        execs=[
            {
                "id": "e1",
                "ticker": "AAA",
                "direction": "sell",
                "saved_at": "2026-06-11T09:00:00",
                "price": 101,
                "quantity": 1,
            }
        ],
    )
    assert result["total_recs"] == 1
    assert result["total_matched"] == 1
    assert result["follow_rate"] == 1.0


def test_daily_commentary_is_opt_in_and_evening_analyzer_skips(monkeypatch) -> None:
    assert inspect.signature(screener.run_full_screen).parameters["ai_comments"].default is False
    monkeypatch.setattr(
        analyzer,
        "send_telegram",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("network path called")),
    )
    result = analyzer.main(now=datetime(2026, 6, 12, 17, 0))
    assert result == {"status": "skipped_evening_commentary"}
