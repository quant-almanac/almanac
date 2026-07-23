"""
tests/test_playbook_injection_partial.py — 2026-07-07 改善3点セット

1. scenario_engine: partial 発動 (required 全成立 / min_signals 未達 → allocation_scale 0.5)
   + news fallback の priority=high 限定 severity=medium 緩和
2. rebalance_engine: 持株会 (EMPLOYER_STOCK_TICKERS) の配分母数からの除外
3. analyst._inject_playbook_actions: プレイブック候補の deterministic 注入
"""
import json
from datetime import datetime
from pathlib import Path

import pytest

import scenario_engine
import rebalance_engine as re_engine
import analyst


# ════════════════════════════════════════════════════════════
# 1a. news fallback — priority=high は severity=medium を弱シグナル採用
#     (2026-06 イラン停戦: medium/conf0.65 が除外され war_end 不発)
# ════════════════════════════════════════════════════════════

def _geo_state_medium(scenario_key: str, confidence: float = 0.65) -> dict:
    return {
        "active_alerts": [],
        "keyword_matches": [
            {
                "scenario_key": scenario_key,
                "score": 2,
                "matched_keywords": ["ceasefire", "イラン停戦"],
                "severity": "medium",
                "confidence": confidence,
                "assessment_status": "assessed",
            }
        ],
    }


def test_news_fallback_accepts_medium_severity_for_high_priority():
    scenario = {
        "id": "war_end",
        "priority": "high",
        "detect": {"news_keywords": ["ceasefire"], "min_keyword_score": 2},
    }
    result = scenario_engine._eval_news(scenario, _geo_state_medium("war_end"))
    assert result["matched"] is True


def test_news_fallback_still_rejects_medium_for_normal_priority():
    scenario = {
        "id": "tariff_war",
        "priority": "medium",
        "detect": {"news_keywords": ["tariff"], "min_keyword_score": 2},
    }
    geo = _geo_state_medium("tariff_war")
    geo["keyword_matches"][0]["matched_keywords"] = ["tariff", "sanctions"]
    result = scenario_engine._eval_news(scenario, geo)
    assert result["matched"] is False


# ════════════════════════════════════════════════════════════
# 1b. partial 発動 — required 全成立 + min_signals 未達
# ════════════════════════════════════════════════════════════

def _war_end_like_scenario() -> dict:
    return {
        "id": "war_end_test",
        "name": "戦争終結ラリー(テスト)",
        "priority": "high",
        "detect": {
            "news_keywords": ["ceasefire"],
            "min_keyword_score": 2,
            "indicators": {
                "vix": {"condition": "below", "threshold": 18, "key": "vix_current"},
            },
            "min_signals": 3,
            "required_signals": ["news_keywords", "vix"],
        },
        "actions": {"phase_1": {"buy": [{"ticker": "TQQQ", "allocation_usd": 5000}]}},
    }


