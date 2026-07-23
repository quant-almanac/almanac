"""Investor-objective E2E acceptance coverage.

These tests intentionally assert user-visible investment outcomes instead of
just component behavior.  A failing/xfailing test is useful: it records where an
objective is not yet wired all the way from signal/context to a recommendation
surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

import action_state_tracker
import analyst
import jp_loanability
import portfolio_manager
import rebalance_engine
import scenario_engine
import scenario_strategy
import short_screener
import tax_harvest_scanner
from analyst.data_gatherer import fmt_earnings_section
from almanac.observability.catalyst_layer import (
    compact_for_opus,
    run as catalyst_run,
    synthesize_from_active_scenarios,
    synthesize_from_disclosure_features,
)


ROOT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _repo_scenario(scenario_id: str) -> dict:
    playbook = json.loads((ROOT / "scenario_playbook.json").read_text(encoding="utf-8"))
    for scenario in playbook.get("scenarios") or []:
        if isinstance(scenario, dict) and scenario.get("id") == scenario_id:
            return scenario
    raise AssertionError(f"scenario not found: {scenario_id}")


def _keyword_geo(scenario_id: str, keywords: list[str], score: int | None = None) -> dict:
    return {
        "keyword_matches": [
            {
                "scenario_key": scenario_id,
                "score": score if score is not None else len(keywords),
                "threshold": 2,
                "matched_keywords": keywords,
                "severity": "high",
                "confidence": 0.9,
                "assessment_status": "keyword_only",
            }
        ]
    }


def _run_single_scenario(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario_id: str,
    *,
    geo: dict | None = None,
    vix: dict | None = None,
    macro: dict | None = None,
    tech: dict | None = None,
    regime: dict | None = None,
    market: dict | None = None,
) -> tuple[dict, dict, dict]:
    """Evaluate one playbook scenario with isolated state-file fixtures."""

    work = tmp_path / scenario_id
    work.mkdir(parents=True, exist_ok=True)

    scenario_def = _repo_scenario(scenario_id)
    _write_json(work / "scenario_playbook.json", {"scenarios": [scenario_def]})
    _write_json(work / "geopolitical_state.json", geo or {})
    _write_json(work / "vix_state.json", vix or {})
    _write_json(work / "macro_state.json", macro or {})
    _write_json(work / "technical_state.json", tech or {})
    _write_json(work / "regime_state.json", regime or {})
    _write_json(work / "market_snapshot.json", market or {})
    _write_json(work / "guard_state.json", {})
    _write_json(work / "scenario_state.json", {})

    monkeypatch.setattr(scenario_engine, "PLAYBOOK_PATH", work / "scenario_playbook.json")
    monkeypatch.setattr(scenario_engine, "VIX_STATE_PATH", work / "vix_state.json")
    monkeypatch.setattr(scenario_engine, "GEO_STATE_PATH", work / "geopolitical_state.json")
    monkeypatch.setattr(scenario_engine, "MACRO_STATE_PATH", work / "macro_state.json")
    monkeypatch.setattr(scenario_engine, "TECH_STATE_PATH", work / "technical_state.json")
    monkeypatch.setattr(scenario_engine, "REGIME_STATE_PATH", work / "regime_state.json")
    monkeypatch.setattr(scenario_engine, "MARKET_SNAPSHOT_PATH", work / "market_snapshot.json")
    monkeypatch.setattr(scenario_engine, "GUARD_STATE_PATH", work / "guard_state.json")
    monkeypatch.setattr(scenario_engine, "SCENARIO_STATE_PATH", work / "scenario_state.json")
    monkeypatch.setattr(scenario_engine, "send_telegram", lambda _msg: None)

    state = scenario_engine.evaluate_scenarios()
    scenario_state = state["scenarios"][scenario_id]
    return scenario_def, scenario_state, state


def _phase_tickers(scenario_state: dict, *phases: str) -> set[str]:
    tickers: set[str] = set()
    recommended = scenario_state.get("recommended_actions") or {}
    for phase in phases:
        for row in recommended.get(phase) or []:
            if isinstance(row, dict) and row.get("ticker"):
                tickers.add(str(row["ticker"]))
    return tickers


def _scenario_hypotheses(state: dict):
    return synthesize_from_active_scenarios(
        state,
        analysis_id="investor-objectives-e2e",
        analysis_date="2026-06-25",
    )


def test_objective_04_credit_crisis_vix_spike_reaches_risk_reduction_recommendations(
    tmp_path, monkeypatch
):
    """#4 信用危機/VIX急騰: detect → active → sell/risk-reduction recommendation."""

    _scenario_def, scenario, state = _run_single_scenario(
        tmp_path,
        monkeypatch,
        "credit_crisis",
        geo=_keyword_geo(
            "credit_crisis",
            ["bank failure", "credit crunch", "liquidity crisis"],
        ),
        vix={
            "vix": {"level": 35.0, "change_1d": 42.0},
            "hy_spread_bps": 650,
        },
        macro={"yield_2y": -20.0},
        tech={
            "tickers": {
                "SPY": {"rsi": 25},
                "XLF": {"change_5d_pct": -12.0, "rsi": 20},
            }
        },
    )

    assert scenario["status"] == "active", scenario["signal_details"]
    assert scenario["readiness"] >= 0.6

    recommended = scenario["recommended_actions"]
    phase_1_sells = [
        row
        for row in recommended.get("phase_1") or []
        if isinstance(row, dict) and str(row.get("action", "")).startswith(("sell", "trim"))
    ]
    assert phase_1_sells or recommended.get("sell_on_trigger"), (
        "gap at recommended_actions: credit_crisis active but no risk-reduction sell surfaced"
    )

    hypotheses = _scenario_hypotheses(state)
    assert any(h.action_type == "sell" for h in hypotheses), (
        "gap at catalyst hypothesis: credit_crisis sell recommendations did not synthesize"
    )


