"""Per-agent reliability aggregation for the ALMANAC catalyst observability layer.

Plan §5 step 10 / Round 6 C6-8.

The derived snapshot is consumed by the analyzer prompt as a bounded context
block.  It is advisory only: deterministic hard-safety gates must never be
overridden by these weights.

This module aggregates ``agent_attribution_log.jsonl`` (one row per agent per
hypothesis per analysis run — R11 #1 flat-row discipline) against
``catalyst_outcome_log.jsonl`` (strict append-only outcomes — R9 #3) by
``hypothesis_id`` to compute per-agent reliability statistics.

The resulting ``agent_reliability.json`` is a derived, recomputable snapshot
written atomically (`.tmp` + ``os.replace``). It is NOT append-only: every
run replaces the previous snapshot with fresh aggregated stats.

Overfit guards (R6 C6-8)
------------------------
- ``n < 10``:   ``weight = None`` (insufficient data — explicit None, NOT 1.0)
- ``10 <= measured_n < 30``: ``weight = 1.0`` (neutral)
- ``measured_n >= 30``:  ``weight = clip(0.5 + 2 * (mean_excess_return_bps / 10000), 0.5, 1.5)``
- any group with no measured finite return yet: ``weight = None``

Public API
----------
- :class:`GroupStats` — frozen dataclass per ``(agent, role, stance)`` group.
- :func:`aggregate_agent_reliability` — pure aggregator over in-memory events.
- :func:`derive_weight` — pure weight derivation from :class:`GroupStats`.
- :func:`snapshot_to_file` — I/O entry point: read both logs, aggregate, write.
"""

from __future__ import annotations

import json
import logging
import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "GroupStats",
    "aggregate_agent_reliability",
    "derive_weight",
    "snapshot_to_file",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GroupStats:
    """Reliability statistics for one ``(agent, role, stance)`` group.

    ``weight`` is the derived reliability weight.  It is ``None`` when
    ``n < 10`` (insufficient data) or no finite return has joined yet,
    ``1.0`` when ``10 <= n < 30`` with measured returns (neutral), and a
    clipped proportional value when ``n >= 30``.
    """

    n: int
    win_rate: float | None
    mean_excess_return_bps: float | None
    payoff_ratio: float | None
    weight: float | None  # the derived reliability weight
    measured_n: int | None = None


