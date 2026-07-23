"""T16: dynamic FX hedge regime × VIX matrix"""
import fx_hedge_manager as fx


def test_bull_low_vix_zero():
    r = fx.compute_target_hedge_ratio(
        regime='bull', vix=15, usdjpy=150, current_hedge_ratio=0.0,
    )
    assert r['base_target'] == 0.0
    assert r['target_hedge_ratio'] == 0.0


def test_neutral_mid_vix():
    r = fx.compute_target_hedge_ratio(
        regime='neutral', vix=22, usdjpy=150,
        current_hedge_ratio=0.10,  # no whipsaw from 10%
    )
    assert r['base_target'] == 0.10


def test_bear_high_iv_triggers_40():
    r = fx.compute_target_hedge_ratio(
        regime='bear', vix=25, usdjpy=150, usdjpy_iv_1m=0.13,
        current_hedge_ratio=0.35,  # close, no whipsaw
    )
    assert r['base_target'] == 0.40


def test_crisis_60():
    r = fx.compute_target_hedge_ratio(
        regime='crisis', vix=35, usdjpy=140,
        usdjpy_iv_1m=0.15, usdjpy_mom_1m=-0.06,
        current_hedge_ratio=0.55,
    )
    assert r['base_target'] == 0.60


def test_jpy_overheating_addon():
    r = fx.compute_target_hedge_ratio(
        regime='neutral', vix=22, usdjpy=160,
        usdjpy_sma_90d=145,  # +10.3%
        current_hedge_ratio=0.10,
    )
    assert 0.10 in r['addons'].values()


def test_whipsaw_clamp():
    r = fx.compute_target_hedge_ratio(
        regime='crisis', vix=35, usdjpy=140,
        current_hedge_ratio=0.0,
    )
    # base=60% but daily delta cap 10% from 0 → 10%
    assert r['target_hedge_ratio'] == 0.10


def test_upper_bound():
    r = fx.compute_target_hedge_ratio(
        regime='crisis', vix=40, usdjpy=170,
        usdjpy_sma_90d=150, usdjpy_avg_5y=130,  # +10 addons
        current_hedge_ratio=0.80,
    )
    assert r['target_hedge_ratio'] <= 0.70