def test_objective_13_sector_rotation_inflation_and_fed_pivot_reach_rotation_candidates(
    tmp_path, monkeypatch
):
    """#13 セクターローテーション: inflation and easing scenarios surface target ETFs."""

    _inflation_def, inflation, inflation_state = _run_single_scenario(
        tmp_path,
        monkeypatch,
        "inflation_resurgence",
        geo=_keyword_geo(
            "inflation_resurgence",
            ["inflation resurgence", "CPI acceleration", "hawkish fed"],
        ),
        vix={"vix": {"level": 25.0}, "yields": {"us_10y": 5.0}},
        macro={"yield_10y": 5.0},
        tech={
            "tickers": {
                "GLD": {"change_5d_pct": 4.0},
                "TLT": {"rsi": 30},
                "XLE": {"macd_crossover": "bullish"},
            }
        },
    )
    assert inflation["status"] == "active", inflation["signal_details"]
    inflation_tickers = _phase_tickers(inflation, "phase_1", "phase_2")
    assert {"1489.T", "XLE"}.issubset(inflation_tickers)
    inflation_hypotheses = _scenario_hypotheses(inflation_state)
    assert {"1489.T", "XLE"}.issubset({h.ticker for h in inflation_hypotheses})

    _pivot_def, pivot, pivot_state = _run_single_scenario(
        tmp_path,
        monkeypatch,
        "fed_pivot",
        geo=_keyword_geo("fed_pivot", ["rate cut", "dovish", "pivot"]),
        vix={
            "vix": {"level": 18.0},
            "yields": {
                "us_2y": -25.0,
                "spread_10y_3m": 0.2,
                "spread_30y_10y": 0.4,
            },
            "dxy": {"change_5d_pct": -2.5},
        },
        macro={"yield_2y": -25.0, "yield_spread": 0.2},
        tech={"tickers": {"TLT": {"rsi": 60}, "IWM": {"macd_crossover": "bullish"}}},
    )
    assert pivot["status"] == "active", pivot["signal_details"]
    pivot_tickers = _phase_tickers(pivot, "phase_1", "phase_2")
    assert {"TLT", "IWM", "VNQ"}.issubset(pivot_tickers)
    pivot_hypotheses = _scenario_hypotheses(pivot_state)
    assert {"TLT", "IWM", "VNQ"}.issubset({h.ticker for h in pivot_hypotheses})


def test_objective_14_fx_regime_yen_carry_unwind_reaches_safe_haven_candidates(
    tmp_path, monkeypatch
):
    """#14 FXレジーム: yen carry unwind active → GLD/TLT and risk reduction surface."""

    _scenario_def, scenario, state = _run_single_scenario(
        tmp_path,
        monkeypatch,
        "yen_carry_unwind",
        geo=_keyword_geo(
            "yen_carry_unwind",
            ["USDJPY below 140", "yen carry trade unwind", "yen surge"],
        ),
        vix={"vix": {"level": 28.0, "change_5d": 35.0}},
        tech={
            "tickers": {
                "EWJ": {"change_5d_pct": -10.0},
                "GLD": {"change_5d_pct": 4.0},
                "EEM": {"rsi": 40},
                "QQQ": {"macd_crossover": "bearish"},
                "TLT": {"rsi": 60},
            }
        },
    )

    assert scenario["status"] == "active", scenario["signal_details"]
    assert {"GLD", "TLT"}.issubset(_phase_tickers(scenario, "phase_1"))
    assert scenario["recommended_actions"].get("sell_on_trigger")

    hypotheses = _scenario_hypotheses(state)
    by_ticker_action = {(h.ticker, h.action_type) for h in hypotheses}
    assert ("GLD", "buy") in by_ticker_action
    assert ("TLT", "buy") in by_ticker_action
    assert any(action == "sell" for _ticker, action in by_ticker_action)


