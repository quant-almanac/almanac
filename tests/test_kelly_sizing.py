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
        'win_rate': 0.7, 'avg_win_pct': 0.1, 'avg_loss_pct': 0.03, 'n': 10,
    })
    assert r['entry_allowed']
    assert r['size_pct'] == k.CAPS_BY_ITYPE['long']  # 5% cap
    assert r['method'] == 'kelly'


def test_size_cap_swing():
    r = k.suggest_size_pct('CRWV', 'swing', overrides={
        'win_rate': 0.6, 'avg_win_pct': 0.08, 'avg_loss_pct': 0.03, 'n': 10,
    })
    assert r['size_pct'] == k.CAPS_BY_ITYPE['swing']  # 2% cap


def test_negative_kelly_rejected():
    r = k.suggest_size_pct('X', 'long', overrides={
        'win_rate': 0.4, 'avg_win_pct': 0.02, 'avg_loss_pct': 0.05, 'n': 10,
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
