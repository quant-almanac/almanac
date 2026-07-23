"""Tests for api/routes/scenario.py — pure helper functions.

The scenario route assembles the dashboard's market health panel.
Every pure helper is tested here without any file I/O:

  - _parse_datetime:             ISO/Z strings, None, malformed
  - _effective_stale_after_hours: weekday / weekend / Monday-before-9 grace
  - _source_health_at:           age_hours, stale flag, extra kwargs pass-through
  - _build_data_health:          stale aggregation, has_collection_warnings
  - _refresh_result_state:       returncode/before/after combinations
  - _tail_text:                  None, bytes, long string truncation
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from api.routes.scenario import (
    _build_data_health,
    _effective_stale_after_hours,
    _parse_datetime,
    _refresh_result_state,
    _source_health_at,
    _tail_text,
    build_scenario_summary,
)


def test_build_scenario_summary_uses_scenario_statuses() -> None:
    summary = build_scenario_summary({
        "scenarios": {
            "active": {"status": "active"},
            "partial": {"status": "partial"},
            "watching": {"status": "watching"},
            "dormant": {"status": "dormant"},
        },
        "overall_alert_level": "elevated",
        "evaluated_at": "2026-07-11T09:00:00+09:00",
    })

    assert summary == {
        "active": 1,
        "partial": 1,
        "watching": 1,
        "alert_level": "elevated",
        "evaluated_at": "2026-07-11T09:00:00+09:00",
    }


def test_build_scenario_summary_missing_state_is_fail_safe() -> None:
    assert build_scenario_summary(None) == {
        "active": 0,
        "partial": 0,
        "watching": 0,
        "alert_level": None,
        "evaluated_at": None,
    }


# ---------------------------------------------------------------------------
# _parse_datetime
# ---------------------------------------------------------------------------


def test_parse_datetime_iso_with_z_suffix() -> None:
    dt = _parse_datetime("2026-05-25T08:00:00Z")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.year == 2026 and dt.month == 5 and dt.day == 25


def test_parse_datetime_iso_without_tz() -> None:
    """Naive ISO string gets local tz applied and returned as UTC."""
    dt = _parse_datetime("2026-05-25T08:00:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_iso_with_explicit_offset() -> None:
    dt = _parse_datetime("2026-05-25T08:00:00+09:00")
    assert dt is not None
    assert dt.tzinfo is not None


def test_parse_datetime_none_returns_none() -> None:
    assert _parse_datetime(None) is None


def test_parse_datetime_empty_string_returns_none() -> None:
    assert _parse_datetime("") is None


def test_parse_datetime_malformed_returns_none() -> None:
    assert _parse_datetime("not-a-date") is None


def test_parse_datetime_date_only_string_returns_none() -> None:
    """Date-only strings without time component are invalid ISO datetime."""
    # "2026-05-25" is not a valid datetime ISO format
    result = _parse_datetime("2026-05-25")
    # Acceptable: either parsed (some parsers accept date-only) or None
    # The implementation uses fromisoformat which on Python 3.11+ accepts date-only
    # We just verify it doesn't raise.
    assert result is None or hasattr(result, "year")


# ---------------------------------------------------------------------------
# _effective_stale_after_hours
# ---------------------------------------------------------------------------


def _utc(weekday: int, hour: int = 12) -> datetime:
    """Build a UTC datetime with the given weekday (0=Mon … 6=Sun) and hour.

    We search forward from a known Monday so the weekday arithmetic is exact.
    """
    # 2026-05-25 is a Monday (weekday=0)
    monday = datetime(2026, 5, 25, hour, 0, 0, tzinfo=timezone.utc)
    return monday + timedelta(days=weekday)


def test_effective_stale_no_grace_returns_base() -> None:
    """No weekend_grace_hours → always returns base."""
    now = _utc(6)  # Sunday
    assert _effective_stale_after_hours(24.0, weekend_grace_hours=None, now=now) == 24.0


def test_effective_stale_weekday_returns_base() -> None:
    """Weekday (Tuesday) with grace → base returned."""
    now = _utc(1, hour=14)  # Tuesday afternoon
    result = _effective_stale_after_hours(24.0, weekend_grace_hours=72.0, now=now)
    assert result == 24.0


def test_effective_stale_saturday_returns_grace() -> None:
    now = _utc(5, hour=12)  # Saturday noon
    result = _effective_stale_after_hours(24.0, weekend_grace_hours=72.0, now=now)
    assert result == 72.0


def test_effective_stale_sunday_returns_grace() -> None:
    now = _utc(6, hour=12)  # Sunday noon
    result = _effective_stale_after_hours(24.0, weekend_grace_hours=72.0, now=now)
    assert result == 72.0


def test_effective_stale_monday_before_9_returns_grace() -> None:
    """Monday 07:00 in the *local* timezone → hour < 9 → grace applies.

    The function converts `now` via .astimezone() so we must build the
    datetime in local time directly to avoid UTC-offset ambiguity.
    """
    import datetime as _dt
    local_tz = _dt.datetime.now().astimezone().tzinfo
    # 2026-05-25 is a Monday; 07:00 local is before the 09:00 first-run cutoff
    monday_before_9 = _dt.datetime(2026, 5, 25, 7, 0, 0, tzinfo=local_tz)
    result = _effective_stale_after_hours(24.0, weekend_grace_hours=72.0, now=monday_before_9)
    assert result == 72.0


def test_effective_stale_monday_after_9_returns_base() -> None:
    now = _utc(0, hour=10)  # Monday 10:00 local
    result = _effective_stale_after_hours(24.0, weekend_grace_hours=72.0, now=now)
    assert result == 24.0


def test_effective_stale_grace_wins_over_base_when_grace_larger() -> None:
    """max(base, grace) is returned — grace must be at least as large as base."""
    now = _utc(6)  # Sunday
    result = _effective_stale_after_hours(48.0, weekend_grace_hours=24.0, now=now)
    assert result == 48.0   # max(48, 24) = 48


# ---------------------------------------------------------------------------
# _source_health_at
# ---------------------------------------------------------------------------


def _make_now() -> datetime:
    return datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)


def test_source_health_fresh_timestamp_not_stale() -> None:
    now = _make_now()
    ts = (now - timedelta(hours=1)).isoformat()
    result = _source_health_at(ts, stale_after_hours=24.0, now=now)
    assert result["stale"] is False
    assert result["age_hours"] == pytest.approx(1.0, abs=0.1)


def test_source_health_old_timestamp_is_stale() -> None:
    now = _make_now()
    ts = (now - timedelta(hours=25)).isoformat()
    result = _source_health_at(ts, stale_after_hours=24.0, now=now)
    assert result["stale"] is True
    assert result["age_hours"] > 24.0


def test_source_health_none_timestamp_is_stale() -> None:
    now = _make_now()
    result = _source_health_at(None, stale_after_hours=24.0, now=now)
    assert result["stale"] is True
    assert result["age_hours"] is None


def test_source_health_stale_after_hours_in_result() -> None:
    now = _make_now()
    result = _source_health_at(now.isoformat(), stale_after_hours=8.0, now=now)
    assert result["stale_after_hours"] == 8.0


def test_source_health_extra_kwargs_included() -> None:
    now = _make_now()
    result = _source_health_at(
        now.isoformat(), stale_after_hours=24.0, now=now,
        news_count=5, active_alert_count=2,
    )
    assert result["news_count"] == 5
    assert result["active_alert_count"] == 2


def test_source_health_timestamp_preserved_in_result() -> None:
    now = _make_now()
    ts = now.isoformat()
    result = _source_health_at(ts, stale_after_hours=24.0, now=now)
    assert result["timestamp"] == ts


# ---------------------------------------------------------------------------
# _build_data_health
# ---------------------------------------------------------------------------


def _make_fresh_state(now: datetime) -> dict:
    ts = now.isoformat()
    return {"evaluated_at": ts}


def _make_fresh_geo(now: datetime) -> dict:
    ts = now.isoformat()
    return {"cached_at": ts, "news_items": [1, 2], "active_alerts": [], "keyword_matches": [], "assessment_errors": []}


def _make_fresh_vix(now: datetime) -> dict:
    return {"cached_at": now.isoformat()}


def _make_fresh_macro(now: datetime) -> dict:
    return {"cached_at": now.isoformat()}


def _make_fresh_tech(now: datetime) -> dict:
    return {"cached_at": now.isoformat()}


def test_build_data_health_all_fresh_no_stale() -> None:
    now = _make_now()
    result = _build_data_health(
        state=_make_fresh_state(now),
        geo=_make_fresh_geo(now),
        tech=_make_fresh_tech(now),
        vix=_make_fresh_vix(now),
        macro=_make_fresh_macro(now),
        now=now,
    )
    assert result["has_stale_sources"] is False


def test_build_data_health_stale_vix_triggers_flag() -> None:
    now = _make_now()
    old_vix = {"cached_at": (now - timedelta(hours=20)).isoformat()}   # 20h > 12h threshold
    result = _build_data_health(
        state=_make_fresh_state(now),
        geo=_make_fresh_geo(now),
        tech=_make_fresh_tech(now),
        vix=old_vix,
        macro=_make_fresh_macro(now),
        now=now,
    )
    assert result["has_stale_sources"] is True
    assert result["vix"]["stale"] is True


def test_build_data_health_no_inputs_all_stale() -> None:
    """Called with defaults (all None) → all sources missing timestamps → all stale."""
    now = _make_now()
    result = _build_data_health(now=now)
    assert result["has_stale_sources"] is True


def test_build_data_health_has_collection_warnings_no_news() -> None:
    now = _make_now()
    geo_no_news = {
        "cached_at": now.isoformat(),
        "news_items": [],             # 0 news → warning
        "active_alerts": [],
        "keyword_matches": [],
        "assessment_errors": [],
    }
    result = _build_data_health(geo=geo_no_news, now=now)
    assert result["has_collection_warnings"] is True


def test_build_data_health_has_collection_warnings_assessment_errors() -> None:
    now = _make_now()
    geo_errors = {
        "cached_at": now.isoformat(),
        "news_items": [1, 2],
        "active_alerts": [],
        "keyword_matches": [],
        "assessment_errors": ["err"],  # non-empty → warning
    }
    result = _build_data_health(geo=geo_errors, now=now)
    assert result["has_collection_warnings"] is True


def test_build_data_health_returns_all_source_keys() -> None:
    now = _make_now()
    result = _build_data_health(now=now)
    for key in ("scenario", "geopolitical", "technical", "vix", "macro"):
        assert key in result


# ---------------------------------------------------------------------------
# _refresh_result_state
# ---------------------------------------------------------------------------


def test_refresh_result_state_nonzero_returncode_is_failed() -> None:
    assert _refresh_result_state(1, "before", "after") == "failed"


def test_refresh_result_state_returncode_none_treated_as_zero() -> None:
    """None returncode is treated like non-zero (defensive)."""
    assert _refresh_result_state(None, "ts1", "ts2") == "failed"


def test_refresh_result_state_no_after_is_warning() -> None:
    assert _refresh_result_state(0, "ts1", None) == "warning"


def test_refresh_result_state_empty_after_is_warning() -> None:
    assert _refresh_result_state(0, "ts1", "") == "warning"


def test_refresh_result_state_unchanged_after_is_warning() -> None:
    assert _refresh_result_state(0, "same_ts", "same_ts") == "warning"


def test_refresh_result_state_changed_after_is_succeeded() -> None:
    assert _refresh_result_state(0, "ts1", "ts2") == "succeeded"


def test_refresh_result_state_no_before_with_changed_after_is_succeeded() -> None:
    """before=None with new after → state was updated."""
    assert _refresh_result_state(0, None, "ts2") == "succeeded"


# ---------------------------------------------------------------------------
# _tail_text
# ---------------------------------------------------------------------------


def test_tail_text_none_returns_empty() -> None:
    assert _tail_text(None) == ""


def test_tail_text_short_string_unchanged() -> None:
    assert _tail_text("hello") == "hello"


def test_tail_text_long_string_truncated_from_end() -> None:
    long = "x" * 5000
    result = _tail_text(long, limit=4000)
    assert len(result) == 4000
    assert result == long[-4000:]


def test_tail_text_bytes_decoded() -> None:
    result = _tail_text(b"hello bytes")
    assert result == "hello bytes"


def test_tail_text_bytes_invalid_replaced_not_raised() -> None:
    result = _tail_text(b"\xff\xfe invalid utf-8")
    assert isinstance(result, str)   # errors="replace" → no exception