def test_objective_05_concentration_overweight_reaches_rebalance_reduce_action():
    """#5 集中リスク超過: concentrated medium sleeve → executable trim/reduce."""

    snapshot = {
        "total_jpy": 10_000_000,
        "positions": [
            {
                "ticker": "NVDA",
                "key": "NVDA",
                "name": "NVIDIA",
                "investment_type": "medium",
                "value_jpy": 8_000_000,
                "account": "特定",
            },
            {
                "ticker": "TLT",
                "key": "TLT",
                "name": "TLT",
                "investment_type": "medium",
                "value_jpy": 2_000_000,
                "account": "特定",
            },
        ],
    }

    result = rebalance_engine.calculate_medium_drift(snapshot)
    reduce_actions = [
        action
        for action in result.get("actions") or []
        if action.get("ticker") == "NVDA" and action.get("type") == "reduce"
    ]
    assert reduce_actions, "gap at rebalance_engine: concentration did not produce trim"
    assert reduce_actions[0].get("executable") is True
    assert reduce_actions[0].get("observe_only") is False


def test_objective_12_dilution_feature_should_reach_short_hypothesis_observe_only(monkeypatch):
    """#12 増資/希薄化: dilution feature should become an observe-only short hypothesis."""

    monkeypatch.setattr(
        jp_loanability,
        "evaluate_short_tradeability",
        lambda ticker: {
            "ticker": ticker,
            "loanable": False,
            "loan_ratio": None,
            "reverse_daily_fee": False,
            "untradeable": True,
            "reasons": ["loanable_not_confirmed"],
        },
    )

    hypotheses = synthesize_from_disclosure_features(
        [
            {
                "ticker": "1234.T",
                "source_event_id": "tdnet:1234:2026-06-25:dilution",
                "dilution_flag": True,
                "dilution_pct": 0.12,
                "directional_confidence": 0.9,
                "catalyst_specificity": 0.8,
                "summary": "公募増資により12%の希薄化懸念。",
                "model_id": "fixture",
                "prompt_version": "investor-objectives-e2e",
                "feature_schema_version": "0.4.0",
                "compute_time": "2026-06-25T00:00:00+09:00",
            }
        ],
        analysis_id="investor-objectives-e2e",
        analysis_date="2026-06-25",
    )

    assert hypotheses, "gap at catalyst detect: dilution feature produced no hypothesis"
    hypothesis = hypotheses[0]
    assert hypothesis.observe_only is True
    assert hypothesis.action_type == "short_sell"
    assert hypothesis.human_execution_only is True
    assert hypothesis.execution_cost_model["standard_credit"]["round_trip_cost_pct"] > 0
    assert hypothesis.execution_cost_model["general_credit"]["round_trip_cost_pct"] > 0
    assert hypothesis.tradeability["untradeable"] is True
    assert hypothesis.tradeability["excluded_from_certify"] is True
    assert "loanable_not_confirmed" in hypothesis.tradeability["reasons"]


def test_objective_15_tax_loss_position_reaches_tax_harvest_candidate(tmp_path, monkeypatch):
    """#15 税最適化: taxable losing lot → tax harvest candidate."""

    monkeypatch.setattr(tax_harvest_scanner, "_load_substitutes", lambda: {})
    # scan_tax_harvest() calls action_state_tracker.record_recommendations() as a
    # side effect (2026-07-12 recommendation_id integration) — without this, the
    # synthetic "7203.T" candidate below leaks into the real production
    # action_state.json on every test run (discovered 2026-07-13, see
    # feedback_financial_ledger_confirmation memory).
    monkeypatch.setattr(action_state_tracker, "STATE_FILE", tmp_path / "action_state.json")

    report = tax_harvest_scanner.scan_tax_harvest(
        min_loss_jpy=30_000,
        lots_snapshot={
            "lots": {
                "7203.T": [
                    {
                        "account": "特定",
                        "remaining_qty": 100,
                        "cost_per_share_jpy": 2_000,
                        "currency": "JPY",
                    }
                ]
            }
        },
        price_provider=lambda ticker, currency: (1_500.0, None),
        recommend_func=lambda *args, **kwargs: {"plan": [{"action": "sell"}]},
    )

    assert report["candidate_count"] == 1
    candidate = report["candidates"][0]
    assert candidate["ticker"] == "7203.T"
    assert candidate["estimated_loss_jpy"] == -50_000
    assert candidate["human_execution_only"] is True


