"""Telegram push for high-signal observe-only disclosure rows."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from almanac.observability.disclosure_features import read_features
from insider_restrictions import is_restricted_ticker

BASE_DIR = Path(__file__).parent
DEFAULT_STATE_PATH = BASE_DIR / "data" / "disclosure_push_state.json"
# A push is an "act on this next session" alert, so it must be bounded by disclosure
# recency — never by dedup state alone. Without this, the first run over a populated
# store (empty state file = every historical row counts as "new") floods Telegram with
# months-old backfilled disclosures. Bounding on publish_time makes the alert mean
# "a genuinely recent high-signal event", and incidentally seeds the dedup state on
# the first run without a burst.
DEFAULT_MAX_AGE_DAYS = 4


def _is_recent(row: dict, *, max_age_days: int, now: datetime) -> bool:
    """True when the disclosure's publish_time is within ``max_age_days`` of ``now``.

    Rows whose publish_time is missing or unparseable are treated as NOT recent —
    an alert we cannot date is not an alert we should fire.
    """
    raw = row.get("publish_time")
    if not raw:
        return False
    try:
        dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    age_days = (now - dt.astimezone(timezone.utc)).total_seconds() / 86400.0
    # A future publish_time (clock skew) is "now-ish" → recent; only stale rows fail.
    return age_days <= max_age_days


def qualifies_for_push(row: dict) -> bool:
    if is_restricted_ticker(row.get("ticker")):
        return False
    try:
        directional = abs(float(row.get("directional_score") or 0))
        confidence = float(row.get("directional_confidence") or 0)
        guidance = abs(float(row.get("guidance_revision_pct") or 0))
    except (TypeError, ValueError):
        return False
    return bool(
        (directional >= 0.6 and confidence >= 0.7)
        or row.get("activist_flag") is True
        or guidance >= 0.10
    )


def format_push(row: dict) -> str:
    details: list[str] = []
    if row.get("guidance_revision_pct") is not None:
        details.append(f"営業利益予想修正 {float(row['guidance_revision_pct']):+.1%}")
    if row.get("activist_flag") is True:
        details.append("大量保有報告（公開アクティビスト名一致）")
    if row.get("directional_score") is not None:
        details.append(
            f"方向 {float(row['directional_score']):+.2f} / "
            f"確信 {float(row.get('directional_confidence') or 0):.2f}"
        )
    summary = str(row.get("summary") or "")[:300]
    return (
        "【未検証・観測のみ / observe_only】\n"
        f"{row.get('ticker')} {row.get('source')} {row.get('disclosure_type')}\n"
        f"{' / '.join(details)}\n{summary}\n"
        "売買推奨ではありません。policy_engine / priority_actions には接続していません。"
    )


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def push_new_disclosure_features(
    *,
    rows: Optional[list[dict]] = None,
    state_path: Path | str = DEFAULT_STATE_PATH,
    send: Optional[Callable[[str], None]] = None,
    max_age_days: int = DEFAULT_MAX_AGE_DAYS,
    now: Optional[datetime] = None,
) -> dict:
    state_file = Path(state_path)
    state = _load_state(state_file)
    sent_ids = set(state.get("sent_feature_ids") or [])
    now = now or datetime.now(timezone.utc)
    sender = send
    if sender is None:
        from telegram_bot import _send

        sender = _send

    sent: list[str] = []
    skipped_stale = 0
    for row in rows if rows is not None else read_features():
        if is_restricted_ticker(row.get("ticker")):
            continue
        feature_id = str(row.get("feature_id") or "")
        if not feature_id or feature_id in sent_ids or not qualifies_for_push(row):
            continue
        # Freshness gate: a high-signal but months-old disclosure is not an alert.
        # This also stops the first-run flood when the dedup state is empty but the
        # feature store is already populated (e.g. by a backfill).
        if not _is_recent(row, max_age_days=max_age_days, now=now):
            skipped_stale += 1
            # Mark as seen so a stale row is not re-evaluated every run, but do NOT
            # send it. (It will never become "recent", so this is safe to remember.)
            sent_ids.add(feature_id)
            continue
        sender(format_push(row))
        sent_ids.add(feature_id)
        sent.append(feature_id)

    state["sent_feature_ids"] = sorted(sent_ids)[-5000:]
    _write_state(state_file, state)
    return {"sent_count": len(sent), "skipped_stale": skipped_stale, "sent_feature_ids": sent}
