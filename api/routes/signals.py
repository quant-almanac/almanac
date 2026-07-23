"""
GET /api/signals
signals_log.json + screen_results.json を返す
- シグナルには有効期限（TTL）を適用: デフォルト14日
- 期限切れシグナルは stale フラグ付き、21日超は除外
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))
from utils import load_json as _load_json

SIGNAL_FRESH_DAYS = 14    # これ以内は「有効」
SIGNAL_STALE_DAYS = 21    # これを超えたら非表示
SIGNAL_MAX_DAYS   = 30    # これを超えたら完全除外（JSONからも削除候補）


def _enrich_signals(signals: dict) -> dict:
    """シグナルに経過日数・鮮度ステータスを付与し、古すぎるものを除外"""
    now = datetime.now()
    enriched = {}

    for ticker, sig in signals.items():
        date_str = sig.get("signal_date", "")
        try:
            sig_date = datetime.strptime(date_str, "%Y-%m-%d %H:%M")
        except (ValueError, TypeError):
            try:
                sig_date = datetime.strptime(date_str, "%Y-%m-%d")
            except (ValueError, TypeError):
                # 日付不明 → 古いとみなして除外
                continue

        days_old = (now - sig_date).days

        # 完全除外
        if days_old > SIGNAL_MAX_DAYS:
            continue

        freshness = "fresh" if days_old <= SIGNAL_FRESH_DAYS else "stale"

        enriched[ticker] = {
            **sig,
            "days_old": days_old,
            "freshness": freshness,
            "expired": days_old > SIGNAL_STALE_DAYS,
        }

    return enriched


@router.get("/api/signals")
async def get_signals():
    signals_raw = _load_json(BASE_DIR / "signals_log.json")
    candidates_raw = _load_json(BASE_DIR / "screen_results.json")

    # シグナルにTTLフィルタ適用
    if isinstance(signals_raw, dict):
        all_signals = _enrich_signals(signals_raw)
        # アクティブ = expired でないもの
        active_signals = {k: v for k, v in all_signals.items() if not v.get("expired")}
        stale_signals = {k: v for k, v in all_signals.items() if v.get("expired")}
    else:
        active_signals = {}
        stale_signals = {}

    # screen_results は list or dict
    if isinstance(candidates_raw, list):
        candidates = candidates_raw[:10]
    elif isinstance(candidates_raw, dict):
        candidates = candidates_raw.get("results", candidates_raw.get("candidates", []))[:10]
    else:
        candidates = []

    return {
        "signals": active_signals,
        "stale_signals": stale_signals,
        "candidates": candidates,
    }
