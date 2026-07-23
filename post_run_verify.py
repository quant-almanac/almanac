"""Post-run consistency verifier for ``analyst.run_analysis`` outputs.

This is the runtime counterpart of the E2E post-condition tests: it checks the
files produced by one AI analysis run and reports wiring regressions before the
system quietly acts on stale or internally inconsistent outputs.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from vix_classification import classify_vix

BASE_DIR = Path(__file__).parent


def _load_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _load_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    try:
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return []
    return rows


def _issue(code: str, message: str, severity: str = "warning", **context: Any) -> dict:
    return {
        "code": code,
        "severity": severity,
        "message": message,
        "context": context,
    }


def _analysis_payload(base_dir: Path) -> dict:
    data = _load_json(base_dir / "ai_portfolio_analysis.json", {})
    if not isinstance(data, dict):
        return {}
    synthesis = data.get("synthesis")
    return synthesis if isinstance(synthesis, dict) else data


def check_vix_consistency(base_dir: Path = BASE_DIR) -> list[dict]:
    issues: list[dict] = []
    vix_state = _load_json(base_dir / "vix_state.json", {})
    market = _load_json(base_dir / "market_snapshot.json", {})

    vix_obj = vix_state.get("vix", {}) if isinstance(vix_state, dict) else {}
    vix_level = vix_obj.get("level") if isinstance(vix_obj, dict) else None
    vix_label = vix_obj.get("classification") if isinstance(vix_obj, dict) else None
    expected = classify_vix(vix_level)
    if vix_level is not None and vix_label and vix_label != expected:
        issues.append(_issue(
            "vix_state_classification_mismatch",
            f"vix_state classification {vix_label} != {expected} for VIX={vix_level}",
            vix_level=vix_level,
            stored=vix_label,
            expected=expected,
        ))

    market_vix = market.get("VIX", {}) if isinstance(market, dict) else {}
    market_level = market_vix.get("price") if isinstance(market_vix, dict) else None
    market_label = market_vix.get("level") if isinstance(market_vix, dict) else None
    market_expected = classify_vix(market_level)
    if market_level is not None and market_label and market_label != market_expected:
        issues.append(_issue(
            "market_snapshot_vix_classification_mismatch",
            f"market_snapshot VIX level {market_label} != {market_expected} for VIX={market_level}",
            vix_level=market_level,
            stored=market_label,
            expected=market_expected,
        ))

    if vix_level is not None and market_level is not None:
        if abs(float(vix_level) - float(market_level)) <= 0.1 and vix_label and market_label and vix_label != market_label:
            issues.append(_issue(
                "vix_sources_disagree",
                f"Same VIX level has conflicting labels: vix_state={vix_label}, market_snapshot={market_label}",
                vix_state_level=vix_level,
                market_level=market_level,
            ))
    return issues


def _iter_scenarios(state: dict) -> list[dict]:
    scenarios = state.get("scenarios") if isinstance(state, dict) else None
    if isinstance(scenarios, dict):
        return [sc for sc in scenarios.values() if isinstance(sc, dict)]
    if isinstance(scenarios, list):
        return [sc for sc in scenarios if isinstance(sc, dict)]
    return []


def check_scenario_null_signals(base_dir: Path = BASE_DIR, max_null_ratio: float = 0.2) -> list[dict]:
    issues: list[dict] = []
    state = _load_json(base_dir / "scenario_state.json", {})
    scenarios = _iter_scenarios(state)
    details: list[dict] = []
    for sc in scenarios:
        details.extend([row for row in sc.get("signal_details", []) if isinstance(row, dict)])

    if not details:
        return [_issue("scenario_state_missing_details", "scenario_state has no signal_details", "warning")]

    nulls = [
        row for row in details
        if row.get("detail") == "データ未取得" or "データ未取得" in str(row.get("detail") or "")
    ]
    ratio = len(nulls) / max(len(details), 1)
    if ratio > max_null_ratio:
        issues.append(_issue(
            "scenario_null_signal_ratio_high",
            f"scenario signal null ratio {ratio:.0%} exceeds {max_null_ratio:.0%}",
            null_count=len(nulls),
            total=len(details),
        ))

    critical_keys = {"SPY_above_MA50", "regime_bull_confirmed"}
    missing_critical = [row.get("key") for row in nulls if row.get("key") in critical_keys]
    if missing_critical:
        issues.append(_issue(
            "scenario_critical_signal_missing",
            "critical scenario signals are inconclusive",
            "error",
            keys=missing_critical,
        ))
    return issues


def _action_dedup_key(action: dict) -> str | None:
    try:
        from action_state_tracker import _account_bucket, _dedup_key
    except Exception:
        return None
    ticker = action.get("ticker")
    atype = action.get("type") or action.get("action_type")
    if not ticker or not atype:
        return None
    return _dedup_key(str(ticker), str(atype), _account_bucket(action))


_ACTION_STATE_REGISTERED_STATUSES = {"pending", "placed", "filled"}


def check_action_state_alignment(base_dir: Path = BASE_DIR) -> list[dict]:
    issues: list[dict] = []
    synthesis = _analysis_payload(base_dir)
    actions = (
        synthesis.get("final_priority_actions")
        or synthesis.get("priority_actions")
        or []
    )
    if not isinstance(actions, list) or not actions:
        return issues

    state = _load_json(base_dir / "action_state.json", {"actions": {}})
    entries = state.get("actions", {}) if isinstance(state, dict) else {}
    registered_keys = set()
    if isinstance(entries, dict):
        for entry in entries.values():
            if (
                isinstance(entry, dict)
                and entry.get("status") in _ACTION_STATE_REGISTERED_STATUSES
            ):
                key = _action_dedup_key(entry)
                if key:
                    registered_keys.add(key)

    missing = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        key = _action_dedup_key(action)
        if key and key not in registered_keys:
            missing.append({"ticker": action.get("ticker"), "type": action.get("type"), "dedup_key": key})

    if missing:
        issues.append(_issue(
            "priority_actions_not_registered_pending",
            f"{len(missing)} final priority_actions are not present in action_state lifecycle",
            "error",
            missing=missing[:10],
            accepted_statuses=sorted(_ACTION_STATE_REGISTERED_STATUSES),
        ))
    return issues


def check_action_stage_executed_alignment(base_dir: Path = BASE_DIR) -> list[dict]:
    """Catch executed stage-log rows that do not correspond to execution records."""
    executions = _load_json(base_dir / "action_executions.json", {"executions": []})
    records = executions.get("executions", []) if isinstance(executions, dict) else []
    real_keys: set[tuple[str, str, str]] = set()
    real_as_of: list[str] = []
    if isinstance(records, list):
        for record in records:
            if not isinstance(record, dict):
                continue
            if record.get("portfolio_applied") is not True:
                continue
            if str(record.get("status") or "").lower() not in {"executed", "partial"}:
                continue
            as_of = str(record.get("saved_at") or "")
            ticker = str(record.get("ticker") or "")
            direction = str(record.get("direction") or "").lower()
            if not as_of or not ticker or not direction:
                continue
            real_keys.add((as_of, ticker, direction))
            real_as_of.append(as_of)

    if not real_keys:
        return []

    min_real_as_of = min(real_as_of)
    orphan_rows: list[dict] = []
    for row in _load_jsonl(base_dir / "action_stage_log.jsonl"):
        if row.get("stage") != "executed":
            continue
        as_of = str(row.get("as_of") or "")
        if not as_of or as_of < min_real_as_of:
            continue
        key = (
            as_of,
            str(row.get("ticker") or ""),
            str(row.get("canonical_action_type") or "").lower(),
        )
        if key not in real_keys:
            orphan_rows.append({
                "as_of": as_of,
                "ticker": row.get("ticker"),
                "type": row.get("canonical_action_type"),
                "estimated_notional_jpy": row.get("estimated_notional_jpy"),
            })

    if not orphan_rows:
        return []
    return [_issue(
        "action_stage_executed_orphan_rows",
        f"{len(orphan_rows)} executed stage-log rows do not match action_executions.json",
        "error",
        orphan_count=len(orphan_rows),
        examples=orphan_rows[:10],
        min_real_execution_saved_at=min_real_as_of,
    )]


def check_observability_logs(base_dir: Path = BASE_DIR) -> list[dict]:
    issues: list[dict] = []
    required = [
        "catalyst_hypothesis_log.jsonl",
        "agent_attribution_log.jsonl",
        "portfolio_decision_log.jsonl",
    ]
    for name in required:
        path = base_dir / name
        if not path.exists() or path.stat().st_size == 0:
            issues.append(_issue(
                "observability_log_missing",
                f"{name} is missing or empty after analysis run",
                "warning",
                file=name,
            ))
    return issues


def check_agent_reliability_join(
    base_dir: Path = BASE_DIR,
    *,
    horizon_days: int = 10,
    min_attribution_ids: int = 10,
    min_outcome_ids: int = 10,
) -> list[dict]:
    """Warn if mature reliability logs have no shared hypothesis IDs."""
    attr_ids = {
        str(row.get("hypothesis_id"))
        for row in _load_jsonl(base_dir / "agent_attribution_log.jsonl")
        if row.get("hypothesis_id")
        and row.get("agent") == "opus_final"
        and row.get("role") == "final_decider"
        and row.get("stance") == "support"
        and row.get("final_candidate_status") == "adopted"
    }
    outcome_ids = {
        str(row.get("hypothesis_id"))
        for row in _load_jsonl(base_dir / "catalyst_outcome_log.jsonl")
        if row.get("hypothesis_id") and row.get("horizon_days") == horizon_days
    }
    if len(attr_ids) < min_attribution_ids or len(outcome_ids) < min_outcome_ids:
        return []
    join_count = len(attr_ids & outcome_ids)
    if join_count:
        return []
    return [_issue(
        "agent_reliability_join_zero",
        "agent attribution IDs have zero overlap with catalyst outcome IDs",
        "warning",
        attribution_unique_ids=len(attr_ids),
        outcome_unique_ids=len(outcome_ids),
        horizon_days=horizon_days,
    )]


def check_portfolio_integrity(base_dir: Path = BASE_DIR, *, repair: bool = False) -> list[dict]:
    """Fail the post-run check when holdings/account/event ledger are inconsistent."""
    issues: list[dict] = []
    try:
        if repair:
            from utils import sync_account_cash_derived_totals
            sync_account_cash_derived_totals(base_dir / "account.json")
        from portfolio_integrity import run_integrity_check

        report = run_integrity_check(base_dir=base_dir)
    except Exception as exc:
        return [_issue(
            "portfolio_integrity_check_failed",
            f"portfolio_integrity check failed: {exc}",
            "error",
        )]

    if isinstance(report, dict) and report.get("ok") is False:
        summary = report.get("summary") if isinstance(report.get("summary"), dict) else {}
        issues.append(_issue(
            "portfolio_integrity_not_ok",
            "Portfolio Ledger Integrity ok=False; final actions must be treated as blocked until reconciled",
            "error",
            blocking_issue_count=report.get("blocking_issue_count", 0),
            unapplied_executed_count=summary.get("unapplied_executed_count", 0),
        ))
    return issues


def check_synthesis_risk_warnings(base_dir: Path = BASE_DIR) -> list[dict]:
    """Catch fail-loud warnings that are only represented in synthesis text."""
    issues: list[dict] = []
    synthesis = _analysis_payload(base_dir)
    text_parts: list[str] = []
    for key in ("aggressive_override_block", "stance_reason", "loss_management"):
        value = synthesis.get(key)
        if value:
            text_parts.append(str(value))
    warnings = synthesis.get("risk_warnings")
    if isinstance(warnings, list):
        text_parts.extend(str(w) for w in warnings)
    text = "\n".join(text_parts)

    if "Portfolio Ledger Integrity ok=False" in text or "Ledger Integrity ok=False" in text:
        issues.append(_issue(
            "synthesis_mentions_ledger_integrity_failure",
            "synthesis contains Portfolio Ledger Integrity ok=False",
            "error",
        ))
    very_stale_warnings = []
    if isinstance(warnings, list):
        very_stale_warnings = [str(w) for w in warnings if "VERY_STALE" in str(w)]
    if "VERY_STALE" in text:
        issues.append(_issue(
            "synthesis_mentions_very_stale_data",
            "synthesis contains VERY_STALE data warning",
            "error",
            warnings=very_stale_warnings[:5],
        ))
    if "cvar_unstable=true" in text:
        issues.append(_issue(
            "synthesis_mentions_cvar_unstable",
            "synthesis contains cvar_unstable=true; margin/aggressive sizing should be constrained",
            "warning",
        ))
    return issues


def _final_action_types(synthesis: dict) -> set[str]:
    actions = synthesis.get("final_priority_actions") or synthesis.get("priority_actions") or []
    if not isinstance(actions, list):
        return set()
    return {
        str(action.get("type") or "").lower()
        for action in actions
        if isinstance(action, dict)
    }


def _has_nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def check_absent_action_rationales(base_dir: Path = BASE_DIR) -> list[dict]:
    """Warn when important no-action diagnostics disappeared from the final analysis."""
    issues: list[dict] = []
    synthesis = _analysis_payload(base_dir)
    if not synthesis:
        return issues
    action_types = _final_action_types(synthesis)

    margin_raw = _load_json(base_dir / "margin_long_candidates.json", {})
    margin_candidates = margin_raw.get("candidates", []) if isinstance(margin_raw, dict) else []
    if (
        "margin_buy" not in action_types
        and isinstance(margin_candidates, list)
        and margin_candidates
        and not _has_nonempty_list(synthesis.get("margin_no_buy_rationale"))
    ):
        issues.append(_issue(
            "margin_no_buy_rationale_missing",
            "margin long candidates exist but final synthesis has no margin_buy and no margin_no_buy_rationale",
            "warning",
            candidate_count=len(margin_candidates),
        ))

    short_raw = _load_json(base_dir / "short_candidates.json", {})
    short_candidates = short_raw.get("candidates", []) if isinstance(short_raw, dict) else []
    scanned = short_raw.get("scanned") if isinstance(short_raw, dict) else None
    shortable_count = short_raw.get("shortable_count") if isinstance(short_raw, dict) else None
    short_scan_happened = bool(short_candidates) or bool(scanned) or shortable_count is not None
    if (
        "short" not in action_types
        and short_scan_happened
        and not _has_nonempty_list(synthesis.get("short_no_action_rationale"))
    ):
        issues.append(_issue(
            "short_no_action_rationale_missing",
            "short scan ran but final synthesis has no short action and no short_no_action_rationale",
            "warning",
            candidate_count=len(short_candidates) if isinstance(short_candidates, list) else None,
            scanned=scanned,
            shortable_count=shortable_count,
        ))
    return issues


def check_decision_summary_conservation(base_dir: Path = BASE_DIR) -> list[dict]:
    """Verify that every AI candidate is accounted for and readiness is explicit."""
    synthesis = _analysis_payload(base_dir)
    if not synthesis:
        return []
    priority = [row for row in (synthesis.get("priority_actions") or []) if isinstance(row, dict)]
    filtered = [row for row in (synthesis.get("_filtered_actions") or []) if isinstance(row, dict)]
    deferred = [row for row in (synthesis.get("order_intent_deferred_actions") or []) if isinstance(row, dict)]
    summary = synthesis.get("decision_summary")
    if not isinstance(summary, dict):
        return [_issue(
            "decision_summary_missing",
            "final synthesis has no deterministic decision_summary",
            "error",
        )]

    missing_readiness = [
        {"ticker": row.get("ticker"), "type": row.get("type")}
        for row in priority
        if str(row.get("execution_readiness") or "") not in {"ready", "review", "blocked"}
    ]
    issues: list[dict] = []
    if missing_readiness:
        issues.append(_issue(
            "priority_action_readiness_missing",
            f"{len(missing_readiness)} final priority actions have no explicit readiness",
            "error",
            examples=missing_readiness[:10],
        ))

    actual = {
        "candidate_count": len(priority) + len(filtered) + len(deferred),
        "executable_count": sum(row.get("execution_readiness") == "ready" for row in priority),
        "review_count": sum(row.get("execution_readiness") != "ready" for row in priority) + len(deferred),
        "filtered_count": len(filtered),
        "deferred_count": len(deferred),
    }
    mismatches = {
        key: {"stored": summary.get(key), "actual": value}
        for key, value in actual.items()
        if summary.get(key) != value
    }
    if mismatches or summary.get("count_conservation_ok") is not True:
        issues.append(_issue(
            "decision_summary_count_mismatch",
            "decision_summary does not conserve final candidate counts",
            "error",
            mismatches=mismatches,
            count_conservation_ok=summary.get("count_conservation_ok"),
        ))
    return issues


def verify_post_run(base_dir: Path = BASE_DIR, *, repair: bool = False) -> dict:
    issues: list[dict] = []
    issues.extend(check_vix_consistency(base_dir))
    issues.extend(check_scenario_null_signals(base_dir))
    issues.extend(check_action_state_alignment(base_dir))
    issues.extend(check_action_stage_executed_alignment(base_dir))
    issues.extend(check_observability_logs(base_dir))
    issues.extend(check_agent_reliability_join(base_dir))
    issues.extend(check_portfolio_integrity(base_dir, repair=repair))
    issues.extend(check_synthesis_risk_warnings(base_dir))
    issues.extend(check_absent_action_rationales(base_dir))
    issues.extend(check_decision_summary_conservation(base_dir))
    return {
        "ok": not any(item.get("severity") == "error" for item in issues),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "issue_count": len(issues),
        "issues": issues,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify post-analysis output consistency")
    parser.add_argument("--base-dir", default=str(BASE_DIR))
    parser.add_argument(
        "--repair",
        action="store_true",
        help="派生cashフィールドを修復してから検証する（既定は完全read-only）",
    )
    args = parser.parse_args(argv)

    report = verify_post_run(Path(args.base_dir), repair=args.repair)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
