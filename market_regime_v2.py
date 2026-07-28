"""Deterministic multi-level market-regime assessment.

The legacy scenario contract collapses the market into BULL/NEUTRAL/BEAR/
CRASH from two MA50 booleans and portfolio P&L.  This module keeps those
compatibility labels for downstream consumers, but derives them from a richer,
auditable state:

* per-market strength: strong bull / mild bull / neutral / mild bear /
  strong bear;
* phase: improving / stable / deteriorating;
* a separate shock overlay.  A shock never instructs the system to sell down
  to a higher cash target after prices have already fallen.

Every threshold below is frozen under ``POLICY_VERSION``.  Changing a
threshold requires a new version and walk-forward comparison; callers must not
move the acceptance line after seeing the candidate result.
"""
from __future__ import annotations

import json
import os
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

POLICY_VERSION = "market_regime_v2.1"
SCHEMA_VERSION = 1
MIN_COVERAGE = 0.70
MIN_BREADTH_OBSERVATIONS = 20
CONFIRMATION_DAYS = 2

LEVEL_NAMES = {
    2: "strong_bull",
    1: "mild_bull",
    0: "neutral",
    -1: "mild_bear",
    -2: "strong_bear",
}

POLICY_BY_LEVEL = {
    2: {
        "cash_target_pct": 3.0,
        "buy_size_multiplier": 1.0,
        "leverage_allowed": True,
        "new_buy_policy": "normal_to_aggressive",
    },
    1: {
        "cash_target_pct": 7.0,
        "buy_size_multiplier": 0.75,
        "leverage_allowed": False,
        "new_buy_policy": "normal",
    },
    0: {
        "cash_target_pct": 12.0,
        "buy_size_multiplier": 0.50,
        "leverage_allowed": False,
        "new_buy_policy": "selective",
    },
    -1: {
        "cash_target_pct": 20.0,
        "buy_size_multiplier": 0.25,
        "leverage_allowed": False,
        "new_buy_policy": "high_conviction_or_dca_only",
    },
    -2: {
        "cash_target_pct": 30.0,
        "buy_size_multiplier": 0.0,
        "leverage_allowed": False,
        "new_buy_policy": "dca_ladder_only",
    },
}

LEVEL_DISPLAY_NAMES = {
    2: "強い強気",
    1: "弱い強気",
    0: "中立",
    -1: "弱い弱気",
    -2: "強い弱気",
}

LEVEL_ACTIONS = {
    2: [
        "現金比率3%を目安に、通常から積極的な買付を許容",
        "新規buyは通常サイズ（1.00x）を上限",
        "信用買いは他のVaR・DD・鮮度ゲートを満たす場合だけ許容",
    ],
    1: [
        "現金比率7%を目安に維持",
        "新規buyは通常サイズの0.75xを上限",
        "レバレッジを使わず、既存の強いシグナルを優先",
    ],
    0: [
        "現金比率12%を目安に維持",
        "新規buyは通常サイズの0.50xを上限",
        "高確信度・分散改善に寄与する候補へ選別",
    ],
    -1: [
        "現金比率20%を目安に維持",
        "新規buyは通常サイズの0.25xを上限",
        "高確信度または有効なDCA tranche以外は見送る",
    ],
    -2: [
        "現金比率30%を目安に段階調整",
        "裁量的な新規buyを止め、activeなDCA trancheだけを再評価",
        "信用買い・新規レバレッジは禁止",
    ],
}

SHOCK_POLICY = {
    "cash_target_pct": 30.0,
    "cash_action": "hold_or_deploy_existing_cash",
    "raise_cash_to_target": False,
    "buy_size_multiplier": 0.0,
    "leverage_allowed": False,
    "new_buy_policy": "active_dca_tranche_only",
}


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _nested_number(data: dict, *path: str) -> Optional[float]:
    current: Any = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return _number(current)


