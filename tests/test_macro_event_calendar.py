from datetime import datetime, timedelta, timezone

from macro_event_calendar import (
    BLS_ICS_URL,
    FOMC_URL,
    curated_bls_events,
    evaluate_macro_event_gate,
    parse_bls_ics,
    parse_fomc_html,
    refresh_macro_event_state,
)


def _state(now: datetime, hours_to_event: float) -> dict:
    return {
        "status": "ok",
        "refreshed_at": now.isoformat(),
        "events": [{
            "event_id": "bls:cpi:test",
            "event_type": "cpi",
            "title": "Consumer Price Index",
            "scheduled_at": (now + timedelta(hours=hours_to_event)).isoformat(),
        }],
    }


def test_parse_bls_ics_keeps_only_supported_market_moving_releases():
    text = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-1
DTSTART:20260714T123000Z
SUMMARY:Consumer Price Index for June 2026
END:VEVENT
BEGIN:VEVENT
UID:other-1
DTSTART:20260715T123000Z
SUMMARY:Import and Export Price Indexes
END:VEVENT
END:VCALENDAR
"""
    events = parse_bls_ics(text)
    assert len(events) == 1
    assert events[0]["event_type"] == "cpi"
    assert events[0]["scheduled_at"] == "2026-07-14T12:30:00+00:00"


def test_curated_bls_fallback_keeps_official_2026_cpi_and_employment_dates():
    events = curated_bls_events(2026)
    by_id = {event["event_id"]: event for event in events}

    assert by_id["bls:employment:2026-08-07"]["scheduled_at"] == "2026-08-07T12:30:00+00:00"
    assert by_id["bls:cpi:2026-08-12"]["scheduled_at"] == "2026-08-12T12:30:00+00:00"
    assert curated_bls_events(2027) == []


def test_parse_fomc_html_scopes_dates_to_requested_calendar_year():
    html = """
    <h4>2026 FOMC Meetings</h4><div>January 27-28</div><div>July 28-29</div>
    <h4>2027 FOMC Meetings</h4><div>January 26-27</div><div>July 27-28</div>
    """
    events = parse_fomc_html(html, year=2026)
    assert [row["event_id"] for row in events] == ["fomc:2026-01-28", "fomc:2026-07-29"]


def test_risk_increasing_order_is_review_24h_before_event():
    now = datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc)
    result = evaluate_macro_event_gate({"type": "buy"}, _state(now, 12), now=now)
    assert result["readiness"] == "review"
    assert result["required_size_multiplier"] == 0.5
    assert result["market_order_allowed"] is False


def test_risk_increasing_order_is_blocked_six_hours_before_through_one_hour_after():
    now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    assert evaluate_macro_event_gate({"type": "buy"}, _state(now, 2), now=now)["readiness"] == "blocked"
    assert evaluate_macro_event_gate({"type": "short"}, _state(now, -0.5), now=now)["readiness"] == "blocked"


def test_risk_reduction_is_not_blocked_by_macro_event():
    now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    result = evaluate_macro_event_gate({"type": "trim"}, _state(now, 1), now=now)
    assert result == {"readiness": "ready", "reasons": []}


def test_stale_calendar_is_review_not_no_event():
    now = datetime(2026, 7, 14, 10, 0, tzinfo=timezone.utc)
    state = {"status": "ok", "refreshed_at": (now - timedelta(hours=40)).isoformat(), "events": []}
    result = evaluate_macro_event_gate({"type": "buy"}, state, now=now)
    assert result["readiness"] == "review"
    assert result["reasons"][0]["code"] == "macro_event_calendar_stale"


class _Response:
    def __init__(self, text: str, *, ok: bool = True) -> None:
        self.text = text
        self.ok = ok

    def raise_for_status(self) -> None:
        if not self.ok:
            raise RuntimeError("HTTP 403")


class _BlsForbiddenSession:
    def get(self, url: str, timeout: int) -> _Response:
        assert timeout == 20
        if url == BLS_ICS_URL:
            return _Response("", ok=False)
        assert url == FOMC_URL
        return _Response("<h4>2026 FOMC Meetings</h4><div>July 28-29</div>")


def test_refresh_uses_curated_schedule_when_bls_ics_is_forbidden(tmp_path):
    now = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)
    state = refresh_macro_event_state(
        state_file=tmp_path / "macro_event_state.json",
        now=now,
        session=_BlsForbiddenSession(),
    )

    assert state["status"] == "degraded"
    assert state["source_health"]["bls"]["status"] == "fallback"
    assert state["source_health"]["bls"]["coverage_through"] == "2027-01-01T05:00:00+00:00"
    assert any(event["event_id"] == "bls:employment:2026-08-07" for event in state["events"])
    assert evaluate_macro_event_gate({"type": "buy"}, state, now=now)["readiness"] == "blocked"


def test_uncovered_bls_failure_is_review_not_no_event():
    now = datetime(2027, 1, 4, 10, 0, tzinfo=timezone.utc)
    state = {
        "status": "degraded",
        "refreshed_at": now.isoformat(),
        "errors": ["bls:HTTPError:403"],
        "source_health": {
            "bls": {"status": "unavailable", "coverage_through": None},
        },
        "events": [],
    }

    result = evaluate_macro_event_gate({"type": "buy"}, state, now=now)
    assert result["readiness"] == "review"
    assert result["reasons"][0]["code"] == "macro_event_calendar_bls_unavailable"
