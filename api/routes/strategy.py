"""
GET /api/strategy  — 現在シナリオ + 戦略推奨
"""
import asyncio
import sys
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _get_strategy() -> dict:
    try:
        sys.path.insert(0, str(BASE_DIR))
        from scenario_strategy import get_strategy
        return get_strategy()
    except Exception as e:
        return {
            "scenario": "NEUTRAL",
            "scenario_name": "中立相場",
            "scenario_icon": "⚖️",
            "scenario_color": "#6366F1",
            "scenario_description": "",
            "cash_ratio_target": 15,
            "long_bias": True,
            "short_allowed": False,
            "leverage_allowed": False,
            "actions": [],
            "opportunity": {"medium_risk": [], "high_risk": []},
            "crisis_protocol": [],
            "high_return_opportunities": [],
            "regime": {},
            "briefing_summary": "",
            "risk_alert": "",
            "opportunity_note": "",
            "as_of": "",
            "error": str(e),
        }


@router.get("/api/strategy")
async def get_strategy_endpoint():
    return await asyncio.to_thread(_get_strategy)
