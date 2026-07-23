"""Live operational truth for the System page."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter

from utils import load_json

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


@router.get("/api/system/status")
async def get_system_status():
    from api.routes.dashboard import _build_data_health
    from auto_tune import get_status as get_auto_tune_status
    from model_router import get_model, resolve_adapter
    from tunable_params import get as tunable

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
        "heartbeats": heartbeats,
        "schedules": {"auto_tune": auto_tune.get("schedule") or {}},
    }
