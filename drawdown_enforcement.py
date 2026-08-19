"""Manual promotion and daily advancement of the DD enforcement state.

Promotion is deliberately an operator action.  The shadow series may be
observed for months without becoming an order gate; this module refuses to
enable it until the v7 evidence conditions and a reconciliation reference are
both present.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drawdown_state_machine import advance_state, enforcement_eligibility, initial_state
from almanac.runtime_config import resolve_db_path
from utils import atomic_write_json


def _paths(base_dir: Path) -> tuple[Path, Path]:
    return base_dir / "flow_adjusted_dd_shadow.json", base_dir / "drawdown_state.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def status(*, base_dir: Path) -> dict[str, Any]:
    shadow_path, state_path = _paths(base_dir)
    shadow = _read_json(shadow_path)
    state = _read_json(state_path)
    return {
        "shadow": shadow,
        "state": state or initial_state(),
        "eligibility": enforcement_eligibility(
            shadow,
            manual_reconciliation_recorded=bool(state.get("manual_reconciliation_reference")),
        ),
    }


def promote(*, base_dir: Path, manual_reconciliation_reference: str) -> dict[str, Any]:
    """Enable the DD state machine after explicit evidence reconciliation."""
    reference = str(manual_reconciliation_reference or "").strip()
    if not reference:
        raise ValueError("manual_reconciliation_reference is required")
    shadow_path, state_path = _paths(base_dir)
    shadow = _read_json(shadow_path)
    # The required reference is the operator's reconciliation record.  The
    # shadow artifact stays ``manual_reconciliation_required=true`` forever so
    # each new deployment cannot silently self-promote; requiring that flag to
    # be edited to false would make this CLI impossible to use in live flow.
    eligibility = enforcement_eligibility(
        shadow, manual_reconciliation_recorded=True,
    )
    if not eligibility["eligible"]:
        raise ValueError("DD enforcement promotion is not eligible: " + ",".join(eligibility["reasons"]))
    dd = shadow.get("flow_adjusted_current_dd_decimal")
    end_date = shadow.get("end_date")
    if dd is None or not end_date:
        raise ValueError("shadow has no usable current drawdown/effective date")
    state = advance_state(
        initial_state(), drawdown_decimal=float(dd), effective_nav_date=str(end_date),
    )
    evidence = {
        "controller_version": "flow_adjusted_dd_v1",
        "policy_version": "2026-08-v2",
        "manual_reconciliation_reference": reference,
        "eligibility": eligibility,
        "initial_dd_state": state.get("dd_state"),
        "initial_drawdown_decimal": float(dd),
        "effective_nav_date": str(end_date),
    }
    evidence_hash = hashlib.sha256(json.dumps(evidence, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    from event_ledger import append_event
    event = append_event(
        event_type="drawdown_controller_promoted", occurred_at=datetime.now(timezone.utc).isoformat(),
        source="manual", note="flow-adjusted DD controller promotion",
        raw_payload={**evidence, "eligibility_snapshot_hash": evidence_hash},
        event_id=f"drawdown-controller-promotion:{evidence_hash}", db_path=resolve_db_path(base_dir),
    )
    state.update({
        "enforcement_enabled": True,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "manual_reconciliation_reference": reference,
        "promotion_eligibility": eligibility,
        "promotion_event_id": event["event_id"],
        "promotion_eligibility_snapshot_hash": evidence_hash,
    })
    atomic_write_json(state_path, state)
    return state


def advance_enforced_state(
    *,
    base_dir: Path,
    freeze_release_approved: bool = False,
    approval_actor: str | None = None,
    approval_reason: str | None = None,
    approval_reference: str | None = None,
) -> dict[str, Any] | None:
    """Advance one daily state observation only after explicit promotion."""
    shadow_path, state_path = _paths(base_dir)
    state = _read_json(state_path)
    if state.get("enforcement_enabled") is not True:
        return None
    shadow = _read_json(shadow_path)
    dd = shadow.get("flow_adjusted_current_dd_decimal")
    end_date = shadow.get("end_date")
    if dd is None or not end_date:
        return None
    approval = None
    if freeze_release_approved:
        actor = str(approval_actor or "").strip()
        reason = str(approval_reason or "").strip()
        reference = str(approval_reference or "").strip()
        if not actor or not reason or not reference:
            raise ValueError("freeze release requires approval actor, reason, and reference")
        if str(state.get("dd_state") or "") != "freeze":
            raise ValueError("freeze release approval is valid only while the current state is freeze")
        approval = {
            "at": datetime.now(timezone.utc).isoformat(),
            "actor": actor,
            "reason": reason,
            "reference": reference,
        }
    next_state = advance_state(
        state, drawdown_decimal=float(dd), effective_nav_date=str(end_date),
        freeze_release_approved=freeze_release_approved,
    )
    if approval is not None:
        if str(next_state.get("dd_state") or "") == "freeze":
            raise ValueError("freeze recovery conditions are not yet satisfied")
        approvals = list(next_state.get("freeze_release_approvals") or [])[-99:]
        approvals.append(approval)
        next_state["freeze_release_approvals"] = approvals
        next_state["freeze_release_approval"] = approval
    atomic_write_json(state_path, next_state)
    return next_state


def recover(
    *,
    base_dir: Path,
    approval_actor: str,
    approval_reason: str,
    approval_reference: str,
) -> dict[str, Any]:
    """Rebuild a missing/corrupt promoted controller from immutable history."""
    actor = str(approval_actor or "").strip()
    reason = str(approval_reason or "").strip()
    reference = str(approval_reference or "").strip()
    if not actor or not reason or not reference:
        raise ValueError("recovery requires approval actor, reason, and reference")
    _, state_path = _paths(base_dir)
    current = _read_json(state_path)
    if current.get("enforcement_enabled") is True:
        raise ValueError("drawdown controller is already enabled; recovery is not applicable")

    db_path = resolve_db_path(base_dir)
    from event_ledger import append_event, query_events

    promotions = query_events(types=["drawdown_controller_promoted"], db_path=db_path)
    if not promotions:
        raise ValueError("no append-only drawdown promotion event exists")
    promotion = promotions[-1]
    raw_evidence = promotion.get("raw_payload")
    if isinstance(raw_evidence, str):
        try:
            raw_evidence = json.loads(raw_evidence)
        except json.JSONDecodeError:
            raw_evidence = None
    evidence = raw_evidence if isinstance(raw_evidence, dict) else {}
    stored_promotion_hash = str(evidence.get("eligibility_snapshot_hash") or "")
    hash_basis = {
        key: value for key, value in evidence.items()
        if key != "eligibility_snapshot_hash"
    }
    calculated_promotion_hash = hashlib.sha256(
        json.dumps(hash_basis, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    if not stored_promotion_hash or stored_promotion_hash != calculated_promotion_hash:
        raise ValueError("promotion evidence hash mismatch")
    promotion_date = str(evidence.get("effective_nav_date") or "")[:10]
    try:
        promotion_dd = float(evidence.get("initial_drawdown_decimal"))
    except (TypeError, ValueError) as exc:
        raise ValueError("promotion evidence has no valid initial drawdown") from exc
    if not promotion_date:
        raise ValueError("promotion evidence has no effective NAV date")

    from nav_recorder import replay_flow_adjusted_drawdown_points

    replay = replay_flow_adjusted_drawdown_points(db_path=db_path)
    if replay.get("status") != "ok":
        raise ValueError(f"drawdown replay is not recoverable: {replay.get('status')}")
    if float(replay.get("flow_coverage") or 0.0) < 0.95 or replay.get("invalid_days"):
        raise ValueError("drawdown replay failed coverage or validity requirements")
    points = [row for row in replay.get("points") or [] if isinstance(row, dict)]
    replay_dates = {str(point.get("effective_nav_date") or "")[:10] for point in points}
    if not points or promotion_date not in replay_dates:
        raise ValueError("replay series does not cover the promotion effective date")

    rebuilt = advance_state(
        initial_state(),
        drawdown_decimal=promotion_dd,
        effective_nav_date=promotion_date,
    )
    for point in points:
        day = str(point.get("effective_nav_date") or "")[:10]
        if not day or day <= promotion_date:
            continue
        try:
            drawdown = float(point.get("drawdown_decimal"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid replay drawdown at {day}") from exc
        rebuilt = advance_state(
            rebuilt,
            drawdown_decimal=drawdown,
            effective_nav_date=day,
        )

    replay_evidence = {
        "promotion_event_id": promotion.get("event_id"),
        "promotion_effective_nav_date": promotion_date,
        "promotion_initial_drawdown_decimal": promotion_dd,
        "replay_start_date": replay.get("start_date"),
        "replay_end_date": replay.get("end_date"),
        "replay_effective_nav_days": replay.get("effective_nav_days"),
        "replay_flow_coverage": replay.get("flow_coverage"),
        "replay_current_drawdown_decimal": replay.get("current_drawdown_decimal"),
        "recovered_dd_state": rebuilt.get("dd_state"),
        "approval_actor": actor,
        "approval_reason": reason,
        "approval_reference": reference,
    }
    replay_hash = hashlib.sha256(
        json.dumps(
            {"evidence": replay_evidence, "points": points},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    event = append_event(
        event_type="drawdown_controller_recovered",
        occurred_at=datetime.now(timezone.utc).isoformat(),
        source="manual",
        note="flow-adjusted DD controller recovery",
        raw_payload={**replay_evidence, "replay_snapshot_hash": replay_hash},
        event_id=f"drawdown-controller-recovery:{replay_hash}",
        db_path=db_path,
    )
    rebuilt.update({
        "enforcement_enabled": True,
        "promoted_at": promotion.get("occurred_at"),
        "manual_reconciliation_reference": evidence.get("manual_reconciliation_reference"),
        "promotion_event_id": promotion.get("event_id"),
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "recovery_event_id": event.get("event_id"),
        "recovery_approval": {
            "actor": actor,
            "reason": reason,
            "reference": reference,
        },
        "replay_snapshot_hash": replay_hash,
    })
    # As with promotion, the append-only proof is committed before mutable
    # state.  A failed JSON write therefore remains a visible controller fault.
    atomic_write_json(state_path, rebuilt)
    return rebuilt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ALMANAC flow-adjusted DD enforcement")
    parser.add_argument("--base-dir", default=str(Path(__file__).parent))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    promote_parser = sub.add_parser("promote")
    promote_parser.add_argument("--manual-reconciliation-reference", required=True)
    release_parser = sub.add_parser("approve-freeze-release")
    release_parser.add_argument("--actor", required=True)
    release_parser.add_argument("--reason", required=True)
    release_parser.add_argument("--reference", required=True)
    recover_parser = sub.add_parser("recover")
    recover_parser.add_argument("--actor", required=True)
    recover_parser.add_argument("--reason", required=True)
    recover_parser.add_argument("--reference", required=True)
    args = parser.parse_args(argv)
    base_dir = Path(args.base_dir)
    if args.command == "status":
        output = status(base_dir=base_dir)
    elif args.command == "promote":
        output = promote(base_dir=base_dir, manual_reconciliation_reference=args.manual_reconciliation_reference)
    elif args.command == "approve-freeze-release":
        output = advance_enforced_state(
            base_dir=base_dir,
            freeze_release_approved=True,
            approval_actor=args.actor,
            approval_reason=args.reason,
            approval_reference=args.reference,
        )
    else:
        output = recover(
            base_dir=base_dir,
            approval_actor=args.actor,
            approval_reason=args.reason,
            approval_reference=args.reference,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
