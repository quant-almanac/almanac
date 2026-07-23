"""
GET  /api/portfolio             — スナップショット（5分キャッシュ）
GET  /api/holdings              — holdings.json 生データ
PUT  /api/holdings/{key}        — 銘柄更新
POST /api/holdings              — 銘柄追加
DELETE /api/holdings/{key}      — 銘柄削除
"""
import asyncio
import sys
import time
from pathlib import Path
from typing import Optional
from fastapi import APIRouter

router = APIRouter()

BASE_DIR = Path(__file__).parent.parent.parent
HOLDINGS_FILE = BASE_DIR / "holdings.json"
sys.path.insert(0, str(BASE_DIR))
from utils import load_json as _load_json, atomic_write_json as _save_json

_snapshot_cache: Optional[dict] = None
_snapshot_time: float = 0
_CACHE_TTL = 300  # 5分


def get_cached_snapshot() -> dict:
    """スナップショットを取得（5分キャッシュ付き）"""
    global _snapshot_cache, _snapshot_time
    now = time.time()
    if _snapshot_cache is not None and now - _snapshot_time < _CACHE_TTL:
        return _snapshot_cache
    try:
        from portfolio_manager import build_portfolio_snapshot
        snap = build_portfolio_snapshot()
        _snapshot_cache = snap
        _snapshot_time = now
        return snap
    except Exception as e:
        return {
            "error": str(e),
            "positions": [],
            "currency_breakdown": {},
            "sector_breakdown": {},
            "total_jpy": 0,
        }


@router.get("/api/portfolio")
async def get_portfolio():
    return await asyncio.to_thread(get_cached_snapshot)


# ─── Holdings CRUD ─────────────────────────────────────────

@router.get("/api/holdings")
async def get_holdings():
    """holdings.json の生データを返す"""
    return _load_json(HOLDINGS_FILE, {})


@router.put("/api/holdings/{key}")
async def update_holding(key: str, body: dict):
    """既存銘柄を更新"""
    holdings = _load_json(HOLDINGS_FILE, {})
    if key not in holdings:
        return {"ok": False, "error": f"'{key}' が見つかりません"}

    # 更新可能フィールドのみ上書き
    EDITABLE = {"shares", "entry_price", "entry_date", "account", "currency",
                "name", "investment_type", "note", "current_nav", "unit",
                "partial_taken", "stop_loss_atr", "strategy", "ticker"}
    for field in EDITABLE:
        if field in body:
            holdings[key][field] = body[field]

    _save_json(HOLDINGS_FILE, holdings)
    _invalidate_cache()
    return {"ok": True, "holding": holdings[key]}


@router.post("/api/holdings")
async def add_holding(body: dict):
    """新規銘柄を追加"""
    key = body.get("key", "").strip()
    if not key:
        return {"ok": False, "error": "key は必須です"}

    holdings = _load_json(HOLDINGS_FILE, {})
    if key in holdings:
        return {"ok": False, "error": f"'{key}' は既に存在します"}

    holdings[key] = {
        "ticker":          body.get("ticker", key),
        "entry_price":     body.get("entry_price", 0),
        "shares":          body.get("shares", 0),
        "entry_date":      body.get("entry_date", ""),
        "account":         body.get("account", "特定"),
        "currency":        body.get("currency", "USD"),
        "name":            body.get("name", key),
        "investment_type": body.get("investment_type", "medium"),
    }
    # オプションフィールド
    for opt in ("unit", "current_nav", "note", "strategy", "stop_loss_atr", "partial_taken"):
        if opt in body:
            holdings[key][opt] = body[opt]

    _save_json(HOLDINGS_FILE, holdings)
    _invalidate_cache()
    return {"ok": True, "holding": holdings[key]}


@router.delete("/api/holdings/{key}")
async def delete_holding(key: str):
    """銘柄を削除"""
    holdings = _load_json(HOLDINGS_FILE, {})
    if key not in holdings:
        return {"ok": False, "error": f"'{key}' が見つかりません"}

    deleted = holdings.pop(key)
    _save_json(HOLDINGS_FILE, holdings)
    _invalidate_cache()
    return {"ok": True, "deleted": deleted}


def _invalidate_cache():
    """スナップショットキャッシュを無効化"""
    global _snapshot_cache, _snapshot_time
    _snapshot_cache = None
    _snapshot_time = 0
