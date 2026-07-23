"""
GET /api/nisa — NISA口座管理データ
"""
import json
import sys
from pathlib import Path
from fastapi import APIRouter

router = APIRouter()
BASE_DIR = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE_DIR))


@router.get("/api/nisa")
async def get_nisa():
    try:
        path = BASE_DIR / "nisa_portfolio.json"
        if not path.exists():
            return {"error": "nisa_portfolio.json not found"}
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        holdings_path = BASE_DIR / "holdings.json"
        holdings = {}
        if holdings_path.exists():
            holdings = json.loads(holdings_path.read_text(encoding="utf-8"))

        # メイン・サブの枠使用率を計算して補足情報を追加
        for person_key in ("husband", "wife"):
            p = data.get(person_key, {})
            ts_limit = p.get("tsumitate_limit_annual", 1200000)
            ts_used  = p.get("tsumitate_used_this_year", 0)
            ts_planned = p.get("tsumitate_planned_this_year", 0)
            gr_limit = p.get("growth_limit_annual", 2400000)
            gr_used  = p.get("growth_used_this_year", 0)
            gr_planned = p.get("growth_planned_this_year", 0)
            lt_limit = p.get("lifetime_limit", 18000000)
            lt_used  = p.get("lifetime_used_estimate", 0)

            p["tsumitate_planned"]        = ts_planned
            p["growth_planned"]           = gr_planned
            p["tsumitate_remaining_before_planned"] = max(0, ts_limit - ts_used)
            p["growth_remaining_before_planned"]    = max(0, gr_limit - gr_used)
            p["tsumitate_remaining"]      = max(0, ts_limit - ts_used - ts_planned)
            p["growth_remaining"]         = max(0, gr_limit - gr_used - gr_planned)
            p["lifetime_remaining"]       = lt_limit - lt_used
            p["tsumitate_used_pct"]       = round(ts_used / ts_limit * 100, 1) if ts_limit else 0
            p["growth_used_pct"]          = round(gr_used / gr_limit * 100, 1) if gr_limit else 0
            p["tsumitate_committed_pct"]  = round((ts_used + ts_planned) / ts_limit * 100, 1) if ts_limit else 0
            p["growth_committed_pct"]     = round((gr_used + gr_planned) / gr_limit * 100, 1) if gr_limit else 0
            p["lifetime_used_pct"]        = round(lt_used / lt_limit * 100, 1) if lt_limit else 0

            # 保有銘柄の含み損益サマリー
            total_value = 0
            total_cost  = 0
            for h in p.get("holdings", {}).values():
                v = h.get("value", 0)
                c = h.get("cost_basis_estimate", h.get("cost_basis", 0))
                total_value += v
                total_cost  += c
            p["holdings_total_value"]     = total_value
            p["holdings_total_cost"]      = total_cost
            p["holdings_total_unrealized"]= total_value - total_cost

        from nisa_allocator import build_placement_proposals
        data["placement_proposals"] = build_placement_proposals(data, holdings)
        try:
            from nisa_migration_planner import build_migration_plan
            from tax_lot import portfolio_lot_snapshot
            lots = portfolio_lot_snapshot().get("lots", {})
            data["migration_plan"] = build_migration_plan(
                nisa_data=data,
                holdings=holdings,
                lots_by_ticker=lots,
                years=3,
            )
        except Exception as exc:
            data["migration_plan"] = {
                "human_execution_only": True,
                "display_only": True,
                "moves": [],
                "error": str(exc),
            }
        data["placement_status"] = "display_only"
        return data
    except Exception as e:
        return {"error": str(e)}
