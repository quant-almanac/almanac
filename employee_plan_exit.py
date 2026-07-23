"""Fail-safe employee share-plan concentration exit proposals."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent
DEFAULT_LIMIT_PCT = 0.08


def load_insider_window(path: Path | str = BASE_DIR / "insider_window.json") -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {"allowed_windows": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"allowed_windows": []}
    return raw if isinstance(raw, dict) else {"allowed_windows": []}


def allowed_window(
    as_of: date,
    *,
    config: dict[str, Any],
) -> tuple[bool, str | None]:
    windows = config.get("allowed_windows") or []
    for window in windows:
        if not isinstance(window, dict):
            continue
        try:
            start = date.fromisoformat(str(window.get("start")))
            end = date.fromisoformat(str(window.get("end")))
        except ValueError:
            continue
        if start <= as_of <= end:
            return True, str(window.get("note") or "")
    return False, None


def build_exit_proposal(
    *,
    portfolio_total_jpy: float,
    current_price_jpy: float,
    current_shares: float,
    purchase_history: list[dict[str, Any]],
    as_of: date | None = None,
    limit_pct: float = DEFAULT_LIMIT_PCT,
    window_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    today = as_of or date.today()
    window_open, window_note = allowed_window(
        today,
        config=window_config or {"allowed_windows": []},
    )
    base = {
        "as_of": today.isoformat(),
        "human_execution_only": True,
        "contribution_continues": True,
        "incentive_rate_assumption": 0.10,
        "limit_pct": limit_pct,
        "window_open": window_open,
        "window_note": window_note,
        "proposal": None,
    }
    if not window_open:
        return {**base, "status": "blocked", "reason": "insider_window_not_open_or_not_configured"}
    if portfolio_total_jpy <= 0 or current_price_jpy <= 0 or current_shares <= 0:
        return {**base, "status": "no_proposal", "reason": "insufficient_position_data"}

    current_value = current_price_jpy * current_shares
    excess_value = max(0.0, current_value - portfolio_total_jpy * limit_pct)
    if excess_value <= 0:
        return {
            **base,
            "status": "no_proposal",
            "reason": "within_concentration_limit",
            "current_ratio": round(current_value / portfolio_total_jpy, 4),
        }

    shares_needed = min(current_shares, excess_value / current_price_jpy)
    remaining = shares_needed
    lots = []
    for index, purchase in enumerate(
        sorted(purchase_history, key=lambda row: str(row.get("date") or ""))
    ):
        available = float(purchase.get("shares") or 0)
        take = min(available, remaining)
        if take <= 0:
            continue
        lots.append({
            "lot_id": purchase.get("lot_id") or f"employee-plan:{index}",
            "purchase_date": purchase.get("date"),
            "quantity": round(take, 6),
            "cost_per_share_jpy": purchase.get("price"),
        })
        remaining -= take
        if remaining <= 1e-9:
            break
    if remaining > 1e-9:
        lots.append({
            "lot_id": "employee-plan:unallocated",
            "purchase_date": None,
            "quantity": round(remaining, 6),
            "cost_per_share_jpy": None,
        })
    return {
        **base,
        "status": "proposal",
        "current_ratio": round(current_value / portfolio_total_jpy, 4),
        "proposal": {
            "sell_shares": round(shares_needed, 6),
            "sell_value_jpy": round(shares_needed * current_price_jpy, 0),
            "selection": "oldest_lots_first",
            "lots": lots,
            "reason": "employee_share_plan_concentration_above_limit",
            "human_execution_only": True,
        },
    }
