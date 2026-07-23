"""Monthly sampler for manual audits of disclosure-feature extraction quality.

This tool deliberately separates extraction correctness from predictive power:
it samples already-stored disclosure feature rows and prepares review items that
can be judged through ``human_feedback_log.py``.  It does not call any model and
does not mutate the feature store.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any, Iterable

import human_feedback_log
from almanac.observability.disclosure_features import (
    AI_CONTEXT_FEATURES,
    DETERMINISTIC_FEATURES,
    MVP_CORE_FEATURES,
    default_store_path,
    read_features,
)

SUBJECT_TYPE = "extraction_audit"
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
_EXCLUDED_REVIEW_FEATURES = frozenset({"placebo_hash_score"})
_REVIEW_FEATURES = tuple(
    name
    for name in (*MVP_CORE_FEATURES, *AI_CONTEXT_FEATURES, *DETERMINISTIC_FEATURES)
    if name not in _EXCLUDED_REVIEW_FEATURES
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _hash_token(*parts: Any, length: int = 20) -> str:
    payload = "|".join(str(part) for part in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def _validate_month(month: str) -> str:
    month = str(month or "").strip()
    if not _MONTH_RE.match(month):
        raise ValueError("month must be YYYY-MM")
    return month


def audit_subject_id(row: dict[str, Any]) -> str:
    """Stable source identity for a feature row.

    ``source_event_id`` is preferred because multiple prompt/model revisions can
    read the same disclosure.  ``feature_id`` is the next best stable key.  A
    content hash is the last-resort fallback for legacy rows.
    """
    for key in ("source_event_id", "feature_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return "row:" + _hash_token(_stable_json(row))


def _audit_id(row: dict[str, Any], month: str) -> str:
    return f"{SUBJECT_TYPE}:{_hash_token(month, audit_subject_id(row))}"


def _row_month(row: dict[str, Any]) -> str | None:
    for key in ("compute_time", "ingest_time", "publish_time"):
        value = str(row.get(key) or "").strip()
        if len(value) >= 7:
            return value[:7]
    return None


def _review_features(row: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name in _REVIEW_FEATURES:
        value = row.get(name)
        if value is None:
            continue
        if isinstance(value, (list, dict)) and not value:
            continue
        out[name] = value
    return out


def build_audit_item(row: dict[str, Any], *, month: str) -> dict[str, Any]:
    """Build one review item from a stored disclosure-feature row."""
    month = _validate_month(month)
    audit_id = _audit_id(row, month)
    source_event_id = row.get("source_event_id")
    ticker = row.get("ticker")
    command = [
        "python",
        "human_feedback_log.py",
        "append",
        "--subject-type",
        SUBJECT_TYPE,
        "--subject-id",
        audit_id,
        "--verdict",
        "<good|bad|mixed|unclear>",
    ]
    if ticker:
        command += ["--ticker", str(ticker)]
    if source_event_id:
        command += ["--source-event-id", str(source_event_id)]
    command += ["--tag", f"extraction_audit:{month}"]

    return {
        "audit_id": audit_id,
        "subject_type": SUBJECT_TYPE,
        "subject_id": audit_id,
        "feedback_subject_id": audit_id,
        "source_subject_id": audit_subject_id(row),
        "month": month,
        "ticker": ticker,
        "source": row.get("source"),
        "disclosure_type": row.get("disclosure_type"),
        "source_event_id": source_event_id,
        "feature_id": row.get("feature_id"),
        "publish_time": row.get("publish_time"),
        "model_id": row.get("model_id"),
        "prompt_version": row.get("prompt_version"),
        "feature_schema_version": row.get("feature_schema_version"),
        "summary": row.get("summary") or "",
        "evidence": list(row.get("evidence") or []),
        "features_to_review": _review_features(row),
        "feedback_command": " ".join(shlex.quote(part) for part in command),
    }


def _reviewed_keys(feedback_rows: Iterable[dict[str, Any]]) -> set[str]:
    keys: set[str] = set()
    for row in feedback_rows:
        if row.get("subject_type") != SUBJECT_TYPE:
            continue
        for key in ("subject_id", "source_event_id", "feature_id"):
            value = str(row.get(key) or "").strip()
            if value:
                keys.add(value)
    return keys


def _selection_key(row: dict[str, Any], *, month: str, seed_salt: str) -> str:
    return _hash_token(month, audit_subject_id(row), seed_salt, length=64)


def sample_extraction_audit(
    features: Iterable[dict[str, Any]],
    *,
    month: str,
    n: int = 20,
    feedback_rows: Iterable[dict[str, Any]] | None = None,
    seed_salt: str = "",
) -> list[dict[str, Any]]:
    """Return a deterministic monthly sample of unrevised extraction rows."""
    month = _validate_month(month)
    if n < 0:
        raise ValueError("n must be non-negative")
    reviewed = _reviewed_keys(feedback_rows or [])
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in features:
        if not isinstance(row, dict):
            continue
        if _row_month(row) != month:
            continue
        subject = audit_subject_id(row)
        audit_id = _audit_id(row, month)
        feature_id = str(row.get("feature_id") or "").strip()
        source_event_id = str(row.get("source_event_id") or "").strip()
        if {subject, audit_id, feature_id, source_event_id} & reviewed:
            continue
        if audit_id in seen:
            continue
        seen.add(audit_id)
        candidates.append(row)

    candidates.sort(
        key=lambda row: (
            _selection_key(row, month=month, seed_salt=seed_salt),
            audit_subject_id(row),
        )
    )
    return [build_audit_item(row, month=month) for row in candidates[:n]]


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _sample_payload(args: argparse.Namespace) -> dict[str, Any]:
    month = _validate_month(args.month)
    if args.n < 0:
        raise ValueError("n must be non-negative")
    feature_path = Path(args.features_path) if args.features_path else default_store_path()
    feedback_path = Path(args.feedback_path) if args.feedback_path else human_feedback_log.DEFAULT_LOG_PATH
    features = read_features(feature_path)
    feedback_rows = human_feedback_log.read_feedback(feedback_path)
    all_unreviewed = sample_extraction_audit(
        features,
        month=month,
        n=len(features),
        feedback_rows=feedback_rows,
        seed_salt=args.seed_salt,
    )
    items = all_unreviewed[: args.n]
    return {
        "month": month,
        "requested_n": args.n,
        "available_unreviewed": len(all_unreviewed),
        "features_path": str(feature_path),
        "feedback_path": str(feedback_path),
        "items": items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample disclosure-feature rows for extraction audit")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sample = sub.add_parser("sample", help="print a deterministic monthly audit sample as JSON")
    sample.add_argument("--month", required=True, help="Month to sample, YYYY-MM")
    sample.add_argument("--n", type=int, default=20)
    sample.add_argument("--features-path", default=str(default_store_path()))
    sample.add_argument("--feedback-path", default=str(human_feedback_log.DEFAULT_LOG_PATH))
    sample.add_argument("--seed-salt", default="")
    sample.add_argument("--output", help="Optional JSON output path")

    args = parser.parse_args(argv)
    if args.cmd == "sample":
        payload = _sample_payload(args)
        if args.output:
            _write_json(Path(args.output), payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
