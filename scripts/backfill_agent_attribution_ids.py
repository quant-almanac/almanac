#!/usr/bin/env python3
"""Backfill Opus-final attribution IDs to the legacy hypothesis namespace.

The runtime writer previously hashed final actions with a local
``final_synthesis:*`` namespace while ``candidate_extractor.extract_from_synthesis``
and the outcome pipeline used ``legacy_producer:opus_final``.  This script
repairs existing ``agent_attribution_log.jsonl`` rows by matching them to
Opus-final legacy hypothesis rows on ``(analysis_date, ticker)``.

It is conservative by design:

- default mode is dry-run;
- ``--apply`` creates a timestamped backup before replacing the attribution log;
- duplicate hypothesis rows with the same ID are deduped;
- genuinely ambiguous matches with multiple unique IDs are skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BackfillSummary:
    total_rows: int
    target_rows: int
    converted_rows: int
    already_canonical_rows: int
    missing_match_rows: int
    ambiguous_match_rows: int
    parse_error_rows: int
    old_outcome_matches: int
    new_outcome_matches: int
    output_path: str | None
    backup_path: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "total_rows": self.total_rows,
            "target_rows": self.target_rows,
            "converted_rows": self.converted_rows,
            "already_canonical_rows": self.already_canonical_rows,
            "missing_match_rows": self.missing_match_rows,
            "ambiguous_match_rows": self.ambiguous_match_rows,
            "parse_error_rows": self.parse_error_rows,
            "old_outcome_matches": self.old_outcome_matches,
            "new_outcome_matches": self.new_outcome_matches,
            "output_path": self.output_path,
            "backup_path": self.backup_path,
        }


def _read_jsonl_preserving_raw(path: Path) -> tuple[list[tuple[str, dict[str, Any] | None]], int]:
    if not path.exists():
        return [], 0
    rows: list[tuple[str, dict[str, Any] | None]] = []
    parse_errors = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            rows.append((raw, None))
            parse_errors += 1
            continue
        rows.append((raw, parsed if isinstance(parsed, dict) else None))
        if not isinstance(parsed, dict):
            parse_errors += 1
    return rows, parse_errors


def _is_opus_final_attr(row: dict[str, Any]) -> bool:
    return (
        row.get("agent") == "opus_final"
        and row.get("role") == "final_decider"
        and row.get("stance") == "support"
        and row.get("final_candidate_status") == "adopted"
        and bool(row.get("analysis_date"))
        and bool(row.get("ticker"))
    )


def _is_opus_final_hypothesis(row: dict[str, Any]) -> bool:
    source_agents = row.get("source_agents")
    if isinstance(source_agents, list) and "opus_final" in source_agents:
        return row.get("hypothesis_type") == "legacy"
    return (
        row.get("hypothesis_type") == "legacy"
        and row.get("primary_source_agent") == "legacy_producer:opus_final"
    )


def _build_hypothesis_index(path: Path) -> dict[tuple[str, str], dict[str, dict[str, Any]]]:
    rows, _ = _read_jsonl_preserving_raw(path)
    index: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for _, row in rows:
        if not row or not _is_opus_final_hypothesis(row):
            continue
        hid = row.get("hypothesis_id")
        analysis_date = row.get("analysis_date")
        ticker = row.get("primary_ticker") or row.get("ticker")
        if not hid or not analysis_date or not ticker:
            continue
        index[(str(analysis_date), str(ticker))][str(hid)] = row
    return dict(index)


def _load_outcome_ids(path: Path, *, horizon_days: int) -> set[str]:
    rows, _ = _read_jsonl_preserving_raw(path)
    ids: set[str] = set()
    for _, row in rows:
        if not row:
            continue
        if row.get("horizon_days") != horizon_days:
            continue
        hid = row.get("hypothesis_id")
        if hid:
            ids.add(str(hid))
    return ids


def _atomic_write_lines(path: Path, lines: list[str]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        if lines:
            fh.write("\n".join(lines) + "\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def backfill(
    *,
    attribution_log_path: Path,
    hypothesis_log_path: Path,
    outcome_log_path: Path | None = None,
    horizon_days: int = 10,
    apply: bool = False,
) -> BackfillSummary:
    rows, parse_errors = _read_jsonl_preserving_raw(attribution_log_path)
    index = _build_hypothesis_index(hypothesis_log_path)
    outcome_ids = _load_outcome_ids(outcome_log_path, horizon_days=horizon_days) if outcome_log_path else set()

    target_rows = 0
    converted_rows = 0
    already_canonical_rows = 0
    missing_match_rows = 0
    ambiguous_match_rows = 0
    old_outcome_matches = 0
    new_outcome_matches = 0
    output_lines: list[str] = []

    for raw, row in rows:
        if not row or not _is_opus_final_attr(row):
            output_lines.append(raw)
            continue

        target_rows += 1
        old_hid = str(row.get("hypothesis_id") or "")
        if old_hid in outcome_ids:
            old_outcome_matches += 1

        matches = index.get((str(row.get("analysis_date")), str(row.get("ticker"))), {})
        if not matches:
            missing_match_rows += 1
            output_lines.append(raw)
            continue
        if len(matches) > 1:
            ambiguous_match_rows += 1
            output_lines.append(raw)
            continue

        new_hid, hypothesis = next(iter(matches.items()))
        if new_hid in outcome_ids:
            new_outcome_matches += 1
        if old_hid == new_hid:
            already_canonical_rows += 1
            output_lines.append(raw)
            continue

        updated = dict(row)
        updated["hypothesis_id"] = new_hid
        updated["hypothesis_type"] = hypothesis.get("hypothesis_type") or updated.get("hypothesis_type")
        if hypothesis.get("horizon_days") is not None:
            updated["time_horizon_days"] = hypothesis.get("horizon_days")
        converted_rows += 1
        output_lines.append(json.dumps(updated, ensure_ascii=False, separators=(",", ":")))

    backup_path: str | None = None
    output_path: str | None = None
    if apply and converted_rows:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = attribution_log_path.with_name(
            f"{attribution_log_path.name}.pre_backfill_{stamp}.bak"
        )
        shutil.copy2(attribution_log_path, backup)
        _atomic_write_lines(attribution_log_path, output_lines)
        backup_path = str(backup)
        output_path = str(attribution_log_path)

    return BackfillSummary(
        total_rows=len(rows),
        target_rows=target_rows,
        converted_rows=converted_rows,
        already_canonical_rows=already_canonical_rows,
        missing_match_rows=missing_match_rows,
        ambiguous_match_rows=ambiguous_match_rows,
        parse_error_rows=parse_errors,
        old_outcome_matches=old_outcome_matches,
        new_outcome_matches=new_outcome_matches,
        output_path=output_path,
        backup_path=backup_path,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--attribution-log-path", type=Path)
    parser.add_argument("--hypothesis-log-path", type=Path)
    parser.add_argument("--outcome-log-path", type=Path)
    parser.add_argument("--horizon-days", type=int, default=10)
    parser.add_argument("--apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = args.root
    summary = backfill(
        attribution_log_path=args.attribution_log_path or root / "agent_attribution_log.jsonl",
        hypothesis_log_path=args.hypothesis_log_path or root / "catalyst_hypothesis_log.jsonl",
        outcome_log_path=args.outcome_log_path or root / "catalyst_outcome_log.jsonl",
        horizon_days=args.horizon_days,
        apply=args.apply,
    )
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    if not args.apply and summary.converted_rows:
        print("dry-run only; rerun with --apply to replace the attribution log after backup")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
