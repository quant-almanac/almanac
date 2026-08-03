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
    from watchdog import EXPECTED_INTERVALS, evaluate_heartbeats

    evaluation = health if health is not None else evaluate_heartbeats(heartbeats)
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
    from risk_policy import POLICY
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
            "risk_policy_version": POLICY.version,
            "daily_loss_limit_pct_points": POLICY.daily_loss_block_decimal * 100,
            "rolling_30_stage_pct_points": [
                POLICY.rolling_30_stage1_decimal * 100,
                POLICY.rolling_30_stage2_decimal * 100,
                POLICY.rolling_30_stage3_decimal * 100,
            ],
            "var_1d_95_pct_points": [
                POLICY.var_bear_decimal * 100,
                POLICY.var_normal_decimal * 100,
                POLICY.var_bull_decimal * 100,
                POLICY.var_absolute_max_decimal * 100,
            ],
            "concentration_pct_points": [
                POLICY.concentration_caution_decimal * 100,
                POLICY.concentration_cap_decimal * 100,
            ],
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