def _setup_engine_paths(tmp_path, monkeypatch, geo_state: dict):
    playbook_path = tmp_path / "scenario_playbook.json"
    playbook_path.write_text(
        json.dumps({"scenarios": [_war_end_like_scenario()]}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / "vix_state.json").write_text(json.dumps({"vix": {"level": 15.5}}), encoding="utf-8")
    (tmp_path / "geopolitical_state.json").write_text(json.dumps(geo_state, ensure_ascii=False), encoding="utf-8")
    for name in ("technical_state.json", "macro_state.json", "regime_state.json",
                 "market_snapshot.json", "guard_state.json"):
        (tmp_path / name).write_text(json.dumps({}), encoding="utf-8")

    monkeypatch.setattr(scenario_engine, "PLAYBOOK_PATH", playbook_path)
    monkeypatch.setattr(scenario_engine, "VIX_STATE_PATH", tmp_path / "vix_state.json")
    monkeypatch.setattr(scenario_engine, "GEO_STATE_PATH", tmp_path / "geopolitical_state.json")
    monkeypatch.setattr(scenario_engine, "TECH_STATE_PATH", tmp_path / "technical_state.json")
    monkeypatch.setattr(scenario_engine, "MACRO_STATE_PATH", tmp_path / "macro_state.json")
    monkeypatch.setattr(scenario_engine, "REGIME_STATE_PATH", tmp_path / "regime_state.json")
    monkeypatch.setattr(scenario_engine, "MARKET_SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    monkeypatch.setattr(scenario_engine, "GUARD_STATE_PATH", tmp_path / "guard_state.json")
    monkeypatch.setattr(scenario_engine, "SCENARIO_STATE_PATH", tmp_path / "scenario_state.json")

    sent = []
    monkeypatch.setattr(scenario_engine, "send_telegram", lambda msg: sent.append(msg))
    return sent


def test_required_signals_met_but_min_signals_fail_becomes_partial(tmp_path, monkeypatch):
    """2026-06 イラン停戦リプレイ: news(medium/0.65 fallback)+VIX 成立 / 5日窓指標なしで
    min_signals 3 未達 → 旧: dormant 落ちで不発 / 新: partial (scale 0.5) で限定発動。"""
    sent = _setup_engine_paths(tmp_path, monkeypatch, _geo_state_medium("war_end_test"))

    state = scenario_engine.evaluate_scenarios()
    sc = state["scenarios"]["war_end_test"]

    assert sc["min_signals_fail"] is True
    assert sc["missing_required_signals"] == []
    assert sc["status"] == "partial"
    assert sc["allocation_scale"] == 0.5
    assert state["partial_count"] == 1
    # ALMANAC: telegram disabled — ai_analysis only (send_telegram no longer invoked)
    assert sent == []


def test_required_signal_missing_still_falls_to_dormant(tmp_path, monkeypatch):
    """required (news) 不成立なら従来どおり dormant 落ち (partial にしない)。"""
    geo = {"active_alerts": [], "keyword_matches": []}
    _setup_engine_paths(tmp_path, monkeypatch, geo)

    state = scenario_engine.evaluate_scenarios()
    sc = state["scenarios"]["war_end_test"]

    assert sc["status"] == "dormant"
    assert sc["allocation_scale"] == 0.0
    assert sc["activation_policy_status"] == "not_configured"
    assert sc["activation_policy_passed"] is None


def test_war_end_activation_policy_rejects_fallback_news_and_opposite_markets(tmp_path, monkeypatch):
    scenario = _war_end_like_scenario()
    scenario["detect"]["indicators"].update({
        "vix_delta_5d": {"condition": "drop_pct", "threshold": -20},
        "oil_wti": {"condition": "drop_pct_5d", "threshold": -10},
    })
    scenario["detect"]["activation_policy"] = {
        "required_any": [["oil_wti", "vix_delta_5d"]],
        "ai_fallback_cannot_satisfy_required": ["news_keywords"],
        "freshness_requirements": {
            "vix": {"state": "vix", "max_age_minutes": 60},
            "oil_wti": {"state": "vix", "max_age_minutes": 60},
            "vix_delta_5d": {"state": "vix", "max_age_minutes": 60},
        },
        "contradiction_veto": [
            {"key": "oil_wti", "condition": "above", "threshold": 0},
            {"key": "vix_delta_5d", "condition": "above", "threshold": 0},
        ],
    }
    playbook_path = tmp_path / "scenario_playbook.json"
    playbook_path.write_text(json.dumps({"scenarios": [scenario]}), encoding="utf-8")
    (tmp_path / "vix_state.json").write_text(json.dumps({
        "cached_at": datetime.now().isoformat(),
        "vix": {"level": 17.05, "change_5d": 3.3},
        "oil": {"change_5d_pct": 6.7},
    }), encoding="utf-8")
    (tmp_path / "geopolitical_state.json").write_text(
        json.dumps(_geo_state_medium("war_end_test")), encoding="utf-8"
    )
    for name in ("technical_state.json", "macro_state.json", "regime_state.json",
                 "market_snapshot.json", "guard_state.json"):
        (tmp_path / name).write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(scenario_engine, "PLAYBOOK_PATH", playbook_path)
    monkeypatch.setattr(scenario_engine, "VIX_STATE_PATH", tmp_path / "vix_state.json")
    monkeypatch.setattr(scenario_engine, "GEO_STATE_PATH", tmp_path / "geopolitical_state.json")
    monkeypatch.setattr(scenario_engine, "TECH_STATE_PATH", tmp_path / "technical_state.json")
    monkeypatch.setattr(scenario_engine, "MACRO_STATE_PATH", tmp_path / "macro_state.json")
    monkeypatch.setattr(scenario_engine, "REGIME_STATE_PATH", tmp_path / "regime_state.json")
    monkeypatch.setattr(scenario_engine, "MARKET_SNAPSHOT_PATH", tmp_path / "market_snapshot.json")
    monkeypatch.setattr(scenario_engine, "GUARD_STATE_PATH", tmp_path / "guard_state.json")
    monkeypatch.setattr(scenario_engine, "SCENARIO_STATE_PATH", tmp_path / "scenario_state.json")

    state = scenario_engine.evaluate_scenarios()
    result = state["scenarios"]["war_end_test"]

    assert result["status"] not in {"active", "partial"}
    assert result["allocation_scale"] == 0.0
    assert result["activation_policy_status"] == "failed"
    assert result["activation_policy_passed"] is False
    codes = {row["code"] for row in result["activation_policy_failures"]}
    assert "activation_required_signal_ai_fallback" in codes
    assert "activation_required_any_missing" in codes
    assert "activation_contradiction_veto" in codes


def test_market_time_only_snapshot_fails_freshness_policy_closed():
    scenario = {
        "id": "japan_standalone_bull",
        "detect": {
            "activation_policy": {
                "freshness_requirements": {
                    "nikkei_or_topix_above_ma50": {
                        "state": "market",
                        "max_age_minutes": 1440,
                    },
                },
            },
        },
    }
    details = [{
        "type": "technical",
        "key": "nikkei_or_topix_above_ma50",
        "matched": True,
        "detail": "日経NK225 ma50_diff=+6.62%",
    }]

    failures = scenario_engine._activation_policy_failures(
        scenario,
        details,
        states={"market": {"as_of": "08:18"}},
        now=datetime.now(),
    )

    assert failures == [{
        "code": "activation_source_stale",
        "signal": "nikkei_or_topix_above_ma50",
        "state": "market",
        "age_minutes": None,
        "max_age_minutes": 1440.0,
    }]
    assert details[0]["matched"] is False
    assert details[0]["detail"] == scenario_engine.INCONCLUSIVE_DETAIL


# ════════════════════════════════════════════════════════════
# 2. 持株会の配分母数除外
# ════════════════════════════════════════════════════════════

def _snapshot_with_employer() -> dict:
    return {
        "total_jpy": 12_000_000,
        "positions": [
            {"ticker": "9999.T", "investment_type": "long", "currency": "JPY",
             "value_jpy": 2_000_000, "sector": "Industrials"},
            {"ticker": "1489.T", "investment_type": "long", "currency": "JPY",
             "value_jpy": 2_000_000, "sector": "ETF"},
            {"ticker": "AAPL", "investment_type": "long", "currency": "USD",
             "value_jpy": 8_000_000, "sector": "Technology"},
        ],
    }


def test_build_core_snapshot_excludes_employer_by_default():
    core = re_engine.build_core_snapshot(_snapshot_with_employer())
    tickers = {p["ticker"] for p in core["positions"]}
    assert "9999.T" not in tickers
    assert core["total_jpy"] == 10_000_000
    # JPY 比率は持株会除外後の 2M/10M = 20%
    assert core["currency_breakdown"]["JPY"]["ratio"] == pytest.approx(0.2)


def test_build_core_snapshot_can_include_employer_explicitly():
    core = re_engine.build_core_snapshot(_snapshot_with_employer(), exclude_employer=False)
    tickers = {p["ticker"] for p in core["positions"]}
    assert "9999.T" in tickers
    assert core["total_jpy"] == 12_000_000


# ════════════════════════════════════════════════════════════
# 3. _inject_playbook_actions
# ════════════════════════════════════════════════════════════

_JP_PLAYBOOK = {
    "scenarios": [
        {
            "id": "japan_standalone_bull",
            "name": "日本株単独強気",
            "priority": "medium",
            "actions": {
                "phase_1": {
                    "buy": [
                        {"ticker": "1489.T", "allocation_jpy": 500000, "reason": "日本高配当ETF"},
                        {"ticker": "1306.T", "allocation_jpy": 500000, "reason": "TOPIX連動ETF"},
                    ]
                },
                # phase_2 は confirmation 前提 — 注入されないこと
                "phase_2": {"buy": [{"ticker": "9999.T", "allocation_jpy": 500000}]},
            },
        }
    ]
}


def _fake_load_json_factory(playbook=None, executions=None, insider=None):
    def _fake(path, default=None):
        name = Path(path).name
        if name == "scenario_playbook.json":
            return playbook if playbook is not None else _JP_PLAYBOOK
        if name == "insider_restricted.json":
            return insider if insider is not None else {"tickers": ["9999.T"]}
        if name == "action_executions.json":
            return {"executions": executions if executions is not None else []}
        return default
    return _fake


def _base_data(status="active", scale=1.0, jp_pct=1.6, jp_target=10.0, total=30_000_000):
    return {
        "portfolio_total": total,
        "jp_exposure": {"jp_equity_ex_employer_pct": jp_pct, "target_pct": jp_target},
        "scenario_monitoring": {
            "active_scenarios": [
                {"id": "japan_standalone_bull", "name": "日本株単独強気",
                 "status": status, "allocation_scale": scale,
                 "readiness_pct": 81, "priority": "medium"},
            ],
        },
    }


def test_inject_active_scenario_phase1_buys(monkeypatch):
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory())
    synthesis = {"priority_actions": [{"type": "trim", "ticker": "LLY"}]}
    result = analyst._inject_playbook_actions(synthesis, _base_data())

    injected_tickers = [a["ticker"] for a in result["injected"]]
    assert injected_tickers == ["1489.T", "1306.T"]
    # phase_2 は注入されない
    assert "9999.T" not in {a.get("ticker") for a in synthesis["priority_actions"]}
    actions = {a["ticker"]: a for a in synthesis["priority_actions"] if isinstance(a, dict)}
    a = actions["1489.T"]
    assert a["type"] == "buy"
    assert a["playbook_injected"] is True
    assert a["amount_hint"] == "¥500,000"
    assert a["source"] == "scenario_playbook"


def test_inject_partial_scenario_halves_allocation(monkeypatch):
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory())
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(
        synthesis, _base_data(status="partial", scale=0.5))
    assert result["injected"], "partial でも注入されること"
    assert synthesis["priority_actions"][0]["amount_hint"] == "¥250,000"
    assert synthesis["priority_actions"][0]["allocation_scale"] == 0.5