def _clip(value: float, lower: float = -1.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _distance_points(value: Optional[float], *, full_scale: float, weight: float) -> Optional[float]:
    if value is None:
        return None
    return round(_clip(value / full_scale) * weight, 4)


def _breadth_points(value: Optional[float], *, weight: float) -> Optional[float]:
    if value is None:
        return None
    return round(_clip((value - 50.0) / 30.0) * weight, 4)


def _vix_points(vix: Optional[float]) -> Optional[float]:
    if vix is None:
        return None
    return round(_clip((25.0 - vix) / 15.0) * 15.0, 4)


def _credit_points(hy_oas_bps: Optional[float]) -> Optional[float]:
    if hy_oas_bps is None:
        return None
    return round(_clip((400.0 - hy_oas_bps) / 200.0) * 15.0, 4)


def _rate_assessment(macro: dict) -> dict:
    """Separate a tightening shock from supportive/stress-driven rate falls."""
    nominal_yield = _number(macro.get("yield_10y"))
    nominal_5d = _number(macro.get("yield_10y_change_5d_bps"))
    nominal_20d = _number(macro.get("yield_10y_change_20d_bps"))
    real_yield = _number(macro.get("real_yield_10y"))
    real_20d = _number(macro.get("real_yield_10y_change_20d_bps"))
    breakeven = _number(macro.get("breakeven_10y"))
    hy_oas = _number(macro.get("hy_oas_bps"))
    curve_10y3m = _number(
        macro.get(
            "yield_spread_10y_3m",
            macro.get(
                "yield_curve_spread_10y_3m",
                macro.get("yield_spread_10y3m"),
            ),
        )
    )

    observed = [
        value for value in (
            nominal_yield,
            nominal_5d,
            nominal_20d,
            real_yield,
            real_20d,
            breakeven,
            curve_10y3m,
        )
        if value is not None
    ]
    if not observed:
        return {
            "status": "unknown",
            "scope": "US_rates_as_global_equity_discount_modifier",
            "points": None,
            "inputs": {},
        }

    points = 0.0
    status = "stable"
    tightening = (
        (nominal_5d is not None and nominal_5d >= 25.0)
        or (nominal_20d is not None and nominal_20d >= 50.0)
        or (real_20d is not None and real_20d >= 25.0)
    )
    easing = (
        (nominal_5d is not None and nominal_5d <= -25.0)
        or (nominal_20d is not None and nominal_20d <= -50.0)
    )

    if tightening:
        status = "tightening_shock"
        points -= 7.0
        if (
            real_yield is not None
            and real_yield >= 2.0
            and real_20d is not None
            and real_20d > 0
        ):
            points -= 3.0
    elif easing and hy_oas is not None and hy_oas >= 400.0:
        status = "easing_stress"
        points -= 5.0
    elif easing:
        status = "easing_support"
        points += 5.0

    # A high but stable discount rate still matters for equity valuation; do
    # not treat only the latest move as information.
    restrictive_level = False
    if real_yield is not None and real_yield >= 2.0:
        points -= 2.0
        restrictive_level = True
        if real_yield >= 2.5:
            points -= 1.0
    if nominal_yield is not None and nominal_yield >= 5.0:
        points -= 1.0
        restrictive_level = True
    if breakeven is not None and breakeven >= 3.0:
        points -= 1.0
        restrictive_level = True
    if restrictive_level and status == "stable":
        status = "restrictive_level"

    if curve_10y3m is not None and curve_10y3m < 0:
        points -= 3.0
        if status == "stable":
            status = "curve_inverted"

    return {
        "status": status,
        "scope": "US_rates_as_global_equity_discount_modifier",
        "points": round(_clip(points / 10.0) * 10.0, 4),
        "inputs": {
            "yield_10y": nominal_yield,
            "yield_10y_change_5d_bps": nominal_5d,
            "yield_10y_change_20d_bps": nominal_20d,
            "real_yield_10y": real_yield,
            "real_yield_10y_change_20d_bps": real_20d,
            "breakeven_10y": breakeven,
            "yield_spread_10y_3m": curve_10y3m,
        },
    }


def _level_from_score(score: float, *, risk_veto: bool) -> int:
    if score >= 45.0 and not risk_veto:
        return 2
    if score >= 15.0:
        return 1
    if score > -15.0:
        return 0
    if score > -45.0:
        return -1
    return -2


def _market_inputs(market_meta: dict, market: str) -> dict:
    prefix = "sp500" if market == "US" else "nikkei"
    breadth_key = "us" if market == "US" else "jp"
    breadth = market_meta.get("breadth") if isinstance(market_meta.get("breadth"), dict) else {}
    market_breadth = breadth.get(breadth_key) if isinstance(breadth.get(breadth_key), dict) else {}
    return {
        "index_vs_ma50_pct": _number(market_meta.get(f"{prefix}_vs_ma50_pct")),
        "index_vs_ma200_pct": _number(market_meta.get(f"{prefix}_vs_ma200_pct")),
        "breadth_above_ma50_pct": _number(market_breadth.get("above_ma50_pct")),
        "breadth_above_ma200_pct": _number(market_breadth.get("above_ma200_pct")),
        "breadth_observed_50": int(market_breadth.get("eligible_ma50") or 0),
        "breadth_observed_200": int(market_breadth.get("eligible_ma200") or 0),
    }


def classify_market(
    market: str,
    *,
    market_meta: dict,
    macro: dict,
    vix_state: Optional[dict] = None,
) -> dict:
    """Return a pure, un-smoothed assessment for one equity market."""
    inputs = _market_inputs(market_meta, market)
    vix = _number(market_meta.get("vix"))
    if vix is None:
        vix = _number(macro.get("vix"))
    hy_oas = _number(macro.get("hy_oas_bps"))
    if hy_oas is None and isinstance(vix_state, dict):
        hy_oas = _number(vix_state.get("hy_spread_bps"))

    rate = _rate_assessment(macro)
    components = {
        "index_vs_ma50": {
            "weight": 15.0,
            "value": inputs["index_vs_ma50_pct"],
            "points": _distance_points(
                inputs["index_vs_ma50_pct"], full_scale=5.0, weight=15.0
            ),
        },
        "index_vs_ma200": {
            "weight": 20.0,
            "value": inputs["index_vs_ma200_pct"],
            "points": _distance_points(
                inputs["index_vs_ma200_pct"], full_scale=10.0, weight=20.0
            ),
        },
        "breadth_ma50": {
            "weight": 15.0,
            "value": inputs["breadth_above_ma50_pct"],
            "points": _breadth_points(
                inputs["breadth_above_ma50_pct"], weight=15.0
            ),
        },
        "breadth_ma200": {
            "weight": 10.0,
            "value": inputs["breadth_above_ma200_pct"],
            "points": _breadth_points(
                inputs["breadth_above_ma200_pct"], weight=10.0
            ),
        },
        "vix": {
            "weight": 15.0,
            "value": vix,
            "points": _vix_points(vix),
        },
        "credit": {
            "weight": 15.0,
            "value": hy_oas,
            "points": _credit_points(hy_oas),
        },
        "rates": {
            "weight": 10.0,
            "value": rate["inputs"],
            "points": rate["points"],
        },
    }
    observed_weight = sum(
        row["weight"] for row in components.values() if row["points"] is not None
    )
    points = sum(
        float(row["points"]) for row in components.values() if row["points"] is not None
    )
    # Keep thresholds comparable even when a non-critical component is missing.
    normalized_score = points * 100.0 / observed_weight if observed_weight else 0.0
    coverage = observed_weight / 100.0
    risk_veto = bool(
        (vix is not None and vix >= 25.0)
        or (hy_oas is not None and hy_oas >= 400.0)
        or rate["status"] == "tightening_shock"
    )
    raw_level = _level_from_score(normalized_score, risk_veto=risk_veto)
    breadth_complete = (
        inputs["breadth_above_ma50_pct"] is not None
        and inputs["breadth_above_ma200_pct"] is not None
        and inputs["breadth_observed_50"] >= MIN_BREADTH_OBSERVATIONS
        and inputs["breadth_observed_200"] >= MIN_BREADTH_OBSERVATIONS
    )
    rate_inputs = rate.get("inputs") if isinstance(rate.get("inputs"), dict) else {}
    rate_complete = all(
        rate_inputs.get(key) is not None
        for key in (
            "yield_10y",
            "yield_10y_change_5d_bps",
            "yield_10y_change_20d_bps",
            "real_yield_10y",
            "real_yield_10y_change_20d_bps",
            "breakeven_10y",
            "yield_spread_10y_3m",
        )
    )
    risk_inputs_complete = vix is not None and hy_oas is not None and rate_complete
    return {
        "market": market,
        "score": round(normalized_score, 2),
        "raw_level": raw_level,
        "raw_label": LEVEL_NAMES[raw_level],
        "coverage": round(coverage, 4),
        "eligible": (
            coverage >= MIN_COVERAGE
            and breadth_complete
            and risk_inputs_complete
        ),
        "breadth_complete": breadth_complete,
        "risk_inputs_complete": risk_inputs_complete,
        "rate_complete": rate_complete,
        "minimum_breadth_observations": MIN_BREADTH_OBSERVATIONS,
        "risk_veto": risk_veto,
        "rate_regime": rate,
        "components": components,
        "inputs": inputs,
    }


def _shock_assessment(macro: dict, guard: dict, market_meta: dict) -> dict:
    vix = _number(market_meta.get("vix"))
    if vix is None:
        vix = _number(macro.get("vix"))
    hy_oas = _number(macro.get("hy_oas_bps"))
    daily_pnl = _number(guard.get("daily_pnl_pct"))
    monthly_pnl = _number(guard.get("monthly_pnl_pct"))
    reasons: list[str] = []
    if vix is not None and vix >= 40.0:
        reasons.append("vix_gte_40")
    if hy_oas is not None and hy_oas >= 700.0:
        reasons.append("hy_oas_gte_700bps")
    # behavioral_guard stores ratios (e.g. -0.05 means -5%), not percentage
    # points.  Keep that unit explicit so the loss shock can actually fire.
    if daily_pnl is not None and daily_pnl <= -0.05:
        reasons.append("portfolio_daily_pnl_lte_minus_5pct")
    if monthly_pnl is not None and monthly_pnl <= -0.10:
        reasons.append("portfolio_monthly_pnl_lte_minus_10pct")
    return {
        "active": bool(reasons),
        "reasons": reasons,
        "inputs": {
            "vix": vix,
            "hy_oas_bps": hy_oas,
            "daily_pnl_pct": daily_pnl,
            "monthly_pnl_pct": monthly_pnl,
            "pnl_unit": "decimal_ratio",
        },
    }


def _normalize_weights(weights: Optional[dict]) -> dict:
    raw = {
        "US": max(0.0, _number((weights or {}).get("US")) or 0.0),
        "JP": max(0.0, _number((weights or {}).get("JP")) or 0.0),
    }
    total = raw["US"] + raw["JP"]
    if total <= 0:
        return {"US": 0.5, "JP": 0.5, "source": "equal_fallback"}
    return {
        "US": raw["US"] / total,
        "JP": raw["JP"] / total,
        "source": "invested_equity_value",
    }


def classify_regime(
    *,
    market_meta: dict,
    macro: dict,
    guard: Optional[dict] = None,
    vix_state: Optional[dict] = None,
    market_weights: Optional[dict] = None,
) -> dict:
    """Return the complete pure raw assessment before hysteresis."""
    markets = {
        market: classify_market(
            market,
            market_meta=market_meta,
            macro=macro,
            vix_state=vix_state,
        )
        for market in ("US", "JP")
    }
    weights = _normalize_weights(market_weights)
    # Candidates may be emitted in either market even when current weight is
    # zero.  Activate the portfolio policy only when both markets are complete.
    portfolio_eligible = all(markets[m]["eligible"] for m in ("US", "JP"))
    weighted_score = sum(markets[m]["score"] * weights[m] for m in ("US", "JP"))
    risk_veto = any(row["risk_veto"] for row in markets.values())
    portfolio_raw_level = _level_from_score(weighted_score, risk_veto=risk_veto)
    shock = _shock_assessment(macro, guard or {}, market_meta)
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "markets": markets,
        "market_weights": weights,
        "portfolio": {
            "score": round(weighted_score, 2),
            "raw_level": portfolio_raw_level,
            "raw_label": LEVEL_NAMES[portfolio_raw_level],
            "eligible": portfolio_eligible,
        },
        "shock": shock,
    }


