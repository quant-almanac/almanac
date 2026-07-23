import inspect
import json
import sys
import types
from datetime import datetime
from zoneinfo import ZoneInfo

import analyst
from analyst.llm_client import _SUBMIT_TOOL


def test_synthesis_failure_distinguishes_error_empty_from_valid_no_trade():
    assert analyst._is_synthesis_failure({
        "error": "synthesis_exception: overloaded",
        "priority_actions": [],
        "hold_notes": [],
    })

    assert not analyst._is_synthesis_failure({
        "overall_stance": "neutral",
        "priority_actions": [],
        "hold_notes": ["no-trade: alpha below threshold"],
    })

    assert not analyst._is_synthesis_failure({
        "error": "partial warning",
        "priority_actions": [{"ticker": "META", "type": "add"}],
    })


def test_screen_candidate_bullish_support_accepts_current_debate_schema():
    assert analyst._screen_candidate_has_bullish_support({
        "ai_debate": {"bull": "出来高急増と52週高値更新は強いブレイクアウト"}
    })
    assert analyst._screen_candidate_has_bullish_support({
        "ai_debate": {"bull_view": "BULLISH"}
    })
    assert not analyst._screen_candidate_has_bullish_support({
        "ai_debate": {"bull": "なし"}
    })


def test_swing_prompt_includes_deterministic_jp_only_candidates_without_ai_comments(monkeypatch):
    captured = {}

    def fake_tier_call(system, prompt, **kwargs):
        captured["prompt"] = prompt
        return {"health": "good", "summary": "ok", "priority_actions": []}

    monkeypatch.setattr(analyst, "call_tier_analysis", fake_tier_call)
    monkeypatch.setattr(analyst, "_compute_ginn_vol", lambda tickers: ("", {}))
    monkeypatch.setattr(analyst, "_fmt_technical_state", lambda tickers, state: "")
    monkeypatch.setattr(analyst, "_fmt_social_sentiment", lambda tickers, state: "")
    monkeypatch.setattr(analyst, "fmt_news_section", lambda news, tickers=None: "")
    monkeypatch.setattr(analyst, "fmt_earnings_section", lambda earnings, tickers=None: "")

    jp_candidate = {
        "ticker": "8795.T",
        "strategy": "モメンタム",
        "score": 38.9,
        "rsi": 50.9,
        "mom_1m": 11.6,
        "screen_source": "jp_only",
        "is_japan": True,
    }
    data = {
        "positions": [{"ticker": "DUMMY", "investment_type": "swing"}],
        "screen_candidates": [],
        "screening": {"jp_screen_candidates": [jp_candidate]},
        "technical_state": {},
        "social_sentiment": {},
        "news": [],
        "earnings": {},
    }

    analyst._analyze_short_positions(data)

    assert "### 日本株スクリーニングWATCH" in captured["prompt"]
    assert "8795.T" in captured["prompt"]
    assert "signal=deterministic" in captured["prompt"]


