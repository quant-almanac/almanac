"""Fail-closed Stage 7A/7B FX hedge shadow-policy orchestration.

This module is the wiring boundary between the instrument look-through
resolver and the target-ratio model. It never creates orders. Shadow state is
advanced only when every material position has an economic-currency
classification and the actual hedge notional is backed by a fresh broker
reconciliation record.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from fx_exposure import resolve_economic_exposure, summarize_fx_exposure
from fx_hedge_manager import (
    HEDGE_MODE_OFF,
    HEDGE_MODE_SHADOW,
    VALID_HEDGE_MODES,
    run_hedge_shadow,
)
from position_identity import position_identity_for_holding

BASE_DIR = Path(__file__).parent
MAX_ACTUAL_HEDGE_AGE_HOURS = 72.0


def _actual_state_path() -> Path:
    root = os.environ.get("ALMANAC_STATE_DIR")
    return (
        Path(root) / "fx_actual_hedge_state.json"
        if root
        else BASE_DIR / "fx_actual_hedge_state.json"
    )


def _load_actual_state(path: Optional[Path] = None) -> dict:
    try:
        value = json.loads((path or _actual_state_path()).read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _as_jst(value: datetime) -> datetime:
    jst = ZoneInfo("Asia/Tokyo")
    return value.replace(tzinfo=jst) if value.tzinfo is None else value.astimezone(jst)


def _instrument_kind(row: dict) -> Optional[bool]:
    """Return is_fund; None means the product kind is not authoritative."""
    if isinstance(row.get("is_fund"), bool):
        return bool(row["is_fund"])
    kind = str(row.get("asset_type") or row.get("instrument_type") or "").lower()
    if kind in {"fund", "etf", "mutual_fund", "investment_trust", "etn"}:
        return True
    if kind in {"stock", "equity", "common_stock", "cash"}:
        return False
    return None


def _actual_notional_contract(
    state: dict,
    *,
    now: datetime,
) -> tuple[Optional[float], list[str], Optional[str]]:
    issues = []
    for key in (
        "observed_actual_hedge_notional_jpy",
        "broker_source",
        "source_as_of",
        "reconciliation_snapshot_hash",
    ):
        if state.get(key) in (None, ""):
            issues.append(f"actual_hedge_{key}_missing")
    try:
        amount = float(state.get("observed_actual_hedge_notional_jpy"))
        if amount < 0:
            raise ValueError
    except (TypeError, ValueError):
        amount = None
        if "actual_hedge_observed_actual_hedge_notional_jpy_missing" not in issues:
            issues.append("actual_hedge_notional_invalid")
    source_as_of = None
    try:
        source_as_of = datetime.fromisoformat(
            str(state.get("source_as_of")).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        if state.get("source_as_of") not in (None, ""):
            issues.append("actual_hedge_source_as_of_invalid")
    if source_as_of is not None:
        age_hours = max(
            0.0,
            (_as_jst(now) - _as_jst(source_as_of)).total_seconds() / 3600,
        )
        if age_hours > MAX_ACTUAL_HEDGE_AGE_HOURS:
            issues.append("actual_hedge_state_stale")
    return amount, sorted(set(issues)), (
        _as_jst(source_as_of).isoformat() if source_as_of is not None else None
    )


def evaluate_portfolio_hedge_shadow(
    positions: list[dict],
    *,
    regime: str,
    vix: float,
    usdjpy: float,
    actual_state: Optional[dict] = None,
    mode: str = HEDGE_MODE_SHADOW,
    now: Optional[datetime] = None,
    decision_snapshot_hash: Optional[str] = None,
    usdjpy_iv_1m: float = 0.10,
) -> dict:
    """Evaluate and, only when fully evidenced, persist the shadow target."""
    if mode not in VALID_HEDGE_MODES:
        raise ValueError(f"invalid FX hedge mode: {mode}")
    if mode == HEDGE_MODE_OFF:
        return {"mode": mode, "status": "off", "state_saved": False}

    now = now or datetime.now(ZoneInfo("Asia/Tokyo"))
    exposures = []
    issues: list[str] = []
    for index, row in enumerate(positions or []):
        if not isinstance(row, dict):
            continue
        try:
            value = float(row.get("value_jpy") or row.get("market_value_jpy") or 0)
        except (TypeError, ValueError):
            value = 0.0
        if value <= 0:
            continue
        identity = position_identity_for_holding(row, key=str(row.get("key") or index))
        if identity is None:
            issues.append(f"position_identity_unknown:{row.get('ticker') or index}")
            continue
        exposure = resolve_economic_exposure(
            position=identity,
            market_value_jpy=value,
            listing_currency=str(row.get("currency") or ""),
            is_fund=_instrument_kind(row),
            now=now,
        )
        exposures.append(exposure)
        if exposure.exposure_source == "unknown":
            issues.append(f"economic_exposure_unknown:{identity.canonical_instrument_id}")

    summary = summarize_fx_exposure(exposures)
    if summary.unknown_tickers:
        issues.extend(
            f"economic_exposure_unknown:{ticker}"
            for ticker in summary.unknown_tickers
        )
    observed_actual, actual_issues, actual_as_of = _actual_notional_contract(
        actual_state if actual_state is not None else _load_actual_state(),
        now=now,
    )
    issues.extend(actual_issues)
    result = {
        "mode": mode,
        "status": "review" if issues else "eligible_for_shadow",
        "state_saved": False,
        "decision_snapshot_hash": decision_snapshot_hash,
        "exposure_summary": asdict(summary),
        "observed_actual_hedge_notional_jpy": observed_actual,
        "observed_actual_as_of": actual_as_of,
        "issues": sorted(set(issues)),
    }
    if issues or observed_actual is None:
        return result

    gross = float(summary.gross_fx_exposure_usd_jpy)
    if gross <= 0:
        result["status"] = "review"
        result["issues"] = ["gross_usd_exposure_non_positive"]
        return result
    shadow = run_hedge_shadow(
        regime,
        vix,
        usdjpy,
        mode=mode,
        now=now,
        usdjpy_iv_1m=usdjpy_iv_1m,
        snapshot_hash=decision_snapshot_hash,
    )
    target_notional = gross * float(shadow["target_hedge_ratio"])
    result.update({
        "status": "shadow_recorded",
        "state_saved": True,
        "target_hedge_notional_jpy": round(target_notional),
        "shadow_proposed_hedge_notional_jpy": round(target_notional),
        "proposed_delta_notional_jpy": round(target_notional - observed_actual),
        "target_ratio": shadow["target_hedge_ratio"],
        "target_model": shadow,
    })
    return result
