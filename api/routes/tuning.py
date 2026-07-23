"""
GET  /api/tuning                       — 全パラメータ一覧（メタ情報込み）
GET  /api/tuning/{key}                 — 単一パラメータ
POST /api/tuning/{key}                 — 値を更新（バリデーション付き）
POST /api/tuning/{key}/reset           — デフォルトに戻す
POST /api/tuning/ai-recommend          — Opus に推奨値を取得
POST /api/tuning/apply-ai/{key}        — AI 推奨値を 1 つ適用
POST /api/tuning/apply-all-ai          — AI 推奨値を一括適用
"""
import sys
from pathlib import Path
from typing import Any, Optional, Literal
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))

import tunable_params as tp


# ─── Pydantic models ──────────────────────────────────────────
class SetValueRequest(BaseModel):
    value: Any = Field(..., description="新しい値")
    rationale: Optional[str] = Field(None, description="変更理由（任意）")


class AutoModeRequest(BaseModel):
    mode: Literal["off", "shadow", "apply"]
    confirm: bool = False


class AutoApplyRequest(BaseModel):
    auto_apply: bool


class BatchValueRequest(BaseModel):
    values: dict[str, Any]
    rationale: Optional[str] = None


class ConfirmRequest(BaseModel):
    confirm: bool = False


# ─── Routes ───────────────────────────────────────────────────
@router.get("/api/tuning")
async def get_all():
    """全パラメータ一覧（カテゴリ別グループ込み）"""
    all_params = tp.list_all()
    grouped = tp.by_category()
    return {
        "params": all_params,
        "by_category": {
            cat: [{"key": k, **v} for k, v in items]
            for cat, items in grouped.items()
        },
        "categories": tp.categories(),
        "total": len(all_params),
    }


# ─── 重要: 具体的ルートを {key} パラメータ化ルートより前に定義 ──────
# FastAPI は宣言順にマッチするため、/ai-recommend が /{key} に飲まれるのを防ぐ

@router.post("/api/tuning/ai-recommend")
async def ai_recommend():
    """Claude に推奨値を取得（同期実行・最大 60 秒）"""
    try:
        from tuning_advisor import generate_recommendations
        result = generate_recommendations()
        return result
    except ImportError:
        raise HTTPException(status_code=503, detail="tuning_advisor モジュール未実装")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI 推奨生成失敗: {e}")


@router.post("/api/tuning/apply-all-ai")
async def apply_all_ai():
    """Unsafe legacy bulk apply is intentionally retired."""
    raise HTTPException(
        status_code=410,
        detail="一括即時適用は廃止しました。Auto Tuneプレビューと確認済みapplyモードを使用してください。",
    )


@router.post("/api/tuning/apply-ai/{key}")
async def apply_ai_one(key: str):
    raise HTTPException(
        status_code=410,
        detail="旧AI即時適用は廃止しました。値を確認して通常の保存APIを使用してください。",
    )


# ─── Auto Mode 関連 ───────────────────────────────────────────

@router.get("/api/tuning/auto-mode")
async def get_auto_mode():
    """Auto Tuneの実効モード、ポリシー、稼働履歴を返す。"""
    from auto_tune import get_status
    return get_status()


@router.post("/api/tuning/auto-mode")
async def update_auto_mode(req: AutoModeRequest):
    """実行モードだけを変更する。ポリシーはAPIから変更しない。"""
    if req.mode == "apply" and not req.confirm:
        raise HTTPException(status_code=400, detail="applyモードへの変更にはconfirm=trueが必要です")
    from auto_tune import set_mode, get_status
    set_mode(req.mode, actor="api")
    return get_status()


@router.post("/api/tuning/auto-tune-now")
async def trigger_auto_tune_now(force: bool = False):
    """即時実行は常にドライラン。forceはcontext重複判定だけを無視する。"""
    try:
        from auto_tune import run as run_auto_tune
        return run_auto_tune(dry_run=True, force=force)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"auto-tune 実行失敗: {e}")


@router.get("/api/tuning/auto-runs")
async def get_auto_runs(limit: int = 20):
    from auto_tune import recent_runs
    runs = recent_runs(limit)
    return {"runs": runs, "count": len(runs)}


@router.post("/api/tuning/auto-runs/{run_id}/rollback")
async def rollback_auto_run(run_id: str, req: ConfirmRequest):
    if not req.confirm:
        raise HTTPException(status_code=400, detail="ロールバックにはconfirm=trueが必要です")
    try:
        from auto_tune import rollback_run
        return rollback_run(run_id, actor="api")
    except tp.TuningConflictError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/api/tuning/{key}/auto-apply")
async def set_auto_apply(key: str, req: AutoApplyRequest):
    raise HTTPException(
        status_code=409,
        detail="auto_applyはversion管理されたポリシーです。APIから変更できません。",
    )


@router.post("/api/tuning/batch")
async def set_batch(req: BatchValueRequest):
    try:
        return tp.apply_batch(req.values, source="user", rationale=req.rationale)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except tp.TuningValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ─── 汎用 {key} ルート（最後に定義）────────────────────────────────

@router.get("/api/tuning/{key}")
async def get_one(key: str):
    """単一パラメータの詳細"""
    meta = tp.get_meta(key)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"unknown key: {key}")
    return {"key": key, **meta}


@router.post("/api/tuning/{key}/reset")
async def reset_one(key: str):
    """デフォルトに戻す"""
    try:
        result = tp.reset(key, source="user")
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown key: {key}")
    return {"key": key, "reset": True, **result}


@router.post("/api/tuning/{key}")
async def set_one(key: str, req: SetValueRequest):
    """値を更新"""
    try:
        result = tp.set_value(key, req.value, source="user", rationale=req.rationale)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"unknown key: {key}")
    except tp.TuningValidationError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"内部エラー: {e}")
    return {"key": key, "updated": True, **result}
