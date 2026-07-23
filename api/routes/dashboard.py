"""
GET /api/dashboard
guard_state.json + regime_state.json を返す
"""
import sys
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from utils import load_json as _load_json
from portfolio_manager import build_portfolio_snapshot

_TIMESTAMP_KEYS = (
    "cached_at",
    "updated_at",
    "updated",
    "as_of",
    "generated_at",
    "timestamp",
    "evaluated_at",
    "last_updated",
    "last_scan",
)

_STATE_SOURCES = {
    "guard": {"filename": "guard_state.json", "stale_after_hours": 12},
    "regime": {"filename": "regime_state.json", "stale_after_hours": 48},
    "ai_analysis": {"filename": "ai_portfolio_analysis.json", "stale_after_hours": 8},
    # scenario/technical は平日 cron。週末から月曜朝の間は金曜夕方の state を許容する。
    "scenario": {
        "filename": "scenario_state.json",
        "stale_after_hours": 24,
        "weekend_grace_hours": 72,
    },
    "vix": {"filename": "vix_state.json", "stale_after_hours": 12},
    "technical": {
        "filename": "technical_state.json",
        "stale_after_hours": 24,
        "weekend_grace_hours": 72,
    },
    "macro": {"filename": "macro_state.json", "stale_after_hours": 48},
    "news_sentiment": {"filename": "news_sentiment_summary.json", "stale_after_hours": 24},
}


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            return dt.replace(tzinfo=local_tz).astimezone(timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_timestamp(data: dict) -> tuple[str | None, str | None]:
    if not isinstance(data, dict):
        return None, None
    for key in _TIMESTAMP_KEYS:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value, key
    return None, None


def _effective_stale_after_hours(
    stale_after_hours: float,
    *,
    weekend_grace_hours: float | None = None,
    now: datetime | None = None,
) -> float:
    """週末をまたぐ平日 cron source の false stale を避ける。"""
    if weekend_grace_hours is None:
        return stale_after_hours
    local_now = (now or datetime.now(timezone.utc)).astimezone()
    is_weekend = local_now.weekday() in (5, 6)
    is_monday_before_first_run = local_now.weekday() == 0 and local_now.hour < 9
    if is_weekend or is_monday_before_first_run:
        return max(stale_after_hours, weekend_grace_hours)
    return stale_after_hours


def _state_file_health(
    filename: str,
    stale_after_hours: float,
    *,
    base_dir: Path = BASE_DIR,
    weekend_grace_hours: float | None = None,
    now: datetime | None = None,
) -> dict:
    effective_stale_after = _effective_stale_after_hours(
        stale_after_hours,
        weekend_grace_hours=weekend_grace_hours,
        now=now,
    )
    path = base_dir / filename
    if not path.exists():
        return {
            "source_file": filename,
            "exists": False,
            "timestamp": None,
            "timestamp_source": None,
            "age_hours": None,
            "stale_after_hours": effective_stale_after,
            "stale": True,
        }

    data = _load_json(path, {})
    timestamp, timestamp_source = _extract_timestamp(data)
    if timestamp:
        dt = _parse_datetime(timestamp)
    else:
        dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        timestamp = dt.isoformat()
        timestamp_source = "mtime"

    age_hours = None
    if dt:
        age_hours = round(((now or datetime.now(timezone.utc)) - dt).total_seconds() / 3600, 1)

    return {
        "source_file": filename,
        "exists": True,
        "timestamp": timestamp,
        "timestamp_source": timestamp_source,
        "age_hours": age_hours,
        "stale_after_hours": effective_stale_after,
        "stale": age_hours is None or age_hours > effective_stale_after,
    }


def _build_data_health(*, base_dir: Path = BASE_DIR, now: datetime | None = None) -> dict:
    sources = {
        key: _state_file_health(
            spec["filename"],
            spec["stale_after_hours"],
            base_dir=base_dir,
            weekend_grace_hours=spec.get("weekend_grace_hours"),
            now=now,
        )
        for key, spec in _STATE_SOURCES.items()
    }
    stale = [key for key, item in sources.items() if item.get("stale")]
    missing = [key for key, item in sources.items() if not item.get("exists")]
    return {
        "sources": sources,
        "stale_sources": stale,
        "missing_sources": missing,
        "stale_count": len(stale),
        "missing_count": len(missing),
        "ok": len(stale) == 0 and len(missing) == 0,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/api/dashboard")
async def get_dashboard():
    guard            = _load_json(BASE_DIR / "guard_state.json")
    regime           = _load_json(BASE_DIR / "regime_state.json")
    news_sentiment   = _load_json(BASE_DIR / "news_sentiment_summary.json")

    # ライブ計算（常に最新値）
    try:
        snap = build_portfolio_snapshot(include_espp=True)
        portfolio_total = snap.get("total_jpy", 0)
    except Exception:
        portfolio_total = 0

    return {
        "guard": guard,
        "regime": regime,
        "portfolio_total": portfolio_total,
        "news_sentiment": news_sentiment if news_sentiment else None,
        "data_health": _build_data_health(),
    }
