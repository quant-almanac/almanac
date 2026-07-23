"""Persist execution-plan observe results and evaluate enforce readiness."""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).parent
OBSERVATION_PATH = BASE_DIR / "execution_plan_observations.jsonl"
MIN_TRADING_DAYS = 10
MIN_CLASSIFICATIONS = 20


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _monthly_attribution_snapshot(value: Any) -> dict[str, int | bool]:
    """Normalize the plan's month-to-date attribution coverage snapshot."""
    if not isinstance(value, dict) or value.get("available") is not True:
        return {
            "available": False,
            "unattributed_count": 0,
            "unattributed_notional_jpy": 0,
        }
    return {
        "available": True,
        "unattributed_count": _nonnegative_int(value.get("unattributed_count")),
        "unattributed_notional_jpy": _nonnegative_int(value.get("unattributed_notional_jpy")),
    }


def build_observation(
    synthesis: dict[str, Any],
    *,
    analysis_id: str | None,
    as_of: str | None,
) -> dict[str, Any] | None:
    if not isinstance(synthesis, dict):
        return None
    post_filter = synthesis.get("post_filter") or {}
    gate = post_filter.get("execution_plan_gate") if isinstance(post_filter, dict) else None
    if not isinstance(gate, dict) or not gate.get("mode"):
        return None

    decisions = gate.get("observed_decisions") or {}
    decisions = {
        str(key): _nonnegative_int(value)
        for key, value in decisions.items()
        if _nonnegative_int(value) > 0
    } if isinstance(decisions, dict) else {}
    batch = gate.get("batch_allocation") if isinstance(gate.get("batch_allocation"), dict) else {}
    monthly_attribution = _monthly_attribution_snapshot(gate.get("monthly_attribution"))
    as_of_text = str(as_of or datetime.now().astimezone().isoformat(timespec="seconds"))
    trading_date = as_of_text[:10]
    return {
        "schema_version": 1,
        "analysis_id": str(analysis_id or synthesis.get("analysis_id") or ""),
        "as_of": as_of_text,
        "trading_date": trading_date,
        "mode": str(gate.get("mode")),
        "classification_count": sum(decisions.values()),
        "observed_decisions": decisions,
        "would_filter_count": _nonnegative_int(gate.get("would_filter_count")),
        "batch_allocation": {
            "applied": bool(batch.get("applied")),
            "accepted_count": _nonnegative_int(batch.get("accepted_count")),
            "over_budget_count": _nonnegative_int(batch.get("over_budget_count")),
            "error": str(batch.get("error") or ""),
        },
        "classification_error_count": _nonnegative_int(decisions.get("execution_plan_error"))
            + (1 if batch.get("error") else 0),
        "metadata_mismatch_count": _nonnegative_int(decisions.get("plan_metadata_mismatch")),
        "monthly_attribution": monthly_attribution,
        "recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def load_observations(path: Path = OBSERVATION_PATH) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except OSError:
        return []
    return rows


def _observe_trading_dates(observations: list[dict[str, Any]]) -> list[str]:
    """Return unique weekday dates represented by observe rows.

    The readiness field is named ``trading_day_count``.  Weekend runs can
    still be recorded for audit, but must not satisfy the observation sample.
    Exchange-holiday calendars are intentionally out of scope here; the
    conservative minimum is Monday-Friday.
    """
    dates: set[str] = set()
    for row in observations:
        if not isinstance(row, dict) or not row.get("trading_date"):
            continue
        raw = str(row["trading_date"])[:10]
        try:
            observed = date.fromisoformat(raw)
        except ValueError:
            continue
        if observed.weekday() < 5:
            dates.add(observed.isoformat())
    return sorted(dates)


def evaluate_enforce_readiness(
    observations: list[dict[str, Any]],
    *,
    min_trading_days: int = MIN_TRADING_DAYS,
    min_classifications: int = MIN_CLASSIFICATIONS,
) -> dict[str, Any]:
    observe_rows = [row for row in observations if isinstance(row, dict) and row.get("mode") == "observe"]
    trading_days = _observe_trading_dates(observe_rows)
    classification_count = sum(_nonnegative_int(row.get("classification_count")) for row in observe_rows)
    error_count = sum(_nonnegative_int(row.get("classification_error_count")) for row in observe_rows)
    mismatch_count = sum(_nonnegative_int(row.get("metadata_mismatch_count")) for row in observe_rows)
    latest_observation = max(
        observe_rows,
        key=lambda row: str(row.get("as_of") or row.get("recorded_at") or row.get("trading_date") or ""),
        default=None,
    )
    monthly_attribution = _monthly_attribution_snapshot(
        latest_observation.get("monthly_attribution") if isinstance(latest_observation, dict) else None
    )
    sample_ready = len(trading_days) >= min_trading_days or classification_count >= min_classifications

    blockers: list[str] = []
    if not sample_ready:
        blockers.append(
            f"observation_sample_short: trading_days={len(trading_days)}/{min_trading_days}, "
            f"classifications={classification_count}/{min_classifications}"
        )
    if error_count:
        blockers.append(f"classification_errors_present: {error_count}")
    if mismatch_count:
        blockers.append(f"plan_metadata_mismatches_present: {mismatch_count}")
    if observe_rows and not monthly_attribution["available"]:
        blockers.append("monthly_attribution_unavailable")
    elif monthly_attribution["unattributed_count"]:
        blockers.append(
            "legacy_monthly_attribution_incomplete: "
            f"count={monthly_attribution['unattributed_count']}, "
            f"notional_jpy={monthly_attribution['unattributed_notional_jpy']}"
        )
    if not observe_rows:
        blockers.append("no_observe_records")

    return {
        "ready_for_enforce": not blockers,
        "observe_run_count": len(observe_rows),
        "trading_day_count": len(trading_days),
        "classification_count": classification_count,
        "classification_error_count": error_count,
        "metadata_mismatch_count": mismatch_count,
        "monthly_attribution": monthly_attribution,
        "minimums": {
            "trading_days": min_trading_days,
            "classifications": min_classifications,
            "sample_rule": "either",
        },
        "blockers": blockers,
        "first_trading_date": trading_days[0] if trading_days else None,
        "last_trading_date": trading_days[-1] if trading_days else None,
    }


def record_observation(
    synthesis: dict[str, Any],
    *,
    analysis_id: str | None = None,
    as_of: str | None = None,
    path: Path = OBSERVATION_PATH,
    fsync: bool = True,
) -> dict[str, Any]:
    observation = build_observation(synthesis, analysis_id=analysis_id, as_of=as_of)
    existing = load_observations(path)
    if observation is None:
        return {
            "recorded": False,
            "reason": "execution_plan_gate_report_missing",
            "readiness": evaluate_enforce_readiness(existing),
        }

    identity = (observation.get("analysis_id"), observation.get("as_of"))
    duplicate = any((row.get("analysis_id"), row.get("as_of")) == identity for row in existing)
    if not duplicate:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(observation, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            if fsync:
                os.fsync(handle.fileno())
        existing.append(observation)
    return {
        "recorded": not duplicate,
        "duplicate": duplicate,
        "observation": observation,
        "readiness": evaluate_enforce_readiness(existing),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect execution-plan enforce readiness")
    parser.add_argument("command", choices=["inspect"])
    parser.add_argument("--path", type=Path, default=OBSERVATION_PATH)
    args = parser.parse_args(argv)
    result = evaluate_enforce_readiness(load_observations(args.path))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
