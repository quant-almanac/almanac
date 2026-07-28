"""T18: half-Kelly + itype caps + negative Kelly rejection"""
import pytest
import kelly_sizing as k


def test_half_kelly_math():
    # p=0.6, b=0.05/0.03=1.667 → raw=(0.6*1.667-0.4)/1.667=0.36
    # half=0.18
    f = k.kelly_fraction(0.6, 0.05, 0.03)
    assert abs(f - 0.18) < 0.01


def test_kelly_zero_on_negative_ev():
    # p=0.4, avg_win=0.02, avg_loss=0.05 → b=0.4, raw = (0.4*0.4-0.6)/0.4 = -1.1 → 0
    assert k.kelly_fraction(0.4, 0.02, 0.05) == 0.0


def test_size_cap_long():
    r = k.suggest_size_pct('NVDA', 'long', overrides={
        'win_rate': 0.7, 'avg_win_pct': 0.1, 'avg_loss_pct': 0.03, 'n': 20,
    })
    assert r['entry_allowed']
    assert r['size_pct'] == k.CAPS_BY_ITYPE['long']  # 5% cap
    assert r['method'] == 'kelly'


def test_size_cap_swing():
    r = k.suggest_size_pct('CRWV', 'swing', overrides={
        'win_rate': 0.6, 'avg_win_pct': 0.08, 'avg_loss_pct': 0.03, 'n': 20,
    })
    assert r['size_pct'] == k.CAPS_BY_ITYPE['swing']  # 2% cap


def test_negative_kelly_rejected():
    r = k.suggest_size_pct('X', 'long', overrides={
        'win_rate': 0.4, 'avg_win_pct': 0.02, 'avg_loss_pct': 0.05, 'n': 20,
    })
    assert not r['entry_allowed']
    assert r['method'] == 'rejected'
    assert r['size_pct'] == 0.0


def test_fallback_insufficient_history():
    """P1-20: 履歴不足時は fail-safe (default-deny + 観察用 size のみ)"""
    r = k.suggest_size_pct('NEW', 'swing', overrides={
        'win_rate': 0.5, 'avg_win_pct': 0.05, 'avg_loss_pct': 0.05, 'n': 2,
        'sufficient': False,
    })
    assert r['method'] == 'fallback'
    # 履歴不足 = 期待値推定不能 = entry_allowed=False（default-deny）
    assert r['entry_allowed'] is False
    # 例外的に許可する場合の観察用 size (0.5%、cap 内)
    assert r['size_pct'] == k.FALLBACK_SIZE_PCT
    assert r['size_pct'] <= k.CAPS_BY_ITYPE['swing']


# ---------------------------------------------------------------------------
# Stage 6A: buy/add/dca のみを母集団にする + (analysis_id,ticker,direction) dedup
# ---------------------------------------------------------------------------


def _rec(ticker, type_, outcome_pct, *, analysis_id=None, verified=True, **extra):
    extra.setdefault('tier', 'swing')
    return {
        'ticker': ticker, 'type': type_, 'outcome_pct': outcome_pct,
        'verified': verified, 'analysis_id': analysis_id,
        **extra,
    }


def test_sell_type_recommendations_are_excluded_from_kelly_population():
    """本題: sell/trim/stop_loss/take_profit を符号反転して混ぜていた旧実装
    は、新規エントリーのサイジング根拠として不適切な母集団を作っていた。"""
    recs = [
        _rec('AVGO', 'buy', 5.0, analysis_id='a1'),
        _rec('AVGO', 'sell', -3.0, analysis_id='a2'),   # 除外されるべき
        _rec('AVGO', 'trim', 2.0, analysis_id='a3'),    # 除外されるべき
        _rec('AVGO', 'stop_loss', -8.0, analysis_id='a4'),  # 除外されるべき
        _rec('AVGO', 'take_profit', 4.0, analysis_id='a5'),  # 除外されるべき
    ]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['AVGO']['n'] == 1  # buy の1件のみ


