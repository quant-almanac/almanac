"""Synthetic cross-writer and interrupted-import regression tests."""
import asyncio
import json

import pytest

import broker_balance_import as importer
import broker_recovery as recovery
import event_ledger as ledger
from api.routes import cash, actions
from fastapi import HTTPException
from tests.test_cash_movement_confirmations import _seed


@pytest.fixture
def state(tmp_path, monkeypatch):
    import utils
    real_append_events = ledger.append_events
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr(ledger, 'append_events', real_append_events)
    monkeypatch.setattr(utils, 'LOCKS_DIR', tmp_path / 'locks')
    monkeypatch.setattr(ledger, 'BASE_DIR', tmp_path)
    monkeypatch.setattr(importer, 'JOURNAL_FILE', tmp_path / 'broker_balance_journal.jsonl')
    monkeypatch.setattr(importer, 'ACCOUNT_FILE', cash.ACCOUNT_FILE)
    monkeypatch.setattr(importer, 'HOLDINGS_FILE', cash.HOLDINGS_FILE)
    monkeypatch.setattr(importer, 'RECONCILE_LOG', tmp_path / 'import-log.jsonl')
    ledger.init_schema()
    before_account = json.loads(cash.ACCOUNT_FILE.read_text())
    before_holdings = json.loads(cash.HOLDINGS_FILE.read_text())
    return {'operation_id': 'synthetic-import', 'mode': 'reset',
            'before_account': before_account, 'before_holdings': before_holdings,
            'next_account': {**before_account, 'balance': 123},
            'next_holdings': {**before_holdings, 'synthetic_marker': {}}, 'ledger_events': []}


def prepare(plan):
    importer._append_journal({'operation_id': plan['operation_id'], 'status': 'prepared', 'plan': plan})


@pytest.mark.parametrize('account_after,holdings_after', [(False, False), (True, False), (False, True), (True, True)])
def test_recovery_accepts_only_recorded_before_after_states(state, account_after, holdings_after):
    prepare(state)
    if account_after:
        cash.ACCOUNT_FILE.write_text(json.dumps(state['next_account']))
    if holdings_after:
        cash.HOLDINGS_FILE.write_text(json.dumps(state['next_holdings']))
    importer._resume_incomplete_journal()
    assert json.loads(cash.ACCOUNT_FILE.read_text()) == state['next_account']
    assert json.loads(cash.HOLDINGS_FILE.read_text()) == state['next_holdings']
    journal = importer.JOURNAL_FILE.read_bytes()
    importer._resume_incomplete_journal()
    assert importer.JOURNAL_FILE.read_bytes() == journal


@pytest.mark.parametrize('file', ['account', 'holdings'])
def test_divergence_prevents_any_write(state, file):
    prepare(state)
    path = cash.ACCOUNT_FILE if file == 'account' else cash.HOLDINGS_FILE
    path.write_text('{"unrelated_change": 1}')
    paths = [cash.ACCOUNT_FILE, cash.HOLDINGS_FILE, importer.JOURNAL_FILE, ledger.DB_PATH]
    before = [p.read_bytes() for p in paths]
    with pytest.raises(recovery.ImportRecoveryRequired, match='state_reconciliation'):
        importer._resume_incomplete_journal()
    assert [p.read_bytes() for p in paths] == before
    assert not importer.RECONCILE_LOG.exists()


def test_legacy_partial_state_is_not_guessed(state):
    state.pop('before_account')
    state.pop('before_holdings')
    prepare(state)
    cash.ACCOUNT_FILE.write_text(json.dumps(state['next_account']))
    with pytest.raises(recovery.ImportRecoveryRequired):
        importer._resume_incomplete_journal()


def test_older_pending_is_not_hidden_by_newer_commit(state):
    prepare(state)
    importer._append_journal({'operation_id': 'later', 'status': 'prepared'})
    importer._append_journal({'operation_id': 'later', 'status': 'committed'})
    with pytest.raises(ledger.PortfolioRecoveryRequired):
        ledger.require_portfolio_recovery_clear()


