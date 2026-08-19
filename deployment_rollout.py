"""Audited observe-to-enforce rollout for capital deployment.

The existing execution-plan gate remains the sole policy switch.  This module
adds evidence and recovery around that switch: five isolated replay cycles,
one production canary, an explicit promotion, and an immediate rollback path.
Runtime evidence is operator-owned state and is never a source of buying power.
"""
from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from utils import atomic_write_json


STATE_FILE = "deployment_rollout_state.json"
AUDIT_FILE = "deployment_rollout_audit.jsonl"
MIN_REPLAY_CYCLES = 5
_BUY_TYPES = {"buy", "add", "dca", "margin_buy"}
_RISK_INCREASING_TYPES = _BUY_TYPES | {"short"}


def _root(base_dir: Path) -> Path:
    base = Path(base_dir)
    state_dir = os.environ.get("ALMANAC_STATE_DIR")
    # An explicit base_dir is an isolation/recovery boundary and must win over
    # the process-wide runtime directory.  The environment redirect applies
    # only to the normal repository-root invocation.
    if state_dir and base.resolve() == Path(__file__).resolve().parent:
        return Path(state_dir)
    return base


def _paths(base_dir: Path) -> tuple[Path, Path]:
    root = _root(Path(base_dir))
    return root / STATE_FILE, root / AUDIT_FILE


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_audit(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def load_state(*, base_dir: Path) -> dict[str, Any]:
    state_path, _ = _paths(base_dir)
    state = _read_json(state_path)
    if state:
        return state
    return {
        "schema_version": 1,
        "mode": "observe",
        "replay_cycles": [],
        "production_canaries": [],
        "last_transition": None,
    }


def _household_buy(action: dict[str, Any]) -> bool:
    action_type = str(action.get("type") or "").strip().lower()
    return action_type in {"buy", "add", "dca", "margin_buy"}


def _ordinary_cash_buy(action: dict[str, Any]) -> bool:
    if str(action.get("type") or "").strip().lower() not in {"buy", "add", "dca"}:
        return False
    source = str(action.get("source") or "").strip().lower()
    tier = str(action.get("tier") or "").strip().lower()
    return not source.startswith("scenario") and tier != "swing"


def validate_analysis(synthesis: dict[str, Any] | None) -> dict[str, Any]:
    """Validate post-allocation invariants without mutating the analysis."""
    payload = synthesis if isinstance(synthesis, dict) else {}
    actions = [row for row in payload.get("priority_actions") or [] if isinstance(row, dict)]
    ready = [row for row in actions if row.get("execution_readiness") == "ready"]
    ready_buys = [row for row in ready if _household_buy(row)]
    errors: list[dict[str, Any]] = []

    if len(ready_buys) > 1:
        errors.append({
            "code": "household_ready_buy_limit_exceeded",
            "actual": len(ready_buys),
            "limit": 1,
        })
    for action in ready_buys:
        ticker = str(action.get("ticker") or "")
        if action.get("capital_allocator_selected") is not True:
            errors.append({"code": "ready_buy_not_allocator_selected", "ticker": ticker})
        notional = _number(action.get("estimated_notional_jpy"), default=-1)
        broad = str(action.get("source") or "").lower() == "scheduled_broad_deployment"
        cap = 500_000 if broad else (250_000 if _ordinary_cash_buy(action) else None)
        if notional <= 0 or (cap is not None and notional > cap + 1):
            errors.append({
                "code": "ready_buy_notional_outside_cap",
                "ticker": ticker,
                "notional_jpy": notional,
                "cap_jpy": cap,
            })
        if action.get("execution_block_reasons"):
            errors.append({"code": "ready_buy_has_block_reasons", "ticker": ticker})
        if broad:
            required = (
                "execution_owner", "execution_broker", "execution_account",
                "cash_wallet_key", "plan_item_id", "route_id",
            )
            missing = [key for key in required if not str(action.get(key) or "").strip()]
            if missing:
                errors.append({
                    "code": "scheduled_broad_route_incomplete",
                    "ticker": ticker,
                    "fields": missing,
                })
            if action.get("human_execution_only") is not True:
                errors.append({"code": "scheduled_broad_not_human_only", "ticker": ticker})

    summary = payload.get("executable_plan_summary")
    if isinstance(summary, dict):
        if int(summary.get("analysis_ready_count") or 0) != len(ready):
            errors.append({
                "code": "readiness_summary_count_mismatch",
                "summary": int(summary.get("analysis_ready_count") or 0),
                "actual": len(ready),
            })
    else:
        errors.append({"code": "readiness_summary_missing"})
    consistency = payload.get("selection_consistency")
    if not isinstance(consistency, dict):
        errors.append({"code": "selection_consistency_missing"})
    else:
        if int(consistency.get("household_ready_risk_buy_count") or 0) != len(ready_buys):
            errors.append({
                "code": "selection_consistency_count_mismatch",
                "summary": int(consistency.get("household_ready_risk_buy_count") or 0),
                "actual": len(ready_buys),
            })
        if len(ready_buys) <= 1 and consistency.get("status") != "ok":
            errors.append({"code": "selection_consistency_status_mismatch"})

    return {
        "schema_version": 1,
        "status": "ok" if not errors else "failed",
        "checked_at": _now(),
        "ready_action_count": len(ready),
        "ready_household_cash_buy_count": len(ready_buys),
        "risk_reducing_ready_count": sum(
            1 for row in ready
            if str(row.get("type") or "").lower()
            in {"sell", "trim", "reduce", "stop_loss", "take_profit", "cover"}
        ),
        "errors": errors,
    }


def _write_transition(
    *,
    base_dir: Path,
    state: dict[str, Any],
    mode: str,
    event_type: str,
    actor: str,
    reason: str,
    reference: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_path, audit_path = _paths(base_dir)
    at = _now()
    transition = {
        "event_type": event_type,
        "at": at,
        "actor": actor,
        "reason": reason,
        "reference": reference,
        "mode": mode,
        "details": details or {},
    }
    next_state = dict(state)
    next_state.update({
        "schema_version": 1,
        "mode": mode,
        "updated_at": at,
        "last_transition": transition,
    })
    # Audit first.  If the mutable JSON write fails, the event still proves
    # that an attempted transition occurred and operators can fail closed.
    _append_audit(audit_path, transition)
    atomic_write_json(state_path, next_state)
    return next_state


def record_replay_cycle(
    *,
    base_dir: Path,
    synthesis: dict[str, Any],
    cycle_id: str,
    simulated_as_of: str,
    scenario: str,
    actor: str,
    reference: str,
) -> dict[str, Any]:
    validation = validate_analysis(synthesis)
    if validation["status"] != "ok":
        raise ValueError("replay cycle invariant failure")
    state = load_state(base_dir=base_dir)
    cycles = [row for row in state.get("replay_cycles") or [] if isinstance(row, dict)]
    cycle_id = str(cycle_id or "").strip()
    if not cycle_id or any(row.get("cycle_id") == cycle_id for row in cycles):
        raise ValueError("cycle_id must be non-empty and unique")
    cycle = {
        "cycle_id": cycle_id,
        "simulated_as_of": str(simulated_as_of or ""),
        "scenario": str(scenario or ""),
        "actor": str(actor or ""),
        "reference": str(reference or ""),
        "recorded_at": _now(),
        "validation": validation,
    }
    if not cycle["simulated_as_of"] or not cycle["scenario"] or not cycle["actor"] or not cycle["reference"]:
        raise ValueError("simulated_as_of, scenario, actor, and reference are required")
    cycles.append(cycle)
    state["replay_cycles"] = cycles[-50:]
    return _write_transition(
        base_dir=base_dir,
        state=state,
        mode=str(state.get("mode") or "observe"),
        event_type="deployment_replay_recorded",
        actor=actor,
        reason=f"isolated replay cycle {cycle_id}",
        reference=reference,
        details={"cycle": cycle},
    )


def record_production_canary(
    *,
    base_dir: Path,
    synthesis: dict[str, Any],
    actor: str,
    reference: str,
) -> dict[str, Any]:
    validation = validate_analysis(synthesis)
    if validation["status"] != "ok":
        raise ValueError("production canary invariant failure")
    state = load_state(base_dir=base_dir)
    canary = {
        "analysis_id": str(synthesis.get("analysis_id") or ""),
        "analysis_as_of": str(synthesis.get("as_of") or ""),
        "actor": str(actor or ""),
        "reference": str(reference or ""),
        "recorded_at": _now(),
        "validation": validation,
    }
    if not canary["analysis_id"] or not canary["actor"] or not canary["reference"]:
        raise ValueError("analysis_id, actor, and reference are required")
    canaries = [row for row in state.get("production_canaries") or [] if isinstance(row, dict)]
    existing_index = next(
        (index for index, row in enumerate(canaries) if row.get("analysis_id") == canary["analysis_id"]),
        None,
    )
    if existing_index is None:
        canaries.append(canary)
    else:
        # Idempotent re-recording may enrich an older entry after an audit
        # reader fix, but must never create duplicate promotion evidence.
        existing = dict(canaries[existing_index])
        existing.update({key: value for key, value in canary.items() if value not in (None, "")})
        canary = existing
        canaries[existing_index] = canary
    state["production_canaries"] = canaries[-20:]
    return _write_transition(
        base_dir=base_dir,
        state=state,
        mode=str(state.get("mode") or "observe"),
        event_type="deployment_canary_recorded",
        actor=actor,
        reason="production observe canary passed",
        reference=reference,
        details={"canary": canary},
    )


def _set_execution_plan_mode(mode: str, *, source: str, rationale: str) -> dict[str, Any]:
    from tunable_params import set_value

    return set_value("execution_plan_gate_mode", mode, source=source, rationale=rationale)


def promote(
    *,
    base_dir: Path,
    actor: str,
    reference: str,
    observer_readiness: dict[str, Any] | None = None,
    set_mode: Callable[..., dict[str, Any]] = _set_execution_plan_mode,
) -> dict[str, Any]:
    state = load_state(base_dir=base_dir)
    good_cycles = [
        row for row in state.get("replay_cycles") or []
        if isinstance(row, dict) and (row.get("validation") or {}).get("status") == "ok"
    ]
    good_canaries = [
        row for row in state.get("production_canaries") or []
        if isinstance(row, dict) and (row.get("validation") or {}).get("status") == "ok"
    ]
    if len({row.get("cycle_id") for row in good_cycles}) < MIN_REPLAY_CYCLES:
        raise ValueError(f"at least {MIN_REPLAY_CYCLES} unique replay cycles are required")
    if not good_canaries:
        raise ValueError("one passing production canary is required")
    if observer_readiness is None:
        from execution_plan_observer import evaluate_enforce_readiness, load_observations

        observer_readiness = evaluate_enforce_readiness(load_observations())
    if observer_readiness.get("ready_for_enforce") is not True:
        raise ValueError("execution-plan observer is not ready for enforce")
    actor = str(actor or "").strip()
    reference = str(reference or "").strip()
    if not actor or not reference:
        raise ValueError("actor and reference are required")
    pending = _write_transition(
        base_dir=base_dir,
        state=state,
        mode="observe",
        event_type="deployment_rollout_promotion_requested",
        actor=actor,
        reason="promotion evidence passed; gate change pending",
        reference=reference,
        details={
            "replay_cycle_count": len({row.get("cycle_id") for row in good_cycles}),
            "production_canary_count": len(good_canaries),
            "observer_readiness": observer_readiness,
        },
    )
    try:
        set_mode(
            "enforce",
            source="deployment_rollout",
            rationale=f"promoted by {actor}; evidence={reference}",
        )
        return _write_transition(
            base_dir=base_dir,
            state=pending,
            mode="enforce",
            event_type="deployment_rollout_promoted",
            actor=actor,
            reason="five isolated replay cycles and one production canary passed",
            reference=reference,
            details={
                "replay_cycle_count": len({row.get("cycle_id") for row in good_cycles}),
                "production_canary_count": len(good_canaries),
                "observer_readiness": observer_readiness,
            },
        )
    except Exception:
        # Never leave an enforce tunable behind when its rollout evidence could
        # not be finalized.  A second failure is intentionally surfaced, but
        # the first recovery attempt is always the conservative observe mode.
        set_mode(
            "observe",
            source="deployment_rollout",
            rationale=f"promotion finalization failed; evidence={reference}",
        )
        _write_transition(
            base_dir=base_dir,
            state=pending,
            mode="quarantine",
            event_type="deployment_rollout_promotion_failed",
            actor=actor,
            reason="promotion finalization failed and was rolled back",
            reference=reference,
        )
        raise


def rollback(
    *,
    base_dir: Path,
    actor: str,
    reason: str,
    reference: str,
    quarantine: bool = True,
    set_mode: Callable[..., dict[str, Any]] = _set_execution_plan_mode,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    actor = str(actor or "").strip()
    reason = str(reason or "").strip()
    reference = str(reference or "").strip()
    if not actor or not reason or not reference:
        raise ValueError("actor, reason, and reference are required")
    set_mode(
        "observe",
        source="deployment_rollout",
        rationale=f"rollback by {actor}: {reason}; evidence={reference}",
    )
    state = load_state(base_dir=base_dir)
    return _write_transition(
        base_dir=base_dir,
        state=state,
        mode="quarantine" if quarantine else "observe",
        event_type="deployment_rollout_quarantined" if quarantine else "deployment_rollout_rolled_back",
        actor=actor,
        reason=reason,
        reference=reference,
        details=details,
    )


def quarantine_analysis_if_needed(
    synthesis: dict[str, Any],
    *,
    base_dir: Path,
    effective_gate_mode: str,
    set_mode: Callable[..., dict[str, Any]] = _set_execution_plan_mode,
) -> dict[str, Any]:
    """Fail closed only for risk-increasing actions when enforce invariants fail."""
    validation = validate_analysis(synthesis)
    synthesis["deployment_rollout_validation"] = validation
    if validation["status"] == "ok" or str(effective_gate_mode) != "enforce":
        return validation
    for action in synthesis.get("priority_actions") or []:
        if not isinstance(action, dict) or action.get("execution_readiness") != "ready":
            continue
        if str(action.get("type") or "").strip().lower() not in _RISK_INCREASING_TYPES:
            continue
        action["execution_readiness"] = "review"
        reasons = action.setdefault("execution_block_reasons", [])
        if not isinstance(reasons, list):
            reasons = []
            action["execution_block_reasons"] = reasons
        reasons.append({
            "code": "deployment_rollout_invariant_quarantine",
            "message": "enforce後の配備不変条件に失敗したためobserveへ自動ロールバックしました",
        })
    try:
        state = rollback(
            base_dir=base_dir,
            actor="analysis-runtime",
            reason="post-allocation deployment invariant failure",
            reference=str(synthesis.get("analysis_id") or synthesis.get("as_of") or "analysis-unknown"),
            quarantine=True,
            set_mode=set_mode,
            details={"validation": validation},
        )
        validation["automatic_rollback"] = {
            "completed": True,
            "mode": state.get("mode"),
        }
    except Exception as exc:
        validation["automatic_rollback"] = {
            "completed": False,
            "error": type(exc).__name__,
        }
    return validation


def _load_analysis(path: Path) -> dict[str, Any]:
    value = _read_json(path)
    synthesis = value.get("synthesis") if isinstance(value.get("synthesis"), dict) else value
    if not isinstance(synthesis, dict):
        return {}
    # Production analysis artifacts keep the timestamp at the document root,
    # while the rollout validator consumes the synthesis payload.  Preserve
    # that root metadata in the audit record without mutating the artifact.
    loaded = dict(synthesis)
    if not loaded.get("as_of") and value.get("as_of"):
        loaded["as_of"] = value.get("as_of")
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ALMANAC deployment rollout control")
    parser.add_argument("--base-dir", type=Path, default=Path(__file__).parent)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    replay = sub.add_parser("record-replay")
    replay.add_argument("--analysis", type=Path, required=True)
    replay.add_argument("--cycle-id", required=True)
    replay.add_argument("--simulated-as-of", required=True)
    replay.add_argument("--scenario", required=True)
    replay.add_argument("--actor", required=True)
    replay.add_argument("--reference", required=True)
    canary = sub.add_parser("record-canary")
    canary.add_argument("--analysis", type=Path, required=True)
    canary.add_argument("--actor", required=True)
    canary.add_argument("--reference", required=True)
    promote_parser = sub.add_parser("promote")
    promote_parser.add_argument("--actor", required=True)
    promote_parser.add_argument("--reference", required=True)
    rollback_parser = sub.add_parser("rollback")
    rollback_parser.add_argument("--actor", required=True)
    rollback_parser.add_argument("--reason", required=True)
    rollback_parser.add_argument("--reference", required=True)
    args = parser.parse_args(argv)
    if args.command == "status":
        output = load_state(base_dir=args.base_dir)
    elif args.command == "record-replay":
        output = record_replay_cycle(
            base_dir=args.base_dir,
            synthesis=_load_analysis(args.analysis),
            cycle_id=args.cycle_id,
            simulated_as_of=args.simulated_as_of,
            scenario=args.scenario,
            actor=args.actor,
            reference=args.reference,
        )
    elif args.command == "record-canary":
        output = record_production_canary(
            base_dir=args.base_dir,
            synthesis=_load_analysis(args.analysis),
            actor=args.actor,
            reference=args.reference,
        )
    elif args.command == "promote":
        output = promote(base_dir=args.base_dir, actor=args.actor, reference=args.reference)
    else:
        output = rollback(
            base_dir=args.base_dir,
            actor=args.actor,
            reason=args.reason,
            reference=args.reference,
            quarantine=False,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
