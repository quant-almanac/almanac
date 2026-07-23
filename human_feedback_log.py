"""Append-only human feedback for AI readings and hypotheses.

The log captures manual judgments such as "this disclosure reading was good" or
"this interpretation was wrong" without mutating prior rows. It is intentionally
small: downstream calibration can join on source_event_id, hypothesis_id,
analysis_id, ticker, or the generic subject_id.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from almanac.observability.append_only_log import append_jsonl_safe

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOG_PATH = BASE_DIR / "human_feedback_log.jsonl"
SCHEMA_VERSION = "1.0"
VALID_VERDICTS = {"good", "bad", "mixed", "unclear"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _feedback_id(row: dict[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, default=str)
    return "human_feedback:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _clean_tags(tags: Iterable[str] | None) -> list[str]:
    if tags is None:
        return []
    out = []
    for tag in tags:
        value = str(tag).strip()
        if value and value not in out:
            out.append(value)
    return out


def record_feedback(
    *,
    subject_type: str,
    subject_id: str,
    verdict: str,
    comment: str = "",
    ticker: str | None = None,
    source_event_id: str | None = None,
    hypothesis_id: str | None = None,
    analysis_id: str | None = None,
    reviewer: str = "user",
    tags: Iterable[str] | None = None,
    occurred_at: str | None = None,
    path: Path | str = DEFAULT_LOG_PATH,
    fsync: bool = True,
) -> dict[str, Any]:
    """Append a single manual feedback row and return the row."""
    subject_type = str(subject_type or "").strip()
    subject_id = str(subject_id or "").strip()
    verdict = str(verdict or "").strip().lower()
    if not subject_type:
        raise ValueError("subject_type is required")
    if not subject_id:
        raise ValueError("subject_id is required")
    if verdict not in VALID_VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VALID_VERDICTS)}")

    row: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "occurred_at": occurred_at or _now_iso(),
        "subject_type": subject_type,
        "subject_id": subject_id,
        "verdict": verdict,
        "comment": str(comment or ""),
        "ticker": ticker,
        "source_event_id": source_event_id,
        "hypothesis_id": hypothesis_id,
        "analysis_id": analysis_id,
        "reviewer": str(reviewer or "user"),
        "tags": _clean_tags(tags),
    }
    row["feedback_id"] = _feedback_id(row)
    append_jsonl_safe(path, row, fsync=fsync)
    return row


def read_feedback(path: Path | str = DEFAULT_LOG_PATH) -> list[dict[str, Any]]:
    p = Path(path)
    if not p.exists():
        return []
    rows: list[dict[str, Any]] = []
    for lineno, line in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError(f"{p}:{lineno}: feedback row must be a JSON object")
        rows.append(row)
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Append/read ALMANAC human feedback JSONL")
    sub = parser.add_subparsers(dest="cmd", required=True)

    append = sub.add_parser("append", help="append a feedback row")
    append.add_argument("--subject-type", required=True)
    append.add_argument("--subject-id", required=True)
    append.add_argument("--verdict", required=True, choices=sorted(VALID_VERDICTS))
    append.add_argument("--comment", default="")
    append.add_argument("--ticker")
    append.add_argument("--source-event-id")
    append.add_argument("--hypothesis-id")
    append.add_argument("--analysis-id")
    append.add_argument("--reviewer", default="user")
    append.add_argument("--tag", action="append", dest="tags")
    append.add_argument("--path", default=str(DEFAULT_LOG_PATH))

    show = sub.add_parser("list", help="print feedback rows as JSON")
    show.add_argument("--path", default=str(DEFAULT_LOG_PATH))

    args = parser.parse_args(argv)
    if args.cmd == "append":
        row = record_feedback(
            subject_type=args.subject_type,
            subject_id=args.subject_id,
            verdict=args.verdict,
            comment=args.comment,
            ticker=args.ticker,
            source_event_id=args.source_event_id,
            hypothesis_id=args.hypothesis_id,
            analysis_id=args.analysis_id,
            reviewer=args.reviewer,
            tags=args.tags,
            path=args.path,
        )
        print(json.dumps(row, ensure_ascii=False, sort_keys=True))
        return 0
    print(json.dumps(read_feedback(args.path), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
