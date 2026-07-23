"""Build the display-only disclosure section used by the morning brief."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from almanac.observability.disclosure_features import read_features

JST = timezone(timedelta(hours=9))


def _previous_business_day(day):
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _signal_strength(row: dict) -> float:
    values = []
    try:
        values.append(abs(float(row.get("directional_score") or 0)) * float(
            row.get("directional_confidence") or 0
        ))
        values.append(abs(float(row.get("guidance_revision_pct") or 0)))
        values.append(abs(float(row.get("monthly_yoy_pct") or 0)))
        values.append(min(1.0, float(row.get("insider_cluster_score") or 0) / 3.0))
    except (TypeError, ValueError):
        pass
    if row.get("activist_flag") is True:
        values.append(1.0)
    if row.get("dilution_flag") is True:
        try:
            values.append(max(0.1, abs(float(row.get("dilution_pct") or 0.5))))
        except (TypeError, ValueError):
            values.append(0.5)
    if row.get("going_concern_flag") is True:
        values.append(1.0)
    return max(values or [0.0])


def yesterday_disclosure_signals(
    *,
    rows: Optional[list[dict]] = None,
    now: Optional[datetime] = None,
    limit: int = 5,
) -> list[dict]:
    current = (now or datetime.now(JST)).astimezone(JST)
    target = _previous_business_day(current.date()).isoformat()
    selected = [
        row for row in (rows if rows is not None else read_features())
        if str(row.get("publish_time") or "")[:10] == target
    ]
    selected.sort(key=_signal_strength, reverse=True)
    return selected[:limit]


def format_brief_section(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["昨日の開示シグナル（未検証・観測のみ）"]
    for row in rows[:5]:
        score = _signal_strength(row)
        lines.append(
            f"- {row.get('ticker')} [{row.get('disclosure_type')}] "
            f"signal={score:.2f}: {str(row.get('summary') or '')[:120]}"
        )
    lines.append("売買推奨ではなく、observe_only の参考情報です。")
    return "\n".join(lines)