def _state_path(base_dir: Optional[Path] = None) -> Path:
    root = os.environ.get("ALMANAC_STATE_DIR")
    return Path(root) / "market_regime_v2_state.json" if root else (
        (base_dir or Path(__file__).parent) / "market_regime_v2_state.json"
    )


def _load_state(path: Path) -> dict:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _advance_scope(
    *,
    prior: dict,
    raw_level: int,
    score: float,
    evaluation_date: str,
    same_evaluation_date: bool,
    immediate: bool,
) -> dict:
    previous_score = _number(prior.get("previous_score"))
    if previous_score is None:
        phase = "stable"
    elif score >= previous_score + 10.0:
        phase = "improving"
    elif score <= previous_score - 10.0:
        phase = "deteriorating"
    else:
        phase = "stable"

    if "committed_level" not in prior:
        committed = raw_level
        pending_level = None
        pending_count = 0
    else:
        committed = int(prior.get("committed_level", raw_level))
        pending_level = prior.get("pending_level")
        pending_count = int(prior.get("pending_count") or 0)
        if immediate:
            committed = raw_level
            pending_level = None
            pending_count = 0
        elif raw_level == committed:
            pending_level = None
            pending_count = 0
        elif not same_evaluation_date:
            if pending_level == raw_level:
                pending_count += 1
            else:
                pending_level = raw_level
                pending_count = 1
            if pending_count >= CONFIRMATION_DAYS:
                committed = raw_level
                pending_level = None
                pending_count = 0

    return {
        "committed_level": committed,
        "committed_label": LEVEL_NAMES[committed],
        "raw_level": raw_level,
        "raw_label": LEVEL_NAMES[raw_level],
        "pending_level": pending_level,
        "pending_count": pending_count,
        "confirmation_days": CONFIRMATION_DAYS,
        "phase": phase,
        "previous_score": score,
        "evaluated_on": evaluation_date,
    }