# ---------------------------------------------------------------------------
# I/O helpers (mirrors verifier_extensions._load_jsonl)
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Read every parseable JSON line.  Missing file → ``[]``."""
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("%s:%d: malformed JSONL row, skipping", p, lineno)
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Pure aggregation primitives
# ---------------------------------------------------------------------------


def _safe_mean(values: list[float]) -> float | None:
    """``mean`` that returns ``None`` for empty input instead of raising."""
    if not values:
        return None
    return sum(values) / len(values)


def _kelly_payoff_ratio(values: list[float]) -> float | None:
    """``mean(positive) / abs(mean(negative))``.

    Returns ``None`` when either side of the ledger is empty.
    """
    wins = [v for v in values if v > 0]
    losses = [v for v in values if v < 0]
    if not wins or not losses:
        return None
    return (sum(wins) / len(wins)) / abs(sum(losses) / len(losses))


def derive_weight(stats: GroupStats) -> float | None:
    """Derive reliability weight from :class:`GroupStats`.

    Overfit guards (R6 C6-8):

    - ``n < 10``:      ``None``  (insufficient data)
    - no finite returns yet: ``None`` (unmeasured)
    - ``10 <= n < 30``: ``1.0``  (neutral)
    - ``n >= 30``:      ``clip(0.5 + 2 * (mean_excess_return_bps / 10000), 0.5, 1.5)``
    """
    if stats.n < 10:
        return None
    sample_n = stats.measured_n if stats.measured_n is not None else stats.n
    mean_bps = stats.mean_excess_return_bps
    if mean_bps is None:
        return None
    if sample_n < 10:
        return None
    if sample_n < 30:
        return 1.0
    # n >= 30: mild proportional weight
    raw = 0.5 + 2.0 * (mean_bps / 10_000.0)
    return max(0.5, min(1.5, raw))


def _build_group_stats(values: list[float], n_total: int) -> GroupStats:
    """Compute :class:`GroupStats` from a list of ``excess_return_bps`` floats.

    ``n_total`` is the number of attribution rows in this group (used as the
    authoritative ``n`` for the weight overfit guard).  ``values`` may be
    smaller when some outcome rows lack a finite ``excess_return_bps``.
    """
    wins = sum(1 for v in values if v > 0)
    win_rate = (wins / len(values)) if values else None
    mean_bps = _safe_mean(values)
    payoff = _kelly_payoff_ratio(values)
    # Build a preliminary stats object to derive weight.
    preliminary = GroupStats(
        n=n_total,
        win_rate=win_rate,
        mean_excess_return_bps=mean_bps,
        payoff_ratio=payoff,
        weight=None,
        measured_n=len(values),
    )
    weight = derive_weight(preliminary)
    return GroupStats(
        n=n_total,
        win_rate=win_rate,
        mean_excess_return_bps=mean_bps,
        payoff_ratio=payoff,
        weight=weight,
        measured_n=len(values),
    )


# ---------------------------------------------------------------------------
# Core aggregation
# ---------------------------------------------------------------------------


def aggregate_agent_reliability(
    attribution_events: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    *,
    horizon_days: int = 10,
) -> dict[str, dict[str, GroupStats]]:
    """Aggregate attribution + outcomes into per-agent reliability stats.

    Join key: ``hypothesis_id``.  Only outcomes whose ``horizon_days`` field
    matches the requested ``horizon_days`` argument are included (R5 #4).
    Because ``hypothesis_id`` is date-independent, repeated adoptions of the
    same hypothesis share one outcome row per horizon; each group joins a
    given ``hypothesis_id`` at most once so ``measured_n`` counts independent
    measurements, not adoption frequency.

    Attribution rows are 1-row-per-agent flat (R6 C6-6 / R11 #1).  The
    ``agent``, ``role``, and ``stance`` fields on each row define the group
    key.  Attribution rows without a ``hypothesis_id`` are silently skipped.

    Outcomes without a matching attribution row are silently ignored (they
    cannot be attributed to any agent).  Attribution rows whose
    ``hypothesis_id`` has no matching outcome land in a group with ``n``
    equal to the attribution count and all metric fields ``None``; the weight
    remains ``None`` until at least one finite return has joined.

    Returns
    -------
    ``{agent: {"{role}/{stance}": GroupStats}}``

    The nested key format is ``"{role}/{stance}"`` (e.g.
    ``"originator/support"``) to match the JSON output schema documented in
    the plan.
    """
    # Index outcomes by hypothesis_id, filtered to the requested horizon.
    # R5 #4: only outcomes matching horizon_days count.
    outcomes_by_hid: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for out in outcomes:
        hid = out.get("hypothesis_id")
        if not hid:
            continue
        if out.get("horizon_days") != horizon_days:
            continue
        outcomes_by_hid[hid].append(out)

    # Accumulate per (agent, role, stance) group.
    # Each group tracks: attribution row count + list of excess_return_bps.
    # Key: (agent, role, stance)
    group_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    group_values: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    # hypothesis_id は日付非依存 (ids.py) なので、同じ仮説を複数日採用すると
    # attribution 行が同一 ID で繰り返される。outcome は (hid, horizon) につき
    # 1 行しか書かれないため、行ごとに join すると同じ測定値が採用回数ぶん
    # 複製され measured_n が水増しされる。group×hid 単位で 1 回だけ join する。
    group_seen_hids: dict[tuple[str, str, str], set[str]] = defaultdict(set)

    for attr in attribution_events:
        hid = attr.get("hypothesis_id")
        if not hid:
            continue
        agent = attr.get("agent")
        role = attr.get("role")
        stance = attr.get("stance")
        if not agent or not role or not stance:
            # Skip attribution rows missing group-key fields.
            continue

        key = (str(agent), str(role), str(stance))
        group_counts[key] += 1

        if str(hid) in group_seen_hids[key]:
            continue
        group_seen_hids[key].add(str(hid))

        # Collect excess_return_bps from matching outcomes.
        for out in outcomes_by_hid.get(hid, []):
            raw = out.get("excess_return_bps")
            if raw is None:
                # Fallback to return_pct × 10000 for backward compat.
                ret = out.get("return_pct")
                if ret is None:
                    continue
                raw = ret * 10_000
            if not isinstance(raw, (int, float)):
                continue
            if not math.isfinite(raw):
                continue
            group_values[key].append(float(raw))

    # Build result dict: {agent: {"{role}/{stance}": GroupStats}}
    result: dict[str, dict[str, GroupStats]] = {}
    all_keys = set(group_counts.keys()) | set(group_values.keys())
    for key in all_keys:
        agent, role, stance = key
        n_total = group_counts[key]
        values = group_values[key]
        stats = _build_group_stats(values, n_total)
        group_key = f"{role}/{stance}"
        if agent not in result:
            result[agent] = {}
        result[agent][group_key] = stats

    return result


# ---------------------------------------------------------------------------
# Snapshot I/O
# ---------------------------------------------------------------------------


def _stats_to_dict(stats: GroupStats) -> dict[str, Any]:
    """Serialise :class:`GroupStats` to a JSON-safe dict."""
    return {
        "n": stats.n,
        "win_rate": stats.win_rate,
        "mean_excess_return_bps": stats.mean_excess_return_bps,
        "payoff_ratio": stats.payoff_ratio,
        "weight": stats.weight,
        "measured_n": stats.measured_n if stats.measured_n is not None else stats.n,
        "measured": stats.mean_excess_return_bps is not None,
    }


def snapshot_to_file(
    attribution_log_path: Path | str,
    outcome_log_path: Path | str,
    output_path: Path | str,
    *,
    horizon_days: int = 10,
) -> dict[str, Any]:
    """Read both append-only logs, aggregate, write ``agent_reliability.json``.

    The output file is written atomically via ``.tmp`` + ``os.replace`` so a
    crashed run cannot corrupt the live file (mirrors
    ``revision_tracker.write_revision_state``).

    Returns the dict that was written so callers can inspect or assert on it
    without a round-trip read.

    Output shape::

        {
            "as_of": "2026-05-24T12:00:00+00:00",
            "horizon_days": 10,
            "agents": {
                "catalyst_layer": {
                    "originator/support": {
                        "n": 42, "win_rate": 0.59,
                        "mean_excess_return_bps": 87.4,
                        "payoff_ratio": 1.6, "weight": 1.07,
                        "measured_n": 42, "measured": true
                    }
                }
            }
        }
    """
    attribution_events = _load_jsonl(attribution_log_path)
    outcomes = _load_jsonl(outcome_log_path)

    agg = aggregate_agent_reliability(
        attribution_events,
        outcomes,
        horizon_days=horizon_days,
    )

    as_of = datetime.now(timezone.utc).isoformat()

    payload: dict[str, Any] = {
        "as_of": as_of,
        "horizon_days": horizon_days,
        "agents": {
            agent: {
                group_key: _stats_to_dict(stats)
                for group_key, stats in groups.items()
            }
            for agent, groups in agg.items()
        },
    }

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_path)

    return payload
