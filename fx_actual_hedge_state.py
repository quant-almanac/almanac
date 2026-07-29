"""Create broker-evidenced FX overlay state for Stage 7B.

An absent hedge position is not the same as a broker-confirmed zero.  This
module writes ``fx_actual_hedge_state.json`` only when every required broker
has supplied a complete position snapshot. Broker balances use event-based
validity: elapsed time alone does not invalidate them. Embedded currency-hedged
ETFs are deliberately excluded: they are represented by ``fx_exposure`` as a
property of the asset, while this state contains only overlay instruments such
as FX forwards and currency futures.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo

from execution_safety import canonical_broker
from utils import atomic_write_json

BASE_DIR = Path(__file__).parent
JST = ZoneInfo("Asia/Tokyo")
OVERLAY_VEHICLE_TYPES = frozenset({"fx_forward", "fx_spot", "currency_future"})


def _state_dir() -> Path:
    return Path(os.environ.get("ALMANAC_STATE_DIR") or BASE_DIR)


def _parse_time(value: object) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=JST) if parsed.tzinfo is None else parsed.astimezone(JST)


def _snapshot_digest(snapshot: dict) -> str:
    payload = json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_actual_hedge_state(
    snapshots: Iterable[dict],
    *,
    required_brokers: Iterable[str],
    now: Optional[datetime] = None,
    max_age_hours: Optional[float] = None,
) -> dict:
    """Validate complete broker snapshots and calculate actual overlay notional.

    ``max_age_hours`` is retained only for call compatibility and deliberately
    ignored. A confirmed hedge balance is invalidated by a later execution
    event, not by the wall clock.
    """
    now = (now or datetime.now(JST)).astimezone(JST)
    required = {canonical_broker(item) for item in required_brokers if canonical_broker(item)}
    issues: list[str] = []
    if not required:
        issues.append("required_brokers_empty")
    by_broker: dict[str, dict] = {}
    for raw in snapshots:
        if not isinstance(raw, dict):
            continue
        broker = canonical_broker(raw.get("broker"))
        if not broker:
            issues.append("snapshot_broker_missing")
            continue
        if broker in by_broker:
            issues.append(f"duplicate_broker_snapshot:{broker}")
            continue
        by_broker[broker] = raw
    issues.extend(
        f"broker_snapshot_missing:{broker}"
        for broker in sorted(required - set(by_broker))
    )
    total_notional = 0.0
    evidence = []
    for broker in sorted(required & set(by_broker)):
        snapshot = by_broker[broker]
        if snapshot.get("complete") is not True:
            issues.append(f"broker_snapshot_incomplete:{broker}")
        source_as_of = _parse_time(snapshot.get("source_as_of"))
        if source_as_of is None:
            issues.append(f"broker_snapshot_source_as_of_invalid:{broker}")
        digest = str(snapshot.get("reconciliation_snapshot_hash") or "")
        if not digest:
            issues.append(f"broker_snapshot_hash_missing:{broker}")
        positions = snapshot.get("positions")
        if not isinstance(positions, list):
            issues.append(f"broker_snapshot_positions_invalid:{broker}")
            positions = []
        broker_notional = 0.0
        for index, position in enumerate(positions):
            if not isinstance(position, dict):
                issues.append(f"broker_snapshot_position_invalid:{broker}:{index}")
                continue
            vehicle = str(position.get("vehicle_type") or "").lower()
            if vehicle not in OVERLAY_VEHICLE_TYPES:
                continue
            try:
                notional = float(position["hedge_notional_jpy"])
            except (KeyError, TypeError, ValueError):
                issues.append(f"overlay_notional_missing:{broker}:{index}")
                continue
            if notional < 0:
                issues.append(f"overlay_notional_negative:{broker}:{index}")
                continue
            broker_notional += notional
        total_notional += broker_notional
        evidence.append({
            "broker": broker,
            "source_as_of": source_as_of.isoformat() if source_as_of else None,
            "reconciliation_snapshot_hash": digest or None,
            "overlay_notional_jpy": round(broker_notional),
        })
    if issues:
        return {
            "schema_version": 1, "status": "review", "written": False,
            "observed_actual_hedge_notional_jpy": None,
            "issues": sorted(set(issues)), "broker_evidence": evidence,
        }
    combined_hash = _snapshot_digest({
        "required_brokers": sorted(required), "broker_evidence": evidence,
    })
    source_as_of = min(
        datetime.fromisoformat(item["source_as_of"])
        for item in evidence if item.get("source_as_of")
    )
    return {
        "schema_version": 1, "status": "eligible", "written": False,
        "observed_actual_hedge_notional_jpy": round(total_notional),
        "broker_source": "complete_position_snapshots:" + ",".join(sorted(required)),
        "source_as_of": source_as_of.isoformat(),
        "reconciliation_snapshot_hash": combined_hash,
        "broker_evidence": evidence,
        "generated_at": now.isoformat(),
        "validation_mode": "event_based",
        "assumption": "no_unreported_external_activity",
        "issues": [],
    }


def write_actual_hedge_state(result: dict, *, path: Optional[Path] = None) -> dict:
    if result.get("status") != "eligible":
        return result
    destination = path or (_state_dir() / "fx_actual_hedge_state.json")
    output = {**result, "written": True}
    atomic_write_json(destination, output)
    return output


def _main() -> None:
    parser = argparse.ArgumentParser(
        description="Build FX actual overlay state from complete broker snapshots"
    )
    parser.add_argument("--snapshot", action="append", required=True)
    parser.add_argument("--required-brokers", required=True, help="comma-separated")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    snapshots = [json.loads(Path(item).read_text(encoding="utf-8")) for item in args.snapshot]
    result = build_actual_hedge_state(
        snapshots, required_brokers=args.required_brokers.split(",")
    )
    if args.write:
        result = write_actual_hedge_state(
            result, path=Path(args.output) if args.output else None
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") != "eligible":
        raise SystemExit(2)


if __name__ == "__main__":
    _main()
