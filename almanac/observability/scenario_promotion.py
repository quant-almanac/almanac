"""Scenario-level promotion view derived from catalyst outcome logs.

This module replaces the standalone ``scenario_shadow_book`` measurement lane
with a read-side snapshot built from the canonical catalyst append-only logs.
AI autonomy v2 uses ``promotion_ready`` as an automatic decision-stage signal:
not-ready observe-only scenarios remain capped provisional candidates, while
ready scenarios may be treated as normal decision context by the final AI
synthesis. Broker execution is still outside this module.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "ScenarioPromotionStats",
    "aggregate_scenario_promotion",
    "snapshot_to_file",
]

DEFAULT_PRIMARY_HORIZON_DAYS = 20
DEFAULT_MIN_MEASURED_EPISODES = 5
DEFAULT_MIN_HIT_RATE = 0.60
DEFAULT_MIN_MEAN_EXCESS_RETURN_BPS = 0.0


@dataclass(frozen=True)
class ScenarioPromotionStats:
    scenario_id: str
    observed_hypotheses: int
    observe_only_hypotheses: int
    measured_episodes: int
    hit_rate: float | None
    mean_excess_return_bps: float | None
    median_excess_return_bps: float | None
    promotion_ready: bool
    auto_decision_stage: str


def _read_jsonl_strict(path: Path | str) -> list[dict[str, Any]]:
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
            except json.JSONDecodeError as exc:
                raise ValueError(f"malformed JSONL: {p}:{lineno}: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"malformed JSONL: {p}:{lineno}: row is not an object")
            rows.append(row)
    return rows


def _event_timestamp(row: Mapping[str, Any]) -> str:
    return str(
        row.get("event_at")
        or row.get("generated_at")
        or row.get("analysis_date")
        or ""
    )


def _latest_generated_events(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in sorted(rows, key=_event_timestamp):
        if row.get("event_type") != "generated":
            continue
        hypothesis_id = row.get("hypothesis_id")
        if not hypothesis_id:
            continue
        latest[str(hypothesis_id)] = dict(row)
    return latest


def _latest_outcomes_by_key(
    rows: Iterable[Mapping[str, Any]],
) -> dict[tuple[str, int], dict[str, Any]]:
    latest: dict[tuple[str, int], dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: str(r.get("measured_at") or "")):
        hypothesis_id = row.get("hypothesis_id")
        horizon_days = row.get("horizon_days")
        if not hypothesis_id or horizon_days is None:
            continue
        try:
            key = (str(hypothesis_id), int(horizon_days))
        except (TypeError, ValueError):
            continue
        latest[key] = dict(row)
    return latest


def _scenario_id_from_event(row: Mapping[str, Any]) -> str | None:
    for field in ("source_event_id", "primary_source_agent"):
        value = str(row.get(field) or "")
        if value.startswith("scenario:") and value != "scenario:":
            return value.split(":", 1)[1]
    htype = str(row.get("hypothesis_type") or "")
    if htype.startswith("scenario_") and htype != "scenario_":
        return htype[len("scenario_") :]
    if htype == "bull_pullback":
        return "bull_pullback"
    return None


def _finite_excess_return_bps(row: Mapping[str, Any]) -> float | None:
    raw = row.get("after_cost_excess_return_bps")
    if raw is None:
        raw = row.get("excess_return_bps")
    if raw is None and row.get("return_pct") is not None:
        raw = float(row["return_pct"]) * 10_000
    if not isinstance(raw, (int, float)):
        return None
    value = float(raw)
    if not math.isfinite(value):
        return None
    return value


def _mean(values: list[float]) -> float | None:
    return (sum(values) / len(values)) if values else None


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return 0.5 * (ordered[mid - 1] + ordered[mid])


def _stats_to_dict(stats: ScenarioPromotionStats) -> dict[str, Any]:
    return {
        "scenario_id": stats.scenario_id,
        "observed_hypotheses": stats.observed_hypotheses,
        "observe_only_hypotheses": stats.observe_only_hypotheses,
        "measured_episodes": stats.measured_episodes,
        "hit_rate": stats.hit_rate,
        "mean_excess_return_bps": stats.mean_excess_return_bps,
        "median_excess_return_bps": stats.median_excess_return_bps,
        "promotion_ready": stats.promotion_ready,
        "auto_decision_stage": stats.auto_decision_stage,
        "promotion_review_required": False,
    }


def aggregate_scenario_promotion(
    hypothesis_events: Iterable[Mapping[str, Any]],
    outcomes: Iterable[Mapping[str, Any]],
    *,
    primary_horizon_days: int = DEFAULT_PRIMARY_HORIZON_DAYS,
    min_measured_episodes: int = DEFAULT_MIN_MEASURED_EPISODES,
    min_hit_rate: float = DEFAULT_MIN_HIT_RATE,
    min_mean_excess_return_bps: float = DEFAULT_MIN_MEAN_EXCESS_RETURN_BPS,
) -> dict[str, ScenarioPromotionStats]:
    """Aggregate catalyst outcomes into scenario-level promotion stats.

    ``measured_episodes`` is the number of distinct scenario hypotheses with a
    finite outcome at ``primary_horizon_days``. This is the catalyst-log
    equivalent of the old shadow book's episode count.
    """
    latest_events = _latest_generated_events(hypothesis_events)
    outcomes_by_key = _latest_outcomes_by_key(outcomes)

    scenario_to_hids: dict[str, set[str]] = {}
    scenario_to_observe_only_hids: dict[str, set[str]] = {}
    for hypothesis_id, event in latest_events.items():
        scenario_id = _scenario_id_from_event(event)
        if scenario_id is None:
            continue
        scenario_to_hids.setdefault(scenario_id, set()).add(hypothesis_id)
        if bool(event.get("observe_only", False)):
            scenario_to_observe_only_hids.setdefault(scenario_id, set()).add(hypothesis_id)

    result: dict[str, ScenarioPromotionStats] = {}
    for scenario_id, hids in sorted(scenario_to_hids.items()):
        values: list[float] = []
        for hypothesis_id in sorted(hids):
            row = outcomes_by_key.get((hypothesis_id, int(primary_horizon_days)))
            if row is None:
                continue
            value = _finite_excess_return_bps(row)
            if value is not None:
                values.append(value)

        wins = sum(1 for value in values if value > 0)
        hit_rate = (wins / len(values)) if values else None
        mean_bps = _mean(values)
        median_bps = _median(values)
        observe_only_count = len(scenario_to_observe_only_hids.get(scenario_id, set()))
        promotion_ready = bool(
            observe_only_count > 0
            and len(values) >= min_measured_episodes
            and hit_rate is not None
            and hit_rate >= min_hit_rate
            and mean_bps is not None
            and mean_bps > min_mean_excess_return_bps
        )
        if promotion_ready:
            auto_decision_stage = "full_decision"
        elif observe_only_count > 0:
            auto_decision_stage = "capped_provisional"
        else:
            auto_decision_stage = "not_observe_only"
        result[scenario_id] = ScenarioPromotionStats(
            scenario_id=scenario_id,
            observed_hypotheses=len(hids),
            observe_only_hypotheses=observe_only_count,
            measured_episodes=len(values),
            hit_rate=hit_rate,
            mean_excess_return_bps=mean_bps,
            median_excess_return_bps=median_bps,
            promotion_ready=promotion_ready,
            auto_decision_stage=auto_decision_stage,
        )
    return result


def snapshot_to_file(
    hypothesis_log_path: Path | str,
    outcome_log_path: Path | str,
    output_path: Path | str,
    *,
    primary_horizon_days: int = DEFAULT_PRIMARY_HORIZON_DAYS,
    min_measured_episodes: int = DEFAULT_MIN_MEASURED_EPISODES,
    min_hit_rate: float = DEFAULT_MIN_HIT_RATE,
    min_mean_excess_return_bps: float = DEFAULT_MIN_MEAN_EXCESS_RETURN_BPS,
) -> dict[str, Any]:
    """Write ``scenario_promotion_summary.json`` from catalyst logs atomically."""
    stats = aggregate_scenario_promotion(
        _read_jsonl_strict(hypothesis_log_path),
        _read_jsonl_strict(outcome_log_path),
        primary_horizon_days=primary_horizon_days,
        min_measured_episodes=min_measured_episodes,
        min_hit_rate=min_hit_rate,
        min_mean_excess_return_bps=min_mean_excess_return_bps,
    )
    payload = {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "primary_horizon_days": primary_horizon_days,
        "thresholds": {
            "min_measured_episodes": min_measured_episodes,
            "min_hit_rate": min_hit_rate,
            "min_mean_excess_return_bps": min_mean_excess_return_bps,
        },
        "by_scenario": {
            scenario_id: _stats_to_dict(item)
            for scenario_id, item in sorted(stats.items())
        },
        "note": "AI autonomy v2: promotion_ready automatically upgrades scenario context from capped_provisional to full_decision; broker execution remains out of scope",
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
