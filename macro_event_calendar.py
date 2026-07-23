"""Official macro-event calendar and deterministic pre-event risk gate."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from html.parser import HTMLParser
import json
import re

import requests

from utils import atomic_write_json


BASE_DIR = Path(__file__).parent
STATE_FILE = BASE_DIR / "macro_event_state.json"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
ET = ZoneInfo("America/New_York")
UTC = timezone.utc
IMPORTANT_BLS = {
    "Consumer Price Index": "cpi",
    "Employment Situation": "employment",
}
# The BLS iCalendar endpoint may return 403 to unattended clients.  These are
# the publication dates from the linked official annual schedule, kept only as
# a time-bounded break-glass fallback.  When a year is not present here, the
# risk gate deliberately degrades to review instead of assuming no CPI/jobs
# release exists.
BLS_CURATED_SCHEDULE_URL = "https://www.bls.gov/schedule/{year}/"
CURATED_BLS_RELEASE_DATES: dict[int, dict[str, tuple[str, ...]]] = {
    2026: {
        "employment": (
            "2026-01-09", "2026-02-11", "2026-03-06", "2026-04-03",
            "2026-05-08", "2026-06-05", "2026-07-02", "2026-08-07",
            "2026-09-04", "2026-10-02", "2026-11-06", "2026-12-04",
        ),
        "cpi": (
            "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
            "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
            "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
        ),
    },
}
RISK_INCREASING_TYPES = {"buy", "add", "dca", "margin_buy", "short", "short_sell"}
RISK_REDUCING_TYPES = {"sell", "trim", "reduce", "take_profit", "stop_loss", "cover", "buy_to_cover"}


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _parse_ics_dt(value: str) -> datetime | None:
    value = value.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
        try:
            dt = datetime.strptime(value, fmt)
            if value.endswith("Z"):
                return dt.replace(tzinfo=UTC)
            if "T" in value:
                return dt.replace(tzinfo=ET).astimezone(UTC)
            return dt.replace(hour=8, minute=30, tzinfo=ET).astimezone(UTC)
        except ValueError:
            continue
    return None


def parse_bls_ics(text: str) -> list[dict]:
    events: list[dict] = []
    current: dict[str, str] | None = None
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current:
                summary = current.get("SUMMARY", "")
                kind = next((code for label, code in IMPORTANT_BLS.items() if label.lower() in summary.lower()), None)
                scheduled = _parse_ics_dt(current.get("DTSTART", ""))
                if kind and scheduled:
                    events.append({
                        "event_id": current.get("UID") or f"bls:{kind}:{scheduled.date().isoformat()}",
                        "event_type": kind,
                        "title": summary,
                        "scheduled_at": scheduled.isoformat(),
                        "source": "bls_ics",
                        "source_url": BLS_ICS_URL,
                    })
            current = None
            continue
        if current is None or ":" not in line:
            continue
        key, value = line.split(":", 1)
        current[key.split(";", 1)[0]] = value
    return events


def curated_bls_events(year: int) -> list[dict]:
    """Return the versioned BLS fallback schedule for *year*, if available."""
    dates_by_type = CURATED_BLS_RELEASE_DATES.get(year, {})
    labels = {
        "cpi": "Consumer Price Index",
        "employment": "Employment Situation",
    }
    events: list[dict] = []
    for event_type, dates in dates_by_type.items():
        for date_text in dates:
            try:
                scheduled = datetime.fromisoformat(date_text).replace(
                    hour=8, minute=30, tzinfo=ET,
                ).astimezone(UTC)
            except ValueError:
                continue
            events.append({
                "event_id": f"bls:{event_type}:{date_text}",
                "event_type": event_type,
                "title": labels[event_type],
                "scheduled_at": scheduled.isoformat(),
                "source": "bls_curated_schedule",
                "source_url": BLS_CURATED_SCHEDULE_URL.format(year=year),
            })
    return events


def _latest_scheduled_at(events: list[dict]) -> str | None:
    values: list[datetime] = []
    for event in events:
        try:
            scheduled = datetime.fromisoformat(str(event.get("scheduled_at") or ""))
            values.append(scheduled.replace(tzinfo=UTC) if scheduled.tzinfo is None else scheduled.astimezone(UTC))
        except Exception:
            continue
    return max(values).isoformat() if values else None


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.parts.append(text)


_MONTHS = {
    name: number for number, name in enumerate(
        ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"),
        start=1,
    )
}


def parse_fomc_html(html: str, *, year: int) -> list[dict]:
    """Parse meeting month/ranges from the official FOMC calendar text."""
    parser = _TextExtractor()
    parser.feed(html)
    text = " ".join(parser.parts)
    # The Federal Reserve page carries several calendar years at once.  Scope
    # month parsing to the requested year's section so a 2027 meeting is never
    # projected into 2026 merely because both appear on the same page.
    markers: list[tuple[int, int]] = []
    for match in re.finditer(r"(?:(20\d{2})\s+FOMC\s+Meetings|FOMC\s+Meetings\s+(20\d{2}))", text, re.IGNORECASE):
        markers.append((match.start(), int(match.group(1) or match.group(2))))
    target = next((index for index, (_, marker_year) in enumerate(markers) if marker_year == year), None)
    if target is not None:
        start = markers[target][0]
        end = markers[target + 1][0] if target + 1 < len(markers) else len(text)
        text = text[start:end]
    events: list[dict] = []
    for month_name, month in _MONTHS.items():
        pattern = rf"{month_name}\s+(\d{{1,2}})(?:\s*[-–]\s*(\d{{1,2}}))?"
        for match in re.finditer(pattern, text):
            start_day = int(match.group(1))
            end_day = int(match.group(2) or start_day)
            try:
                # The scheduled statement is normally released at 2 p.m. ET
                # on the final meeting day.
                scheduled = datetime(year, month, end_day, 14, 0, tzinfo=ET).astimezone(UTC)
            except ValueError:
                continue
            event_id = f"fomc:{scheduled.date().isoformat()}"
            if any(row["event_id"] == event_id for row in events):
                continue
            events.append({
                "event_id": event_id,
                "event_type": "fomc",
                "title": f"FOMC meeting ({month_name} {start_day}-{end_day})",
                "scheduled_at": scheduled.isoformat(),
                "source": "federal_reserve_calendar",
                "source_url": FOMC_URL,
            })
    return events


def refresh_macro_event_state(
    *,
    state_file: Path = STATE_FILE,
    now: datetime | None = None,
    session=requests,
) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    errors: list[str] = []
    events: list[dict] = []
    bls_events: list[dict] = []
    bls_status = "unavailable"
    bls_coverage_through: str | None = None
    try:
        response = session.get(BLS_ICS_URL, timeout=20)
        response.raise_for_status()
        bls_events = parse_bls_ics(response.text)
        if bls_events:
            bls_status = "primary"
            bls_coverage_through = _latest_scheduled_at(bls_events)
        else:
            errors.append("bls:empty_supported_release_calendar")
    except Exception as exc:
        errors.append(f"bls:{type(exc).__name__}:{exc}")

    if not bls_events:
        fallback_events = curated_bls_events(now.astimezone(ET).year)
        if fallback_events:
            bls_events = fallback_events
            bls_status = "fallback"
            bls_coverage_through = datetime(
                now.astimezone(ET).year + 1, 1, 1, tzinfo=ET,
            ).astimezone(UTC).isoformat()
    events.extend(bls_events)

    fomc_events: list[dict] = []
    try:
        response = session.get(FOMC_URL, timeout=20)
        response.raise_for_status()
        fomc_events = parse_fomc_html(response.text, year=now.astimezone(ET).year)
        events.extend(fomc_events)
    except Exception as exc:
        errors.append(f"fomc:{type(exc).__name__}:{exc}")

    cutoff_before = now - timedelta(days=2)
    cutoff_after = now + timedelta(days=370)
    normalized = []
    seen: set[str] = set()
    for event in sorted(events, key=lambda row: row.get("scheduled_at") or ""):
        try:
            scheduled = datetime.fromisoformat(str(event["scheduled_at"]))
        except Exception:
            continue
        if not cutoff_before <= scheduled <= cutoff_after:
            continue
        if event["event_id"] in seen:
            continue
        seen.add(event["event_id"])
        normalized.append(event)
    state = {
        "refreshed_at": now.isoformat(),
        "status": "ok" if not errors else ("degraded" if normalized else "error"),
        "sources": [BLS_ICS_URL, FOMC_URL],
        "errors": errors,
        "source_health": {
            "bls": {
                "status": bls_status,
                "event_count": len(bls_events),
                "coverage_through": bls_coverage_through,
                "source_url": BLS_ICS_URL if bls_status == "primary" else BLS_CURATED_SCHEDULE_URL.format(year=now.astimezone(ET).year),
            },
            "fomc": {
                "status": "primary" if fomc_events else "unavailable",
                "event_count": len(fomc_events),
                "coverage_through": _latest_scheduled_at(fomc_events),
                "source_url": FOMC_URL,
            },
        },
        "events": normalized,
    }
    atomic_write_json(state_file, state)
    return state


def load_macro_event_state(path: Path = STATE_FILE) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def calendar_age_hours(state: dict, *, now: datetime) -> float | None:
    try:
        refreshed = datetime.fromisoformat(str(state.get("refreshed_at") or ""))
        if refreshed.tzinfo is None:
            refreshed = refreshed.replace(tzinfo=UTC)
        return max(0.0, (now.astimezone(UTC) - refreshed.astimezone(UTC)).total_seconds() / 3600)
    except Exception:
        return None


def _bls_coverage_is_current(state: dict, *, now: datetime) -> bool:
    """Whether BLS releases cover the full risk-gate look-ahead window."""
    health = state.get("source_health")
    bls = health.get("bls") if isinstance(health, dict) else None
    if isinstance(bls, dict):
        if bls.get("status") not in {"primary", "fallback"}:
            return False
        try:
            coverage = datetime.fromisoformat(str(bls.get("coverage_through") or ""))
            if coverage.tzinfo is None:
                coverage = coverage.replace(tzinfo=UTC)
            return coverage.astimezone(UTC) >= now.astimezone(UTC) + timedelta(hours=24)
        except Exception:
            return False

    # Pre-source-health states remain compatible unless they already record a
    # BLS failure.  The latter must not silently behave as a no-event day.
    return not any(str(error).startswith("bls:") for error in state.get("errors") or [])


def evaluate_macro_event_gate(action: dict, state: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    action_type = str(action.get("type") or "").lower()
    if action_type not in RISK_INCREASING_TYPES:
        return {"readiness": "ready", "reasons": []}
    source = str(action.get("source") or "").lower()
    if source == "dca_ladder" or action.get("scheduled_contribution") is True:
        return {"readiness": "ready", "reasons": []}

    age = calendar_age_hours(state, now=now)
    if age is None or age > 36 or state.get("status") == "error":
        return {
            "readiness": "review",
            "reasons": [{
                "code": "macro_event_calendar_stale",
                "message": "重要指標カレンダーが36時間以内に確認できていないため新規リスク注文は要確認",
            }],
        }
    if not _bls_coverage_is_current(state, now=now):
        return {
            "readiness": "review",
            "reasons": [{
                "code": "macro_event_calendar_bls_unavailable",
                "message": "CPI・雇用統計カレンダーの24時間先までの確認ができないため新規リスク注文は要確認",
            }],
        }

    nearest: tuple[float, dict] | None = None
    for event in state.get("events") or []:
        try:
            scheduled = datetime.fromisoformat(str(event.get("scheduled_at") or ""))
            if scheduled.tzinfo is None:
                scheduled = scheduled.replace(tzinfo=UTC)
        except Exception:
            continue
        hours = (scheduled.astimezone(UTC) - now.astimezone(UTC)).total_seconds() / 3600
        if -1 <= hours <= 24 and (nearest is None or abs(hours) < abs(nearest[0])):
            nearest = (hours, event)
    if nearest is None:
        return {"readiness": "ready", "reasons": []}
    hours, event = nearest
    event_context = {
        "event_id": event.get("event_id"),
        "event_type": event.get("event_type"),
        "event_title": event.get("title"),
        "scheduled_at": event.get("scheduled_at"),
        "hours_to_event": round(hours, 2),
    }
    if -1 <= hours <= 6:
        return {
            "readiness": "blocked",
            "event_context": event_context,
            "reasons": [{
                "code": "macro_event_blackout",
                "message": "重要指標の6時間前から発表後1時間までは新規リスク注文を停止",
            }],
        }
    return {
        "readiness": "review",
        "event_context": event_context,
        "required_size_multiplier": 0.5,
        "market_order_allowed": False,
        "reasons": [{
            "code": "macro_event_24h_caution",
            "message": "重要指標24時間以内のため成行禁止・通常サイズの50%以下で再確認が必要",
        }],
    }
