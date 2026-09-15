"""Interrupted JSON recovery must not overwrite a later portfolio snapshot."""
import json
import os
from pathlib import Path
import select
import signal
import sqlite3
import subprocess
import sys

import pytest
from fastapi import HTTPException

import event_ledger
from api.routes import actions


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(event_ledger, 'DB_PATH', tmp_path / 'ledger.db')
    monkeypatch.setattr(actions, 'HOLDINGS_FILE', tmp_path / 'holdings.json')
    monkeypatch.setattr(actions, 'ACCOUNT_FILE', tmp_path / 'account.json')
    monkeypatch.setattr(actions, '_invalidate_portfolio_cache', lambda: None)
    return tmp_path


def write(path, value):
    path.write_text(json.dumps(value), encoding='utf-8')


def prepare(**kwargs):
    event_ledger.prepare_portfolio_application(
        event_id='synthetic-event', holdings_after={'position': {'shares': 1}},
        account_after={'cash': 90}, event_kwargs={'event_id': 'synthetic-event'},
        result={'updated': True},
        **kwargs,
    )


def test_old_prepared_snapshot_cannot_overwrite_later_state(isolated, monkeypatch):
    prepare()
    write(actions.HOLDINGS_FILE, {'position': {'shares': 2}})
    write(actions.ACCOUNT_FILE, {'cash': 80})
    before = (actions.HOLDINGS_FILE.read_bytes(), actions.ACCOUNT_FILE.read_bytes())
    monkeypatch.setattr(actions, '_ledger_event_exists', lambda _: True)
    with pytest.raises(HTTPException) as error:
        actions._recover_prepared_portfolio_application('synthetic-event')
    assert error.value.status_code == 409
    assert (actions.HOLDINGS_FILE.read_bytes(), actions.ACCOUNT_FILE.read_bytes()) == before
    assert event_ledger.get_portfolio_application('synthetic-event')['status'] == 'prepared'


@pytest.mark.parametrize('written', [(), ('holdings',), ('account',), ('holdings', 'account')])
def test_bound_recovery_accepts_only_before_or_after_each_file(isolated, monkeypatch, written):
    prepare(holdings_before={}, account_before={'cash': 100})
    write(actions.HOLDINGS_FILE, {'position': {'shares': 1}} if 'holdings' in written else {})
    write(actions.ACCOUNT_FILE, {'cash': 90} if 'account' in written else {'cash': 100})
    calls = []
    monkeypatch.setattr(actions, '_ledger_event_exists', lambda _: False)
    monkeypatch.setattr(actions, '_record_trade_event', lambda **kw: calls.append(kw))
    assert actions._recover_prepared_portfolio_application('synthetic-event')['updated']
    assert json.loads(actions.HOLDINGS_FILE.read_text()) == {'position': {'shares': 1}}
    assert json.loads(actions.ACCOUNT_FILE.read_text()) == {'cash': 90}
    assert len(calls) == 1
    assert event_ledger.get_portfolio_application('synthetic-event')['status'] == 'complete'
    assert actions._recover_prepared_portfolio_application('synthetic-event') is None


@pytest.mark.parametrize('bad_file', ['holdings', 'account'])
def test_one_divergent_file_prevents_all_recovery_writes(isolated, monkeypatch, bad_file):
    prepare(holdings_before={}, account_before={'cash': 100})
    write(actions.HOLDINGS_FILE, {} if bad_file != 'holdings' else {'later': True})
    write(actions.ACCOUNT_FILE, {'cash': 100 if bad_file != 'account' else 80})
    monkeypatch.setattr(actions, '_save_json', lambda *a: pytest.fail('must inspect all before writes'))
    monkeypatch.setattr(actions, '_record_trade_event', lambda **kw: pytest.fail('must not create receipt'))
    with pytest.raises(HTTPException, match='reconciliation required'):
        actions._recover_prepared_portfolio_application('synthetic-event')


def test_legacy_intent_can_only_acknowledge_existing_after_state(isolated, monkeypatch):
    prepare()
    write(actions.HOLDINGS_FILE, {'position': {'shares': 1}})
    write(actions.ACCOUNT_FILE, {'cash': 90})
    monkeypatch.setattr(actions, '_ledger_event_exists', lambda _: True)
    monkeypatch.setattr(actions, '_record_trade_event', lambda **kw: pytest.fail('receipt already present'))
    assert actions._recover_prepared_portfolio_application('synthetic-event')['updated']


