"""T4: バックテスト lookahead bias 除去（entry = i+1 日目 Open）"""
import pandas as pd
import numpy as np
import backtest as bt


def _synthetic_hist(n=50, close_start=100.0, step=1.0):
    """合成 OHLCV: Open を Close とズラして i+1 Open が一意に識別できるようにする。"""
    dates = pd.date_range('2025-01-01', periods=n, freq='B')
    close = close_start + np.arange(n) * step
    # Open は Close の +0.5（同日内の価格ギャップ）。よって i+1 日目 Open は close[i+1] + 0.5
    open_ = close + 0.5
    high = close + 2.0
    low  = close - 2.0
    atr  = np.ones(n) * 1.0
    return pd.DataFrame({
        'Date': dates, 'Open': open_, 'High': high, 'Low': low, 'Close': close, 'ATR': atr,
    })


def test_entry_is_next_day_open():
    hist = _synthetic_hist(n=50)
    entry_idx = 10
    # signal 判定日 = entry_idx (close = 100 + 10*1 = 110), 約定日 = entry_idx+1 (open = 111.5)
    pnl, reason, _ = bt.simulate_trade(hist, entry_idx, hold_days=3, stop_atr_multiplier=10.0,
                                       trail_days=100, is_japan=False)
    # hold_days=3 → 最終日 close = 110 + 4 = 114 → (114 - 111.5)/111.5 *100 ≈ 2.242% − cost
    # 約定価格が 111.5（next-day open）である確認: signal 日 close (110) を使っていたら +3.636%
    expected_entry = 111.5   # i+1 day open
    expected_exit  = 114.0   # i+1+3 day close (entry_idx+4 = 14, close = 114)
    expected_raw_pct = (expected_exit - expected_entry) / expected_entry * 100
    expected_cost = bt._round_trip_cost_pct(is_japan=False)
    expected_pnl = expected_raw_pct - expected_cost
    assert abs(pnl - expected_pnl) < 1e-6, f'pnl {pnl} != expected {expected_pnl}'


def test_returns_zero_when_no_next_day():
    """entry_idx + 1 が存在しない場合、約定不能で 0%"""
    hist = _synthetic_hist(n=20)
    pnl, reason, hold = bt.simulate_trade(hist, entry_idx=19, hold_days=5, is_japan=False)
    assert pnl == 0.0
    assert 'シグナル無効' in reason


def test_stop_loss_uses_future_bars_only():
    """ストップ監視は entry_idx+2 以降の Low のみ参照（約定日の Low で即 stop しない）"""
    hist = _synthetic_hist(n=50)
    # ATR を大きくしてストップを深く設定、タイムストップまで走るはず
    hist['ATR'] = 5.0
    pnl, reason, _ = bt.simulate_trade(hist, entry_idx=10, hold_days=3, stop_atr_multiplier=10.0,
                                       trail_days=100, is_japan=False)
    assert reason == 'タイムストップ'   # stop/trail 発動せず