def test_inject_does_not_revive_explicit_zero_allocation(monkeypatch):
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory())
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(
        synthesis, _base_data(status="partial", scale=0.0))
    assert result["injected"] == []
    assert synthesis["priority_actions"] == []
    assert {row["reason"] for row in result["skipped"]} == {"allocation_scale_zero"}


def test_inject_skips_ticker_already_in_actions(monkeypatch):
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory())
    synthesis = {"priority_actions": [{"type": "buy", "ticker": "1489.T"}]}
    result = analyst._inject_playbook_actions(synthesis, _base_data())
    assert "1489.T" not in [a["ticker"] for a in result["injected"]]
    assert any(s["ticker"] == "1489.T" and "already" in s["reason"] for s in result["skipped"])


def test_inject_skips_recently_executed_buy(monkeypatch):
    """直近7日以内に executed/ordered の buy がある ticker は再注入しない。"""
    from datetime import datetime
    executions = [
        {"ticker": "1306.T", "direction": "buy", "status": "executed",
         "saved_at": datetime.now().isoformat()},
    ]
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory(executions=executions))
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(synthesis, _base_data())
    assert [a["ticker"] for a in result["injected"]] == ["1489.T"]
    assert any(s["ticker"] == "1306.T" and "実行/発注済み" in s["reason"]
               for s in result["skipped"])


