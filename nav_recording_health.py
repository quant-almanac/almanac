"""Read-only NAV publication checks, not effective-NAV/DD qualification.

The expected date is a RECORDING schedule date (weekdays at 23:00 JST,
30-minute completion grace), not a JP/US trading-session date. Holidays do
not cancel the configured weekday job. A manual snapshot can satisfy this
publication check; no claim of scheduled execution or historical on-time
arrival is made. created_at/mtime and aggregate flow coverage are not used.
"""
from contextlib import closing
from datetime import date, datetime, time, timedelta
import json
import math
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo


JST = ZoneInfo("Asia/Tokyo")


def _day(value):
    if not isinstance(value, str):
        raise ValueError("date must be canonical")
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("date must be canonical")
    return parsed


def expected_recording_date(now: datetime) -> date:
    """Most recent weekday whose 23:30 JST completion deadline has passed."""
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("aware now required")
    local = now.astimezone(JST)
    day = local.date()
    if local.time() < time(23, 30):
        day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def read_nav_recording_health(*, db_path: Path, heartbeat_path: Path, now: datetime) -> dict:
    """Verify publication evidence with no schema init or writer imports.

SQLite SELECT runs in a read transaction. The heartbeat bytes are checked
again before return to detect ordinary concurrent changes; this is NOT a
cross-file atomic snapshot or ABA detector. No monetary values are returned.
"""
    expected = expected_recording_date(now)
    base = {"expected_recording_date": expected.isoformat(),
            "publication_status": "unknown", "dd_evidence_status": "unknown",
            "dd_evidence_reason": "per_row_flow_and_arrival_evidence_unavailable",
            "dd_recovery_qualified": None, "policy_change_authorized": False,
            "scheduled_execution_verified": False, "historical_timeliness_verified": False}

    def result(status, reason, **extra):
        return {**base, "publication_status": status, "reason": reason, **extra}

    try:
        heartbeat_bytes = heartbeat_path.read_bytes()
        hb = json.loads(heartbeat_bytes)
        if not isinstance(hb, dict) or not isinstance(hb.get("nav_recorder"), dict):
            return result("unknown", "nav_heartbeat_missing_or_malformed")
        entry = hb["nav_recorder"]
        ts = entry.get("last_run_ts")
        if type(ts) not in (int, float) or not math.isfinite(ts):
            return result("unknown", "nav_heartbeat_timestamp_invalid")
        if ts > now.timestamp():
            return result("unknown", "nav_heartbeat_in_future")
        extra = entry.get("extra")
        if not isinstance(extra, dict):
            return result("unknown", "nav_heartbeat_date_invalid")
        reported_day = _day(extra.get("date"))
        # The current writer snapshots today's local date and emits the
        # heartbeat afterwards. An earlier timestamp cannot attest this row.
        start = datetime.combine(reported_day, time(), tzinfo=JST).timestamp()
        if ts < start or reported_day > now.astimezone(JST).date():
            return result("unknown", "nav_heartbeat_date_timestamp_mismatch")
        if entry.get("status") != "ok":
            return result("unknown", "nav_snapshot_did_not_report_success")
        # mode=ro refuses a missing DB; do not call event_ledger.query_events,
        # whose init_schema path can create or migrate a supposedly read DB.
        uri = db_path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            row = db.execute("SELECT date, estimated FROM daily_performance ORDER BY date DESC LIMIT 1").fetchone()
            reported_row = db.execute("SELECT estimated FROM daily_performance WHERE date=?",
                                      (reported_day.isoformat(),)).fetchone()
        if heartbeat_path.read_bytes() != heartbeat_bytes:
            return result("unknown", "nav_sources_changed_during_read")
        if row is None:
            return result("missing", "nav_table_empty")
        latest = _day(row[0])
        if latest > now.astimezone(JST).date():
            return result("unknown", "nav_recording_date_in_future")
        if type(row[1]) is not int or row[1] not in (0, 1):
            return result("unknown", "nav_estimated_flag_invalid")
        if latest < expected or reported_day < expected:
            return result("missing", "nav_publication_behind_schedule")
        if reported_row is None:
            return result("unknown", "nav_heartbeat_row_absent")
        if type(reported_row[0]) is not int or reported_row[0] not in (0, 1):
            return result("unknown", "nav_estimated_flag_invalid")
        if reported_row[0] != 0 or row[1] != 0:
            return result("unknown", "nav_publication_estimated")
        return result("reported", "nav_publication_reported",
                      latest_recording_date=latest.isoformat())
    except FileNotFoundError:
        return result("unknown", "nav_heartbeat_file_missing")
    except sqlite3.Error:
        return result("unknown", "nav_database_unreadable_or_schema_invalid")
    except (OSError, ValueError, TypeError, OverflowError):
        return result("unknown", "nav_source_unreadable_or_malformed")
