"""ALMANAC's versioned, non-tunable portfolio risk policy.

Risk thresholds are deliberately kept outside ``tunable_params``.  They are
portfolio mandates, not parameters that an optimisation or advisory process may
relax during a favourable regime.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


RISK_POLICY_VERSION = "2026-08-v2"

# These historical tunable keys remain readable for audit only.  Neither the
# tuning advisor nor an auto-tuning run may produce a new recommendation for
# them, and no v7 risk consumer reads their values.
RETIRED_TUNABLE_RISK_KEYS = frozenset({
    "daily_loss_limit_pct",
    "monthly_stage1_pct",
    "monthly_stage2_pct",
    "monthly_stage3_pct",
    "dd_50pct_reduce",
    "dd_full_liquidate",
})


@dataclass(frozen=True)
class RiskPolicy:
    version: str = RISK_POLICY_VERSION
    daily_loss_block_decimal: float = -0.03
    rolling_30_stage1_decimal: float = -0.06
    rolling_30_stage2_decimal: float = -0.09
    rolling_30_stage3_decimal: float = -0.12
    var_bear_decimal: float = 0.012
    var_normal_decimal: float = 0.014
    var_bull_decimal: float = 0.016
    var_absolute_max_decimal: float = 0.018
    concentration_caution_decimal: float = 0.08
    concentration_cap_decimal: float = 0.10
    max_short_positions: int = 3
    dd_caution_decimal: float = -0.05
    dd_block_decimal: float = -0.08
    dd_derisk_decimal: float = -0.10
    dd_freeze_decimal: float = -0.12
    dd_objective_breach_decimal: float = -0.15

    def as_public_dict(self) -> dict[str, Any]:
        return asdict(self)


POLICY = RiskPolicy()


def concentration_limits(*, broad_family: str | None = None, investment_type: str | None = None, employer_stock: bool = False) -> dict[str, Any]:
    """Return versioned prospective concentration limits.

    Broad treatment is allowed only for explicitly classified long exposure;
    missing classifications retain the historic 10% hard cap.
    """
    tier = str(investment_type or "").lower()
    if employer_stock:
        return {"classification": "employer", "caution_decimal": 0.08, "cap_decimal": 0.10, "family_cap_decimal": None}
    if broad_family and tier == "long":
        return {"classification": "broad_long", "broad_family": str(broad_family), "caution_decimal": 0.20, "cap_decimal": 0.25, "family_cap_decimal": 0.40}
    if tier == "medium":
        return {"classification": "medium", "caution_decimal": 0.04, "cap_decimal": 0.05, "family_cap_decimal": None}
    if tier == "swing":
        return {"classification": "swing", "caution_decimal": 0.016, "cap_decimal": 0.02, "family_cap_decimal": None}
    return {"classification": "legacy_or_long", "caution_decimal": POLICY.concentration_caution_decimal, "cap_decimal": POLICY.concentration_cap_decimal, "family_cap_decimal": None}


def loss_guard_state(
    *,
    daily_pnl_decimal: float | None,
    rolling_30_pnl_decimal: float | None,
) -> dict[str, Any]:
    """Classify loss guards without mutating guard state.

    ``actual_dd_stage`` is retained temporarily for downstream compatibility,
    but is explicitly a loss-guard state, never a drawdown measurement.
    """
    if daily_pnl_decimal is None or rolling_30_pnl_decimal is None:
        return {
            "loss_guard_stage": "data_confidence_caution",
            "actual_dd_stage": "data_confidence_caution",
            "new_risk_allowed": None,
            "trading_allowed": True,
            "reason_code": "loss_guard_data_unavailable",
        }
    daily = float(daily_pnl_decimal)
    rolling = float(rolling_30_pnl_decimal)
    daily_block = daily <= POLICY.daily_loss_block_decimal
    if rolling <= POLICY.rolling_30_stage3_decimal:
        return {
            "loss_guard_stage": "stage_3",
            "actual_dd_stage": "stage_3",
            "new_risk_allowed": False,
            # Stage 3 freezes risk increases only.  De-risking execution
            # paths remain available and must never be represented as a
            # wholesale trading halt.
            "trading_allowed": True,
            "reason_code": "rolling_30_risk_increase_freeze",
        }
    if rolling <= POLICY.rolling_30_stage2_decimal:
        return {
            "loss_guard_stage": "stage_2",
            "actual_dd_stage": "stage_2",
            "new_risk_allowed": False,
            "trading_allowed": True,
            "reason_code": "rolling_30_stage_2",
        }
    if rolling <= POLICY.rolling_30_stage1_decimal:
        return {
            "loss_guard_stage": "stage_1",
            "actual_dd_stage": "stage_1",
            "new_risk_allowed": False,
            "trading_allowed": True,
            "reason_code": "rolling_30_stage_1",
        }
    if daily_block:
        return {
            "loss_guard_stage": "daily_block",
            "actual_dd_stage": "daily_block",
            "new_risk_allowed": False,
            "trading_allowed": True,
            "reason_code": "daily_loss_block",
        }
    return {
        "loss_guard_stage": "ok",
        "actual_dd_stage": "ok",
        "new_risk_allowed": True,
        "trading_allowed": True,
        "reason_code": "ok",
    }


def var_threshold_decimal(*, bull: bool, vix: float | None, stressed: bool) -> float:
    if stressed:
        return POLICY.var_bear_decimal
    if bull and vix is not None and vix < 25:
        return POLICY.var_bull_decimal
    return POLICY.var_normal_decimal


def classify_drawdown(drawdown_decimal: float | None) -> dict[str, Any]:
    """Classify only a canonical, flow-adjusted drawdown value.

    A missing or provisional series must *not* be relabelled as a benign
    drawdown.  Consumers use ``data_confidence_caution`` to ask for an
    explicit human acknowledgement instead of silently taking a DD gate from
    a P&L proxy.
    """
    if drawdown_decimal is None:
        return {
            "dd_stage": "data_confidence_caution",
            "risk_increase_allowed": None,
            "reason_code": "canonical_drawdown_unavailable",
        }
    dd = float(drawdown_decimal)
    if dd <= POLICY.dd_objective_breach_decimal:
        return {"dd_stage": "objective_breach", "risk_increase_allowed": False,
                "reason_code": "drawdown_objective_breach"}
    if dd <= POLICY.dd_freeze_decimal:
        return {"dd_stage": "freeze", "risk_increase_allowed": False,
                "reason_code": "drawdown_risk_increase_freeze"}
    if dd <= POLICY.dd_derisk_decimal:
        return {"dd_stage": "derisk_review", "risk_increase_allowed": False,
                "reason_code": "drawdown_derisk_review"}
    if dd <= POLICY.dd_block_decimal:
        return {"dd_stage": "block", "risk_increase_allowed": False,
                "reason_code": "drawdown_block"}
    if dd <= POLICY.dd_caution_decimal:
        return {"dd_stage": "caution", "risk_increase_allowed": True,
                "reason_code": "drawdown_caution"}
    return {"dd_stage": "ok", "risk_increase_allowed": True, "reason_code": "ok"}


def classify_execution_risk(
    *,
    daily_pnl_decimal: float | None,
    rolling_30_pnl_decimal: float | None,
    var_1d_95_decimal: float | None,
    concentration_decimal: float | None,
    canonical_drawdown_decimal: float | None,
    canonical_drawdown_stage: str | None = None,
    var_policy_threshold_decimal: float | None = None,
    risk_increasing: bool,
    action_direction: str | None = None,
    current_short_positions: int | None = None,
    concentration_caution_decimal: float | None = None,
    concentration_cap_decimal: float | None = None,
    family_concentration_decimal: float | None = None,
    family_concentration_cap_decimal: float | None = None,
) -> dict[str, Any]:
    """Return the deterministic preflight disposition.

    The check is intentionally conservative about unavailable data, but it is
    not silently fail-closed: unavailable non-hard metrics require a visible
    acknowledgement.  This preserves the human overnight workflow while
    making every exception auditable.  Only the explicit policy caps are hard
    rejections.
    """
    loss = loss_guard_state(
        daily_pnl_decimal=daily_pnl_decimal,
        rolling_30_pnl_decimal=rolling_30_pnl_decimal,
    )
    dd = classify_drawdown(canonical_drawdown_decimal)
    if canonical_drawdown_stage:
        # The promoted DD controller includes five-day/2pt hysteresis.  Its
        # state is authoritative over a raw point estimate until it advances.
        dd = {**dd, "dd_stage": str(canonical_drawdown_stage)}
        dd["reason_code"] = f"drawdown_{dd['dd_stage']}"
    reasons: list[dict[str, Any]] = []
    hard_reasons: list[dict[str, Any]] = []

    def add(code: str, message: str, *, hard: bool = False) -> None:
        item = {"code": code, "message": message}
        (hard_reasons if hard else reasons).append(item)

    if not risk_increasing:
        return {
            "disposition": "ready",
            "reasons": [],
            "hard_reasons": [],
            "loss_guard": loss,
            "drawdown": dd,
        }

    if str(action_direction or "").lower() == "short":
        if current_short_positions is None:
            add(
                "short_position_count_unavailable",
                "現在の空売りポジション数を確認できません。人間の明示確認が必要です",
            )
        elif int(current_short_positions) >= POLICY.max_short_positions:
            add(
                "max_short_positions",
                f"空売りポジションが{int(current_short_positions)}/{POLICY.max_short_positions}上限に達しています",
                hard=True,
            )

    var_threshold = (
        float(var_policy_threshold_decimal)
        if var_policy_threshold_decimal is not None
        else POLICY.var_normal_decimal
    )
    if var_1d_95_decimal is None:
        add("var_unavailable", "ex-ante VaRを確認できません。人間の明示確認が必要です")
    elif float(var_1d_95_decimal) >= POLICY.var_absolute_max_decimal:
        add("var_absolute_cap", "VaRが絶対上限1.8%に達しています", hard=True)
    elif float(var_1d_95_decimal) >= var_threshold:
        add(
            "var_policy_threshold",
            f"VaRがレジーム別リスク予算{var_threshold * 100:.1f}%を超えています",
        )

    concentration_caution = float(concentration_caution_decimal) if concentration_caution_decimal is not None else POLICY.concentration_caution_decimal
    concentration_cap = float(concentration_cap_decimal) if concentration_cap_decimal is not None else POLICY.concentration_cap_decimal
    if concentration_decimal is None:
        add("concentration_unavailable", "注文後の集中度を確認できません。人間の明示確認が必要です")
    elif float(concentration_decimal) >= concentration_cap:
        add("concentration_cap", f"注文後の単一銘柄集中度が{concentration_cap * 100:.0f}%上限に達します", hard=True)
    elif float(concentration_decimal) >= concentration_caution:
        add("concentration_caution", f"注文後の単一銘柄集中度が{concentration_caution * 100:.1f}%注意水準です")
    if family_concentration_cap_decimal is not None:
        if family_concentration_decimal is None:
            add("concentration_family_unavailable", "注文後のfamily集中度を確認できません。人間の明示確認が必要です")
        elif float(family_concentration_decimal) >= float(family_concentration_cap_decimal):
            add("concentration_family_cap", f"注文後のfamily集中度が{float(family_concentration_cap_decimal) * 100:.0f}%上限に達します", hard=True)

    if loss["loss_guard_stage"] != "ok":
        loss_message = (
            "日次または30日P&Lを確認できません。人間の明示確認が必要です"
            if loss["loss_guard_stage"] == "data_confidence_caution"
            else "日次または30日P&Lのリスク増加ガードが発動しています"
        )
        add(loss["reason_code"], loss_message)
    if dd["dd_stage"] != "ok":
        add(dd["reason_code"], "ピーク比ドローダウン状態の人間確認が必要です")

    if hard_reasons:
        disposition = "hard_reject"
    elif reasons:
        disposition = "confirmation_required"
    else:
        disposition = "ready"
    return {
        "disposition": disposition,
        "reasons": reasons,
        "hard_reasons": hard_reasons,
        "loss_guard": loss,
        "drawdown": dd,
        "var_policy_threshold_decimal": var_threshold,
    }
