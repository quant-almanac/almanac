"""
api/routes/performance.py — 整理 #6: 新メトリクスを API 経由で公開

エンドポイント:
  GET /api/twr?from=YYYY-MM-DD&to=YYYY-MM-DD
      → Modified Dietz TWR + benchmark + excess return
  GET /api/tax-lots?ticker=XXX
      → portfolio lot snapshot (全 ticker / 指定 ticker)
  GET /api/realized-pnl?year=2026
      → 年内の確定損益サマリ
  GET /api/policy-decisions
      → 直近 ai_portfolio_analysis.json の policy_decision (rejected actions / modifications)
  GET /api/ledger-events?from=YYYY-MM-DD&to=YYYY-MM-DD&type=trade
      → event_ledger の検索 (debug 用)
  GET /api/portfolio-integrity
      → account/holdings/executions/event_ledger の内部整合性監査
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))


# ────────────────────────────────────────────────────────
# TWR
# ────────────────────────────────────────────────────────

_OBJECTIVE_REQUIRED_DAYS = 365
_OBJECTIVE_EXCESS_PCT_MIN = 2.0
_OBJECTIVE_MAX_DD_PCT_LIMIT = -15.0


def _twr_failure(error: Exception) -> dict:
    return {
        "twr_pct": None,
        "benchmark_twr_pct": None,
        "excess_return_pct": None,
        "excess_suppressed_reason": "no_nav_data",
        "confirmed": False,
        "period_days_actual": 0,
        "error": str(error),
    }


def _build_objective_status(*, today: date | None = None, db_path: Path | None = None) -> dict:
    """365日クリーン期間に限定した投資目標の判定。"""
    from config_clean_baseline import clean_nav_since_iso
    from nav_recorder import compute_max_drawdown, modified_dietz_twr

    current = today or date.today()
    date_to = current.isoformat()
    date_from = (current - timedelta(days=_OBJECTIVE_REQUIRED_DAYS)).isoformat()
    clean_since = clean_nav_since_iso()
    kwargs = {
        "date_from": date_from,
        "date_to": date_to,
        "clean_since": clean_since,
        "min_clean_days": _OBJECTIVE_REQUIRED_DAYS,
        "db_path": db_path,
    }
    try:
        twr = modified_dietz_twr(**kwargs)
    except Exception as exc:
        twr = _twr_failure(exc)
    try:
        max_dd = compute_max_drawdown(**kwargs)
    except Exception as exc:
        max_dd = {
            "dd_pct": None,
            "confirmed": False,
            "period_days_actual": 0,
            "error": str(exc),
        }

    twr_days = int(twr.get("period_days_actual") or 0)
    dd_days = int(max_dd.get("period_days_actual") or 0)
    clean_days = min(twr_days, dd_days) if twr_days and dd_days else max(twr_days, dd_days)
    both_confirmed = bool(twr.get("confirmed") and max_dd.get("confirmed"))
    excess = twr.get("excess_return_pct")
    dd_pct = max_dd.get("dd_pct")
    if not both_confirmed:
        judgment = "pending"
    elif isinstance(excess, (int, float)) and isinstance(dd_pct, (int, float)) and excess >= _OBJECTIVE_EXCESS_PCT_MIN and dd_pct >= _OBJECTIVE_MAX_DD_PCT_LIMIT:
        judgment = "met"
    else:
        judgment = "not_met"

    return {
        "as_of": date_to,
        "twr": twr,
        "max_dd_12m": max_dd,
        "judgment": judgment,
        "clean_days": clean_days,
        "required_days": _OBJECTIVE_REQUIRED_DAYS,
        "clean_since": clean_since,
        "thresholds": {
            "excess_pct_min": _OBJECTIVE_EXCESS_PCT_MIN,
            "max_dd_pct_limit": _OBJECTIVE_MAX_DD_PCT_LIMIT,
        },
    }


@router.get("/api/objective-status")
async def get_objective_status():
    return _build_objective_status()

@router.get("/api/twr")
async def get_twr(
    date_from: str = Query(..., alias="from"),
    date_to:   str = Query(..., alias="to"),
):
    """期間 TWR + benchmark + excess を返す。"""
    try:
        from nav_recorder import modified_dietz_twr
        r = modified_dietz_twr(date_from=date_from, date_to=date_to)
        return r
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        return {"error": str(e), "twr_pct": None}


# ────────────────────────────────────────────────────────
# Tax lots
# ────────────────────────────────────────────────────────

@router.get("/api/tax-lots")
async def get_tax_lots(ticker: Optional[str] = None):
    """全銘柄の open lots、または指定 ticker のみ。"""
    try:
        from tax_lot import portfolio_lot_snapshot, build_lots
        if ticker:
            state = build_lots(ticker)
            return {
                "ticker": ticker,
                "open_lots": [
                    {
                        "lot_id":             l.lot_id,
                        "purchase_date":      l.purchase_date,
                        "remaining_qty":      l.remaining_qty,
                        "cost_per_share":     l.cost_per_share,
                        "cost_per_share_jpy": l.cost_per_share_jpy,
                        "currency":           l.currency,
                        "account":            l.account,
                    }
                    for l in state.open_lots if l.is_open
                ],
                "realized_trade_count": len(state.realized_trades),
            }
        return portfolio_lot_snapshot()
    except Exception as e:
        return {"error": str(e)}


@router.get("/api/realized-pnl")
async def get_realized_pnl(year: int = Query(..., ge=2000, le=2100)):
    """年内の確定損益サマリ。"""
    try:
        from tax_lot import realized_pnl_in_year
        return realized_pnl_in_year(year)
    except Exception as e:
        return {"error": str(e), "year": year}


# ────────────────────────────────────────────────────────
# Policy Engine decisions
# ────────────────────────────────────────────────────────

@router.get("/api/policy-decisions")
async def get_policy_decisions():
    """
    直近 ai_portfolio_analysis.json の synthesis.policy_decision を返す。
    Policy Engine で reject / modify された AI 提案を audit する用。
    """
    path = BASE_DIR / "ai_portfolio_analysis.json"
    if not path.exists():
        return {"error": "ai_portfolio_analysis.json not found (analyzer 未実行)"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"parse error: {e}"}
    synthesis = data.get("synthesis") or {}
    pd_ = synthesis.get("policy_decision") or {}
    return {
        "as_of":          data.get("as_of"),
        "policy_decision": pd_,
        "accepted_count":  pd_.get("accepted_count", 0),
        "rejected_count":  pd_.get("rejected_count", 0),
        "modified_count":  pd_.get("modified_count", 0),
    }


# ────────────────────────────────────────────────────────
# Ledger events (debug)
# ────────────────────────────────────────────────────────

@router.get("/api/ledger-events")
async def get_ledger_events(
    date_from: Optional[str] = Query(None, alias="from"),
    date_to:   Optional[str] = Query(None, alias="to"),
    type:      Optional[str] = Query(None, description="trade | cash_flow | dividend | tax | fee | fx_conversion | split | merge | nisa_use"),
    ticker:    Optional[str] = None,
    limit:     int = Query(200, ge=1, le=1000),
):
    """event_ledger を期間・種別で検索 (audit 用)。"""
    try:
        from event_ledger import query_events
        events = query_events(
            date_from=date_from,
            date_to=date_to,
            types=[type] if type else None,
            ticker=ticker,
        )
        return {"events": events[-limit:], "total": len(events)}
    except Exception as e:
        return {"error": str(e), "events": []}


# ────────────────────────────────────────────────────────
# Portfolio integrity
# ────────────────────────────────────────────────────────

@router.get("/api/portfolio-integrity")
async def get_portfolio_integrity():
    """内部台帳の整合性監査結果を返す。"""
    try:
        from portfolio_integrity import run_integrity_check
        return run_integrity_check()
    except Exception as e:
        return {"ok": False, "error": str(e), "issues": []}