def evaluate_and_record(
    *,
    market_meta: dict,
    macro: dict,
    guard: Optional[dict] = None,
    vix_state: Optional[dict] = None,
    market_weights: Optional[dict] = None,
    input_snapshot_hash: Optional[str] = None,
    now: Optional[datetime] = None,
    base_dir: Optional[Path] = None,
    state_path: Optional[Path] = None,
    mode: Optional[str] = None,
) -> dict:
    """Classify, apply per-date hysteresis, and atomically persist audit state."""
    now = now or datetime.now()
    evaluation_date = now.date().isoformat()
    mode = str(
        mode or os.environ.get("ALMANAC_MARKET_REGIME_V2_MODE", "advisory")
    ).strip().lower()
    if mode not in {"off", "shadow", "advisory"}:
        mode = "shadow"

    raw = classify_regime(
        market_meta=market_meta,
        macro=macro,
        guard=guard,
        vix_state=vix_state,
        market_weights=market_weights,
    )
    path = state_path or _state_path(base_dir)
    prior = _load_state(path)
    same_date = prior.get("last_evaluation_date") == evaluation_date
    prior_scopes = prior.get("scopes") if isinstance(prior.get("scopes"), dict) else {}

    scopes: dict[str, dict] = {}
    for scope in ("US", "JP", "portfolio"):
        source = raw["portfolio"] if scope == "portfolio" else raw["markets"][scope]
        eligible = bool(source.get("eligible"))
        prior_scope = prior_scopes.get(scope) if isinstance(prior_scopes.get(scope), dict) else {}
        if eligible:
            scopes[scope] = _advance_scope(
                prior=prior_scope,
                raw_level=int(source["raw_level"]),
                score=float(source["score"]),
                evaluation_date=evaluation_date,
                same_evaluation_date=same_date,
                immediate=bool(raw["shock"]["active"]),
            )
        else:
            fallback_level = int(prior_scope.get("committed_level", 0))
            scopes[scope] = {
                "committed_level": fallback_level,
                "committed_label": LEVEL_NAMES[fallback_level],
                "raw_level": int(source["raw_level"]),
                "raw_label": source["raw_label"],
                "pending_level": None,
                "pending_count": 0,
                "confirmation_days": CONFIRMATION_DAYS,
                "phase": "unknown",
                "previous_score": source["score"],
                "evaluated_on": evaluation_date,
                "fallback_reason": "insufficient_component_coverage",
            }

    assessment = deepcopy(raw)
    for scope in ("US", "JP"):
        assessment["markets"][scope].update(scopes[scope])
    assessment["portfolio"].update(scopes["portfolio"])
    assessment["mode"] = mode
    assessment["status"] = (
        "off" if mode == "off"
        else "review" if not assessment["portfolio"]["eligible"]
        else "shock" if assessment["shock"]["active"]
        else "ok"
    )
    # This covers only inputs consumed by this classifier.  It is not the
    # broader DecisionSnapshot hash produced later by analysis_snapshot.py.
    assessment["input_snapshot_hash"] = input_snapshot_hash
    assessment["evaluated_at"] = now.isoformat()
    assessment["policy"] = policy_for_assessment(assessment)
    assessment["action_effect"] = (
        "none" if mode in {"off", "shadow"} or not assessment["portfolio"]["eligible"]
        else "advisory_recommendation_and_deterministic_size_cap"
    )

    payload = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "updated_at": now.isoformat(),
        "last_evaluation_date": evaluation_date,
        "last_input_snapshot_hash": input_snapshot_hash,
        "scopes": scopes,
        "assessment": assessment,
    }
    if mode != "off":
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    return assessment


