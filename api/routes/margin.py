"""
GET /api/margin  — 信用建玉・証拠金維持率
GET /api/short   — 空売りスクリーニング候補
"""
import asyncio
import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _get_margin_summary() -> dict:
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from margin_manager import get_summary
        result = get_summary()
        # float('inf') は JSON シリアライズ不可なので null に変換
        ratio = result.get("maintenance_ratio", float("inf"))
        result["maintenance_ratio"] = None if not isinstance(ratio, (int, float)) or ratio == float("inf") or ratio != ratio else ratio
        return result
    except Exception as e:
        # P0-1: エラー fallback でも get_fx_rate_cached() を使用
        try:
            from utils import get_fx_rate_cached
            _fx, _ = get_fx_rate_cached()
            _fx_fallback = float(_fx)
        except Exception:
            _fx_fallback = 150.0
        return {
            "open_positions": [], "closed_positions": [],
            "collateral": 0, "maintenance_ratio": None,
            "margin_status": "safe", "total_unrealized": 0,
            "total_realized": 0, "expiry_alerts": [],
            "fx_usdjpy": _fx_fallback, "as_of": "", "error": str(e),
        }


def _get_short_candidates() -> dict:
    try:
        path = BASE_DIR / "short_candidates.json"
        if not path.exists():
            return {"candidates": [], "regime": "", "as_of": ""}
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        return {"candidates": [], "error": str(e)}


@router.get("/api/margin")
async def get_margin():
    return await asyncio.to_thread(_get_margin_summary)


@router.get("/api/short")
async def get_short():
    return await asyncio.to_thread(_get_short_candidates)