def test_buy_add_dca_are_all_included():
    recs = [
        _rec('NVDA', 'buy', 5.0, analysis_id='a1'),
        _rec('NVDA', 'add', 3.0, analysis_id='a2'),
        _rec('NVDA', 'dca', -1.0, analysis_id='a3'),
    ]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['NVDA']['n'] == 3


def test_duplicate_analysis_id_ticker_is_deduped():
    """本題: 同一 analysis_id からの重複ログ行は母集団を水増ししない。"""
    recs = [
        _rec('AVGO', 'buy', 5.0, analysis_id='same-analysis'),
        _rec('AVGO', 'buy', 5.0, analysis_id='same-analysis'),  # 重複
        _rec('AVGO', 'buy', 3.0, analysis_id='different-analysis'),
    ]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['AVGO']['n'] == 2  # 重複1件を除いた2件


def test_missing_analysis_id_is_not_deduped_away():
    """analysis_id の無い古いログ行は dedup キーを構成できないため、
    fail-open で常に採用する (過去ログを一律で捨てない)。"""
    recs = [
        _rec('AVGO', 'buy', 5.0, analysis_id=None),
        _rec('AVGO', 'buy', 3.0, analysis_id=None),
    ]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['AVGO']['n'] == 2


def test_stats_entries_carry_direction_and_horizon():
    recs = [_rec('AVGO', 'buy', 5.0, analysis_id='a1')]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['AVGO']['direction'] == 'buy'
    assert stats['AVGO']['horizon'] == '5d'


def test_recommendation_kelly_stats_is_an_alias():
    """名称は recommendation_kelly 系にして実売買の勝率と誤認させない
    (プラン契約)。"""
    assert k.recommendation_kelly_stats is k.aggregate_ticker_stats


def test_unverified_entries_are_still_excluded():
    recs = [_rec('AVGO', 'buy', 5.0, analysis_id='a1', verified=False)]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert 'AVGO' not in stats


def test_signal_population_uses_signal_evaluable_not_execution_eligible():
    recs = [
        _rec(
            'AVGO', 'buy', 5.0, analysis_id='cash-blocked',
            signal_evaluable=True, execution_eligible=False,
        ),
        _rec(
            'AVGO', 'buy', -20.0, analysis_id='stale-input',
            signal_evaluable=False, execution_eligible=True,
        ),
    ]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['AVGO']['n'] == 1
    assert stats['AVGO']['win_rate'] == 1.0


def test_medium_uses_20d_outcome_instead_of_legacy_5d_value():
    recs = [
        _rec(
            'AVGO', 'buy', -99.0, analysis_id='medium-1', tier='medium',
            horizons={'5d': {'outcome_pct': -10.0}, '20d': {'outcome_pct': 6.0}},
        ),
    ]
    stats = k.aggregate_ticker_stats(recs, min_trades=1)
    assert stats['AVGO']['by_horizon']['20d']['win_rate'] == 1.0
    assert '5d' not in stats['AVGO']['by_horizon']


def test_long_without_60d_outcome_is_excluded():
    recs = [
        _rec(
            'AVGO', 'buy', 5.0, analysis_id='long-1', tier='long',
            horizons={'5d': {'outcome_pct': 5.0}, '20d': {'outcome_pct': 4.0}},
        ),
    ]
    assert 'AVGO' not in k.aggregate_ticker_stats(recs, min_trades=1)


def test_suggest_size_selects_matching_investment_horizon():
    stats = {
        'AVGO': {
            'by_horizon': {
                '5d': {
                    'win_rate': 0.2, 'avg_win_pct': 0.01,
                    'avg_loss_pct': 0.1, 'n': 20, 'sufficient': True,
                    'horizon': '5d',
                },
                '20d': {
                    'win_rate': 0.7, 'avg_win_pct': 0.08,
                    'avg_loss_pct': 0.03, 'n': 20, 'sufficient': True,
                    'horizon': '20d',
                },
            },
        },
    }
    result = k.suggest_size_pct('AVGO', 'medium', stats=stats)
    assert result['entry_allowed'] is True
    assert result['inputs']['horizon'] == '20d'
