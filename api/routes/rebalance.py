"""
GET /api/rebalance
コア（long）ポジションのみでリバランス分析を実行

H1: コアスナップショットの再集計ロジックは rebalance_engine.build_core_snapshot()
に一元化されている（calculate_rebalance_actions() も同じヘルパーを使う）。
"""
import asyncio
from fastapi import APIRouter

router = APIRouter()


def _calc_rebalance() -> dict:
    try:
        from api.routes.portfolio import get_cached_snapshot
        from rebalance_engine import (
            CURRENCY_TARGETS,
            analyze_currency_balance,
            analyze_sector_balance,
            build_core_snapshot,
        )

        snapshot = get_cached_snapshot()
        if "error" in snapshot and not snapshot.get("positions"):
            return {"error": snapshot["error"], "action_plan": []}

        core_snap = build_core_snapshot(snapshot)

        # AI 動的外貨方針を解決 (basis=long_tier・未期限切れのみ)。無効なら static に fail-closed。
        currency_targets = CURRENCY_TARGETS
        currency_policy_meta = {"source": "static_fallback", "reason": "未解決"}
        try:
            import currency_policy
            currency_targets, currency_policy_meta = currency_policy.resolve_effective_targets(
                static=CURRENCY_TARGETS)
        except Exception as _e:
            currency_policy_meta = {"source": "static_fallback", "reason": f"resolve 失敗: {_e}"}

        currency = analyze_currency_balance(core_snap, targets=currency_targets)
        sector = analyze_sector_balance(core_snap)

        # アクションを統合して優先順位でソート
        all_actions = sorted(
            currency.get("actions", []) + sector.get("actions", []),
            key=lambda x: x.get("priority", 99),
        )

        return {
            "currency": {
                "status": currency.get("status", "ok"),
                "data": currency.get("currencies", {}),
                "actions": currency.get("actions", []),
            },
            "sector": {
                "status": sector.get("status", "ok"),
                "data": sector.get("sectors", {}),
                "actions": sector.get("actions", []),
            },
            "action_plan": all_actions,
            "core_total_jpy": core_snap.get("total_jpy", 0),
            "core_position_count": len(core_snap.get("positions", [])),
            "currency_policy": currency_policy_meta,
        }
    except Exception as e:
        return {"error": str(e), "action_plan": []}


@router.get("/api/rebalance")
async def get_rebalance():
    return await asyncio.to_thread(_calc_rebalance)