def test_historical_repeat_after_commit_is_supported_but_new_pending_blocks(state):
    prepare(state)
    importer._append_journal({'operation_id': state['operation_id'], 'status': 'committed'})
    prepare(state)
    assert recovery.read_import_recovery_status(importer.JOURNAL_FILE)['pending_count'] == 1
    importer._append_journal({'operation_id': state['operation_id'], 'status': 'committed'})
    assert recovery.read_import_recovery_status(importer.JOURNAL_FILE)['status'] == 'clear'


@pytest.mark.parametrize('line', ['{broken', '[]', '{"operation_id":"x","status":"bogus"}',
                                  '{"operation_id":"x","status":"committed"}'])
def test_corrupt_journal_is_unknown_and_blocks(state, line):
    importer.JOURNAL_FILE.write_text(line + '\n')
    assert recovery.read_import_recovery_status(importer.JOURNAL_FILE)['status'] == 'unknown'
    with pytest.raises(ledger.PortfolioRecoveryRequired):
        ledger.require_portfolio_recovery_clear()


def test_cash_and_execution_are_blocked_by_import_pending(state):
    prepare(state)
    before = cash.ACCOUNT_FILE.read_bytes()
    with pytest.raises(HTTPException) as exc:
        cash._apply_cash_change(cash.CashRequest(amount=1, currency='JPY', broker='rakuten'), cash.TxType.deposit)
    assert exc.value.status_code == 409
    with pytest.raises(actions.PortfolioApplicationPending):
        actions._require_portfolio_recovery_clear('synthetic-fill')
    assert cash.ACCOUNT_FILE.read_bytes() == before


def test_completed_cash_replay_remains_available(state):
    request = cash.FXConversionConfirmRequest(from_currency='USD', to_currency='JPY', from_amount=1,
        fx_rate_usdjpy=100, idempotency_key='synthetic-completed', source='synthetic')
    asyncio.run(cash.confirm_fx_conversion(request))
    prepare(state)
    assert asyncio.run(cash.confirm_fx_conversion(request))['idempotent_replay']


@pytest.mark.parametrize('operation', ['add', 'update', 'delete'])
def test_holdings_crud_rejects_before_reading_or_writing(state, monkeypatch, operation):
    from api.routes import portfolio
    prepare(state)
    monkeypatch.setattr(portfolio, 'load_json_strict', lambda *_: pytest.fail('blocked before read'))
    call = {'add': lambda: portfolio.add_holding({'key': 'synthetic'}),
            'update': lambda: portfolio.update_holding('synthetic', {}),
            'delete': lambda: portfolio.delete_holding('synthetic')}[operation]
    with pytest.raises(HTTPException) as error:
        asyncio.run(call())
    assert error.value.status_code == 409


def test_position_import_rejects_before_build(state, monkeypatch, tmp_path):
    import broker_position_import as positions
    prepare(state)
    source = tmp_path / 'synthetic.csv'
    source.write_text('synthetic')
    monkeypatch.setattr(positions, 'parse_rakuten_positions', lambda *_: [])
    monkeypatch.setattr(positions, 'build_reconciled_holdings', lambda **kw: pytest.fail('must not build'))
    with pytest.raises(ledger.PortfolioRecoveryRequired):
        positions.apply_reconcile(rakuten_csv=source, apply=True)


def test_alert_stale_snapshot_cannot_overwrite_new_holdings(state, monkeypatch):
    import alert
    monkeypatch.setattr(alert, 'HOLDINGS_FILE', cash.HOLDINGS_FILE)
    before = cash.HOLDINGS_FILE.read_bytes()
    with pytest.raises(RuntimeError, match='holdings_changed'):
        alert.save_holdings({'stale': {}}, expected_before={})
    assert cash.HOLDINGS_FILE.read_bytes() == before
    original = json.loads(before)
    alert.save_holdings({**original, 'synthetic_updated': {}}, expected_before=original)
    assert 'synthetic_updated' in json.loads(cash.HOLDINGS_FILE.read_text())


def test_alert_does_not_update_while_import_pending(state, monkeypatch):
    import alert
    prepare(state)
    monkeypatch.setattr(alert, 'HOLDINGS_FILE', cash.HOLDINGS_FILE)
    original = cash.HOLDINGS_FILE.read_bytes()
    with pytest.raises(ledger.PortfolioRecoveryRequired):
        alert.save_holdings({}, expected_before=json.loads(original))
    assert cash.HOLDINGS_FILE.read_bytes() == original