def test_final_synthesis_logs_llm_usage(monkeypatch):
    monkeypatch.setenv("ALMANAC_PRIVACY_MODE", "anthropic_book_aware")
    rows = []

    class FakeMessages:
        def create(self, **kwargs):
            return types.SimpleNamespace(
                content=[
                    types.SimpleNamespace(
                        type="tool_use",
                        name="submit_analysis",
                        input={
                            "result": {
                                "overall_stance": "neutral",
                                "priority_actions": [],
                                "hold_notes": ["no trade"],
                            }
                        },
                    )
                ],
                usage=types.SimpleNamespace(input_tokens=1234, output_tokens=234),
            )

    class FakeAnthropicClient:
        def __init__(self, **kwargs):
            self.messages = FakeMessages()

    fake_anthropic = types.SimpleNamespace(
        Anthropic=FakeAnthropicClient,
        APIStatusError=type("APIStatusError", (Exception,), {}),
        APITimeoutError=type("APITimeoutError", (Exception,), {}),
        APIConnectionError=type("APIConnectionError", (Exception,), {}),
    )

    monkeypatch.setitem(sys.modules, "anthropic", fake_anthropic)
    monkeypatch.setattr(analyst, "_append_llm_call_log", lambda row: rows.append(row), raising=False)
    monkeypatch.setattr(analyst, "fetch_web_search_news", lambda: "")
    monkeypatch.setattr(analyst, "load_history_context", lambda: "")
    monkeypatch.setattr(analyst, "fmt_news_section", lambda news, tickers=None: "")
    monkeypatch.setattr(analyst, "fmt_earnings_section", lambda earnings, tickers=None: "")
    monkeypatch.setattr(analyst, "_load_bl_views_for_opus", lambda: "")
    monkeypatch.setattr(analyst, "_load_catalyst_context_for_opus", lambda scenario=None: "")
    monkeypatch.setattr(analyst, "_format_beliefs_context", lambda beliefs, max_items=15: "")
    monkeypatch.setattr(analyst, "_load_beliefs", lambda: [])
    monkeypatch.setattr(analyst, "_format_execution_quality_for_prompt", lambda eq: "")
    monkeypatch.setattr(analyst, "_load_execution_quality_summary", lambda: None)
    monkeypatch.setattr(analyst, "_format_agent_reliability_for_prompt", lambda max_entries=8: "")
    monkeypatch.setattr(analyst, "_fmt_tunable_limits_context", lambda: "")
    monkeypatch.setattr(analyst, "_format_recent_own_recs_for_prompt", lambda days=14: "")
    monkeypatch.setattr(analyst, "_format_earnings_blackout_for_prompt", lambda within_business_days=5: "")
    monkeypatch.setattr(analyst, "_format_done_list_for_prompt", lambda days=7: "")
    monkeypatch.setattr(analyst, "_build_consolidated_rebalance_context", lambda *args, **kwargs: "")
    monkeypatch.setattr(analyst, "_fmt_scenario_monitoring", lambda scenario_monitoring: "")
    monkeypatch.setattr(analyst, "_extract_tax_urgent_actions", lambda data: "")
    monkeypatch.setattr(analyst, "_gather_chart_context", None, raising=False)
    monkeypatch.setattr(analyst, "_format_chart_for_prompt", None, raising=False)

    import model_router
    import behavioral_guard

    monkeypatch.setattr(model_router, "get_model", lambda key: "claude-opus-4-20250514")
    monkeypatch.setattr(
        behavioral_guard,
        "evaluate_leverage_health",
        lambda portfolio_total_jpy=0: {
            "current_leverage": 1.0,
            "leverage_cap": 1.2,
            "max_leverage_setting": 1.2,
            "status": "safe",
            "action": "ok",
            "new_buy_allowed": True,
            "margin_buy_allowed": True,
        },
    )

    result = analyst._synthesize(
        {"health": "good", "priority_actions": []},
        {"health": "good", "priority_actions": []},
        {"health": "good", "priority_actions": []},
        {"health": "good", "priority_actions": []},
        {"health": "good", "priority_actions": []},
        portfolio_total=1_000_000,
        scenario={"key": "base", "name": "Base", "cash_ratio_target": 0},
        risk={},
        market_meta={"vix": 15, "us10y_yield": {}, "us2y_yield": {}},
        news=[],
        earnings={},
        cash_info={"total_cash_jpy": 0, "fx_rate_usdjpy": 150},
    )

    assert result["overall_stance"] == "neutral"
    assert rows, "final_synthesis should be included in logs/llm_calls.jsonl accounting"
    row = rows[-1]
    assert row["role"] == "final_synthesis"
    assert row["model"] == "claude-opus-4-20250514"
    assert row["status"] == "ok"
    assert row["input_tokens"] == 1234
    assert row["output_tokens"] == 234


def test_final_synthesis_prompt_allows_margin_buy_actions():
    source = inspect.getsource(analyst._synthesize)

    assert '"type":"buy|add|margin_buy|sell|rebalance|short|cover|dca|trim|reduce|stop_loss|take_profit"' in source
    assert "buy/add/margin_buy/sell/rebalance/trim/reduce/dca/stop_loss/take_profit/short/cover" in source
    assert "`margin_buy` = 信用買いエントリー" in source


