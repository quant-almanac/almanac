"""T20: execution_quality slippage + 100bps alert"""
from datetime import datetime
import json

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


def test_execution_quality_reads_corrected_route_without_mutating_ledger(tmp_path, monkeypatch):
    from execution_reconciliation import record_route_correction

    raw = _mk('XLF', 'sell', 56.0, 55.9, 56.1)
    ledger = tmp_path / 'action_executions.json'
    ledger.write_text(json.dumps({'executions': [raw]}), encoding='utf-8')
    monkeypatch.setattr(eq, 'EXEC_LOG', ledger)
    record_route_correction(
        execution_record=raw,
        corrected_route={
            'execution_owner': 'husband',
            'execution_broker': 'rakuten',
            'execution_account': 'NISA成長投資枠',
        },
        evidence={'row_hash': 'fixture'},
        reason='broker evidence',
        approved_by='test',
        state_path=tmp_path / 'execution_reconciliation_state.json',
    )

    loaded = eq._load_execs()['executions'][0]

    assert loaded['execution_account'] == 'nisa_growth'
    assert loaded['execution_reconciliation_status'] == 'corrected'
    assert json.loads(ledger.read_text())['executions'][0] == raw