def test_legacy_command_barrier_is_before_network_and_writes(state, monkeypatch):
    import importlib
    monkeypatch.setenv('TELEGRAM_TOKEN', 'synthetic')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'synthetic')
    commands = importlib.import_module('bot_commands')
    prepare(state)
    monkeypatch.setattr(commands, 'load_holdings', lambda: pytest.fail('must not read'))
    monkeypatch.setattr(commands, 'get_account_info', lambda: pytest.fail('must not read'))
    for func, args in [(commands.cmd_buy, ['/buy', 'SYNTH', '1', '1']),
                       (commands.cmd_sell, ['/sell', 'SYNTH']),
                       (commands.cmd_setbalance, ['/setbalance', '1']),
                       (commands.cmd_setrisk, ['/setrisk', '1'])]:
        assert func(args).startswith('未適用')


def test_import_monitor_is_redacted_and_notifies(state, monkeypatch):
    import watchdog
    import sys
    from types import SimpleNamespace
    prepare(state)
    monkeypatch.setattr(watchdog, 'BASE_DIR', importer.JOURNAL_FILE.parent)
    monkeypatch.setattr(watchdog, 'resolve_db_path', lambda *_: ledger.DB_PATH)
    monkeypatch.setitem(sys.modules, 'portfolio_integrity', SimpleNamespace(run_integrity_check=lambda **kw: {'issues': []}))
    issues = watchdog._check_portfolio_integrity()
    assert issues == [{'severity': 'critical', 'check': 'broker_import_recovery',
                       'message': 'broker_import_recovery_required', 'pending_count': 1}]


@pytest.mark.parametrize('boundary', ['holdings', 'ledger', 'commit'])
def test_interrupted_import_replays_real_writes_once(state, monkeypatch, boundary):
    state['ledger_events'] = [{'event_id': 'synthetic-import-flow', 'event_type': 'cash_flow',
        'occurred_at': '2026-09-15T00:00:00+00:00', 'quantity': 1, 'price': 1,
        'currency': 'JPY', 'direction': 'in', 'source': 'synthetic'}]
    original_write, original_ledger, original_journal = importer.atomic_write_json, importer._append_ledger_events, importer._append_journal
    def write(path, payload):
        if boundary == 'holdings' and path == importer.HOLDINGS_FILE:
            raise OSError('synthetic interruption')
        return original_write(path, payload)
    def append(events):
        if boundary == 'ledger':
            raise OSError('synthetic interruption')
        return original_ledger(events)
    def journal(record):
        if boundary == 'commit' and record['status'] == 'committed':
            raise OSError('synthetic interruption')
        return original_journal(record)
    with monkeypatch.context() as patch:
        patch.setattr(importer, 'atomic_write_json', write)
        patch.setattr(importer, '_append_ledger_events', append)
        patch.setattr(importer, '_append_journal', journal)
        with pytest.raises(OSError):
            importer._apply_plan(state)
    importer._resume_incomplete_journal()
    importer._resume_incomplete_journal()
    assert json.loads(cash.ACCOUNT_FILE.read_text()) == state['next_account']
    assert json.loads(cash.HOLDINGS_FILE.read_text()) == state['next_holdings']
    assert ledger.cash_flow_sum_jpy(date_from='2026-09-01', date_to='2026-09-30') == 1


def test_conflicting_plan_and_multiple_pending_are_not_replayed(state):
    prepare(state)
    before = importer.JOURNAL_FILE.read_bytes()
    with pytest.raises(recovery.ImportRecoveryRequired, match='plan_conflict'):
        importer._apply_plan({**state, 'next_account': {}})
    assert importer.JOURNAL_FILE.read_bytes() == before
    importer._append_journal({'operation_id': 'second', 'status': 'prepared', 'plan': state})
    with pytest.raises(recovery.ImportRecoveryRequired, match='multiple_pending'):
        importer._resume_incomplete_journal()


def test_maintenance_rollforward_cannot_bypass_pending_import(state, monkeypatch):
    import holdings_freshness
    prepare(state)
    monkeypatch.setattr(holdings_freshness, 'plan_rollforward', lambda **kw: pytest.fail('must not derive balances'))
    with pytest.raises(ledger.PortfolioRecoveryRequired):
        holdings_freshness.apply_rollforward(base_dir=importer.JOURNAL_FILE.parent)