def test_final_synthesis_prompt_allows_kabu_mini_for_jp_cash_buys():
    source = inspect.getsource(analyst._synthesize)

    assert "ローカルのかぶミニ対象台帳で確認できる現物 `buy/add`" in source
    assert 'execution_channel="rakuten_kabu_mini_open"' in source
    assert "台帳未確認の普通株は100株単位に戻す" in source
    assert "かぶミニは現物専用" in source
    assert "公式売買単位が1口のETFなら小口で提案可" in source
    assert "1489.T=1口、1306.T=10口" in source


def test_final_synthesis_requires_explicit_nisa_route():
    source = inspect.getsource(analyst._synthesize)

    assert '"execution_owner":"husband|wife"' in source
    assert '"execution_broker":"rakuten|sbi"' in source
    assert "名義・証券会社を入力根拠から特定できない場合は推測せず" in source
    assert '"execution_account":"特定|一般|NISA成長投資枠|NISAつみたて投資枠|信用"' in source
    assert "売却元口座の整合必須" in source
    assert "`action` 本文の口座名・保有株数とも食い違わせない" in source


def test_tier_prompts_allow_attack_mode_action_types():
    long_source = inspect.getsource(analyst._analyze_long)
    medium_source = inspect.getsource(analyst._analyze_medium)
    margin_source = inspect.getsource(analyst._analyze_margin_long)

    assert '"type":"buy|add|sell|rebalance|trim|dca"' in long_source
    assert '"type":"buy|add|sell|trim|stop_loss|take_profit"' in medium_source
    assert '"type":"margin_buy|buy"' in margin_source


def test_final_synthesis_prompt_exposes_cash_amounts():
    source = inspect.getsource(analyst._synthesize)

    assert "現金残高（口座別・攻めモード判定の最重要入力）" in source
    assert "deployable_cash_to_target_jpy" in source
    assert "具体的な金額は省略" not in source


def test_final_synthesis_drawdown_rule_has_correct_sign():
    source = inspect.getsource(analyst._synthesize)

    assert "current_dd<=-8%" in source
    assert "drawdown>-4%" not in source


def test_data_freshness_tracks_cash_and_holdings_sources():
    source = inspect.getsource(analyst._compute_data_freshness)

    assert "account_cash" in source
    assert "holdings" in source
    assert "__mtime__" in source


def test_final_synthesis_includes_portfolio_integrity_context():
    source = inspect.getsource(analyst._synthesize)

    assert "Portfolio Ledger Integrity" in source
    assert "unapplied_executed_count" in source
    assert "holdings/account は未反映の可能性" in source


def test_tool_schema_description_mentions_margin_buy():
    desc = _SUBMIT_TOOL["input_schema"]["properties"]["result"]["properties"]["priority_actions"]["description"]

    assert "margin_buy" in desc


def test_degraded_mode_detects_multiple_tier_failures():
    info = analyst._build_degraded_mode_info({
        "Long分析": {"error": "long timeout", "priority_actions": []},
        "Medium分析": {"summary": "分析エラー", "health": "caution", "priority_actions": []},
        "Swing分析": {"health": "good", "priority_actions": [{"ticker": "META"}]},
        "MarginLong分析": {"health": "good", "margin_long_picks": [{"ticker": "AVGO"}]},
        "ShortSell分析": {"health": "safe", "priority_actions": []},
    })

    assert info["enabled"] is True
    assert info["failed_count"] == 2
    assert "action_cap" not in info
    assert "Long分析" in info["reason"]


def test_degraded_mode_does_not_flag_valid_caution_no_trade():
    info = analyst._build_degraded_mode_info({
        "Long分析": {"health": "caution", "summary": "期待alpha不足", "priority_actions": []},
        "Medium分析": {"health": "good", "priority_actions": []},
        "Swing分析": {"health": "good", "priority_actions": []},
    })

    assert info["enabled"] is False
    assert info["failed_count"] == 0


