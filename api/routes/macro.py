"""
GET /api/macro — FREDマクロ経済指標
macro_state.json キャッシュを返す（TTL: 6h）。FRED_API_KEY 未設定時はデフォルト値。
"""
import asyncio
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent


def _get_macro() -> dict:
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        from macro_fetcher import get_macro_context
        return get_macro_context()
    except Exception as e:
        return {
            "fed_rate": None, "yield_10y": None, "yield_2y": None,
            "yield_spread": None, "yield_inverted": False,
            "cpi_yoy": None, "unemp_rate": None,
            "macro_adj": 0, "source": "error", "error": str(e),
        }


@router.get("/api/macro")
async def get_macro():
    return await asyncio.to_thread(_get_macro)
