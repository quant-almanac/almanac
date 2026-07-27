"""T16: dynamic FX hedge regime × VIX matrix"""
from datetime import datetime

import pytest

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


# ---------------------------------------------------------------------------
# Stage 7B: 日次制約の評価日単位冪等化 (プラン必須テスト)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_hedge_state(tmp_path, monkeypatch):
    monkeypatch.setattr(fx, "HEDGE_STATE", tmp_path / "hedge_target.json")
    monkeypatch.setattr(fx, "SHADOW_HEDGE_STATE", tmp_path / "hedge_target_shadow.json")
    return tmp_path


def test_three_calls_same_day_advance_only_one_delta_worth():
    """本題 (プラン必須テスト): 同日3回実行しても実質1回分 (±10pt) しか
    動かない。旧実装は persist_target が呼ぶたびに last_target を更新して
    いたため、同日3回で最大±30pt動いてしまっていた。"""
    day1 = datetime(2026, 7, 27, 9, 0, 0)

    # crisis (base=60%) を3回連続で評価・保存する。初期値0%から始まる。
    r1 = fx.compute_target_hedge_ratio('crisis', 40, 155, now=day1)
    fx.persist_target(r1)
    r2 = fx.compute_target_hedge_ratio('crisis', 40, 155, now=datetime(2026, 7, 27, 12, 0, 0))
    fx.persist_target(r2)
    r3 = fx.compute_target_hedge_ratio('crisis', 40, 155, now=datetime(2026, 7, 27, 18, 0, 0))
    fx.persist_target(r3)

    # 3回とも同じ結果になる (冪等) — 1回目のクランプ (0% → 10%) を超えて進まない
    assert r1['target_hedge_ratio'] == pytest.approx(0.10)
    assert r2['target_hedge_ratio'] == pytest.approx(0.10)
    assert r3['target_hedge_ratio'] == pytest.approx(0.10)


def test_next_business_day_advances_at_most_one_delta_from_previous_close():
    """本題 (プラン必須テスト): 翌営業日は前営業日確定値から最大10pt。"""
    day1 = datetime(2026, 7, 27, 9, 0, 0)
    day2 = datetime(2026, 7, 28, 9, 0, 0)

    r1 = fx.compute_target_hedge_ratio('crisis', 40, 155, now=day1)
    fx.persist_target(r1)
    assert r1['target_hedge_ratio'] == pytest.approx(0.10)  # 0% → 10% (1日目)

    r2 = fx.compute_target_hedge_ratio('crisis', 40, 155, now=day2)
    fx.persist_target(r2)
    assert r2['target_hedge_ratio'] == pytest.approx(0.20)  # 10% → 20% (2日目、+10ptのみ)
    assert r2['previous_business_date_target'] == pytest.approx(0.10)


def test_rerunning_same_snapshot_does_not_duplicate_history():
    """本題 (プラン必須テスト): 同一 snapshot (同一評価日) の再実行で
    履歴を重複追加しない。"""
    day1 = datetime(2026, 7, 27, 9, 0, 0)
    for hour in (9, 12, 18):
        r = fx.compute_target_hedge_ratio('crisis', 40, 155, now=datetime(2026, 7, 27, hour, 0, 0))
        fx.persist_target(r)

    state = fx._load_state()
    assert len(state['history']) == 1  # 3回実行しても履歴は1件のまま


def test_shadow_mode_off_does_nothing():
    result = fx.run_hedge_shadow('crisis', 40, 155, mode=fx.HEDGE_MODE_OFF)
    assert result == {'mode': fx.HEDGE_MODE_OFF, 'skipped': True}


def test_shadow_mode_invalid_raises():
    with pytest.raises(ValueError, match="mode"):
        fx.run_hedge_shadow('crisis', 40, 155, mode='enforce')  # enforce は許可しない


def test_shadow_execution_never_touches_actual_state():
    """本題 (プラン必須テスト): shadow が actual notional (本番 state) を変えない。"""
    now = datetime(2026, 7, 27, 9, 0, 0)
    fx.run_hedge_shadow('crisis', 40, 155, mode=fx.HEDGE_MODE_SHADOW, now=now)

    assert not fx.HEDGE_STATE.exists()          # 本番 state は一切作られない
    assert fx.SHADOW_HEDGE_STATE.exists()        # shadow state だけが作られる


def test_shadow_execution_does_not_read_actual_state_as_baseline():
    """影実行と本番実行は別々の基準値を持つ (state が完全分離)。"""
    now1 = datetime(2026, 7, 27, 9, 0, 0)
    r_actual = fx.compute_target_hedge_ratio('crisis', 40, 155, now=now1)
    fx.persist_target(r_actual)  # 本番 state: 0% → 10%

    now2 = datetime(2026, 7, 28, 9, 0, 0)
    r_shadow = fx.run_hedge_shadow('crisis', 40, 155, mode=fx.HEDGE_MODE_SHADOW, now=now2)
    # shadow は本番の10%を基準にせず、shadow 独自の0%から始まる
    assert r_shadow['previous_business_date_target'] == pytest.approx(0.0)


def test_shadow_advisory_mode_also_isolated():
    now = datetime(2026, 7, 27, 9, 0, 0)
    result = fx.run_hedge_shadow('crisis', 40, 155, mode=fx.HEDGE_MODE_ADVISORY, now=now)
    assert result['mode'] == fx.HEDGE_MODE_ADVISORY
    assert not fx.HEDGE_STATE.exists()


# ---------------------------------------------------------------------------
# Stage 7B: vehicle 別 adapter (置換 vs overlay)
# ---------------------------------------------------------------------------


def test_hedged_etf_is_replacement_not_overlay():
    result = fx.resolve_vehicle_adapter('2634.T', corresponding_unhedged_holding_jpy=1_000_000)
    assert result.adapter_kind == 'replacement'
    assert result.replaceable_up_to_jpy == pytest.approx(1_000_000)


def test_hedged_etf_without_corresponding_holding_is_unavailable():
    """本題: 対応する無ヘッジ資産の保有額が不明なら unavailable
    (0円分の置換ができると憶測しない)。"""
    result = fx.resolve_vehicle_adapter('2634.T', corresponding_unhedged_holding_jpy=None)
    assert result.adapter_kind == 'unavailable'
    assert result.replaceable_up_to_jpy is None
    assert result.reason is not None


def test_futures_fx_instrument_is_overlay():
    instrument = fx.ACTIVE_HEDGE_INSTRUMENTS[0]
    result = fx.resolve_vehicle_adapter(instrument)
    assert result.adapter_kind == 'overlay'
    assert result.replaceable_up_to_jpy is None


def test_unknown_vehicle_is_unavailable():
    result = fx.resolve_vehicle_adapter('UNKNOWN_TICKER')
    assert result.adapter_kind == 'unavailable'
