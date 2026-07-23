import json
import sys
import types

import analyst
from analyst import data_gatherer as dg
from insider_restrictions import is_restricted_ticker


def _write_json(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _install_gather_data_test_stubs(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(dg, "BASE_DIR", tmp_path)
    monkeypatch.setattr(dg, "gather_market_indicators", lambda: {})
    monkeypatch.setattr(dg, "gather_jp_fundamentals", lambda: {})
    monkeypatch.setattr(dg, "gather_news", lambda: {"market": [], "holdings": {}})
    monkeypatch.setattr(dg, "gather_earnings_context", lambda tickers: {})

    import analyst.cache as cache
    import utils

    monkeypatch.setattr(cache, "write_progress", lambda *args, **kwargs: None)
    monkeypatch.setattr(utils, "get_fx_rate_cached", lambda *args, **kwargs: (150.0, "test"))

    portfolio_snapshot = {"positions": [], "total_jpy": 1_000_000}
    monkeypatch.setitem(
        sys.modules,
        "portfolio_manager",
        types.SimpleNamespace(build_portfolio_snapshot=lambda: portfolio_snapshot),
    )
    monkeypatch.setitem(
        sys.modules,
        "rebalance_engine",
        types.SimpleNamespace(calculate_rebalance_actions=lambda snap, available_cash: {}),
    )
    monkeypatch.setitem(
        sys.modules,
        "scenario_strategy",
        types.SimpleNamespace(get_strategy=lambda: {"scenario": "NEUTRAL", "name": "中立"}),
    )
    monkeypatch.setitem(
        sys.modules,
        "margin_manager",
        types.SimpleNamespace(get_summary=lambda: {}),
    )
    monkeypatch.setitem(
        sys.modules,
        "portfolio_integrity",
        types.SimpleNamespace(run_integrity_check=lambda: {"ok": True}),
    )
    monkeypatch.setitem(
        sys.modules,
        "tax_optimizer",
        types.SimpleNamespace(get_full_tax_report=lambda: {}),
    )
    monkeypatch.setitem(
        sys.modules,
        "espp_plan_manager",
        types.SimpleNamespace(get_dashboard_data=lambda portfolio_total: {}),
    )


def _seed_active_china_shock_state(tmp_path) -> None:
    _write_json(tmp_path / "holdings.json", {})
    _write_json(tmp_path / "account.json", {"balance": 0, "usd_balance": 0, "fx_rate_usdjpy": 150})
    _write_json(tmp_path / "guard_state.json", {})
    _write_json(tmp_path / "regime_state.json", {"spy_above": True, "nk_above": True})
    _write_json(tmp_path / "geopolitical_state.json", {"active_alerts": []})
    _write_json(tmp_path / "macro_state.json", {"fed_rate": 3.6, "yield_10y": 4.5})
    _write_json(tmp_path / "vix_state.json", {"vix": {"level": 16.0}, "sector_flows": {}})
    _write_json(
        tmp_path / "technical_state.json",
        {
            "tickers": {
                "9999.T": {"price": 2100, "rsi": 55, "change_20d_pct": 3.0},
                "1306.T": {"price": 3100, "rsi": 48, "change_20d_pct": 1.5},
                "GLD": {"price": 250, "rsi": 62, "change_20d_pct": 4.2},
            },
            "market_breadth": {"above_ma50_pct": 55},
        },
    )
    _write_json(
        tmp_path / "scenario_playbook.json",
        {
            "scenarios": [
                {
                    "id": "china_shock",
                    "description": "China shock monitor",
                    "priority": "high",
                    "enabled_for_decision": True,
                }
            ]
        },
    )
    _write_json(
        tmp_path / "scenario_state.json",
        {
            "evaluated_at": "2026-06-17T08:00:00+09:00",
            "overall_alert_level": "amber",
            "scenarios": {
                "china_shock": {
                    "status": "active",
                    "name": "China shock",
                    "readiness": 0.8,
                    "signals_met": 3,
                    "signals_total": 5,
                    "enabled_for_decision": True,
                    "observe_only": False,
                    "signal_details": [
                        {"matched": True, "type": "indicator", "key": "cnh", "detail": "CNH stress"},
                        {"matched": False, "type": "news", "key": "policy", "detail": "No escalation"},
                    ],
                    "recommended_actions": {
                        "phase_1": [
                            {"ticker": "9999.T", "allocation_jpy": 100_000, "reason": "restricted buy"},
                            {"ticker": "1306.T", "allocation_jpy": 100_000, "reason": "allowed jp hedge"},
                        ],
                        "phase_2": [
                            {"ticker": "9999", "action": "sell", "allocation_jpy": 50_000, "reason": "restricted sell"},
                            {"ticker": "GLD", "allocation_usd": 1_000, "reason": "allowed defensive asset"},
                        ],
                        "sell_on_trigger": ["9999.T", "GLD"],
                    },
                }
            },
        },
    )


def _china_shock_monitoring_context(tmp_path, monkeypatch) -> dict:
    _install_gather_data_test_stubs(monkeypatch, tmp_path)
    _seed_active_china_shock_state(tmp_path)

    result = dg.gather_data()
    scenario_monitoring = result["scenario_monitoring"]
    assert "error" not in scenario_monitoring
    return scenario_monitoring


def test_gather_data_filters_restricted_scenario_playbook_actions_and_triggers(tmp_path, monkeypatch):
    scenario_monitoring = _china_shock_monitoring_context(tmp_path, monkeypatch)
    active = scenario_monitoring["active_scenarios"]
    china_shock = next(sc for sc in active if sc["id"] == "china_shock")

    action_tickers = [action["ticker"] for action in china_shock["playbook_actions"]]
    assert all(not is_restricted_ticker(ticker) for ticker in action_tickers)
    assert action_tickers == ["1306.T", "GLD"]
    assert "allowed jp hedge" in {action["reason"] for action in china_shock["playbook_actions"]}
    assert "restricted buy" not in {action["reason"] for action in china_shock["playbook_actions"]}
    assert "restricted sell" not in {action["reason"] for action in china_shock["playbook_actions"]}

    sell_triggers = china_shock["sell_triggers"]
    assert sell_triggers and all("9999" not in trigger for trigger in sell_triggers)
    assert any(trigger.startswith("GLD") for trigger in sell_triggers)


def test_gather_data_exposes_short_candidate_meta(tmp_path, monkeypatch):
    _install_gather_data_test_stubs(monkeypatch, tmp_path)
    _seed_active_china_shock_state(tmp_path)
    _write_json(
        tmp_path / "short_candidates.json",
        {
            "scanned": 76,
            "shortable_count": 0,
            "vix_blocked": False,
            "candidates": [{"ticker": "TSLA"}],
        },
    )

    result = dg.gather_data()

    screening = result["screening"]
    assert screening["short_candidates"] == [{"ticker": "TSLA"}]
    assert screening["short_candidates_meta"] == {
        "scanned": 76,
        "shortable_count": 0,
        "vix_blocked": False,
    }


def test_scenario_monitoring_formatter_does_not_emit_restricted_tickers_from_gathered_context(tmp_path, monkeypatch):
    scenario_monitoring = _china_shock_monitoring_context(tmp_path, monkeypatch)

    text = analyst._fmt_scenario_monitoring(scenario_monitoring)

    assert "9999.T" not in text
    assert "restricted buy" not in text
    assert "restricted sell" not in text
    assert "1306.T" in text
    assert "GLD" in text
