"""
GET /api/dca — DCA ラダー (bottom-fishing) 発動状態
POST /api/dca/evaluate — エンジンを強制再評価して signals.json を更新

bottom_fishing_signals.json を返す。ファイルが無い/平時の場合は active_tranche=None。
"""
import asyncio
import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
SIGNALS_FILE = BASE_DIR / "bottom_fishing_signals.json"


def _load_signals() -> dict:
    if not SIGNALS_FILE.exists():
        return {
            "active_tranche": None,
            "evaluated_at":   None,
            "recommended_buys": [],
            "note":           "signals file not found — run drawdown_dca_engine.py evaluate",
        }
    try:
        return json.loads(SIGNALS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        return {"active_tranche": None, "error": str(e)}


def _evaluate_and_save() -> dict:
    import sys
    sys.path.insert(0, str(BASE_DIR))
    try:
        from drawdown_dca_engine import generate_ladder_signals, persist, _estimate_cash_jpy
        cash = _estimate_cash_jpy()
        sig = generate_ladder_signals(cash_jpy=cash, dry_run=True)
        persist(sig)
        return sig
    except Exception as e:
        return {"active_tranche": None, "error": str(e)}


@router.get("/api/dca")
async def get_dca_signals():
    """現在の DCA ラダー発動状態を返す。Frontend Panel が polling 購読する。"""
    return await asyncio.to_thread(_load_signals)


@router.post("/api/dca/evaluate")
async def evaluate_dca():
    """エンジンを再評価して signals.json を更新し、結果を返す。"""
    return await asyncio.to_thread(_evaluate_and_save)
