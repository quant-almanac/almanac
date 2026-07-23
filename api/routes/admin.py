"""
GET /api/admin — 持株会 + クレカ積立管理データ
POST /api/admin/credit-card/purchase — 月次積立を記録
POST /api/admin/credit-card/nav      — NAV更新
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))


# ─── Pydantic models ──────────────────────────────────────────
class PurchaseRequest(BaseModel):
    person: str = Field(..., pattern="^(husband|wife)$")
    amount: int = Field(..., gt=0, description="積立金額（円）")
    nav: float = Field(..., gt=0, description="買付時のNAV（円/口）")
    purchase_date: Optional[str] = Field(None, description="YYYY-MM-DD（省略時=今日）")

    @field_validator("person")
    @classmethod
    def _valid_person(cls, v: str) -> str:
        if v not in ("husband", "wife"):
            raise ValueError("person は husband または wife")
        return v


class NavUpdateRequest(BaseModel):
    person: str = Field(..., pattern="^(husband|wife)$")
    current_nav: float = Field(..., gt=0)


class UndoPurchaseRequest(BaseModel):
    person: str = Field(..., pattern="^(husband|wife)$")
    purchase_date: str = Field(..., description="取り消す積立のYYYY-MM-DD")


def _build_admin_data() -> dict:
    result: dict = {}

    # ── 持株会 ──
    try:
        kpath = BASE_DIR / "espp_plan.json"
        if kpath.exists():
            with open(kpath, encoding="utf-8") as f:
                espp = json.load(f)

            # 現在価格を取得して評価額計算
            try:
                import yfinance as yf
                symbol = str(espp.get("ticker") or "9999.T")
                hist = yf.Ticker(symbol).history(period="5d")
                current_price = float(hist["Close"].iloc[-1]) if not hist.empty else None
            except Exception:
                current_price = None

            shares = espp.get("current_shares", 0)
            avg_cost = espp.get("avg_cost", 0)
            total_incentive = espp.get("total_incentive", 0)
            adjusted_cost = avg_cost - (total_incentive / shares) if shares > 0 else avg_cost

            espp["current_price"]      = current_price
            espp["market_value"]       = round(current_price * shares, 0) if current_price else None
            espp["unrealized_jpy"]     = round((current_price - avg_cost) * shares, 0) if current_price else None
            espp["unrealized_pct"]     = round((current_price / avg_cost - 1) * 100, 2) if current_price and avg_cost else None
            espp["adjusted_cost"]      = round(adjusted_cost, 2)
            espp["adjusted_unrealized_pct"] = round((current_price / adjusted_cost - 1) * 100, 2) if current_price and adjusted_cost else None
            result["espp"] = espp
    except Exception as e:
        result["espp"] = {"error": str(e)}

    # ── クレカ積立 ──
    try:
        cpath = BASE_DIR / "credit_card_plans.json"
        if cpath.exists():
            with open(cpath, encoding="utf-8") as f:
                credit = json.load(f)

            # 月額・年間ポイント計算
            total_monthly = sum(
                v.get("monthly_amount", 0)
                for k, v in credit.items()
                if isinstance(v, dict) and k not in ("sell_strategy",)
            )
            annual_points = sum(
                v.get("monthly_amount", 0) * v.get("point_rate", 0) * 12
                for k, v in credit.items()
                if isinstance(v, dict) and k not in ("sell_strategy",)
            )
            credit["_summary"] = {
                "total_monthly_amount": total_monthly,
                "annual_points_estimate": round(annual_points, 0),
            }
            result["credit_card"] = credit
    except Exception as e:
        result["credit_card"] = {"error": str(e)}

    return result


@router.get("/api/admin")
async def get_admin():
    return await asyncio.to_thread(_build_admin_data)


@router.post("/api/admin/credit-card/purchase")
async def record_purchase(req: PurchaseRequest):
    """月次クレカ積立を記録（credit_card_plans.json 更新）"""
    def _do():
        from credit_card_investment import record_monthly_purchase
        result = record_monthly_purchase(
            person=req.person,
            amount=req.amount,
            nav=req.nav,
            purchase_date=req.purchase_date,
        )
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return {"ok": True, "account": result}
    return await asyncio.to_thread(_do)


@router.post("/api/admin/credit-card/nav")
async def update_nav(req: NavUpdateRequest):
    """NAV（基準価額）を更新（評価額再計算用）"""
    def _do():
        from credit_card_investment import update_nav as _update_nav
        result = _update_nav(person=req.person, current_nav=req.current_nav)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return {"ok": True, "account": result}
    return await asyncio.to_thread(_do)


@router.post("/api/admin/credit-card/purchase/undo")
async def undo_purchase(req: UndoPurchaseRequest):
    """指定日の積立記録を取り消す（誤POST・二重送信からの復旧用）"""
    def _do():
        from credit_card_investment import remove_purchase
        result = remove_purchase(person=req.person, purchase_date=req.purchase_date)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return {"ok": True, **result}
    return await asyncio.to_thread(_do)
