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


# ---------------------------------------------------------------------------
# Stage 3: FX 単体是正 (配線しない) の回帰テスト
# ---------------------------------------------------------------------------


def test_momentum_is_accepted_but_never_changes_the_ratio():
    """本題: usdjpy_mom_1m には検証済み係数もバックテストも無いため、
    どんな値を渡しても target_hedge_ratio/base_target/raw_target が
    変わらないこと (受け取って記録するだけで比率計算には使わない)。"""
    kwargs = dict(regime='crisis', vix=35, usdjpy=140, usdjpy_iv_1m=0.15, current_hedge_ratio=0.55)
    r_no_mom = fx.compute_target_hedge_ratio(usdjpy_mom_1m=0.0, **kwargs)
    r_big_mom = fx.compute_target_hedge_ratio(usdjpy_mom_1m=-0.30, **kwargs)
    assert r_no_mom['base_target'] == r_big_mom['base_target']
    assert r_no_mom['raw_target'] == r_big_mom['raw_target']
    assert r_no_mom['target_hedge_ratio'] == r_big_mom['target_hedge_ratio']
    # 記録はされる (デバッグ用の透明性は保つ)
    assert r_big_mom['inputs']['usdjpy_mom_1m'] == -0.30


def test_passive_hedge_etfs_excludes_confirmed_unhedged_products():
    """1655/2631/1545 は円建てだが無ヘッジ、2040 はレバレッジETNであり、
    いずれも「JPYヘッジ付きETF」の候補として提案してはならない。"""
    all_instruments = fx.PASSIVE_HEDGE_ETFS['sp500'] + fx.PASSIVE_HEDGE_ETFS['nasdaq']
    for wrong_ticker in ('1655.T', '2631.T', '1545.T', '2040.T'):
        assert wrong_ticker not in all_instruments


def test_passive_hedge_etfs_only_lists_confirmed_hedged_products():
    assert fx.PASSIVE_HEDGE_ETFS['sp500'] == ['2634.T']
    assert fx.PASSIVE_HEDGE_ETFS['nasdaq'] == ['2632.T']
    assert 'developed' not in fx.PASSIVE_HEDGE_ETFS  # 検証済み代替が無いため削除
    assert 'sector' not in fx.PASSIVE_HEDGE_ETFS


def test_recommend_method_never_suggests_delisted_wrong_products():
    for target in (0.10, 0.25, 0.40, 0.60):
        method = fx._recommend_method(target, 'crisis', 0.12)
        for wrong_ticker in ('1655.T', '2631.T', '1545.T', '2040.T'):
            assert wrong_ticker not in method['instruments']


def test_active_hedge_6j_direction_is_buy_not_sell():
    """本題: 6J は USD per JPY 建て (USDJPY と逆気配) のため、USD資産の
    円高ヘッジには買い建てが正しい。旧実装の「6J 先物売」は方向が逆で
    ヘッジどころか損失を増幅させる誤りだった。"""
    text = " ".join(fx.ACTIVE_HEDGE_INSTRUMENTS)
    assert "6J" in text
    assert "6J 先物買い" in text or ("6J" in text and "買い" in text)
    assert "6J 先物売" not in text
