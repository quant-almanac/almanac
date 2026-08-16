"""Deterministic final allocator for ordinary risk-increasing actions.

The analyst produces candidates and evidence.  This module is deliberately
small and deterministic: it selects at most one normal buy per run, enforces
the normal ¥250k ceiling, and records why every ready candidate was or was
not selected.  It never creates a new candidate, wallet, or account route.
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from typing import Any

from utils import atomic_write_json, load_json

BASE_DIR = Path(__file__).parent
COMPARISON_FILE = "capital_allocator_comparisons.json"
NORMAL_BUY_TYPES = {"buy", "add", "dca"}
NORMAL_ACTION_CAP_JPY = 250_000


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _direction(action: dict[str, Any]) -> str:
    action_type = str(action.get("type") or "").strip().lower()
    if action_type in {"buy", "add", "dca", "margin_buy"}:
        return "buy"
    if action_type in {"sell", "trim", "reduce", "stop_loss", "take_profit"}:
        return "sell"
    if action_type == "short":
        return "short"
    if action_type == "cover":
        return "cover"
    return "neutral"


def _normal_buy(action: dict[str, Any]) -> bool:
    if str(action.get("type") or "").lower() not in NORMAL_BUY_TYPES:
        return False
    source = str(action.get("source") or "").lower()
    tier = str(action.get("tier") or "").lower()
    return not source.startswith("scenario") and tier != "swing"


def _estimated_notional_jpy(action: dict[str, Any], *, fx_rate: float) -> float:
    value = _number(action.get("estimated_notional_jpy"), default=-1)
    if value >= 0:
        return value
    quantity = _number(action.get("quantity", action.get("requested_buy_quantity")), default=0)
    price = _number(action.get("decision_price", action.get("limit_price")), default=0)
    if quantity <= 0 or price <= 0:
        return 0.0
    currency = str(action.get("currency") or "").upper()
    return quantity * price * (fx_rate if currency == "USD" else 1.0)


def _ranking_key(action: dict[str, Any]) -> tuple:
    """Fixed ranking order; uncalibrated return claims are intentionally absent."""
    objective_match = int(bool(action.get("execution_plan_item_id") or action.get("execution_plan_objective_match")))
    calibrated_excess = _number(action.get("calibrated_after_cost_excess_return_bps"), default=0.0)
    concentration = _number(action.get("concentration_improvement_score"), default=0.0)
    sector = _number(action.get("sector_improvement_score"), default=0.0)
    currency = _number(action.get("currency_improvement_score"), default=0.0)
    correlation = _number(action.get("correlation_improvement_score"), default=0.0)
    # Lower marginal CVaR / volatility is preferable; missing data remains neutral.
    marginal_cvar = _number(action.get("marginal_cvar"), default=0.0)
    volatility = _number(action.get("marginal_volatility"), default=0.0)
    screener = _number(action.get("fallback_source_score", action.get("screener_score")), default=0.0)
    confidence = _number(action.get("confidence_pct"), default=0.0)
    ticker = str(action.get("ticker") or "")
    return (
        -objective_match,
        -calibrated_excess,
        -(concentration + sector + currency + correlation),
        marginal_cvar,
        volatility,
        -screener,
        -confidence,
        ticker,
    )


def _append_reason(action: dict[str, Any], code: str, message: str) -> None:
    reasons = action.setdefault("execution_block_reasons", [])
    if not isinstance(reasons, list):
        reasons = []
        action["execution_block_reasons"] = reasons
    if not any(isinstance(row, dict) and row.get("code") == code for row in reasons):
        reasons.append({"code": code, "message": message})


def _cap_quantity(
    action: dict[str, Any],
    *,
    cap_jpy: int,
    min_trade_jpy: float,
    fx_rate: float,
) -> tuple[dict[str, Any], str | None]:
    """Keep an actionable order within cap without forcing a sub-minimum trade."""
    row = dict(action)
    estimated = _estimated_notional_jpy(row, fx_rate=fx_rate)
    if estimated <= cap_jpy:
        return row, None
    quantity = int(_number(row.get("quantity", row.get("requested_buy_quantity")), default=0))
    if quantity <= 0 or estimated <= 0:
        return row, "capital_allocator_notional_unresolved"
    unit_jpy = estimated / quantity
    resized = int(math.floor(cap_jpy / unit_jpy))
    if resized <= 0 or resized * unit_jpy < min_trade_jpy:
        return row, "capital_allocator_quantity_below_minimum"
    row["quantity"] = resized
    row["requested_buy_quantity"] = resized
    row["estimated_notional_jpy"] = round(resized * unit_jpy)
    row["capital_allocator_resized_from_quantity"] = quantity
    row["capital_allocator_resized_reason"] = "normal_action_cap_jpy"
    return row, None


def allocate_actions(
    actions: list[dict[str, Any]],
    *,
    mode: str = "enforce",
    fx_rate: float = 150.0,
    min_trade_jpy: float = 150_000,
    normal_action_cap_jpy: int = NORMAL_ACTION_CAP_JPY,
    prior_normal_buys_today: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Allocate final ordinary-buy capacity without bypassing existing gates."""
    copied = [dict(action) for action in actions if isinstance(action, dict)]
    enabled = str(mode).lower() == "enforce"
    candidates = [
        action for action in copied
        if _normal_buy(action) and action.get("execution_readiness") == "ready"
    ]
    candidates.sort(key=_ranking_key)
    selected_ticker: str | None = None
    selected: dict[str, Any] | None = None
    exclusions: list[dict[str, Any]] = []

    if enabled and prior_normal_buys_today < 1:
        for candidate in candidates:
            resized, failure = _cap_quantity(
                candidate,
                cap_jpy=int(normal_action_cap_jpy),
                min_trade_jpy=float(min_trade_jpy),
                fx_rate=float(fx_rate),
            )
            if failure:
                candidate.update(resized)
                candidate["execution_readiness"] = "review"
                _append_reason(candidate, failure, "通常枠の上限内では最低取引額を満たせません")
                exclusions.append({"ticker": candidate.get("ticker"), "reason": failure})
                continue
            candidate.update(resized)
            selected = candidate
            selected_ticker = str(candidate.get("ticker") or "")
            candidate["capital_allocator_selected"] = True
            break
    elif enabled and candidates:
        for candidate in candidates:
            candidate["execution_readiness"] = "review"
            _append_reason(candidate, "capital_allocator_daily_buy_limit", "通常買付は1日1件までです")
            exclusions.append({"ticker": candidate.get("ticker"), "reason": "capital_allocator_daily_buy_limit"})

    if enabled:
        for candidate in candidates:
            if candidate is selected or candidate.get("execution_readiness") != "ready":
                continue
            candidate["execution_readiness"] = "review"
            _append_reason(candidate, "capital_allocator_daily_buy_limit", "本日の通常買付枠は上位候補に配分しました")
            exclusions.append({"ticker": candidate.get("ticker"), "reason": "capital_allocator_daily_buy_limit"})

    legacy_ready = [str(action.get("ticker") or "") for action in candidates]
    return copied, {
        "mode": "enforce" if enabled else "legacy",
        "normal_action_cap_jpy": int(normal_action_cap_jpy),
        "prior_normal_buys_today": int(prior_normal_buys_today),
        "candidate_count": len(candidates),
        "legacy_ready_tickers": legacy_ready,
        "selected_ticker": selected_ticker,
        "selected_count": 1 if selected is not None else 0,
        "exclusions": exclusions,
        "explainability": "structured_route_cap_nisa_concentration_objective",
    }