def test_objective_16_cash_drag_high_cash_reaches_deployable_cash_context(monkeypatch):
    """#16 キャッシュドラッグ: high cash+BULL-like context → deployable cash suggestions."""

    monkeypatch.setattr(
        portfolio_manager,
        "load_account",
        lambda: {"balance": 5_000_000, "usd_balance": 0, "fx_rate_usdjpy": 150.0},
    )
    monkeypatch.setattr(portfolio_manager, "_load_cash_state", lambda: {})
    monkeypatch.setattr(portfolio_manager, "_tp_pm", lambda _key, fallback: fallback)

    result = portfolio_manager.detect_cash_drag(
        {"total_jpy": 30_000_000, "positions": []},
        persist=False,
    )

    assert result["level"] == "critical"
    assert result["cash_jpy"] == 5_000_000
    assert result["cash_ratio"] > 0.15
    assert result["suggestions"], "gap at deployable_cash context: no routing candidates"
    assert result["suggestions"][0]["currency"] == "JPY"


def test_objective_09_overheat_bull_regime_surfaces_observe_only_contrarian_short_candidate(
    tmp_path, monkeypatch
):
    """#9 過熱/逆張り空売り: BULL overheat fixture should allow contrarian short."""

    assert scenario_strategy.SCENARIOS["BULL"]["short_allowed"] is False

    close = pd.Series([100.0] * 56)
    volume = pd.Series([2_000_000] * len(close))

    monkeypatch.setattr(short_screener, "_get_vix", lambda: 18.0)
    monkeypatch.setattr(short_screener, "_prefetch_sector_cache", lambda _tickers: None)
    monkeypatch.setattr(
        short_screener,
        "_bulk_download",
        lambda _tickers: {"7203.T": {"close": close, "volume": volume}},
    )
    monkeypatch.setattr(
        short_screener,
        "_calc_indicators",
        lambda _close, _volume: {
            "price": 4_000.0,
            "ma50": 3_200.0,
            "rsi": 86.0,
            "pct_from_ma50": 25.0,
            "vol20": 42.0,
            "avg_volume_30d": 2_000_000,
        },
    )
    monkeypatch.setattr(
        short_screener,
        "_get_sector_cached",
        lambda _ticker: {
            "name": "Toyota",
            "sector": "Consumer Cyclical",
            "short_ratio": 3.0,
        },
    )
    monkeypatch.setattr(
        short_screener,
        "_short_fundamental_overlay",
        lambda _ticker: {"fundamental_score": 50.0, "flags": []},
    )
    monkeypatch.setattr(short_screener, "_save_candidates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        jp_loanability,
        "evaluate_short_tradeability",
        lambda ticker: {
            "ticker": ticker,
            "loanable": True,
            "loan_ratio": 2.0,
            "reverse_daily_fee": False,
            "untradeable": False,
            "reasons": [],
        },
    )

    payload = short_screener.screen_candidates(
        tickers=["7203.T"],
        regime="A_強気",
        include_holdings=False,
    )
    assert payload["candidates"], "gap at detect: BULL overheat fixture produced no short candidate"
    candidate = payload["candidates"][0]
    assert candidate["strategy"] == "bull_overheat_contrarian_short"
    assert candidate["observe_only"] is True
    assert candidate["human_execution_only"] is True
    assert candidate["risk_controls"]["size_cap_pct_nav"] <= 0.005
    assert "hard_stop_required" in candidate["constraints"]
    assert candidate["execution_cost_model"]["standard_credit_round_trip_cost_pct"] > 0
    assert candidate["tradeability"]["loanable"] is True

    output = catalyst_run(
        screener_payloads={"short": payload},
        catalyst_log_path=tmp_path / "catalyst_hypothesis_log.jsonl",
        analysis_id="investor-objectives-e2e",
        analysis_date="2026-06-25",
        write_log=False,
    )
    hypotheses = [
        h
        for h in output.all_hypotheses
        if h.ticker == "7203.T" and h.action_type == "short_sell"
    ]
    assert hypotheses, "gap at catalyst: overheat short candidate did not surface"
    hypothesis = hypotheses[0]
    assert hypothesis.observe_only is True
    assert hypothesis.human_execution_only is True
    assert hypothesis.risk_controls["requires_squeeze_guard"] is True
    assert hypothesis.risk_controls["size_cap_pct_nav"] <= 0.005
    assert hypothesis.risk_controls["stop_loss"]

    context = compact_for_opus(output, scenario_readiness=0.0, max_items=3)
    assert "OBSERVE-ONLY REVIEW" in context
    assert "7203.T" in context
    assert "short" in context.lower()
    assert "risk_controls" in context
    assert "human_execution_only" in context


