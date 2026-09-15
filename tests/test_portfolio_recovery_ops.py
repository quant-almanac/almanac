"""Read-only diagnostics and shared-writer recovery barriers, synthetic only."""
import asyncio
import hashlib
import json
import sqlite3
import sys
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import event_ledger as ledger
import watchdog
import broker_balance_import as importer
from api.routes import cash, system_status
from tests.test_cash_movement_confirmations import _seed


@pytest.fixture
def state(tmp_path, monkeypatch):
    import utils
    monkeypatch.setattr(utils, 'LOCKS_DIR', tmp_path / 'locks')
    _seed(monkeypatch, tmp_path)
    ledger.init_schema()
    return tmp_path


def prepare():
    ledger.prepare_portfolio_application(
        event_id='synthetic-pending', holdings_before={}, account_before={},
        holdings_after={'private-test-marker': 1}, account_after={},
        event_kwargs={}, result={},
    )


def confirmed_fixture_route():
    holdings = json.loads(cash.HOLDINGS_FILE.read_text())
    for broker in cash.CashBroker:
        for owner in cash.CashOwner:
            try:
                key = cash._holdings_key(cash.CashCurrency.JPY, broker, owner)
            except HTTPException:
                continue
            if 'available_to_trade_jpy' in holdings.get(key, {}):
                return broker, owner
    pytest.fail('confirmed fixture route is missing')


def test_missing_database_diagnostic_never_creates_it(tmp_path):
    path = tmp_path / 'missing' / 'ledger.db'
    result = ledger.read_portfolio_recovery_status(db_path=path)
    assert result['status'] == 'unknown'
    assert result['pending_count'] is None
    assert not path.parent.exists()


def test_reader_does_not_migrate_existing_database(tmp_path):
    path = tmp_path / 'empty.db'
    with sqlite3.connect(path) as connection:
        connection.execute('CREATE TABLE sentinel(n)')
    before = path.read_bytes()
    assert ledger.read_portfolio_recovery_status(db_path=path)['status'] == 'unknown'
    assert path.read_bytes() == before


def test_reader_reports_counts_without_identity_or_payload(state):
    prepare()
    before = ledger.DB_PATH.read_bytes()
    result = ledger.read_portfolio_recovery_status()
    assert result['status'] == 'needs_reconciliation'
    assert result['pending_count'] == 1 and result['unbound_count'] == 0
    assert result['execution_authorized'] is False
    assert 'synthetic-pending' not in json.dumps(result)
    assert 'private-test-marker' not in json.dumps(result)
    assert ledger.DB_PATH.read_bytes() == before
    ledger.complete_portfolio_application('synthetic-pending')
    assert ledger.read_portfolio_recovery_status()['status'] == 'clear'


def test_legacy_and_unknown_status_are_visible(state):
    ledger.prepare_portfolio_application(event_id='legacy', holdings_after={}, account_after={}, event_kwargs={}, result={})
    with sqlite3.connect(ledger.DB_PATH) as connection:
        connection.execute("UPDATE portfolio_application_journal SET status='invalid'")
        connection.execute('DROP TABLE portfolio_application_preconditions')
    result = ledger.read_portfolio_recovery_status()
    assert (result['pending_count'], result['unbound_count'], result['invalid_status_count']) == (1, 1, 1)
    with sqlite3.connect(ledger.DB_PATH) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='portfolio_application_preconditions'").fetchall() == []


@pytest.mark.parametrize('operation', ['deposit', 'withdraw', 'fx', 'reconcile'])
def test_cash_writer_leaves_state_untouched_when_recovery_pending(state, operation, monkeypatch):
    prepare()
    paths = [cash.ACCOUNT_FILE, cash.HOLDINGS_FILE, cash.TX_FILE]
    before = [p.read_bytes() for p in paths]
    monkeypatch.setattr(cash, '_event_fx_rate', lambda *a: pytest.fail('must reject before network'))
    with pytest.raises(HTTPException) as error:
        if operation in {'deposit', 'withdraw'}:
            cash._apply_cash_change(cash.CashRequest(amount=1, currency='JPY', broker='rakuten'), cash.TxType(operation))
        elif operation == 'fx':
            asyncio.run(cash.confirm_fx_conversion(cash.FXConversionConfirmRequest(
                from_currency='USD', to_currency='JPY', from_amount=1, fx_rate_usdjpy=100,
                idempotency_key='synthetic-fx-key', source='synthetic confirmation')))
        else:
            broker, owner = confirmed_fixture_route()
            asyncio.run(cash.reconcile_cash(cash.CashReconcileRequest(
                broker=broker, owner=owner, currency='JPY', reported_balance=500, reported_as_of='2026-09-15', source='synthetic confirmation')))
    assert error.value.status_code == 409
    assert error.value.detail['code'] == 'earlier_portfolio_application_unresolved'
    assert [p.read_bytes() for p in paths] == before


