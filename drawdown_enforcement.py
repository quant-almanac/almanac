"""Manual promotion and daily advancement of the DD enforcement state.

Promotion is deliberately an operator action.  The shadow series may be
observed for months without becoming an order gate; this module refuses to
enable it until the v7 evidence conditions and a reconciliation reference are
both present.
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from drawdown_state_machine import advance_state, enforcement_eligibility, initial_state
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
    state.update({
        "enforcement_enabled": True,
        "promoted_at": datetime.now(timezone.utc).isoformat(),
        "manual_reconciliation_reference": reference,
        "promotion_eligibility": eligibility,
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
    args = parser.parse_args(argv)
    base_dir = Path(args.base_dir)
    if args.command == "status":
        output = status(base_dir=base_dir)
    elif args.command == "promote":
        output = promote(base_dir=base_dir, manual_reconciliation_reference=args.manual_reconciliation_reference)
    else:
        output = advance_enforced_state(
            base_dir=base_dir,
            freeze_release_approved=True,
            approval_actor=args.actor,
            approval_reason=args.reason,
            approval_reference=args.reference,
        )
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
