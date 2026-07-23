"""
GET  /api/ai-analysis          — キャッシュされた AI 分析を返す
POST /api/ai-analysis/refresh  — バックグラウンドで新規分析を実行
GET  /api/ai-analysis/progress — 分析進捗を返す
"""
import json
import sys
from pathlib import Path
from fastapi import APIRouter, BackgroundTasks

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

# P1-15: モジュール内 bool 排他 → file lock に置換
# 旧実装は _refresh_running = False/True で uvicorn --reload や複数プロセスで破綻していた。
from utils import process_lock, is_locked, LockBusy  # noqa: E402

LOCK_NAME = "ai_analysis"


def _run_analysis_bg(send_telegram: bool = False):
    try:
        with process_lock(LOCK_NAME):
            from portfolio_analyst import run_analysis, send_to_telegram
            result = run_analysis(force=True)
            if send_telegram and result:
                ok = send_to_telegram(result)
                print(f"[ai_analysis] Telegram送信: {'✅ 完了' if ok else '❌ 失敗'}")
    except LockBusy:
        print(f"[ai_analysis] 別プロセスが分析中のため skip")
    except Exception as e:
        print(f"[ai_analysis] background error: {e}")


@router.get("/api/ai-analysis")
async def get_analysis():
    try:
        from portfolio_analyst import get_cached, _is_cache_valid
        data = get_cached()
        data["cache_valid"] = _is_cache_valid()
        data["refresh_running"] = is_locked(LOCK_NAME)
        return data
    except Exception as e:
        return {
            "error": str(e),
            "cache_valid": False,
            "refresh_running": is_locked(LOCK_NAME),
        }


@router.post("/api/ai-analysis/refresh")
async def refresh_analysis(background_tasks: BackgroundTasks):
    if is_locked(LOCK_NAME):
        return {"status": "already_running", "message": "AI分析は既に実行中です（約1〜2分）"}
    background_tasks.add_task(_run_analysis_bg, send_telegram=True)
    return {
        "status": "started",
        "message": "AI分析をバックグラウンドで開始しました（約1〜2分かかります）。完了後 Telegram に通知します。",
    }


@router.get("/api/ai-analysis/progress")
async def get_progress():
    """analysis_progress.json を読むだけ（AI API 呼び出しなし）"""
    path = BASE_DIR / "analysis_progress.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"step": 0, "total": 8, "label": "待機中", "detail": "", "pct": 0}


@router.get("/api/ai-analysis/history")
async def get_history():
    """過去の分析サマリー履歴を返す"""
    path = BASE_DIR / "ai_analysis_history.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        records = data.get("history", [])
        return {"history": list(reversed(records))}  # 新しい順
    except Exception:
        return {"history": []}


# ─── 注文方法だけを軽量再評価（オンデマンド）─────────────────

_order_refresh_running = False


_order_last_result: dict | None = None


def _run_order_strategy_bg(send_telegram: bool = True):
    global _order_refresh_running, _order_last_result
    try:
        sys.path.insert(0, str(BASE_DIR))
        from analyst.order_strategy import re_evaluate
        result = re_evaluate(send_telegram=send_telegram)
        _order_last_result = result
        print(f"[order_strategy] refresh完了: status={result.get('status')} updated={result.get('updated',0)}")
    except Exception as e:
        _order_last_result = {"status": "error", "message": f"background error: {e}"}
        print(f"[order_strategy] background error: {e}")
    finally:
        _order_refresh_running = False


@router.post("/api/ai-analysis/order-strategy/refresh")
async def refresh_order_strategy(background_tasks: BackgroundTasks, telegram: bool = True):
    """priority_actions の order_type / limit_price / expiry_minutes / execution_reason
    だけを最新の市場価格で再評価する（Sonnet 1ショット、所要 30秒程度）。

    UI: 「📋 注文方法だけ再分析」ボタンから呼ばれる想定。
    """
    global _order_refresh_running
    if _order_refresh_running:
        return {"status": "already_running", "message": "注文方法の再分析は既に実行中です"}
    _order_refresh_running = True
    background_tasks.add_task(_run_order_strategy_bg, send_telegram=telegram)
    return {
        "status": "started",
        "message": "注文方法の再分析を開始しました（約30秒）。完了後ダッシュボードを再読み込みすると最新の order_type/limit_price が反映されます。",
    }


@router.get("/api/ai-analysis/order-strategy/status")
async def get_order_strategy_status():
    """注文方法再分析の実行状態と最終更新時刻を返す"""
    global _order_refresh_running
    refreshed_at = None
    try:
        sys.path.insert(0, str(BASE_DIR))
        from portfolio_analyst import get_cached
        data = get_cached() or {}
        syn = data.get("synthesis") or {}
        refreshed_at = syn.get("order_strategy_refreshed_at")
    except Exception:
        pass
    return {
        "running":       _order_refresh_running,
        "refreshed_at":  refreshed_at,
        "last_result":   _order_last_result,
    }
