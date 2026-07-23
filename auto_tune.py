"""Guarded Auto Tune orchestrator.

The LaunchAgent may evaluate recommendations four times per weekday.  Only an
explicit ``mode=apply`` runtime state can mutate parameters; ``force`` never
bypasses that boundary and is limited to context de-duplication for dry-runs.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
POLICY_FILE = BASE_DIR / "tuning_auto_policy.json"
AUTO_MODE_FILE = BASE_DIR / "tuning_auto_state.json"  # compatibility alias used by tests/tools
LEGACY_MODE_FILE = BASE_DIR / "tuning_auto_mode.json"
RUN_LOG_FILE = BASE_DIR / "tuning_auto_runs.jsonl"
LOG_FILE = BASE_DIR / "logs" / "auto_tune.log"
PARAMS_FILE = BASE_DIR / "tunable_params.json"
HISTORY_FILE = BASE_DIR / "tunable_params_history.jsonl"

_SOURCE_SPECS = {
    "guard": ("guard_state.json", ("updated_at", "last_updated", "timestamp")),
    "vix": ("vix_state.json", ("cached_at", "updated_at", "as_of", "timestamp")),
    "regime": ("regime_state.json", ("updated", "updated_at", "as_of", "timestamp")),
    "macro": ("macro_state.json", ("cached_at", "updated_at", "as_of", "timestamp")),
}


def _log(message: str) -> None:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


def _load_json(path: Path, default: Any) -> Any:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value
    except Exception:
        return default


def _load_policy() -> dict:
    policy = _load_json(POLICY_FILE, {})
    if not isinstance(policy, dict):
        return {}
    return policy


def _migrated_legacy_state() -> dict:
    legacy = _load_json(LEGACY_MODE_FILE, {})
    if not isinstance(legacy, dict):
        legacy = {}
    mode = "apply" if legacy.get("enabled") else "off"
    return {
        "version": 2,
        "mode": mode,
        "disabled_reason": legacy.get("disabled_reason"),
        "last_run": legacy.get("last_run"),
        "last_regime": legacy.get("last_regime"),
        "last_vix": legacy.get("last_vix"),
        "last_changes": legacy.get("last_changes") or [],
        "audit_reconciliation": legacy.get("audit_reconciliation") or {},
        "migrated_from": "tuning_auto_mode.json",
    }


def _load_state() -> dict:
    state = _load_json(AUTO_MODE_FILE, {})
    if isinstance(state, dict) and state:
        if "mode" not in state and "enabled" in state:
            state["mode"] = "apply" if state.get("enabled") else "off"
        return state
    return _migrated_legacy_state()


def _save_state(state: dict) -> None:
    from utils import atomic_write_json

    state = dict(state)
    state["version"] = 2
    state["enabled"] = state.get("mode") in {"shadow", "apply"}
    atomic_write_json(AUTO_MODE_FILE, state)


def set_mode(mode: str, *, actor: str = "api") -> dict:
    if mode not in {"off", "shadow", "apply"}:
        raise ValueError(f"unknown auto tune mode: {mode}")
    state = _load_state()
    state["mode"] = mode
    state["mode_changed_at"] = datetime.now().astimezone().isoformat()
    state["mode_changed_by"] = actor
    if mode == "off":
        state.setdefault("disabled_reason", "disabled by operator")
    else:
        state["disabled_reason"] = None
        state["enabled_at"] = state.get("enabled_at") or state["mode_changed_at"]
        state["enabled_by"] = actor
    _save_state(state)
    return state


def _effective_mode(state: dict) -> str:
    mode = state.get("mode")
    if mode in {"off", "shadow", "apply"}:
        return str(mode)
    return "apply" if state.get("enabled") else "off"


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            local_tz = datetime.now().astimezone().tzinfo or timezone.utc
            parsed = parsed.replace(tzinfo=local_tz)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _extract_timestamp(data: dict, keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value, key
    return None, None


def _source_health(name: str, *, maximum_age_hours: float, now: datetime) -> dict:
    filename, timestamp_keys = _SOURCE_SPECS[name]
    path = BASE_DIR / filename
    if not path.exists():
        return {"source": name, "file": filename, "exists": False, "fresh": False, "reason": "missing"}
    data = _load_json(path, {})
    if not isinstance(data, dict):
        return {"source": name, "file": filename, "exists": True, "fresh": False, "reason": "invalid_json"}
    timestamp, timestamp_source = _extract_timestamp(data, timestamp_keys)
    parsed = _parse_ts(timestamp)
    if parsed is None:
        parsed = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        timestamp = parsed.isoformat()
        timestamp_source = "mtime"
    age = max(0.0, (now - parsed).total_seconds() / 3600)
    return {
        "source": name,
        "file": filename,
        "exists": True,
        "timestamp": timestamp,
        "timestamp_source": timestamp_source,
        "age_hours": round(age, 2),
        "maximum_age_hours": maximum_age_hours,
        "fresh": age <= maximum_age_hours,
        "reason": None if age <= maximum_age_hours else "stale",
    }


def _collect_input_health(policy: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    freshness = policy.get("freshness_hours") or {}
    sources = {
        name: _source_health(name, maximum_age_hours=float(freshness.get(name, 0)), now=now)
        for name in _SOURCE_SPECS
    }
    try:
        from portfolio_manager import build_portfolio_snapshot

        portfolio = build_portfolio_snapshot()
        total = float(portfolio.get("total_jpy") or 0)
        portfolio_health = {
            "fresh": total > 0 and not portfolio.get("error"),
            "total_jpy": total,
            "as_of": portfolio.get("as_of"),
            "cash_total_jpy": portfolio.get("cash_total_jpy", portfolio.get("cash_jpy")),
            "reason": None if total > 0 and not portfolio.get("error") else "portfolio_unavailable",
        }
    except Exception as exc:
        portfolio_health = {"fresh": False, "reason": f"portfolio_error:{exc}"}
    action_log = BASE_DIR / "action_stage_log.jsonl"
    action_log_health = {
        "readable": action_log.exists() and action_log.is_file(),
        "file": action_log.name,
        "reason": None if action_log.exists() else "missing",
    }
    sources["portfolio"] = portfolio_health
    sources["action_stage_log"] = action_log_health
    blockers = [
        key for key, row in sources.items()
        if not bool(row.get("fresh", row.get("readable", False)))
    ]
    return {"ok": not blockers, "sources": sources, "blockers": blockers, "checked_at": now.isoformat()}


def _get_current_regime() -> str | None:
    value = _load_json(BASE_DIR / "regime_state.json", {})
    return value.get("regime") if isinstance(value, dict) else None


def _get_current_vix() -> float | None:
    value = _load_json(BASE_DIR / "vix_state.json", {})
    if not isinstance(value, dict):
        return None
    if isinstance(value.get("vix"), dict):
        level = value["vix"].get("level")
        if isinstance(level, (int, float)):
            return float(level)
    for key in ("level", "vix", "value"):
        level = value.get(key)
        if isinstance(level, (int, float)):
            return float(level)
    return None


def _context_hash(context: dict, *, revision: int) -> str:
    def rounded(value: Any, quantum: float) -> float | None:
        try:
            return round(float(value) / quantum) * quantum
        except Exception:
            return None

    stable = {
        "regime": context.get("regime"),
        "vix_bucket": math.floor(float(context.get("vix")) / 5) * 5 if context.get("vix") is not None else None,
        "daily_pnl": rounded(context.get("daily_pnl_pct"), 0.5),
        "monthly_pnl": rounded(context.get("monthly_pnl_pct"), 0.5),
        "cash_ratio": rounded(context.get("cash_ratio_pct"), 1.0),
        "top_sector": context.get("top_sector"),
        "top_sector_pct": rounded(context.get("top_sector_pct"), 1.0),
        "post_filter": {
            key: context.get(key)
            for key in sorted(context)
            if key.startswith("recent_")
        },
        "tunable_revision": revision,
    }
    payload = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def _read_history() -> list[dict]:
    try:
        rows = []
        for line in HISTORY_FILE.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue
        return rows
    except Exception:
        return []


def _trading_days_since(timestamp: str | None, *, today: date | None = None) -> int:
    parsed = _parse_ts(timestamp)
    if parsed is None:
        return 999
    start = parsed.astimezone().date()
    end = today or datetime.now().astimezone().date()
    if end <= start:
        return 0
    try:
        import pandas_market_calendars as market_calendars

        sessions: set[date] = set()
        for calendar_name in ("NYSE", "JPX"):
            schedule = market_calendars.get_calendar(calendar_name).schedule(start_date=start, end_date=end)
            sessions.update(index.date() for index in schedule.index)
        return len([session for session in sessions if start < session <= end])
    except Exception:
        cursor = start + timedelta(days=1)
        count = 0
        while cursor <= end:
            if cursor.weekday() < 5:
                count += 1
            cursor += timedelta(days=1)
        return count


def _last_auto_change_by_key() -> dict[str, str]:
    latest: dict[str, str] = {}
    for row in _read_history():
        if row.get("source") != "ai_auto" or not row.get("key") or not row.get("timestamp"):
            continue
        if str(row["timestamp"]) >= latest.get(str(row["key"]), ""):
            latest[str(row["key"])] = str(row["timestamp"])
    return latest


def _validate_recommendations(recommendations: list[dict], allowlist: list[str], params: dict) -> list[dict]:
    import tunable_params as tp

    if not isinstance(recommendations, list):
        raise ValueError("recommendations must be a list")
    seen: set[str] = set()
    normalized: list[dict] = []
    allowed = set(allowlist)
    for row in recommendations:
        if not isinstance(row, dict):
            raise ValueError("recommendation row must be an object")
        key = row.get("key")
        if key not in allowed:
            raise ValueError(f"unknown or non-allowlisted recommendation: {key}")
        if key in seen:
            raise ValueError(f"duplicate recommendation: {key}")
        seen.add(key)
        if row.get("error"):
            raise ValueError(f"invalid recommendation for {key}: {row.get('error')}")
        rationale = str(row.get("rationale") or "").strip()
        if not rationale:
            raise ValueError(f"missing rationale: {key}")
        recommended = tp.validate_candidate(str(key), row.get("recommended"))
        normalized.append({
            "key": key,
            "current": params[key].get("value"),
            "recommended": recommended,
            "rationale": rationale,
        })
    missing = [key for key in allowlist if key not in seen]
    if missing:
        raise ValueError(f"missing allowlisted recommendations: {','.join(missing)}")
    return normalized


def _group_recommendations(recommendations: list[dict], policy: dict) -> list[dict]:
    by_key = {row["key"]: row for row in recommendations}
    allowlist = list(policy.get("auto_apply_allowlist") or [])
    atomic_groups = [list(group) for group in (policy.get("atomic_groups") or [])]
    used: set[str] = set()
    groups: list[dict] = []
    max_steps = policy.get("max_absolute_step") or {}
    risk_classes = policy.get("risk_class") or {}
    last_change = _last_auto_change_by_key()
    cooldown_days = int(policy.get("cooldown_trading_days") or 0)

    raw_groups = atomic_groups + [[key] for key in allowlist if not any(key in group for group in atomic_groups)]
    for keys in raw_groups:
        keys = [key for key in keys if key in by_key and key not in used]
        if not keys:
            continue
        used.update(keys)
        rows = [by_key[key] for key in keys]
        changed_rows = [row for row in rows if row["recommended"] != row["current"]]
        if not changed_rows:
            groups.append({"keys": keys, "rows": rows, "decision": "skip", "reason": "unchanged"})
            continue
        violations = []
        for row in changed_rows:
            maximum = float(max_steps.get(row["key"], 0))
            delta = abs(float(row["recommended"]) - float(row["current"]))
            if maximum <= 0 or delta > maximum + 1e-8:
                violations.append(f"{row['key']}:delta={delta}>max={maximum}")
            changed_at = last_change.get(row["key"])
            if changed_at and _trading_days_since(changed_at) < cooldown_days:
                violations.append(f"{row['key']}:cooldown")
        if violations:
            groups.append({"keys": keys, "rows": rows, "decision": "reject", "reason": ";".join(violations)})
            continue
        risk_class = str(risk_classes.get(keys[0], "high"))
        score = max(
            abs(float(row["recommended"]) - float(row["current"])) / float(max_steps[row["key"]])
            for row in changed_rows
        )
        groups.append({
            "keys": keys,
            "rows": rows,
            "decision": "candidate",
            "risk_class": risk_class,
            "score": score,
        })

    limits = policy.get("max_groups_per_risk_class") or {}
    selected_count: dict[str, int] = {}
    candidates = sorted(
        [group for group in groups if group["decision"] == "candidate"],
        key=lambda group: (-float(group["score"]), min(allowlist.index(key) for key in group["keys"])),
    )
    for group in candidates:
        risk_class = group["risk_class"]
        if selected_count.get(risk_class, 0) >= int(limits.get(risk_class, 0)):
            group["decision"] = "skip"
            group["reason"] = "risk_class_limit"
        else:
            group["decision"] = "select"
            selected_count[risk_class] = selected_count.get(risk_class, 0) + 1
    return groups


def _append_run(record: dict) -> None:
    RUN_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with RUN_LOG_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def recent_runs(limit: int = 20) -> list[dict]:
    try:
        rows = []
        for line in RUN_LOG_FILE.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue
        return rows[-max(1, min(limit, 100)):][::-1]
    except Exception:
        return []


def _record_result(record: dict, state: dict, *, heartbeat_status: str = "ok") -> dict:
    finished = datetime.now().astimezone().isoformat()
    record["finished_at"] = finished
    _append_run(record)
    state["last_run"] = finished
    state["last_run_id"] = record.get("run_id")
    state["last_status"] = record.get("status")
    state["last_result"] = {
        key: record.get(key)
        for key in ("status", "dry_run", "applied_count", "skipped_count", "rejected_count", "blockers")
        if key in record
    }
    _save_state(state)
    try:
        from utils import heartbeat

        heartbeat("auto_tune", status=heartbeat_status, error=record.get("error"), extra=state["last_result"])
    except Exception:
        pass
    return record


def run(dry_run: bool = False, force: bool = False) -> dict:
    from utils import LockBusy, process_lock

    state = _load_state()
    policy = _load_policy()
    mode = _effective_mode(state)
    effective_dry_run = bool(dry_run or mode == "shadow")
    if mode == "off" and not dry_run:
        return {"status": "disabled", "mode": mode, "applied": 0, "applied_count": 0}
    run_id = str(uuid.uuid4())
    started = datetime.now().astimezone().isoformat()
    record: dict[str, Any] = {
        "run_id": run_id,
        "started_at": started,
        "mode": mode,
        "dry_run": effective_dry_run,
        "policy_version": policy.get("version"),
    }

    try:
        with process_lock("auto_tune"):
            _log(f"▶️ Auto-tune start run_id={run_id} mode={mode} dry_run={effective_dry_run}")
            health = _collect_input_health(policy)
            record["input_health"] = health
            if not health.get("ok"):
                record.update({
                    "status": "blocked_stale_inputs",
                    "blockers": health.get("blockers") or [],
                    "applied_count": 0,
                    "skipped_count": 0,
                    "rejected_count": 0,
                })
                return _record_result(record, state, heartbeat_status="warn")

            import tunable_params as tp
            from tuning_advisor import generate_recommendations, load_market_context

            params = tp.list_all()
            allowlist = list(policy.get("auto_apply_allowlist") or [])
            denylist = set(policy.get("auto_apply_denylist") or [])
            invalid_policy = [
                key for key in allowlist
                if key in denylist or key not in params or not params[key].get("auto_apply", False)
            ]
            if invalid_policy:
                raise ValueError(f"invalid allowlist policy: {','.join(invalid_policy)}")

            market_context = load_market_context()
            context_hash = _context_hash(market_context, revision=tp.get_revision())
            record["context_hash"] = context_hash
            record["market_context"] = market_context
            if not force and state.get("last_evaluated_context_hash") == context_hash:
                record.update({
                    "status": "skipped_same_context",
                    "applied_count": 0,
                    "skipped_count": len(allowlist),
                    "rejected_count": 0,
                })
                return _record_result(record, state)

            advisor_result = generate_recommendations(keys=allowlist, market_context=market_context)
            if advisor_result.get("error"):
                raise RuntimeError(str(advisor_result.get("error")))
            recommendations = _validate_recommendations(
                advisor_result.get("recommendations") or [], allowlist, params
            )
            groups = _group_recommendations(recommendations, policy)
            selected_groups = [group for group in groups if group["decision"] == "select"]
            skipped_groups = [group for group in groups if group["decision"] == "skip"]
            rejected_groups = [group for group in groups if group["decision"] == "reject"]
            selected_rows = [row for group in selected_groups for row in group["rows"] if row["recommended"] != row["current"]]
            changes = {row["key"]: row["recommended"] for row in selected_rows}
            expected = {row["key"]: row["current"] for row in selected_rows}
            rationales = {
                row["key"]: f"[Auto Tune] {row['rationale']} | context={context_hash}"
                for row in selected_rows
            }
            prospective = {key: row.get("value") for key, row in params.items()}
            prospective.update(changes)
            tp.validate_invariants(prospective)

            record.update({
                "recommendations": recommendations,
                "groups": groups,
                "selected": selected_rows,
                "skipped": skipped_groups,
                "rejected": rejected_groups,
                "skipped_count": sum(len(group["keys"]) for group in skipped_groups),
                "rejected_count": sum(len(group["keys"]) for group in rejected_groups),
            })
            state["last_evaluated_context_hash"] = context_hash
            state["last_regime"] = market_context.get("regime")
            state["last_vix"] = market_context.get("vix")

            if effective_dry_run:
                record["status"] = "shadow" if mode == "shadow" and not dry_run else "dry_run"
                record["applied_count"] = 0
                record["would_apply_count"] = len(changes)
                # An explicit dry-run must not consume the next real apply context.
                if dry_run:
                    state.pop("last_evaluated_context_hash", None)
                return _record_result(record, state)

            if not changes:
                record.update({"status": "no_change", "applied_count": 0})
                return _record_result(record, state)

            record["phase"] = "prepared"
            transaction = tp.apply_batch(
                changes,
                source="ai_auto",
                rationale_by_key=rationales,
                expected_values=expected,
                run_id=run_id,
            )
            after = {key: tp.get(key) for key in changes}
            tp.validate_invariants({key: row.get("value") for key, row in tp.list_all().items()})
            if after != changes:
                tp.apply_batch(
                    expected,
                    source="ai_auto_rollback",
                    rationale="post-apply verification failed",
                    expected_values=after,
                    run_id=run_id,
                )
                raise RuntimeError("post-apply verification failed; rolled back")
            record.update({
                "phase": "committed",
                "status": "applied",
                "applied_count": len(changes),
                "changes": {
                    key: {"old": expected[key], "new": changes[key]}
                    for key in changes
                },
                "tunable_revision": transaction.get("revision"),
            })
            state["last_applied_context_hash"] = context_hash
            state["last_changes"] = record["changes"]
            _log(f"✅ Auto-tune applied run_id={run_id} count={len(changes)}")
            return _record_result(record, state)
    except LockBusy:
        record.update({"status": "lock_busy", "applied_count": 0, "error": "auto_tune already running"})
        return _record_result(record, state, heartbeat_status="warn")
    except Exception as exc:
        _log(f"❌ Auto-tune failed run_id={run_id}: {exc}")
        record.update({"status": "failed", "applied_count": 0, "error": str(exc)})
        return _record_result(record, state, heartbeat_status="error")


def rollback_run(run_id: str, *, actor: str = "api") -> dict:
    import tunable_params as tp

    source = next((row for row in recent_runs(100) if row.get("run_id") == run_id), None)
    if not source or source.get("status") != "applied" or not source.get("changes"):
        raise ValueError(f"applied run not found: {run_id}")
    changes = source["changes"]
    restore = {key: row["old"] for key, row in changes.items()}
    expected = {key: row["new"] for key, row in changes.items()}
    rollback_id = str(uuid.uuid4())
    transaction = tp.apply_batch(
        restore,
        source="ai_auto_rollback",
        rationale=f"rollback run {run_id} by {actor}",
        expected_values=expected,
        run_id=rollback_id,
    )
    record = {
        "run_id": rollback_id,
        "rollback_of": run_id,
        "started_at": datetime.now().astimezone().isoformat(),
        "finished_at": datetime.now().astimezone().isoformat(),
        "status": "rolled_back",
        "actor": actor,
        "changes": {key: {"old": expected[key], "new": restore[key]} for key in restore},
        "tunable_revision": transaction.get("revision"),
    }
    _append_run(record)
    return record


def audit_state_consistency() -> dict:
    import tunable_params as tp

    issues: list[dict] = []
    reconciled: list[dict] = []
    policy = _load_policy()
    state = _load_state()
    params = tp.list_all()
    allowlist = policy.get("auto_apply_allowlist") or []
    for key in allowlist:
        row = params.get(key)
        if not row:
            issues.append({"type": "allowlist_key_missing", "key": key})
        elif not row.get("auto_apply", False):
            issues.append({"type": "allowlist_key_not_auto_apply", "key": key})
    try:
        tp.validate_invariants({key: row.get("value") for key, row in params.items()})
    except Exception as exc:
        issues.append({"type": "invariant_violation", "error": str(exc)})
    latest: dict[str, dict] = {}
    for row in _read_history():
        key = row.get("key")
        if key and str(row.get("timestamp") or "") >= str(latest.get(key, {}).get("timestamp") or ""):
            latest[str(key)] = row
    for key, row in latest.items():
        if row.get("source") == "ai_auto" and params.get(key, {}).get("value") != row.get("new_value"):
            mismatch = {
                "type": "latest_history_current_mismatch",
                "key": key,
                "current": params.get(key, {}).get("value"),
                "history": row.get("new_value"),
                "history_timestamp": row.get("timestamp"),
            }
            reconciliation = state.get("audit_reconciliation") or {}
            known_rows = reconciliation.get("reconciled_history_mismatches") or []
            known = any(
                isinstance(item, dict)
                and item.get("key") == key
                and item.get("canonical_value") == mismatch["current"]
                and item.get("latest_history_value") == mismatch["history"]
                and item.get("history_timestamp") == mismatch["history_timestamp"]
                for item in known_rows
            )
            if reconciliation.get("canonical_source") == "tunable_params.json" and known:
                reconciled.append(mismatch)
            else:
                issues.append(mismatch)
    return {
        "status": "ok" if not issues else "issues_found",
        "mode": _effective_mode(state),
        "enabled": _effective_mode(state) in {"shadow", "apply"},
        "policy_version": policy.get("version"),
        "allowlist_count": len(allowlist),
        "issue_count": len(issues),
        "issues": issues,
        "reconciled_count": len(reconciled),
        "reconciled": reconciled,
        "last_run": state.get("last_run"),
        "last_status": state.get("last_status"),
    }


def get_status() -> dict:
    state = _load_state()
    policy = _load_policy()
    mode = _effective_mode(state)
    run_summaries = [
        {key: row.get(key) for key in (
            "run_id", "started_at", "finished_at", "status", "dry_run",
            "applied_count", "would_apply_count", "blockers", "error", "changes", "rollback_of",
        ) if key in row}
        for row in recent_runs(10)
    ]
    return {
        "mode": mode,
        "enabled": mode in {"shadow", "apply"},
        "effective_apply": mode == "apply",
        "disabled_reason": state.get("disabled_reason"),
        "enabled_at": state.get("enabled_at"),
        "enabled_by": state.get("enabled_by"),
        "policy_version": policy.get("version"),
        "allowlist": policy.get("auto_apply_allowlist") or [],
        "denylist": policy.get("auto_apply_denylist") or [],
        "risk_class": policy.get("risk_class") or {},
        "schedule": policy.get("schedule") or {},
        "last_run": state.get("last_run"),
        "last_run_id": state.get("last_run_id"),
        "last_status": state.get("last_status"),
        "last_result": state.get("last_result") or {},
        "recent_runs": run_summaries,
        "audit": audit_state_consistency(),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="evaluate without changing tunable values")
    parser.add_argument("--force", action="store_true", help="dry-run only: bypass same-context skip")
    parser.add_argument("--audit", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()
    if args.force and not args.dry_run:
        print(json.dumps({"status": "invalid_args", "error": "--force requires --dry-run"}, ensure_ascii=False))
        sys.exit(2)
    result = get_status() if args.status else audit_state_consistency() if args.audit else run(dry_run=args.dry_run, force=args.force)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    sys.exit(0 if result.get("status") not in {"failed", "issues_found", "invalid_args"} else 1)