@pytest.mark.parametrize('malformation', ['missing', 'invalid', 'nonfinite', 'boolean'])
def test_unreadable_or_type_changed_state_is_not_overwritten(isolated, malformation):
    prepare(holdings_before={}, account_before={'cash': 1})
    write(actions.HOLDINGS_FILE, {})
    if malformation == 'invalid':
        actions.ACCOUNT_FILE.write_text('{', encoding='utf-8')
    elif malformation == 'nonfinite':
        actions.ACCOUNT_FILE.write_text('{"cash":NaN}', encoding='utf-8')
    elif malformation == 'boolean':
        write(actions.ACCOUNT_FILE, {'cash': True})
    before = actions.HOLDINGS_FILE.read_bytes()
    with pytest.raises(HTTPException):
        actions._recover_prepared_portfolio_application('synthetic-event')
    assert actions.HOLDINGS_FILE.read_bytes() == before


def test_prepare_retry_cannot_reset_completed_status_or_replace_payload(isolated):
    prepare(holdings_before={}, account_before={'cash': 100})
    event_ledger.complete_portfolio_application('synthetic-event')
    previous = event_ledger.get_portfolio_application('synthetic-event')
    prepare(holdings_before={}, account_before={'cash': 100})
    assert event_ledger.get_portfolio_application('synthetic-event') == previous
    with pytest.raises(ValueError, match='conflicting'):
        prepare(holdings_before={}, account_before={'cash': 999})
    assert event_ledger.get_portfolio_application('synthetic-event') == previous


def test_legacy_retry_cannot_attach_fabricated_before_state(isolated):
    prepare()
    with pytest.raises(ValueError, match='conflicting'):
        prepare(holdings_before={}, account_before={'cash': 100})
    assert event_ledger.get_portfolio_application('synthetic-event')['account_before_json'] is None


def test_discard_removes_preconditions_for_new_attempt(isolated):
    prepare(holdings_before={}, account_before={'cash': 100})
    event_ledger.discard_portfolio_application('synthetic-event')
    prepare(holdings_before={'new': True}, account_before={'cash': 101})
    assert json.loads(event_ledger.get_portfolio_application('synthetic-event')['account_before_json']) == {'cash': 101}


def test_conflicting_prepare_does_not_discard_original_intent(isolated, monkeypatch):
    prepare(holdings_before={}, account_before={'cash': 100})
    original = event_ledger.get_portfolio_application('synthetic-event')
    monkeypatch.setattr(actions, '_ledger_event_exists', lambda _: False)
    monkeypatch.setattr(actions, '_save_json', lambda *a: pytest.fail('conflict must not write JSON'))
    with pytest.raises(ValueError, match='conflicting'):
        actions._commit_portfolio_event(
            holdings_before={}, account_before={'cash': 999},
            holdings_after={'position': {'shares': 1}}, account_after={'cash': 90},
            event_kwargs={'event_id': 'synthetic-event'},
            recovery_result={'updated': True}, trade_args=(),
        )
    assert event_ledger.get_portfolio_application('synthetic-event') == original


def test_real_commit_records_before_state_before_first_json_write(isolated, monkeypatch):
    class Interrupted(BaseException):
        pass
    def stop(*args):
        journal = event_ledger.get_portfolio_application('synthetic-event')
        assert json.loads(journal['holdings_before_json']) == {}
        assert json.loads(journal['account_before_json']) == {'cash': 100}
        raise Interrupted
    monkeypatch.setattr(actions, '_save_json', stop)
    with pytest.raises(Interrupted):
        actions._commit_portfolio_event(
            holdings_before={}, account_before={'cash': 100},
            holdings_after={'position': {'shares': 1}}, account_after={'cash': 90},
            event_kwargs={'event_id': 'synthetic-event'},
            recovery_result={'updated': True}, trade_args=(),
        )


