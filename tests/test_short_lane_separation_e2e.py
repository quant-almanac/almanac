"""Step D 受入 E2E: 空売り 3レーン分離(overheat / event / bear)。

outcome 契約:
- screener の technical 候補は lane を持つ。BULL過熱逆張り → overheat、
  弱気レジーム regime_short → bear。単一の classify_short_lane を経由する。
- catalyst の dilution/going-concern short_sell 仮説は event レーンとして
  risk_controls['short_lane'] に記録され、outcome 計測がレーン別に分離できる。
- 全レーンとも observe_only / human_execution_only 不変(自動発注しない)。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ── screener 側: lane 付与 ────────────────────────────────

def test_screener_overheat_candidate_is_overheat_lane():
    import short_screener as ss
    cand = {"ticker": "NVDA", "rsi": 85.0, "pct_from_ma50": 25.0,
            "strategy": "bull_overheat_contrarian_short"}
    assert ss._assign_short_lane(cand, "A_強気") == "overheat"


def test_screener_regime_short_candidate_is_bear_lane():
    import short_screener as ss
    cand = {"ticker": "XYZ", "rsi": 67.0, "pct_from_ma50": 12.0,
            "strategy": "regime_short"}
    assert ss._assign_short_lane(cand, "C_弱気") == "bear"


def test_screener_lane_uses_single_classifier():
    """screener の lane 判定は short_universe.classify_short_lane を経由する。"""
    import short_screener as ss
    import short_universe as su
    # overheat の閾値(rsi>=80 & ma50>=20)は classifier と一致
    assert su.classify_short_lane({"rsi": 85, "ma50_diff_pct": 25}) == "overheat"
    cand = {"ticker": "NVDA", "rsi": 85.0, "pct_from_ma50": 25.0,
            "strategy": "bull_overheat_contrarian_short"}
    assert ss._assign_short_lane(cand, "A_強気") == "overheat"


# ── catalyst 側: dilution/going-concern → event レーン ────

def _dilution_feature(**over):
    row = {
        "ticker": "1234.T",
        "source_event_id": "tdnet-1234-dilution",
        "dilution_flag": True,
        "dilution_pct": 0.08,
        "compute_time": "2026-06-25T15:00:00+09:00",
        "summary": "第三者割当による新株式発行",
    }
    row.update(over)
    return row


def test_catalyst_dilution_short_is_event_lane():
    from almanac.observability.catalyst_layer import synthesize_from_disclosure_features
    hyps = synthesize_from_disclosure_features(
        [_dilution_feature()], analysis_id="t-d", analysis_date="2026-06-26")
    shorts = [h for h in hyps if h.action_type == "short_sell"]
    assert shorts, "dilution は short_sell 仮説になる"
    h = shorts[0]
    assert (h.risk_controls or {}).get("short_lane") == "event"
    # observe_only-first 不変
    assert h.observe_only is True
    assert h.human_execution_only is True


def test_catalyst_going_concern_short_is_event_lane():
    from almanac.observability.catalyst_layer import synthesize_from_disclosure_features
    row = _dilution_feature(dilution_flag=False, dilution_pct=None,
                            going_concern_flag=True,
                            source_event_id="tdnet-1234-gc",
                            summary="継続企業の前提に関する注記")
    hyps = synthesize_from_disclosure_features(
        [row], analysis_id="t-gc", analysis_date="2026-06-26")
    shorts = [h for h in hyps if h.action_type == "short_sell"]
    assert shorts
    assert (shorts[0].risk_controls or {}).get("short_lane") == "event"