def policy_for_assessment(assessment: dict) -> dict:
    portfolio = assessment.get("portfolio") or {}
    markets = assessment.get("markets") or {}
    level = int(portfolio.get("committed_level", portfolio.get("raw_level", 0)))
    shock = bool((assessment.get("shock") or {}).get("active"))
    base = dict(SHOCK_POLICY if shock else POLICY_BY_LEVEL[level])
    base.update({
        "policy_version": POLICY_VERSION,
        "portfolio_level": level,
        "portfolio_label": LEVEL_NAMES[level],
        "legacy_scenario_key": (
            "CRASH" if shock
            else "BULL" if level > 0
            else "BEAR" if level < 0
            else "NEUTRAL"
        ),
        "market_buy_size_multipliers": {
            market: POLICY_BY_LEVEL[
                int((markets.get(market) or {}).get("committed_level", level))
            ]["buy_size_multiplier"]
            for market in ("US", "JP")
        },
        "market_levels": {
            market: int((markets.get(market) or {}).get("committed_level", level))
            for market in ("US", "JP")
        },
    })
    if not shock:
        base.setdefault("cash_action", "target")
        base.setdefault("raise_cash_to_target", level < 0)
    return base


def apply_policy_to_legacy_scenario(scenario: dict, assessment: dict) -> dict:
    """Return a compatibility scenario with v2 policy fields.

    ``shadow`` and insufficient-coverage assessments never change the legacy
    action.  ``advisory`` changes recommendations and deterministic size caps;
    it still does not place orders.
    """
    result = dict(scenario or {})
    policy = assessment.get("policy") or policy_for_assessment(assessment)
    result["market_regime_v2"] = assessment
    result["proposed_market_regime_policy"] = policy
    if (
        assessment.get("mode") != "advisory"
        or not bool((assessment.get("portfolio") or {}).get("eligible"))
    ):
        result["market_regime_v2_applied"] = False
        return result

    key = str(policy["legacy_scenario_key"])
    level = int(policy["portfolio_level"])
    display_name = (
        "クラッシュ/危機"
        if key == "CRASH"
        else f"{LEVEL_DISPLAY_NAMES[level]}相場"
    )
    result.update({
        "scenario": key,
        "key": key,
        "name": display_name,
        "description": (
            "複数のトレンド・市場幅・VIX・信用スプレッド・長期金利を"
            f"合成した5段階判定: {LEVEL_DISPLAY_NAMES[level]}"
        ),
        "cash_ratio_target": policy["cash_target_pct"],
        "cash_action": policy["cash_action"],
        "raise_cash_to_target": policy["raise_cash_to_target"],
        "buy_size_multiplier": policy["buy_size_multiplier"],
        "market_buy_size_multipliers": policy["market_buy_size_multipliers"],
        "market_levels": policy["market_levels"],
        "leverage_allowed": policy["leverage_allowed"],
        "long_bias": int(policy["portfolio_level"]) >= 0,
        "short_allowed": int(policy["portfolio_level"]) <= 0,
        "market_regime_v2_applied": True,
        "legacy_scenario_before_v2": {
            "scenario": scenario.get("scenario"),
            "key": scenario.get("key"),
            "cash_ratio_target": scenario.get("cash_ratio_target"),
        },
    })
    if key == "CRASH":
        result["actions"] = [
            "暴落後に機械的な換金売りで現金比率を引き上げない",
            "既に確保した戦術的現金をactive DCA trancheだけで段階投入",
            "信用買い・新規レバレッジは禁止",
            "個別仮説崩壊・信用リスク・上限超過の売却だけを別途評価",
        ]
    else:
        result["actions"] = list(LEVEL_ACTIONS[level])
    return result