def test_inject_reproposes_when_recommended_but_not_executed(monkeypatch):
    """推奨が出ただけで未執行なら抑制しない — シナリオ継続中は毎回再提案する
    (2026-07-07 ユーザー指示)。cancelled/skip 済みの執行記録も抑制対象にしない。"""
    from datetime import datetime
    executions = [
        {"ticker": "1489.T", "direction": "buy", "status": "cancelled",
         "saved_at": datetime.now().isoformat()},
    ]
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory(executions=executions))
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(synthesis, _base_data())
    # cancelled は執行ではない → 1489.T も再提案される
    assert [a["ticker"] for a in result["injected"]] == ["1489.T", "1306.T"]


def test_inject_skips_old_execution_outside_window(monkeypatch):
    """8日前の executed は 7 日窓の外 → 再注入してよい。"""
    from datetime import datetime, timedelta
    executions = [
        {"ticker": "1489.T", "direction": "buy", "status": "executed",
         "saved_at": (datetime.now() - timedelta(days=8)).isoformat()},
    ]
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory(executions=executions))
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(synthesis, _base_data())
    assert [a["ticker"] for a in result["injected"]] == ["1489.T", "1306.T"]


def test_inject_skips_jp_when_target_reached(monkeypatch):
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory())
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(
        synthesis, _base_data(jp_pct=12.0, jp_target=10.0))
    assert result["injected"] == []
    assert all("目標" in s["reason"] for s in result["skipped"])


