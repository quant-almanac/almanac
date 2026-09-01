#!/usr/bin/env python3
"""Verify the scheduled formal-analysis -> default-Agent canary without writes.

The verifier has three outcomes:

* ``pass``: today's formal analysis and exactly one successful default Agent run
  satisfy the projection, scope, tool-use, and accounting contracts.
* ``pending``: the requested formal analysis or Agent run has not happened yet.
* ``fail``: a run happened, but one or more contracts are broken.

It never invokes an LLM and never writes portfolio state.  The JSON report can
be redirected to an operator-controlled log if a durable record is desired.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from agent_projection import (  # noqa: E402
    AGENT_MAX_TURNS,
    SCHEMA_VERSION,
    build_agent_projection,
    projection_sha256,
    resolve_agent_model,
    validate_agent_output,
)

JST = ZoneInfo("Asia/Tokyo")
DEFAULT_COST_CAP_USD = 0.50


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    # Runtime artifacts historically use naive timestamps for JST cron jobs.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=JST)
    return parsed.astimezone(timezone.utc)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _agent_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and row.get("role") == "agent_sdk_run":
            rows.append(row)
    return rows


def _raw_output(briefing: dict) -> dict:
    actions = []
    for row in briefing.get("actions", []):
        if not isinstance(row, dict):
            actions.append(row)
            continue
        actions.append({
            key: row.get(key)
            for key in ("rank", "candidate_id", "action_type", "actionability", "reason")
        })
    return {
        "headline": briefing.get("headline"),
        "overall_stance": briefing.get("overall_stance"),
        "actions": actions,
        "risk_warnings": briefing.get("risk_warnings"),
    }


def verify_canary(
    *,
    base_dir: Path,
    since: datetime,
    cost_cap_usd: float = DEFAULT_COST_CAP_USD,
) -> dict:
    """Return a read-only canary report for one scheduled analysis cycle."""
    base_dir = Path(base_dir)
    since_utc = since.astimezone(timezone.utc)
    checks: list[dict] = []

    def check(name: str, ok: bool, detail: object) -> None:
        checks.append({"name": name, "ok": bool(ok), "detail": detail})

    analysis = _read_json(base_dir / "ai_portfolio_analysis.json")
    analysis_at = _parse_time(analysis.get("as_of") if isinstance(analysis, dict) else None)
    if analysis_at is None or analysis_at < since_utc:
        return {
            "status": "pending",
            "reason": "formal_analysis_not_yet_available",
            "since": since_utc.isoformat(),
            "analysis_as_of": analysis_at.isoformat() if analysis_at else None,
            "checks": checks,
        }
    check("formal_analysis_current", True, analysis_at.isoformat())

    briefing = _read_json(base_dir / "agent_briefing.json")
    briefing_at = _parse_time(briefing.get("as_of") if isinstance(briefing, dict) else None)
    if briefing_at is None or briefing_at < analysis_at:
        return {
            "status": "pending",
            "reason": "default_agent_not_yet_available",
            "since": since_utc.isoformat(),
            "analysis_as_of": analysis_at.isoformat(),
            "briefing_as_of": briefing_at.isoformat() if briefing_at else None,
            "checks": checks,
        }

    check("briefing_mode", briefing.get("mode") == "default", briefing.get("mode"))
    check("briefing_schema", briefing.get("schema_version") == SCHEMA_VERSION,
          briefing.get("schema_version"))
    check("commentary_non_actionable",
          briefing.get("commentary_is_non_actionable") is True,
          briefing.get("commentary_is_non_actionable"))

    rows = [
        row for row in _agent_rows(base_dir / "logs" / "llm_calls.jsonl")
        if row.get("mode") == "default"
        and (_parse_time(row.get("ts")) or datetime.min.replace(tzinfo=timezone.utc))
        >= analysis_at
    ]
    if not rows:
        return {
            "status": "pending",
            "reason": "default_agent_accounting_not_yet_available",
            "since": since_utc.isoformat(),
            "analysis_as_of": analysis_at.isoformat(),
            "briefing_as_of": briefing_at.isoformat(),
            "checks": checks,
        }
    check("exactly_one_agent_run", len(rows) == 1, len(rows))
    row = rows[-1]
    check("agent_status", row.get("status") == "success", row.get("status"))
    check("agent_model", row.get("model") == resolve_agent_model(), row.get("model"))
    check("max_turns", row.get("max_turns") == AGENT_MAX_TURNS, row.get("max_turns"))
    check("structured_output_transport",
          row.get("structured_output_transport_seen") is True,
          row.get("structured_output_transport_seen"))
    check("forbidden_tool_use",
          row.get("forbidden_tool_use_seen") is False,
          row.get("forbidden_tool_use_seen"))
    check("use_tool", row.get("use_tool") is False, row.get("use_tool"))
    cost = row.get("cost_usd")
    cost_ok = (
        isinstance(cost, (int, float))
        and not isinstance(cost, bool)
        and math.isfinite(float(cost))
        and 0.0 < float(cost) <= cost_cap_usd
    )
    check("cost_within_cap", cost_ok, cost)

    evaluation_at = _parse_time(briefing.get("evaluation_as_of"))
    if evaluation_at is None:
        check("projection_rebuild", False, "missing evaluation_as_of")
    else:
        try:
            projection = build_agent_projection(
                "default", base_dir=base_dir, now=evaluation_at)
            digest_ok = projection_sha256(projection) == briefing.get("projection_sha256")
            check("projection_hash", digest_ok, briefing.get("projection_sha256"))
            validate_agent_output(_raw_output(briefing), projection)
            check("action_scope", True, len(briefing.get("actions", [])))
        except Exception as exc:
            check("projection_rebuild", False, f"{type(exc).__name__}: {exc}")

    failed = [item for item in checks if not item["ok"]]
    return {
        "status": "pass" if not failed else "fail",
        "since": since_utc.isoformat(),
        "analysis_as_of": analysis_at.isoformat(),
        "briefing_as_of": briefing_at.isoformat(),
        "accounting_rows": len(rows),
        "checks": checks,
    }


def _default_since() -> datetime:
    return datetime.combine(datetime.now(JST).date(), time.min, tzinfo=JST)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-dir", type=Path, default=_ROOT)
    parser.add_argument(
        "--since",
        help="ISO timestamp; naive values are interpreted as JST (default: today 00:00 JST)",
    )
    parser.add_argument("--cost-cap-usd", type=float, default=DEFAULT_COST_CAP_USD)
    args = parser.parse_args()
    since = _parse_time(args.since) if args.since else _default_since()
    if since is None:
        parser.error("--since must be a valid ISO timestamp")
    report = verify_canary(
        base_dir=args.base_dir,
        since=since,
        cost_cap_usd=args.cost_cap_usd,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return {"pass": 0, "pending": 2, "fail": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
