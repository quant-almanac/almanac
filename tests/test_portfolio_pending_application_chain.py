"""A pending earlier snapshot must not contaminate a later fill's balances."""
import asyncio
import json
import sqlite3

import pytest
from fastapi import HTTPException

from api.routes import actions
import event_ledger
from tests.test_actions_save_execution import (
    ExecutionRequest, isolated as execution_files,  # noqa: F401
)


@pytest.fixture
def isolated(request):
    return request.getfixturevalue('execution_files')


def interrupted(files):
    account = json.loads(files['account'].read_text())
    holdings = json.loads(files['holdings'].read_text())
    event_ledger.prepare_portfolio_application(
        event_id='synthetic-earlier', holdings_before=holdings, account_before=account,
        holdings_after={**holdings, 'SYNTHETIC_OLD': {'shares': 1}},
        account_after={**account, 'balance': account['balance'] - 10},
        event_kwargs=dict(event_id='synthetic-earlier', direction='buy', ticker='SYNTHETIC_OLD',
                          price=10, quantity=1, currency='JPY', account='synthetic-account'),
        result={'updated': True},
    )


def test_later_fill_is_retained_without_touching_interrupted_snapshot(isolated):
    files = isolated
    interrupted(files)
    before = (files['holdings'].read_bytes(), files['account'].read_bytes())
    request = ExecutionRequest(ticker='SYNTHETIC_NEW', direction='buy', quantity=1,
                               price=10, currency='JPY', account='特定', status='executed')
    result = asyncio.run(actions.save_execution(request))
    assert result['portfolio_application_status'] == 'pending'
    assert result['portfolio_application_reasons'][0]['code'] == 'earlier_portfolio_application_unresolved'
    assert (files['holdings'].read_bytes(), files['account'].read_bytes()) == before
    assert len(json.loads(files['executions'].read_text())['executions']) == 1
    assert event_ledger.query_events() == []


def test_import_pending_retains_actual_fill_at_public_entry(isolated, monkeypatch):
    files = isolated
    root = files['account'].parent
    monkeypatch.setattr(event_ledger, 'BASE_DIR', root)
    journal = root / 'broker_balance_journal.jsonl'
    journal.write_text(json.dumps({'operation_id': 'synthetic-import', 'status': 'prepared'}) + '\n')
    before = (files['holdings'].read_bytes(), files['account'].read_bytes())
    result = asyncio.run(actions.save_execution(ExecutionRequest(
        ticker='SYNTHETIC_NEW', direction='buy', quantity=1, price=10,
        currency='JPY', account='特定', status='executed')))
    assert result['portfolio_application_status'] == 'pending'
    assert result['portfolio_application_reasons'][0]['code'] == 'broker_import_recovery_required'
    assert (files['holdings'].read_bytes(), files['account'].read_bytes()) == before
    assert len(json.loads(files['executions'].read_text())['executions']) == 1
    assert event_ledger.query_events() == []


def test_recover_earlier_then_resolve_retained_fill_once(isolated):
    files = isolated
    interrupted(files)
    balance = json.loads(files['account'].read_text())['balance']
    request = ExecutionRequest(ticker='SYNTHETIC_NEW', direction='buy', quantity=1,
                               price=10, currency='JPY', account='特定', status='executed')
    pending = asyncio.run(actions.save_execution(request))
    record = json.loads(files['executions'].read_text())['executions'][0]
    resolution = actions.PortfolioResolutionRequest(resolution='apply')
    with pytest.raises(HTTPException) as error:
        asyncio.run(actions.resolve_execution_portfolio(record['id'], resolution))
    assert error.value.status_code == 409
    assert json.loads(files['executions'].read_text())['executions'][0]['portfolio_application_status'] == 'pending'
    assert actions._recover_prepared_portfolio_application('synthetic-earlier')['updated']
    result = asyncio.run(actions.resolve_execution_portfolio(record['id'], resolution))
    assert result['portfolio_application_status'] == 'applied'
    assert json.loads(files['account'].read_text())['balance'] == balance - 20
    assert len(event_ledger.query_events()) == 2
    assert len(json.loads(files['executions'].read_text())['executions']) == 1
    replay = asyncio.run(actions.save_execution(request))
    assert replay['portfolio_application_status'] == 'applied'
    assert replay['idempotent_replay']
    assert pending['portfolio_application_status'] == 'pending'
    assert json.loads(files['account'].read_text())['balance'] == balance - 20


@pytest.mark.parametrize('status', ['ordered', 'cancelled', 'skip'])
def test_non_balance_records_do_not_enter_recovery_gate(isolated, status):
    interrupted(isolated)
    result = actions._apply_event_to_ledger(
        event_id='synthetic-noop', ticker='SYNTHETIC_NEW', direction='buy',
        quantity=1, price=10, currency='JPY', account=None,
        investment_type='medium', status=status, sell_all=False, name=None,
    )
    assert result['updated'] is False
    assert event_ledger.get_portfolio_application('synthetic-earlier')['status'] == 'prepared'


def test_own_prepared_intent_is_exempt_but_unknown_status_is_not(isolated):
    interrupted(isolated)
    assert event_ledger.unresolved_portfolio_application_ids() == ('synthetic-earlier',)
    assert event_ledger.unresolved_portfolio_application_ids(recovering_event_id='synthetic-earlier') == ()
    with sqlite3.connect(isolated['ledger_db']) as connection:
        connection.execute("UPDATE portfolio_application_journal SET status='unrecognized'")
    with pytest.raises(actions.PortfolioApplicationPending):
        actions._require_portfolio_recovery_clear('synthetic-earlier')


def test_completed_intent_does_not_block_later_fill(isolated):
    interrupted(isolated)
    actions._recover_prepared_portfolio_application('synthetic-earlier')
    request = ExecutionRequest(ticker='SYNTHETIC_NEW', direction='buy', quantity=1,
                               price=10, currency='JPY', account='特定', status='executed')
    assert asyncio.run(actions.save_execution(request))['portfolio_application_status'] == 'applied'


def test_journal_read_failure_retains_fill_as_pending(isolated, monkeypatch):
    def unavailable(**kwargs):
        raise sqlite3.OperationalError('synthetic read failure')
    monkeypatch.setattr(event_ledger, 'unresolved_portfolio_application_ids', unavailable)
    request = ExecutionRequest(ticker='SYNTHETIC_NEW', direction='buy', quantity=1,
                               price=10, currency='JPY', account='特定', status='executed')
    result = asyncio.run(actions.save_execution(request))
    assert result['portfolio_application_reasons'][0]['code'] == 'portfolio_application_journal_unavailable'
    assert len(json.loads(isolated['executions'].read_text())['executions']) == 1
    assert event_ledger.query_events() == []


def test_direct_commit_cannot_bypass_pending_intent(isolated, monkeypatch):
    interrupted(isolated)
    original = event_ledger.get_portfolio_application('synthetic-earlier')
    monkeypatch.setattr(actions, '_save_json', lambda *a: pytest.fail('must not write'))
    with pytest.raises(actions.PortfolioApplicationPending):
        actions._commit_portfolio_event(
            holdings_before={}, account_before={}, holdings_after={}, account_after={},
            event_kwargs={'event_id': 'synthetic-later'}, recovery_result={}, trade_args=(),
        )
    assert event_ledger.get_portfolio_application('synthetic-earlier') == original
    assert event_ledger.get_portfolio_application('synthetic-later') is None
