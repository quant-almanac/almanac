"""Step D 計測精緻化: 空売り 3レーンを outcome/certify レベルで分離する。

screener の short 候補は candidate['lane'](overheat/event/bear)を hypothesis_type に
反映し、レーン別に forward outcome を計測・昇格判定できるようにする。lane 無しは
従来どおり screener_short にフォールバック(後方互換)。lane_registry / promotion criteria
とも整合する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from almanac.observability.screener_hypotheses import extract_screener_packets
from almanac.observability.lane_registry import validate_lane_registry, REQUIRED_LANES
from scenario_invariants import OBSERVE_ONLY_PROMOTION_CRITERIA


def _short_payload(*cands):
    return {"short": {"candidates": list(cands)}}


def test_overheat_candidate_maps_to_overheat_lane():
    pkts = extract_screener_packets(
        _short_payload({"ticker": "NVDA", "composite_score": 90, "lane": "overheat"}),
        analysis_date="2026-06-27")
    p = next(p for p in pkts if p["ticker"] == "NVDA")
    assert p["hypothesis_type"] == "screener_short_overheat"
    assert "short_overheat" in p["source_event_id"]
    assert p["action_type"] == "short_sell"
    assert p["observe_only"] is True
    assert p["risk_controls"].get("short_lane") == "overheat"


def test_bear_and_event_lanes():
    pkts = extract_screener_packets(
        _short_payload(
            {"ticker": "AAA", "composite_score": 80, "lane": "bear"},
            {"ticker": "BBB", "composite_score": 80, "lane": "event"},
        ),
        analysis_date="2026-06-27")
    by = {p["ticker"]: p["hypothesis_type"] for p in pkts}
    assert by["AAA"] == "screener_short_bear"
    assert by["BBB"] == "screener_short_event"


def test_missing_lane_falls_back_to_screener_short():
    pkts = extract_screener_packets(
        _short_payload({"ticker": "XYZ", "composite_score": 70}),
        analysis_date="2026-06-27")
    assert pkts[0]["hypothesis_type"] == "screener_short"


def test_sub_lanes_produce_distinct_hypothesis_ids():
    pkts = extract_screener_packets(
        _short_payload(
            {"ticker": "SAME", "composite_score": 80, "lane": "overheat"},
            {"ticker": "SAME", "composite_score": 80, "lane": "bear"},
        ),
        analysis_date="2026-06-27")
    ids = [p["hypothesis_id"] for p in pkts if p["ticker"] == "SAME"]
    assert len(ids) == 2 and ids[0] != ids[1], "レーンが違えば別仮説として計測される"


def test_margin_long_lane_unaffected():
    pkts = extract_screener_packets(
        {"margin_long": {"candidates": [{"ticker": "T1", "score": 80}]}},
        analysis_date="2026-06-27")
    assert pkts[0]["hypothesis_type"] == "screener_margin_long"


def test_sub_lanes_registered_and_have_promotion_criteria():
    for sub in ("screener_short_overheat", "screener_short_event", "screener_short_bear"):
        assert sub in REQUIRED_LANES
        assert sub in OBSERVE_ONLY_PROMOTION_CRITERIA
    # registry 検証(必須レーン充足 + measured/measurement_path)が通る
    assert validate_lane_registry("lane_registry.json") == []
