from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from nav_recording_health import expected_recording_date, read_nav_recording_health


NOW = datetime.fromisoformat("2026-09-11T23:40:00+09:00")


@pytest.fixture
def sources(tmp_path):
    db, hb = tmp_path / "test.db", tmp_path / "heartbeat.json"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE daily_performance (date TEXT PRIMARY KEY, estimated INTEGER, created_at TEXT)")
        conn.execute("INSERT INTO daily_performance VALUES ('2026-09-11', 0, 'not-an-arrival')")
    entry = {"last_run_ts": datetime.fromisoformat("2026-09-11T23:01:00+09:00").timestamp(),
             "status": "ok", "extra": {"date": "2026-09-11"}}
    hb.write_text(json.dumps({"nav_recorder": entry}))
    return db, hb


def read(sources, now=NOW):
    return read_nav_recording_health(db_path=sources[0], heartbeat_path=sources[1], now=now)


def edit_hb(sources, **changes):
    hb = json.loads(sources[1].read_text())
    hb["nav_recorder"].update(changes)
    sources[1].write_text(json.dumps(hb))


def test_publication_is_not_dd_permission_and_no_writes(sources):
    before = [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources]
    got = read(sources)
    assert got["publication_status"] == "reported"
    assert got["dd_evidence_status"] == "unknown"
    assert got["dd_recovery_qualified"] is None
    assert got["policy_change_authorized"] is False
    assert got["scheduled_execution_verified"] is False
    assert got["historical_timeliness_verified"] is False
    assert before == [hashlib.sha256(p.read_bytes()).hexdigest() for p in sources]


@pytest.mark.parametrize("instant,expected", [
    ("2026-09-11T23:29:59+09:00", "2026-09-10"),
    ("2026-09-11T23:30:00+09:00", "2026-09-11"),
    ("2026-09-11T14:30:00+00:00", "2026-09-11"),
    ("2026-09-12T12:00:00+09:00", "2026-09-11"),
    ("2026-09-14T23:29:59+09:00", "2026-09-11"),
    ("2026-09-14T23:30:00+09:00", "2026-09-14"),
])
def test_schedule_is_recording_weekdays_not_exchange_calendar(instant, expected):
    assert expected_recording_date(datetime.fromisoformat(instant)).isoformat() == expected


@pytest.mark.parametrize("ts", [None, True, "42", float("nan"), float("inf"), 10**400, NOW.timestamp() + 1])
def test_bad_heartbeat_time_cannot_report_success(sources, ts):
    edit_hb(sources, last_run_ts=ts)
    assert read(sources)["publication_status"] == "unknown"


@pytest.mark.parametrize("extra", [None, [], {"date": None}, {"date": "20260911"}, {"date": "2026-09-12"}])
def test_bad_recording_date(sources, extra):
    edit_hb(sources, extra=extra)
    assert read(sources)["publication_status"] == "unknown"


def test_backfill_or_recent_mtime_cannot_substitute_for_heartbeat(sources):
    edit_hb(sources, last_run_ts=NOW.timestamp(), extra={"date": "2026-09-10"})
    assert read(sources)["reason"] == "nav_publication_behind_schedule"


@pytest.mark.parametrize("estimated", [1, None, 2, "bad"])
def test_estimated_or_unknown_never_reported(sources, estimated):
    with sqlite3.connect(sources[0]) as conn:
        conn.execute("UPDATE daily_performance SET estimated=?", (estimated,))
    assert read(sources)["publication_status"] == "unknown"


def test_heartbeat_without_matching_row_is_unknown(sources):
    with sqlite3.connect(sources[0]) as conn:
        conn.execute("DELETE FROM daily_performance")
    assert read(sources)["reason"] == "nav_table_empty"


def test_missing_database_is_not_created(sources, tmp_path):
    absent = tmp_path / "absent.db"
    assert read((absent, sources[1]))["reason"] == "nav_database_unreadable_or_schema_invalid"
    assert not absent.exists()


@pytest.mark.parametrize("text", ["{}", "[]", "null", "broken", '{"nav_recorder":42}'])
def test_malformed_or_missing_heartbeat(sources, text):
    sources[1].write_text(text)
    assert read(sources)["publication_status"] == "unknown"


def test_concurrent_heartbeat_replacement_is_unknown(sources, monkeypatch):
    original = Path.read_bytes
    calls = []

    def changed(path):
        data = original(path)
        if path == sources[1]:
            calls.append(path)
            if len(calls) == 2:
                return data + b" "
        return data

    monkeypatch.setattr(Path, "read_bytes", changed)
    assert read(sources)["reason"] == "nav_sources_changed_during_read"