def test_apply_degraded_mode_annotates_without_hiding_actions():
    info = {
        "enabled": True,
        "failed_count": 3,
        "failed_tiers": [{"tier": "Long分析", "reason": "timeout"}],
        "confidence_penalty": 15,
        "reason": "tier failures 3/5",
    }
    synthesis = {
        "health": "good",
        "telegram_message": "📊 stance=moderately_aggressive",
        "priority_actions": [
            {"rank": 1, "ticker": "META", "type": "buy", "confidence_pct": 76},
            {"rank": 2, "ticker": "LLY", "type": "buy", "confidence_pct": 70},
            {"rank": 3, "ticker": "TXN", "type": "sell", "confidence_pct": 65},
            {"rank": 4, "ticker": "AVGO", "type": "add", "confidence_pct": 80},
            {"rank": 5, "ticker": "GLD", "type": "trim", "confidence_pct": 60},
        ],
    }

    result = analyst._apply_degraded_mode(synthesis, info)

    assert result["degraded_mode"] is True
    assert result["health"] == "caution"
    assert len(result["priority_actions"]) == 5
    assert [a["ticker"] for a in result["priority_actions"]] == ["META", "LLY", "TXN", "AVGO", "GLD"]
    assert result["priority_actions"][0]["confidence_before_degraded"] == 76
    assert result["priority_actions"][0]["confidence_pct"] == 61
    assert all(a["confidence_degraded"] for a in result["priority_actions"])
    assert "_degraded_filtered_actions" not in result
    assert result["degraded_action_policy"] == "annotate_only"
    assert result["telegram_message"].startswith("⚠️ DEGRADED MODE")


def test_final_synthesis_accepts_degraded_context():
    sig = inspect.signature(analyst._synthesize)
    source = inspect.getsource(analyst._synthesize)

    assert "degraded_context" in sig.parameters
    assert "DEGRADED MODE コンテキスト" in source
    assert "件数制限だけで非表示化しない" in source


def test_run_analysis_keeps_raw_post_policy_and_final_action_fields():
    source = inspect.getsource(analyst.run_analysis)

    assert "raw_priority_actions" in source
    assert "post_policy_priority_actions" in source
    assert "policy_filtered_actions" in source
    assert "final_priority_actions" in source


def test_run_analysis_reads_catalyst_context_presence_from_synthesis():
    source = inspect.getsource(analyst.run_analysis)
    start = source.index("_apply_degraded_mode(synthesis, _degraded_info)")
    end = source.index("_ensure_information_lane_verdicts(synthesis)", start)
    context_block = source[start:end]

    assert "catalyst_ctx" not in context_block
    assert '"catalyst": bool(_ctx_blocks.get("catalyst"))' in context_block


def test_run_analysis_annotates_jp_disclosure_boundary_after_brief_attachment():
    source = inspect.getsource(analyst.run_analysis)
    start = source.index('synthesis["disclosure_brief"] = {')
    end = source.index("audit = synthesis.get", start)
    disclosure_block = source[start:end]

    assert "_annotate_jp_disclosure_observe_only_boundary(synthesis)" in disclosure_block


def test_final_synthesis_records_catalyst_context_presence():
    source = inspect.getsource(analyst._synthesize)

    assert '_context_blocks["catalyst"] = bool(catalyst_ctx.strip())' in source


def test_redteam_default_token_budget_covers_larger_v2_schema(monkeypatch):
    seen = {}

    def fake_call_claude(**kwargs):
        seen.update(kwargs)
        return {
            "attacks": [{
                "ticker": "NVDA",
                "action": "add",
                "expected_return_pct": 10,
                "rationale": "test",
                "risk_note": "test",
            }],
            "underutilized": [],
        }

    monkeypatch.delenv("KAIROS_REDTEAM_MAX_TOKENS", raising=False)
    monkeypatch.setattr(analyst, "call_claude", fake_call_claude)

    result = analyst._analyze_redteam({"positions": []}, shared_ctx="market context")

    assert result["attacks"]
    assert seen["max_tokens"] >= 12000


def test_run_analysis_persists_currency_breakdown_in_cache_result():
    source = inspect.getsource(analyst.run_analysis)
    start = source.index("result = {")
    end = source.index("save_cache(result)", start)
    result_block = source[start:end]

    assert '"currency_breakdown": data.get("currency_breakdown", {})' in result_block


def test_gather_data_exposes_snapshot_currency_breakdown():
    from analyst import data_gatherer

    source = inspect.getsource(data_gatherer.gather_data)

    assert '"currency_breakdown": portfolio.get("currency_breakdown", {})' in source


