"""T20: execution_quality slippage + 100bps alert"""
from datetime import datetime

import execution_quality as eq


def _mk(ticker, direction, price, bid, ask, ot='market'):
    return {
        'id':          f'{ticker}_{direction}',
        'ticker':      ticker,
        'direction':   direction,
        'price':       price,
        'bid_at_order': bid,
        'ask_at_order': ask,
        'order_type':  ot,
        'saved_at':    datetime.now().strftime('%Y-%m-%dT12:00:00'),
    }


def test_slippage_formula_buy():
    # buy at 100.5 with mid 100 → +50bps
    ex = _mk('X', 'buy', 100.50, 99.90, 100.10)
    slip = eq._compute_slippage_bps(ex)
    assert 40 < slip < 60


def test_slippage_formula_sell_favorable():
    # sell at 100.5 with mid 100 → favorable (-50bps)
    ex = _mk('X', 'sell', 100.50, 99.90, 100.10)
    slip = eq._compute_slippage_bps(ex)
    assert slip < 0


def test_alert_on_3_high_slippage():
    ym = datetime.now().strftime('%Y-%m')
    execs = [
        _mk('A', 'buy', 110, 100, 101),  # ~900bps
        _mk('B', 'buy', 105, 100, 101),  # ~450bps
        _mk('C', 'buy', 103, 100, 101),  # ~250bps
    ]
    s = eq.monthly_summary(execs=execs, ym=ym)
    assert s['high_slippage_count'] >= 3
    assert s['alert_triggered'] is True


def test_incomplete_data_returns_none():
    # missing bid/ask
    ex = {'id': 'x', 'ticker': 'X', 'direction': 'buy', 'price': 100}
    assert eq._compute_slippage_bps(ex) is None