def test_objective_06_stop_loss_breach_surfaces_to_swing_context_precondition(monkeypatch):
    """#6 stop-loss/thesis: LLM判断のため context-precondition のみ検証."""

    captured: dict[str, str] = {}

    def fake_call(_system, prompt, **_kwargs):
        captured["prompt"] = prompt
        return {"health": "caution", "summary": "captured", "priority_actions": []}

    monkeypatch.setattr(analyst, "call_tier_analysis", fake_call)
    monkeypatch.setattr(analyst, "_compute_ginn_vol", lambda _tickers: ("", {}))

    result = analyst._analyze_short_positions(
        {
            "positions": [
                {
                    "ticker": "CRWV",
                    "name": "CoreWeave",
                    "investment_type": "swing",
                    "shares": 10,
                    "value_jpy": 100_000,
                    "current_price": 70,
                    "unrealized_pct": -0.25,
                    "unrealized_jpy": -30_000,
                    "holding_days": 5,
                    "entry_date": "2026-06-01",
                    "stop_loss": 80,
                    "stop_loss_source": "manual",
                }
            ],
            "technical_state": {},
            "social_sentiment": {},
            "news": {},
            "earnings": {},
            "screen_candidates": [],
        }
    )

    assert not result.get("error")
    prompt = captured["prompt"]
    assert "CRWV" in prompt
    assert '"current_price": 70' in prompt
    assert '"stop_loss": 80' in prompt
    assert '"stop_loss_source": "manual"' in prompt
    assert "損切りライン" in prompt


def test_objective_10_earnings_surprise_surfaces_to_prompt_context_precondition():
    """#10 決算サプライズ: LLM判断のため context-precondition のみ検証."""

    text = fmt_earnings_section(
        {
            "NVDA": {
                "status": "reported",
                "date": "2026-06-24",
                "days_ago": 1,
                "eps_actual": 6.0,
                "eps_estimate": 5.0,
                "beat_miss": "beat",
                "surprise_pct": 20.0,
            }
        },
        tickers=["NVDA"],
    )

    assert "NVDA" in text
    assert "✅ beat" in text
    assert "surprise: +20.0%" in text


def test_objective_17_regime_shift_stance_context_surfaces_precondition(monkeypatch):
    """#17 レジームシフト stance伝播: LLM判断のため context-precondition のみ検証."""

    monkeypatch.setattr(
        analyst,
        "_compute_regime_consensus",
        lambda _data: "## レジーム合意\nBULL->BEAR shift candidate",
    )

    context = analyst._build_shared_market_context(
        {
            "market_meta": {
                "vix": 32,
                "vix_level": "high",
                "us10y_yield": {"value": 4.8, "change_pct": 0.1},
                "us2y_yield": {"value": 4.6},
                "yield_curve_spread": 0.2,
                "yield_curve_status": "normalizing",
                "dxy": {"value": 106, "change_pct": 0.4},
                "crude_oil": {"value": 82, "change_pct": 1.0},
                "gold": {"value": 2400},
                "sp500_vs_ma50_pct": -3.5,
                "nikkei_vs_ma50_pct": -4.0,
                "nikkei": {"value": 38000, "change_pct": -2.0},
            },
            "scenario": {
                "key": "CRASH",
                "name": "クラッシュ/危機",
                "actions": ["現金比率を40〜60%まで引き上げ"],
                "high_return_opportunities": [],
                "short_allowed": True,
                "leverage_allowed": False,
            },
            "regime": {"spy_above": False, "nk_above": False},
            "guard_state": {"daily_pnl_pct": -6.0},
            "risk": {"current_dd": -0.1, "var_status": "elevated"},
            "positions": [{"ticker": "NVDA", "value_jpy": 1_000_000}],
        }
    )

    assert "現在のシナリオ: CRASH" in context
    assert "空売り許可: True" in context
    assert "VIX: 32" in context
    assert "BULL->BEAR shift candidate" in context