def test_inject_respects_total_cap(monkeypatch):
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory())
    synthesis = {"priority_actions": []}
    # 総資産 15M → cap 5% = ¥750k → 500k+500k の 2 本目は上限超過で skip
    result = analyst._inject_playbook_actions(synthesis, _base_data(total=15_000_000))
    assert [a["ticker"] for a in result["injected"]] == ["1489.T"]
    assert any("上限" in s["reason"] for s in result["skipped"])


def test_inject_skips_insider_restricted(monkeypatch):
    playbook = {
        "scenarios": [
            {"id": "japan_standalone_bull", "name": "日本株単独強気",
             "actions": {"phase_1": {"buy": [{"ticker": "9999.T", "allocation_jpy": 500000}]}}}
        ]
    }
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory(playbook=playbook))
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(synthesis, _base_data())
    assert result["injected"] == []
    assert result["skipped"][0]["reason"] == "insider_restricted"


# ════════════════════════════════════════════════════════════
# 4. dynamic_jp_equity_target — 市場環境連動の日本株目標比率
# ════════════════════════════════════════════════════════════

from analyst.data_gatherer import dynamic_jp_equity_target


def _sm_with_jp(readiness_pct: float) -> dict:
    return {
        "active_scenarios": [
            {"id": "japan_standalone_bull", "status": "active",
             "readiness_pct": readiness_pct},
        ]
    }


def test_jp_target_scales_with_scenario_readiness():
    """readiness 81% → base 10% + 8.1pt = 18.1%。"""
    out = dynamic_jp_equity_target(10.0, scenario_monitoring=_sm_with_jp(81), vix=15.5)
    assert out["target_pct"] == pytest.approx(18.1)
    assert out["boost_pct"] == pytest.approx(8.1)


def test_jp_target_reverts_to_base_when_scenario_dormant():
    """dormant はシナリオ文脈に載らない → boost 0 = base。"""
    out = dynamic_jp_equity_target(10.0, scenario_monitoring={"active_scenarios": []}, vix=15.5)
    assert out["target_pct"] == pytest.approx(10.0)
    assert out["jp_scenario_readiness"] is None


def test_jp_target_frozen_on_high_vix():
    out = dynamic_jp_equity_target(10.0, scenario_monitoring=_sm_with_jp(81), vix=32.0)
    assert out["target_pct"] == pytest.approx(10.0)
    assert "VIX" in out["frozen_reason"]


def test_jp_target_frozen_on_guard_block():
    out = dynamic_jp_equity_target(
        10.0, scenario_monitoring=_sm_with_jp(81), vix=15.5,
        guard={"trading_allowed": True, "new_entry_allowed": False})
    assert out["target_pct"] == pytest.approx(10.0)
    assert "new_entry_allowed" in out["frozen_reason"]


def test_jp_target_clamped_to_max():
    out = dynamic_jp_equity_target(
        15.0, scenario_monitoring=_sm_with_jp(100), vix=15.5, max_pct=20.0)
    assert out["target_pct"] == pytest.approx(20.0)


def test_jp_target_fail_closed_on_garbage_inputs():
    out = dynamic_jp_equity_target(10.0, scenario_monitoring="broken", vix="n/a", guard=[])
    assert out["target_pct"] == pytest.approx(10.0)


def test_inject_converts_usd_allocation(monkeypatch):
    playbook = {
        "scenarios": [
            {"id": "war_end", "name": "戦争終結ラリー",
             "actions": {"phase_1": {"buy": [{"ticker": "TQQQ", "allocation_usd": 5000}]}}}
        ]
    }
    monkeypatch.setattr(analyst, "load_json", _fake_load_json_factory(playbook=playbook))
    import utils
    monkeypatch.setattr(utils, "get_fx_rate_cached",
                        lambda **kw: (150.0, "test"), raising=False)
    data = _base_data()
    data["scenario_monitoring"]["active_scenarios"] = [
        {"id": "war_end", "name": "戦争終結ラリー", "status": "partial",
         "allocation_scale": 0.5, "readiness_pct": 55, "priority": "high"},
    ]
    synthesis = {"priority_actions": []}
    result = analyst._inject_playbook_actions(synthesis, data)
    assert [a["ticker"] for a in result["injected"]] == ["TQQQ"]
    # $5000 × 150 × 0.5 = ¥375,000
    assert synthesis["priority_actions"][0]["amount_hint"] == "¥375,000"