def test_preconditions_upgrade_preserves_legacy_journal(isolated):
    import sqlite3
    # Existing database without the new companion table.
    prepare()
    with sqlite3.connect(event_ledger.DB_PATH) as connection:
        connection.execute('DROP TABLE portfolio_application_preconditions')
    event_ledger.init_schema()
    row = event_ledger.get_portfolio_application('synthetic-event')
    assert row['status'] == 'prepared'
    assert row['holdings_before_json'] is None


def test_preconditions_failure_rolls_back_new_intent(isolated):
    import sqlite3
    event_ledger.init_schema()
    with sqlite3.connect(event_ledger.DB_PATH) as connection:
        connection.execute('''CREATE TRIGGER fail_preconditions
            BEFORE INSERT ON portfolio_application_preconditions
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END''')
    with pytest.raises(sqlite3.IntegrityError):
        prepare(holdings_before={}, account_before={'cash': 100})
    assert event_ledger.get_portfolio_application('synthetic-event') is None


CRASH_WRITER = r'''
import sys
from pathlib import Path
import event_ledger
from api.routes import actions
root, checkpoint = Path(sys.argv[1]), sys.argv[2]
event_ledger.DB_PATH = root / 'ledger.db'
actions.HOLDINGS_FILE = root / 'holdings.json'
actions.ACCOUNT_FILE = root / 'account.json'
actions._invalidate_portfolio_cache = lambda: None
actions._append_trade = lambda *args: None
original_save = actions._save_json
original_complete = event_ledger.complete_portfolio_application
def pause():
    print('checkpoint:' + checkpoint, flush=True)
    sys.stdin.readline()
    raise RuntimeError('parent must kill, not release')
def save(path, data):
    original_save(path, data)
    if Path(path).stem == checkpoint:
        pause()
def complete(event_id):
    if checkpoint == 'ledger':
        pause()
    original_complete(event_id)
actions._save_json = save
event_ledger.complete_portfolio_application = complete
actions._commit_portfolio_event(
    holdings_before={}, account_before={'cash': 100},
    holdings_after={'SYNTHETIC': {'shares': 1}}, account_after={'cash': 90},
    event_kwargs=dict(event_id='synthetic-event', direction='buy', ticker='SYNTHETIC',
                      price=10, quantity=1, currency='JPY', account='synthetic-account'),
    recovery_result={'updated': True}, trade_args=(),
)
'''


@pytest.mark.parametrize('journal_mode', ['DELETE', 'WAL'])
@pytest.mark.parametrize('checkpoint', ['holdings', 'account', 'ledger'])
def test_real_process_kill_across_json_and_ledger_recovers_once(isolated, checkpoint, journal_mode):
    write(actions.HOLDINGS_FILE, {})
    write(actions.ACCOUNT_FILE, {'cash': 100})
    event_ledger.init_schema()
    with sqlite3.connect(event_ledger.DB_PATH) as connection:
        assert connection.execute(f'PRAGMA journal_mode={journal_mode}').fetchone()[0].upper() == journal_mode
    repo = Path(__file__).resolve().parents[1]
    child = subprocess.Popen(
        [sys.executable, '-c', CRASH_WRITER, str(isolated), checkpoint], cwd=repo,
        env={**os.environ, 'PYTHONPATH': str(repo), 'ALMANAC_STATE_DIR': str(isolated),
             'PYTHONDONTWRITEBYTECODE': '1'},
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        assert select.select([child.stdout], [], [], 15)[0], 'writer checkpoint timed out'
        assert child.stdout.readline().strip() == 'checkpoint:' + checkpoint
        child.kill()
        assert child.wait(timeout=10) == -signal.SIGKILL
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
        for stream in (child.stdin, child.stdout, child.stderr):
            stream.close()
    assert event_ledger.get_portfolio_application('synthetic-event')['status'] == 'prepared'
    assert len(event_ledger.query_events()) == int(checkpoint == 'ledger')
    assert actions._recover_prepared_portfolio_application('synthetic-event')['updated']
    assert json.loads(actions.HOLDINGS_FILE.read_text()) == {'SYNTHETIC': {'shares': 1}}
    assert json.loads(actions.ACCOUNT_FILE.read_text()) == {'cash': 90}
    assert len(event_ledger.query_events()) == 1
    assert event_ledger.get_portfolio_application('synthetic-event')['status'] == 'complete'
    assert actions._recover_prepared_portfolio_application('synthetic-event') is None
