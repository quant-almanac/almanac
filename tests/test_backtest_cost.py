"""T5: 取引コスト・スプレッドが往復で差し引かれる"""
import backtest as bt


def test_roundtrip_cost_us():
    # US: 49.5 + 5.0 = 54.5 bps 片道、往復 109 bps = 1.09%
    assert abs(bt._round_trip_cost_pct(is_japan=False) - 1.09) < 1e-9


def test_roundtrip_cost_jp():
    # JP: 5.0 + 2.0 = 7.0 bps 片道、往復 14 bps = 0.14%
    assert abs(bt._round_trip_cost_pct(is_japan=True) - 0.14) < 1e-9


def test_us_cost_higher_than_jp():
    us = bt._round_trip_cost_pct(is_japan=False)
    jp = bt._round_trip_cost_pct(is_japan=True)
    assert us > jp
    # US は JP の約 7-8 倍
    assert us / jp > 5
