"""Live operational truth for the System page."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from utils import load_json

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _heartbeat_rows(
    heartbeats: dict,
    *,
    health: dict | None = None,
) -> list[dict]:
    """Normalize raw heartbeats and watchdog verdicts for the System UI."""
    from watchdog import EXPECTED_INTERVALS, evaluate_health

    evaluation = health if health is not None else evaluate_health()
    stale_by_key = {
        str(row.get("script")): row
        for row in evaluation.get("stale", [])
        if isinstance(row, dict) and row.get("script")
    }
    error_by_key = {
        str(row.get("script")): row
        for row in evaluation.get("errors", [])
        if isinstance(row, dict) and row.get("script")
    }
    ok = {str(key) for key in evaluation.get("ok", [])}
    keys = sorted(set(EXPECTED_INTERVALS) | set(heartbeats))
    rows = []
    for key in keys:
        raw = heartbeats.get(key)
        raw = raw if isinstance(raw, dict) else {}
        cfg = EXPECTED_INTERVALS.get(key)
        last_run_ts = raw.get("last_run_ts")
        age_hours = None
        if isinstance(last_run_ts, (int, float)):
            age_hours = round(
                max(0.0, datetime.now(timezone.utc).timestamp() - float(last_run_ts))
                / 3600,
                1,
            )
        if key in error_by_key:
            freshness = "error"
            detail = error_by_key[key].get("error") or raw.get("error")
        elif key in stale_by_key:
            freshness = "stale"
            detail = stale_by_key[key].get("reason")
        elif key in ok:
            freshness = "fresh"
            detail = None
        elif raw.get("status") == "error":
            freshness = "error"
            detail = raw.get("error")
        elif raw.get("status") in {"warn", "warning"}:
            freshness = "warning"
            detail = raw.get("error") or f"raw_status_{raw.get('status')}"
        elif raw:
            freshness = "unmonitored"
            detail = None
        else:
            freshness = "missing"
            detail = "never_run"
        rows.append({
            "key": key,
            "status": raw.get("status") or ("missing" if not raw else "unknown"),
            "freshness_status": freshness,
            "monitored": cfg is not None,
            "last_run_iso": raw.get("last_run_iso"),
            "age_hours": age_hours,
            "max_age_hours": (
                round(cfg["max_stale_sec"] / 3600, 1) if cfg else None
            ),
            "error": detail,
        })
    return rows


@router.get("/api/system/status")
async def get_system_status():
    from api.routes.dashboard import _build_data_health
    from auto_tune import get_status as get_auto_tune_status
    from model_router import get_model, resolve_adapter
    from tunable_params import get as tunable
    from feature_controls import list_feature_statuses

    roles = (
        "tier_analysis_long",
        "tier_analysis_medium",
        "tier_analysis_short",
        "tier_analysis_margin_long",
        "tier_analysis_shortsell",
        "final_synthesis",
        "red_team_1",
        "red_team_2",
        "red_team_3",
        "tuning_advisor",
    )
    model_routes = [
        {"role": role, "model": get_model(role), "adapter": resolve_adapter(role)}
        for role in roles
    ]
    execution_plan = load_json(BASE_DIR / "execution_plan_state.json", default={}) or {}
    heartbeats = load_json(BASE_DIR / "heartbeats.json", default={}) or {}
    heartbeats = heartbeats if isinstance(heartbeats, dict) else {}
    heartbeat_statuses = _heartbeat_rows(heartbeats)
    auto_tune = get_auto_tune_status()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "data_health": _build_data_health(),
        "auto_tune": auto_tune,
        "model_routes": model_routes,
        "guards": {
            "daily_loss_limit_pct": tunable("daily_loss_limit_pct", -5),
            "monthly_stage1_pct": tunable("monthly_stage1_pct", -10),
            "monthly_stage2_pct": tunable("monthly_stage2_pct", -15),
            "monthly_stage3_pct": tunable("monthly_stage3_pct", -20),
            "max_short_positions": tunable("max_short_positions", 3),
            "sector_rebalance_threshold_pct": tunable("sector_rebalance_threshold_pct", 35),
            "sector_max_pct": tunable("sector_max_pct", 40),
        },
        "feature_modes": {
            "execution_plan": execution_plan.get("mode") or execution_plan.get("gate_mode") or "observe",
            "auto_tune": auto_tune.get("mode"),
            "disclosure_features": "observe_only",
        },
        "feature_controls": list_feature_statuses(),
        "heartbeats": heartbeats,
        "heartbeat_statuses": heartbeat_statuses,
        "schedules": {"auto_tune": auto_tune.get("schedule") or {}},
    }