def comparison_path(base_dir: Path = BASE_DIR) -> Path:
    return Path(base_dir) / COMPARISON_FILE


def record_comparison(run_id: str, comparison: dict[str, Any], *, base_dir: Path = BASE_DIR) -> dict[str, Any]:
    """Persist the first-five-run human review record without financial side effects."""
    path = comparison_path(base_dir)
    state = load_json(path, default={}) or {}
    rows = state.get("runs") if isinstance(state, dict) else []
    rows = rows if isinstance(rows, list) else []
    row = {"run_id": str(run_id), "recorded_at": datetime.now().isoformat(timespec="seconds"), **comparison}
    rows = [item for item in rows if isinstance(item, dict) and item.get("run_id") != str(run_id)]
    rows.append(row)
    state = {"schema_version": 1, "runs": rows[-20:]}
    atomic_write_json(path, state)
    return row


def review_comparison(run_id: str, decision: str, *, base_dir: Path = BASE_DIR) -> dict[str, Any] | None:
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    path = comparison_path(base_dir)
    state = load_json(path, default={}) or {}
    rows = state.get("runs") if isinstance(state, dict) else []
    if not isinstance(rows, list):
        return None
    for row in rows:
        if isinstance(row, dict) and row.get("run_id") == str(run_id):
            row["review"] = {"decision": decision, "reviewed_at": datetime.now().isoformat(timespec="seconds")}
            atomic_write_json(path, {"schema_version": 1, "runs": rows})
            return row
    return None
