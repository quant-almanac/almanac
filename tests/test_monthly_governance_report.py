"""monthly_governance_report: レーン横断ドラフト判定の回帰テスト"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import monthly_governance_report as mgr  # noqa: E402


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def test_missing_files_report_unavailable_not_fabricated(tmp_path):
    report = mgr.generate_report(base_dir=tmp_path)

    assert report["analyst_agents"]["available"] is False
    assert report["scenarios"]["available"] is False
    assert report["tax_harvest"]["available"] is False
    assert report["leveraged_decay"]["available"] is False


def test_generate_report_accepts_an_isolated_state_root(tmp_path):
    _write(tmp_path / "action_state.json", {"actions": {}})
    report = mgr.generate_report(base_dir=tmp_path)
    out_path = mgr.write_report(report, output_dir=tmp_path / "out")
    assert report["tax_harvest"]["available"] is True
    assert out_path.parent == tmp_path / "out"
    assert out_path.exists()


def test_leveraged_decay_zero_positions_is_insufficient_data_not_retire():
    section = mgr._leveraged_decay_section({"positions_total": 0, "positions_flagged": 0, "positions": []})
    assert section["verdict"] == "insufficient_data"
    assert section["positions_total"] == 0


def test_leveraged_decay_with_positions_still_insufficient_until_60_days():
    section = mgr._leveraged_decay_section({"positions_total": 2, "positions_flagged": 1, "positions": []})
    assert section["verdict"] == "insufficient_data"
    assert section["positions_total"] == 2
    assert section["positions_flagged"] == 1


def test_red_team_section_insufficient_data_when_no_verdicts(tmp_path):
    section = mgr._red_team_section(base_dir=tmp_path)
    assert section["available"] is True
    assert section["verdict"] == "insufficient_data"


def test_generate_report_includes_red_team_and_leveraged_decay_sections():
    report = mgr.generate_report()
    assert "red_team" in report
    assert "leveraged_decay" in report
    assert "leveraged_decay" not in report["lanes_without_instrumentation"]


def test_generate_report_includes_disclosure_features_section():
    report = mgr.generate_report()
    assert "disclosure_features" in report
    assert report["disclosure_features"]["available"] is True
    assert "disclosure_feature" not in report["lanes_without_instrumentation"]


def test_generate_report_includes_swing_lane_section():
    report = mgr.generate_report()
    assert "swing_lane" in report
    assert report["swing_lane"]["available"] is True
    assert "swing" not in report["lanes_without_instrumentation"]


def test_swing_lane_section_smoke_against_real_data():
    section = mgr._swing_lane_section()
    assert section["available"] is True
    assert "verdict" in section
    assert "n_closed" in section


def _shadow_trade(*, market, horizon, net_return, untradeable=False):
    return {
        "market": market,
        "horizon_days": horizon,
        "net_return": net_return,
        "untradeable": untradeable,
    }


def test_jp_event_drift_missing_file_reports_unavailable():
    section = mgr._jp_event_drift_section(None)
    assert section["available"] is False


def test_jp_event_drift_us_dominated_book_is_insufficient_not_promote():
    # 実データで観測した構成: US中心・JPほぼゼロ。全体成績が良くてもJPレーンは昇格させない
    data = {"trades": [_shadow_trade(market="US", horizon=20, net_return=0.05) for _ in range(40)]}
    section = mgr._jp_event_drift_section(data)
    assert section["verdict"] == "insufficient_data"
    assert "JP n=0" in section["reason"]


def test_jp_event_drift_promote_when_jp_meets_thresholds():
    trades = [_shadow_trade(market="JP", horizon=20, net_return=0.03) for _ in range(20)]
    trades += [_shadow_trade(market="JP", horizon=20, net_return=0.01) for _ in range(4)]
    trades += [_shadow_trade(market="JP", horizon=20, net_return=-0.01) for _ in range(8)]
    # n=32, hit=24/32=75%, mean=(0.6+0.04-0.08)/32=+1.75%
    section = mgr._jp_event_drift_section({"trades": trades})
    assert section["verdict"] == "promote"


def test_jp_event_drift_retire_when_jp_large_n_and_nonpositive_mean():
    trades = [_shadow_trade(market="JP", horizon=20, net_return=-0.005) for _ in range(50)]
    section = mgr._jp_event_drift_section({"trades": trades})
    assert section["verdict"] == "retire"


def test_jp_event_drift_untradeable_and_null_net_return_excluded():
    trades = [_shadow_trade(market="JP", horizon=20, net_return=0.05, untradeable=True) for _ in range(40)]
    trades += [_shadow_trade(market="JP", horizon=20, net_return=None) for _ in range(40)]
    section = mgr._jp_event_drift_section({"trades": trades})
    assert section["trade_count_total"] == 0
    assert section["verdict"] == "insufficient_data"


def test_generate_report_includes_jp_event_drift_section():
    report = mgr.generate_report()
    assert "jp_event_drift" in report


def test_screener_lane_missing_file_reports_unavailable():
    section = mgr._screener_lane_section(None)
    assert section["available"] is False


def _screener_book(returns):
    return {"generated_at": "x", "measured_return_count": len(returns),
            "pending_episode_count": 0, "returns": returns}


def test_screener_lane_insufficient_data_below_threshold():
    book = _screener_book([
        {"strategy": "モメンタム", "horizon_days": 20, "net_return": 0.05} for _ in range(10)
    ])
    section = mgr._screener_lane_section(book)
    row = next(r for r in section["rows"] if r["strategy"] == "モメンタム")
    assert row["verdict"] == "insufficient_data"


def test_screener_lane_promote_when_strategy_works():
    returns = [{"strategy": "モメンタム", "horizon_days": 20, "net_return": 0.03} for _ in range(24)]
    returns += [{"strategy": "モメンタム", "horizon_days": 20, "net_return": -0.01} for _ in range(8)]
    # n=32, hit=24/32=75%, mean>0
    section = mgr._screener_lane_section(_screener_book(returns))
    row = next(r for r in section["rows"] if r["strategy"] == "モメンタム")
    assert row["verdict"] == "promote"


def test_screener_lane_retire_when_strategy_loses_at_large_n():
    returns = [{"strategy": "逆張り", "horizon_days": 20, "net_return": -0.01} for _ in range(50)]
    section = mgr._screener_lane_section(_screener_book(returns))
    row = next(r for r in section["rows"] if r["strategy"] == "逆張り")
    assert row["verdict"] == "retire"


def test_generate_report_includes_screener_lane_section():
    report = mgr.generate_report()
    assert "screener_lane" in report
    assert "screener (momentum/long_term/margin_long/news)" not in report["lanes_without_instrumentation"]


def test_agent_promote_verdict(tmp_path, monkeypatch):
    path = tmp_path / "agent_reliability.json"
    _write(path, {
        "as_of": "2026-07-12T00:00:00Z",
        "agents": {
            "opus_final": {
                "final_decider/support": {
                    "n": 50, "measured_n": 20,
                    "win_rate": 0.7, "mean_excess_return_bps": 15.0,
                }
            }
        },
    })
    monkeypatch.setattr(mgr, "AGENT_RELIABILITY_PATH", path)

    section = mgr._analyst_agents_section(mgr._load_json(path))
    assert section["rows"][0]["verdict"] == "promote"


def test_agent_retire_verdict_on_low_win_rate(tmp_path):
    path = tmp_path / "agent_reliability.json"
    _write(path, {
        "agents": {"x": {"y": {"n": 30, "measured_n": 10, "win_rate": 0.3, "mean_excess_return_bps": -5.0}}},
    })
    section = mgr._analyst_agents_section(mgr._load_json(path))
    assert section["rows"][0]["verdict"] == "retire"


def test_agent_insufficient_data_below_measured_n_threshold(tmp_path):
    path = tmp_path / "agent_reliability.json"
    _write(path, {
        "agents": {"x": {"y": {"n": 5, "measured_n": 2, "win_rate": 0.9, "mean_excess_return_bps": 50.0}}},
    })
    section = mgr._analyst_agents_section(mgr._load_json(path))
    assert section["rows"][0]["verdict"] == "insufficient_data"


def test_scenario_promotion_ready_maps_to_promote(tmp_path):
    path = tmp_path / "scenario_promotion_summary.json"
    _write(path, {
        "by_scenario": {
            "test_scenario": {
                "measured_episodes": 10, "hit_rate": 0.65,
                "mean_excess_return_bps": 5.0, "promotion_ready": True,
                "auto_decision_stage": "full_decision",
            }
        }
    })
    section = mgr._scenario_section(mgr._load_json(path))
    assert section["rows"][0]["verdict"] == "promote"


def test_tax_harvest_execution_rate_and_retire_threshold():
    action_state = {
        "actions": {
            f"id{i}": {"action_type": "loss_harvest_sell", "status": "cancelled"}
            for i in range(8)
        }
    }
    action_state["actions"]["id_filled"] = {"action_type": "loss_harvest_sell", "status": "filled"}
    section = mgr._tax_harvest_section(action_state)
    assert section["n_total"] == 9
    assert section["n_filled"] == 1
    assert section["n_cancelled"] == 8
    assert section["execution_rate"] == round(1 / 9, 3)
    assert section["verdict"] == "retire"


def test_tax_harvest_no_entries_yet_is_insufficient_data_not_retire():
    section = mgr._tax_harvest_section({"actions": {}})
    assert section["verdict"] == "insufficient_data"


def test_main_isolated_writes_only_report_and_success_heartbeat(tmp_path, monkeypatch):
    _write(tmp_path / "action_state.json", {"actions": {}})
    heartbeats = []
    monkeypatch.setattr(mgr, "_record_heartbeat", lambda status, **kwargs: heartbeats.append((status, kwargs)))

    assert mgr.main(["--base-dir", str(tmp_path), "--output-dir", str(tmp_path / "reports")]) == 0
    assert list((tmp_path / "reports").glob("governance_*.json"))
    assert heartbeats[0][0] == "ok"
    assert heartbeats[0][1]["extra"]["automatic_promotion"] is False


def test_main_failure_is_fail_loud_and_records_error_heartbeat(tmp_path, monkeypatch):
    heartbeats = []
    monkeypatch.setattr(mgr, "generate_report", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("boom")))
    monkeypatch.setattr(mgr, "_record_heartbeat", lambda status, **kwargs: heartbeats.append((status, kwargs)))

    with pytest.raises(RuntimeError, match="boom"):
        mgr.main(["--base-dir", str(tmp_path), "--output-dir", str(tmp_path / "reports")])
    assert heartbeats == [("error", {"error": "RuntimeError: boom"})]
