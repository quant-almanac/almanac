"""
GET  /api/decision/log     — 過去の意思決定ログ
POST /api/decision/analyze — 新規分析実行 (Sonnet)
POST /api/decision/judge   — Opus 最終判断
"""
import asyncio
import json
from pathlib import Path
from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
DECISION_LOG = BASE_DIR / "decision_log.json"


# ── リクエストモデル ──
class AnalyzeRequest(BaseModel):
    case_type: str          # A / B / C / D / E
    ticker: str | None = None
    signal: str | None = None
    strategy: str | None = None
    reason: str | None = None
    person: str = "husband"
    question: str = ""


class JudgeRequest(BaseModel):
    case_result: dict
    user_preference: str = ""


# ── ログ読み込み ──
def _load_log() -> list:
    if not DECISION_LOG.exists():
        return []
    try:
        with open(DECISION_LOG, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else []
    except Exception:
        return []


# ── Sonnet 分析 ──
def _run_analysis(req: AnalyzeRequest) -> dict:
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        import decision_support as ds

        ct = req.case_type.upper()
        if ct == "A":
            return ds.run_case_a(
                ticker=req.ticker or "",
                signal=req.signal or "買い",
                strategy=req.strategy or "短期モメンタム",
                question=req.question,
            )
        elif ct == "B":
            return ds.run_case_b(
                ticker=req.ticker or "",
                reason=req.reason or "",
                question=req.question,
            )
        elif ct == "C":
            return ds.run_case_c(question=req.question)
        elif ct == "D":
            return ds.run_case_d(person=req.person, question=req.question)
        elif ct == "E":
            return ds.run_case_e(question=req.question)
        else:
            return {"error": f"未対応のケース: {ct}"}
    except Exception as e:
        return {"error": str(e), "case_type": req.case_type}


# ── Opus 最終判断 ──
def _run_judgment(req: JudgeRequest) -> dict:
    try:
        import sys
        sys.path.insert(0, str(BASE_DIR))
        import decision_support as ds
        judgment = ds.get_opus_judgment(req.case_result, req.user_preference)
        return {"judgment": judgment}
    except Exception as e:
        return {"error": str(e), "judgment": ""}


@router.get("/api/decision/log")
async def get_decision_log():
    return await asyncio.to_thread(_load_log)


@router.post("/api/decision/analyze")
async def post_decision_analyze(req: AnalyzeRequest):
    return await asyncio.to_thread(_run_analysis, req)


@router.post("/api/decision/judge")
async def post_decision_judge(req: JudgeRequest):
    return await asyncio.to_thread(_run_judgment, req)