def test_completed_movement_retry_does_not_require_new_application(state):
    request = cash.FXConversionConfirmRequest(
        from_currency='USD', to_currency='JPY', from_amount=1, fx_rate_usdjpy=100,
        idempotency_key='synthetic-replay-key', source='synthetic confirmation')
    asyncio.run(cash.confirm_fx_conversion(request))
    prepare()
    assert asyncio.run(cash.confirm_fx_conversion(request))['idempotent_replay'] is True


def test_import_apply_stops_before_own_resume_but_dry_run_is_allowed(state, monkeypatch):
    prepare()
    monkeypatch.setattr(importer, 'parse_rakuten_asset_balance', lambda _: {})
    monkeypatch.setattr(importer, '_resume_incomplete_journal', lambda: pytest.fail('cannot overwrite pending application'))
    monkeypatch.setattr(importer, 'build_reconciled_state', lambda **kw: ({}, {}, {'before': {}, 'after': {}}))
    monkeypatch.setattr(importer, '_compute_cash_deltas', lambda *a: {})
    monkeypatch.setattr(importer, '_build_ledger_events_for_mode', lambda **kw: [])
    assert importer.apply_reconcile(rakuten_csv=state/'synthetic.csv', apply=False)['dry_run']
    with pytest.raises(ledger.PortfolioRecoveryRequired):
        importer.apply_reconcile(rakuten_csv=state/'synthetic.csv', apply=True)


def test_watchdog_notification_chain_and_cooldown(state, monkeypatch):
    prepare()
    monkeypatch.setattr(watchdog, 'resolve_db_path', lambda *_: ledger.DB_PATH)
    monkeypatch.setitem(sys.modules, 'portfolio_integrity', SimpleNamespace(run_integrity_check=lambda **kw: {'issues': []}))
    issues = watchdog._check_portfolio_integrity()
    assert issues[0]['check'] == 'portfolio_application_recovery'
    report = {'stale': [], 'errors': [], 'fx_stale': False, 'ok': [], 'integrity_issues': issues}
    view = watchdog._notification_report(report)
    assert watchdog._notification_problem_count(view) == 1
    assert 'unresolved_portfolio_applications' in watchdog._build_watchdog_message(view)
    monkeypatch.setattr(watchdog, 'WATCHDOG_STATE', state/'watchdog.json')
    monkeypatch.setattr(watchdog, 'evaluate_health', lambda: report)
    sent = []
    monkeypatch.setitem(sys.modules, 'alert', SimpleNamespace(send_telegram=lambda text: sent.append(text) or True))
    for _ in range(4):
        assert watchdog.run_check(notify=True) == 1
    assert len(sent) == 1


def test_system_api_exposes_redacted_readonly_status(state, monkeypatch):
    import auto_tune
    from api.routes import dashboard
    from almanac import runtime_config
    prepare()
    monkeypatch.setattr(runtime_config, 'resolve_db_path', lambda *_: ledger.DB_PATH)
    monkeypatch.setattr(dashboard, '_build_data_health', lambda: {})
    monkeypatch.setattr(auto_tune, 'get_status', lambda: {})
    monkeypatch.setattr(system_status, 'load_json', lambda *a, **kw: {})
    monkeypatch.setattr(system_status, '_heartbeat_rows', lambda *_: [])
    before = hashlib.sha256(ledger.DB_PATH.read_bytes()).hexdigest()
    result = asyncio.run(system_status.get_system_status())['portfolio_recovery']
    assert result['pending_count'] == 1
    assert hashlib.sha256(ledger.DB_PATH.read_bytes()).hexdigest() == before


def test_watchdog_recovery_reader_failure_remains_visible(state, monkeypatch):
    def broken_reader(**kwargs):
        raise RuntimeError('synthetic private details')
    monkeypatch.setattr(ledger, 'read_portfolio_recovery_status', broken_reader)
    monkeypatch.setitem(sys.modules, 'portfolio_integrity', SimpleNamespace(run_integrity_check=lambda **kw: {'issues': []}))
    issues = watchdog._check_portfolio_integrity()
    assert issues == [{'severity': 'critical', 'check': 'portfolio_application_recovery',
                       'message': 'recovery_journal_unavailable', 'pending_count': None}]
