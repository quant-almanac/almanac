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

from action_amounts import synchronize_resized_action_prose
from instrument_metadata import broad_execution_metadata, canonical_ticker, quantity_label_for_ticker, trading_unit_for_ticker
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
    return not source.startswith("scenario") and source != "scheduled_broad_deployment" and tier != "swing"


def _scheduled_broad_buy(action: dict[str, Any]) -> bool:
    return str(action.get("source") or "").lower() == "scheduled_broad_deployment" and str(action.get("type") or "").lower() in NORMAL_BUY_TYPES


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


def _lot_size_for_action(action: dict[str, Any]) -> int:
    """Return the smallest executable quantity for this already-ready action."""
    ticker = canonical_ticker(action.get("ticker"))
    if not ticker.endswith(".T"):
        return 1
    channel = str(action.get("execution_channel") or action.get("broker_channel") or "").lower()
    if str(action.get("type") or "").lower() in {"buy", "add"} and channel == "rakuten_kabu_mini_open":
        return 1
    return max(1, int(trading_unit_for_ticker(ticker)))


def _resize_quantity_fields(
    row: dict[str, Any], *, quantity: int, resized: int,
    old_estimated_jpy: float, estimated_jpy: float,
) -> dict[str, Any]:
    """Synchronize structured and displayed values after a deterministic cap."""
    label = quantity_label_for_ticker(canonical_ticker(row.get("ticker")))
    old_hint = str(row.get("amount_hint") or f"{quantity}{label}")
    new_hint = f"{resized}{label}"
    out = dict(row)
    out["quantity"] = resized
    out["requested_buy_quantity"] = resized
    out["amount_hint"] = new_hint
    out["estimated_notional_jpy"] = round(estimated_jpy)
    out["capital_allocator_size_applied"] = {
        "quantity": {"from": quantity, "to": resized},
        "amount_hint": {"from": old_hint, "to": new_hint},
        "estimated_notional_jpy": {"from": round(old_estimated_jpy), "to": round(estimated_jpy)},
    }
    out, _ = synchronize_resized_action_prose(
        out, old_hint=old_hint, new_hint=new_hint,
        old_notional_jpy=old_estimated_jpy, new_notional_jpy=estimated_jpy,
    )
    return out


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
    lot_size = _lot_size_for_action(row)
    resized = int(math.floor(cap_jpy / unit_jpy))
    resized = (resized // lot_size) * lot_size
    if resized <= 0 or resized * unit_jpy < min_trade_jpy:
        return row, "capital_allocator_quantity_below_minimum"
    row = _resize_quantity_fields(
        row, quantity=quantity, resized=resized,
        old_estimated_jpy=estimated, estimated_jpy=resized * unit_jpy,
    )
    if row.get("action_quantity_sync_failed") or row.get("prose_numeric_sync_failed"):
        return row, "capital_allocator_quantity_sync_failed"
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
                message = {
                    "capital_allocator_quantity_below_minimum": "通常枠の上限内では最低取引額を満たせません",
                    "capital_allocator_quantity_sync_failed": "通常枠への数量縮小後、表示文を安全に同期できませんでした",
                    "capital_allocator_notional_unresolved": "通常枠を適用するための数量または金額を解決できませんでした",
                }.get(failure, "通常枠の確認が必要です")
                _append_reason(candidate, failure, message)
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


def allocate_scheduled_broad_actions(
    actions: list[dict[str, Any]], *, mode: str = "enforce", fx_rate: float = 150.0,
    min_trade_jpy: float = 150_000, max_actions_per_week: int = 2,
    max_single_jpy: int = 500_000, weekly_cap_jpy: int = 1_000_000,
    weekly_paced_remaining_jpy: int | None = None,
    prior_scheduled_actions_this_week: int = 0, prior_scheduled_notional_jpy: int = 0,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Apply an independent, human-executed scheduled broad batch contract."""
    copied = [dict(action) for action in actions if isinstance(action, dict)]
    enabled = str(mode).lower() == "enforce"
    candidates = [action for action in copied if _scheduled_broad_buy(action) and action.get("execution_readiness") == "ready"]
    candidates.sort(key=_ranking_key)
    weekly_cap = min(max(0, int(weekly_cap_jpy)), max(0, int(weekly_paced_remaining_jpy)) if weekly_paced_remaining_jpy is not None else max(0, int(weekly_cap_jpy)))
    weekly_cap = max(0, weekly_cap - max(0, int(prior_scheduled_notional_jpy)))
    selected: list[str] = []
    remaining, exclusions = weekly_cap, []
    if enabled:
        for candidate in candidates:
            if broad_execution_metadata(candidate.get("ticker")) is None:
                candidate["execution_readiness"] = "review"
                _append_reason(candidate, "scheduled_broad_metadata_unregistered", "scheduled broad対象は登録済みallowlistに限定します")
                exclusions.append({"ticker": candidate.get("ticker"), "reason": "scheduled_broad_metadata_unregistered"})
                continue
            if int(prior_scheduled_actions_this_week) + len(selected) >= max(0, int(max_actions_per_week)) or remaining <= 0:
                candidate["execution_readiness"] = "review"
                _append_reason(candidate, "scheduled_broad_weekly_limit", "今週のscheduled broad配備枠を使い切りました")
                exclusions.append({"ticker": candidate.get("ticker"), "reason": "scheduled_broad_weekly_limit"})
                continue
            cap = min(int(max_single_jpy), remaining)
            resized, failure = _cap_quantity(candidate, cap_jpy=cap, min_trade_jpy=float(min_trade_jpy), fx_rate=float(fx_rate))
            if failure:
                candidate.update(resized)
                candidate["execution_readiness"] = "review"
                _append_reason(candidate, failure, "scheduled broad枠では最低取引額またはlotを満たせません")
                exclusions.append({"ticker": candidate.get("ticker"), "reason": failure})
                continue
            candidate.update(resized)
            candidate["scheduled_broad_selected"] = True
            candidate["human_execution_only"] = True
            candidate["scheduled_broad_max_single_jpy"] = cap
            selected.append(str(candidate.get("ticker") or ""))
            remaining -= int(round(_estimated_notional_jpy(candidate, fx_rate=float(fx_rate))))
    return copied, {"mode": "enforce" if enabled else "legacy", "strategy_class": "scheduled_broad_deployment", "max_actions_per_week": int(max_actions_per_week), "prior_scheduled_actions_this_week": int(prior_scheduled_actions_this_week), "prior_scheduled_notional_jpy": int(prior_scheduled_notional_jpy), "max_single_jpy": int(max_single_jpy), "weekly_cap_jpy": weekly_cap, "weekly_remaining_jpy": max(0, remaining), "selected_tickers": selected, "selected_count": len(selected), "exclusions": exclusions}


def comparison_path(base_dir: Path = BASE_DIR) -> Path:
    return Path(base_dir) / COMPARISON_FILE


def _action_identity(action: dict[str, Any]) -> tuple[str, str, str, str, str]:
    return (
        canonical_ticker(action.get("ticker")), str(action.get("type") or "").lower(),
        str(action.get("execution_owner") or "").lower(), str(action.get("execution_broker") or "").lower(),
        str(action.get("execution_account") or "").lower(),
    )


def build_comparison(
    legacy_actions: list[dict[str, Any]], allocated_actions: list[dict[str, Any]], report: dict[str, Any], *,
    count_conservation_ok: bool | None,
) -> dict[str, Any]:
    """Describe whether allocator changes have a structured explanation."""
    reasons: list[str] = []
    legacy_by_id = {_action_identity(row): row for row in legacy_actions if isinstance(row, dict)}
    selected = [row for row in allocated_actions if isinstance(row, dict) and row.get("capital_allocator_selected")]
    if len(selected) > 1:
        reasons.append("multiple_allocator_selected_actions")
    for row in selected:
        original = legacy_by_id.get(_action_identity(row))
        if original is None:
            reasons.append("allocator_selected_outside_legacy_candidates")
            continue
        if not all(row.get(key) for key in ("execution_owner", "execution_broker", "execution_account")):
            reasons.append("allocator_selected_route_incomplete")
        original_quantity = _number(original.get("quantity", original.get("requested_buy_quantity")), default=0)
        selected_quantity = _number(row.get("quantity", row.get("requested_buy_quantity")), default=0)
        if selected_quantity != original_quantity:
            applied = row.get("capital_allocator_size_applied")
            if not (
                isinstance(applied, dict)
                and applied.get("quantity", {}).get("from") == int(original_quantity)
                and applied.get("quantity", {}).get("to") == int(selected_quantity)
                and str(row.get("amount_hint") or "").startswith(f"{int(selected_quantity)}")
                and not row.get("action_quantity_sync_failed")
                and not row.get("prose_numeric_sync_failed")
            ):
                reasons.append("allocator_quantity_change_unexplained")
    if count_conservation_ok is not True:
        reasons.append("decision_count_conservation_unverified")
    return {
        "mode": report.get("mode"), "legacy_ready_tickers": report.get("legacy_ready_tickers") or [],
        "allocator_selected_ticker": report.get("selected_ticker"), "exclusions": report.get("exclusions") or [],
        "explanation_status": "explainable" if not reasons else "unexplainable",
        "explanation_reasons": reasons, "normal_daily_buy_limit": 1,
        "count_conservation_ok": count_conservation_ok is True,
    }


def record_comparison(run_id: str, comparison: dict[str, Any], *, base_dir: Path = BASE_DIR) -> dict[str, Any]:
    """Persist the first-five-run human review record without financial side effects."""
    path = comparison_path(base_dir)
    state = load_json(path, default={}) or {}
    rows = state.get("runs") if isinstance(state, dict) else []
    rows = rows if isinstance(rows, list) else []
    row = {"run_id": str(run_id), "recorded_at": datetime.now().isoformat(timespec="seconds"), **comparison}
    rows = [item for item in rows if isinstance(item, dict) and item.get("run_id") != str(run_id)]
    rows.append(row)
    initial_rows = state.get("initial_review_runs") if isinstance(state, dict) else []
    initial_rows = initial_rows if isinstance(initial_rows, list) else []
    initial_rows = [item for item in initial_rows if isinstance(item, dict) and item.get("run_id") != str(run_id)]
    if len(initial_rows) < 5:
        initial_rows.append(dict(row))
    row["initial_review_window"] = any(item.get("run_id") == str(run_id) for item in initial_rows)
    state = {"schema_version": 2, "runs": rows[-20:], "initial_review_runs": initial_rows[:5]}
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
    reviewed: dict[str, Any] | None = None
    for row in rows:
        if isinstance(row, dict) and row.get("run_id") == str(run_id):
            row["review"] = {"decision": decision, "reviewed_at": datetime.now().isoformat(timespec="seconds")}
            reviewed = row
            break
    if reviewed is None:
        return None
    initial_rows = state.get("initial_review_runs") if isinstance(state, dict) else []
    if not isinstance(initial_rows, list):
        initial_rows = []
    for row in initial_rows:
        if isinstance(row, dict) and row.get("run_id") == str(run_id):
            row["review"] = dict(reviewed["review"])
    atomic_write_json(path, {"schema_version": 2, "runs": rows, "initial_review_runs": initial_rows})
    return reviewed
