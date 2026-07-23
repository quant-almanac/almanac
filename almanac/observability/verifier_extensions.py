"""Aggregation sidecar for the existing :mod:`recommendation_verifier`.

Plan §5 step 4 extends the verifier surface to group outcomes by
``(hypothesis_type × ticker × horizon × candidate_status × execution_state)``.
Per the architecture invariants (Round 6 / Round 11 #C), the legacy
``recommendation_verifier.py`` (the 18:45 cron) is **not rewired** — it
keeps computing its ``action_type × urgency`` stats from
``ai_recommendation_log.json`` unchanged.

Instead, this module reads the new append-only logs
(``catalyst_hypothesis_log.jsonl`` + ``catalyst_outcome_log.jsonl``,
plus optional ``sell_decision_log.jsonl``) and produces the
multi-dimensional EV / hit-rate / payoff-ratio rollups consumers need.

Why side-by-side instead of modifying the legacy verifier
---------------------------------------------------------

1. The legacy verifier uses ``ai_recommendation_log.json`` as its source
   of truth. The new logs are append-only JSONL and have an entirely
   different schema (Round 9 #3 architectural split).
2. The legacy verifier writes back the verification status into the
   same file — that's a mutate pattern incompatible with the
   append-only discipline we now enforce.
3. The new EV-centric metrics (plan §5 step 4, Round 4 C4-1) replace
   the legacy hit-rate-centric metrics. Running both side-by-side for
   4-8 weeks lets us compare them before retiring the legacy file
   (Phase 2 / Phase 3).

Pure functions
--------------

All non-I/O functions in this module are pure. The two I/O entry
points (:func:`read_hypothesis_events`, :func:`read_outcomes`) accept a
path and return parsed rows; the rest of the API takes those rows as
parameters so tests can construct any history they like.
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .status import CandidateStatus

__all__ = [
    "read_hypothesis_events",
    "read_outcomes",
    "latest_candidate_status",
    "compute_group_stats",
    "aggregate_by_dimensions",
    "summarize",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path | str) -> list[dict[str, Any]]:
    """Read every parseable JSON line. Missing file → ``[]``."""
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


def read_hypothesis_events(path: Path | str) -> list[dict[str, Any]]:
    """Read ``catalyst_hypothesis_log.jsonl`` (every event_type)."""
    return _load_jsonl(path)


def read_outcomes(path: Path | str) -> list[dict[str, Any]]:
    """Read ``catalyst_outcome_log.jsonl``."""
    return _load_jsonl(path)


# ---------------------------------------------------------------------------
# Pure aggregation primitives
# ---------------------------------------------------------------------------


def _event_timestamp(row: Mapping[str, Any]) -> str:
    """Best-available timestamp for a hypothesis-log row.

    Generated rows carry ``event_at`` (UTC ISO from the writer); legacy
    rows may only have ``generated_at`` or just ``analysis_date``.
    Returns an empty string when none of the three is present — sorts
    those rows first, which is the conservative choice (later rows then
    override on shared keys).
    """
    return (
        row.get("event_at")
        or row.get("generated_at")
        or row.get("analysis_date")
        or ""
    )


def latest_candidate_status(
    events: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """For each ``hypothesis_id``, return a chronologically-merged snapshot.

    Walks every event row in timestamp order and applies it as an
    update onto the per-hypothesis accumulator. Fields the latest row
    omits (such as ``hypothesis_type`` on a ``status_transition`` row,
    which only carries the new ``candidate_status``) are preserved from
    the earlier ``generated`` row that established them.

    Newer values for fields the latest row **does** carry (notably
    ``candidate_status`` flipping from ``injected`` to
    ``injected_rejected`` or ``adopted``) overwrite older ones, which
    is the property tests rely on.

    Events without a ``hypothesis_id`` are ignored — they cannot
    participate in the join.
    """
    # Pre-sort so the forward-merge respects chronology. ISO 8601
    # zero-padded timestamps sort lexicographically.
    rows = [ev for ev in events if ev.get("hypothesis_id")]
    rows.sort(key=_event_timestamp)
    merged: dict[str, dict[str, Any]] = {}
    for ev in rows:
        hid = ev["hypothesis_id"]
        if hid not in merged:
            merged[hid] = dict(ev)
        else:
            merged[hid].update(ev)
    return merged


def _kelly_payoff_ratio(returns: list[float]) -> float | None:
    """``mean(positive) / abs(mean(negative))``.

    Returns ``None`` when either side of the ledger is empty (a payoff
    ratio is meaningless without both wins and losses to compare).
    """
    wins = [r for r in returns if r > 0]
    losses = [r for r in returns if r < 0]
    if not wins or not losses:
        return None
    return sum(wins) / len(wins) / abs(sum(losses) / len(losses))


def _safe_mean(values: list[float]) -> float | None:
    """``mean`` that returns ``None`` for empty input instead of raising."""
    if not values:
        return None
    return sum(values) / len(values)


def _safe_std(values: list[float]) -> float | None:
    """Population standard deviation; ``None`` for fewer than 2 samples."""
    if len(values) < 2:
        return None
    mu = sum(values) / len(values)
    var = sum((v - mu) ** 2 for v in values) / len(values)
    return math.sqrt(var)


def compute_group_stats(
    outcomes: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Compute the EV / hit-rate / payoff-ratio bundle for a group.

    Uses ``excess_return_bps`` as the primary signal (it is already
    benchmark-adjusted per Round 9 #6 / Round 8 #7 — the cleaner read
    of "did this hypothesis beat its benchmark"). Falls back to
    ``return_pct`` × 10000 for backward compat when ``excess_return_bps``
    is missing.

    Returns a dict with explicit ``None`` for metrics that cannot be
    computed (e.g. payoff ratio with no losses) so callers can present
    "n/a" cleanly instead of zero-divs.
    """
    values: list[float] = []
    for row in outcomes:
        raw = row.get("excess_return_bps")
        if raw is None:
            ret = row.get("return_pct")
            if ret is None:
                continue
            raw = ret * 10_000
        if not isinstance(raw, (int, float)):
            continue
        if not math.isfinite(raw):
            continue
        values.append(float(raw))
    n = len(values)
    wins = sum(1 for v in values if v > 0)
    losses = sum(1 for v in values if v < 0)
    return {
        "n": n,
        "ev_bps": _safe_mean(values),                  # primary metric (R4 C4-1)
        "median_bps": _median(values),
        "std_bps": _safe_std(values),
        "hit_rate": (wins / n) if n else None,          # demoted to secondary
        "payoff_ratio": _kelly_payoff_ratio(values),
        "win_count": wins,
        "loss_count": losses,
        "max_gain_bps": max(values) if values else None,
        "max_loss_bps": min(values) if values else None,
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return 0.5 * (s[mid - 1] + s[mid])


def aggregate_by_dimensions(
    hypothesis_events: Iterable[Mapping[str, Any]],
    outcomes: Iterable[Mapping[str, Any]],
    *,
    dimensions: tuple[str, ...] = (
        "hypothesis_type",
        "candidate_status",
        "horizon_days",
    ),
) -> dict[tuple, dict[str, Any]]:
    """Group outcomes by ``dimensions``; compute stats per group.

    The join key is ``hypothesis_id``: each outcome row inherits its
    grouping dimensions from the **latest** hypothesis_log row carrying
    that ``hypothesis_id``. Outcomes whose ``hypothesis_id`` has no
    matching event are bucketed under ``hypothesis_type="unknown"``
    rather than dropped — silent loss is the worst possible failure
    mode for an audit log.

    ``dimensions`` may include any field present on the joined row.
    Typical groupings:

    - ``("hypothesis_type", "candidate_status", "horizon_days")`` —
      the plan §5 step 4 default.
    - ``("hypothesis_type", "ticker", "horizon_days")`` — for per-
      ticker post-mortem on a specific playbook.
    - ``("source_agents", "candidate_status")`` — feeds the agent
      attribution report.

    Returns a dict keyed by the dimension tuple in declaration order,
    where each value is the :func:`compute_group_stats` bundle.
    """
    latest = latest_candidate_status(hypothesis_events)
    buckets: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for out in outcomes:
        hid = out.get("hypothesis_id")
        if not hid:
            continue
        hyp_row = latest.get(hid)
        joined = dict(out)
        if hyp_row:
            # Hypothesis context wins for hypothesis_type / ticker /
            # candidate_status; the outcome row keeps its horizon_days,
            # excess_return_bps, etc.
            for k in ("hypothesis_type", "ticker", "candidate_status"):
                if k in hyp_row:
                    joined.setdefault(k, hyp_row[k])
            # Codex Round 12 P2 #4: catalyst_hypothesis_log writes
            # ``primary_ticker`` (catalyst-layer convention), not
            # ``ticker``. Fall through so per-ticker dimensions group
            # correctly instead of bucketing every catalyst entry under
            # ``ticker=None``.
            if joined.get("ticker") is None:
                pt = hyp_row.get("primary_ticker") or out.get("primary_ticker")
                if pt is not None:
                    joined["ticker"] = pt
        else:
            joined.setdefault("hypothesis_type", "unknown")
            joined.setdefault("candidate_status", CandidateStatus.legacy.value)
            # Even without an event row, the outcome may carry its own
            # primary_ticker; honour it so the same fallback applies.
            if joined.get("ticker") is None and out.get("primary_ticker"):
                joined["ticker"] = out["primary_ticker"]
        key = tuple(joined.get(dim) for dim in dimensions)
        buckets[key].append(joined)
    return {
        key: compute_group_stats(rows) for key, rows in buckets.items()
    }


# ---------------------------------------------------------------------------
# Top-level convenience
# ---------------------------------------------------------------------------


def summarize(
    *,
    hypothesis_log_path: Path | str,
    outcome_log_path: Path | str,
    dimensions: tuple[str, ...] = (
        "hypothesis_type",
        "candidate_status",
        "horizon_days",
    ),
) -> dict[str, Any]:
    """Read both logs, run the default aggregation, return a flat report.

    Output shape::

        {
            "n_hypothesis_events": int,
            "n_outcomes": int,
            "n_groups": int,
            "dimensions": tuple[str, ...],   # echoed for reproducibility
            "groups": [
                {
                    "key": {"hypothesis_type": "...", ...},
                    "stats": {"n": ..., "ev_bps": ..., ...},
                },
                ...
            ],
        }
    """
    events = read_hypothesis_events(hypothesis_log_path)
    outcomes = read_outcomes(outcome_log_path)
    agg = aggregate_by_dimensions(events, outcomes, dimensions=dimensions)
    groups = []
    for raw_key, stats in agg.items():
        key_dict = {dim: raw_key[i] for i, dim in enumerate(dimensions)}
        groups.append({"key": key_dict, "stats": stats})
    return {
        "n_hypothesis_events": len(events),
        "n_outcomes": len(outcomes),
        "n_groups": len(groups),
        "dimensions": dimensions,
        "groups": groups,
    }