def test_runtime_observability_writes_portfolio_attribution_and_sell(tmp_path, monkeypatch):
    monkeypatch.setattr(analyst, "BASE_DIR", tmp_path)
    (tmp_path / "scenario_state.json").write_text(json.dumps({
        "scenarios": {
            "bull_pullback": {"status": "watching", "readiness": 0.59, "name": "押し目"}
        }
    }, ensure_ascii=False), encoding="utf-8")
    synthesis = {
        "overall_stance": "neutral",
        "raw_priority_actions": [
            {"ticker": "META", "type": "buy"},
            {"ticker": "TXN", "type": "trim"},
        ],
        "final_priority_actions": [
            {"ticker": "META", "type": "buy", "confidence_pct": 61, "reason": "test"},
            {"ticker": "TXN", "type": "trim", "amount_hint": "1株", "confidence_pct": 55, "reason": "risk cut"},
        ],
        "_filtered_actions": [
            {"ticker": "NVDA", "type": "buy", "filtered_reason": "too_small: below threshold"},
        ],
    }
    data = {"portfolio_total": 10_000_000, "cash_info": {"total_cash_jpy": 1_000_000}}

    result = analyst._write_runtime_observability_logs(
        synthesis, data, analysis_id="analysis-1", fsync=False
    )

    assert result["errors"] == []
    assert result["written"] == 4
    portfolio = (tmp_path / "portfolio_decision_log.jsonl").read_text(encoding="utf-8").splitlines()
    attr = (tmp_path / "agent_attribution_log.jsonl").read_text(encoding="utf-8").splitlines()
    sell = (tmp_path / "sell_decision_log.jsonl").read_text(encoding="utf-8").splitlines()
    assert json.loads(portfolio[0])["portfolio_decision_state"] == "action_taken"
    assert json.loads(portfolio[0])["rejected_count_by_reason"]["too_small"] == 1
    assert len(attr) == 2
    attr_rows = [json.loads(line) for line in attr]
    assert attr_rows[0]["agent"] == "opus_final"

    from almanac.observability.candidate_extractor import extract_from_synthesis

    expected_packets = extract_from_synthesis(
        {"priority_actions": synthesis["final_priority_actions"]},
        analysis_id="analysis-1",
        analysis_date="2026-07-02",
    )
    assert {r["hypothesis_id"] for r in attr_rows} == {
        p["hypothesis_id"] for p in expected_packets
    }
    assert {r["time_horizon_days"] for r in attr_rows} == {
        p["time_horizon_days"] for p in expected_packets
    }
    assert json.loads(sell[0])["ticker"] == "TXN"


def test_us_holiday_actions_are_annotated_not_blocked():
    synthesis = {
        "telegram_message": "📊 stance=aggressive",
        "priority_actions": [
            {"ticker": "META", "type": "buy", "confidence_pct": 70, "reason": "alpha"},
            {"ticker": "1489.T", "type": "buy", "confidence_pct": 70, "reason": "jp"},
            {"ticker": "SLIM_SP500", "type": "buy", "confidence_pct": 70, "reason": "fund"},
        ],
    }
    # 2026-05-26 00:49 JST is 2026-05-25 in New York (Memorial Day).
    now = datetime(2026, 5, 26, 0, 49, tzinfo=ZoneInfo("Asia/Tokyo"))

    result = analyst._annotate_us_holiday_actions(synthesis, now=now)

    assert len(result["priority_actions"]) == 3
    meta = result["priority_actions"][0]
    assert meta["market_closed_degraded"] is True
    assert meta["market_closed_date"] == "2026-05-25"
    assert meta["confidence_pct"] == 60
    assert "NYSE休場" in meta["execution_note"]
    assert "market_closed_degraded" not in result["priority_actions"][1]
    assert "market_closed_degraded" not in result["priority_actions"][2]
    assert result["telegram_message"].startswith("⚠️ NYSE休場 2026-05-25")


def test_us_trading_day_actions_are_not_annotated():
    synthesis = {"priority_actions": [{"ticker": "META", "type": "buy", "confidence_pct": 70}]}
    now = datetime(2026, 5, 27, 10, 0, tzinfo=ZoneInfo("Asia/Tokyo"))

    result = analyst._annotate_us_holiday_actions(synthesis, now=now)

    assert "market_closed_degraded" not in result["priority_actions"][0]
