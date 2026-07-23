"""
GET /api/batch — Anthropic Batch API ステータス
long_term_batch_state.json を読んでステータスを返す。
"""
import json
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
BATCH_STATE_FILE = BASE_DIR / "long_term_batch_state.json"


@router.get("/api/batch")
async def get_batch_status():
    if not BATCH_STATE_FILE.exists():
        return {"status": "none", "batch_id": None, "submitted_at": None, "count": 0}
    try:
        data = json.loads(BATCH_STATE_FILE.read_text(encoding="utf-8"))
        return {
            "status":       data.get("status", "submitted"),
            "batch_id":     data.get("batch_id"),
            "submitted_at": data.get("submitted_at"),
            "count":        data.get("count", 0),
        }
    except Exception as e:
        return {"status": "error", "error": str(e)}
