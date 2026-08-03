"""Deterministic validation for user-facing AI analysis prose.

The synthesis model is allowed to explain decisions, but it is not the
authority for portfolio arithmetic or for the semantic class of a risk
metric.  This module converts the small subset of claims that can affect a
user's risk interpretation into calculations backed by structured state.
"""
from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any


_ACTUAL_DD_RE = re.compile(
    r"(?:(?:実|actual(?:\s+current)?)\s*DD|current[_\s-]?DD)"
    r"\s*[:=]?\s*[+\-−]?\d+(?:\.\d+)?\s*%",
    re.IGNORECASE,
)
_CLEAN_DD_RE = re.compile(
    r"\bclean(?:\s+NAV)?(?:\s+DD)?\s*[:=]?\s*"
    r"(?P<value>[+\-−]?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_TWENTY_DAY_SHOCK_RE = re.compile(
    r"\b(?P<label>[A-Z][A-Z0-9.\-]{0,11})\s*20d\s*"
    r"(?P<shock>[+\-−]\s*\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_GENERIC_SHOCK_RE = re.compile(
    r"(?:ショック|下落|調整)[^%]{0,24}?"
    r"(?P<shock>[+\-−]\s*\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
_LOSS_CLAIM_RE = re.compile(r"推定損(?:失)?[^。\n]*")


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def risk_context_for_prompt(raw: dict | None) -> dict:
    """Return a unit-labelled risk view that cannot masquerade synthetic DD.

    ``synthetic_current_dd`` is useful for ex-ante audit, but exposing it next
    to generic ``current_dd`` invited the model to call it "actual DD".  The
    prompt receives an explicit tagged structure instead.  The original risk
    payload remains available to deterministic policy code and audit logs.
    """
    risk = raw if isinstance(raw, dict) else {}
    passthrough = (
        "source",
        "observations",
        "var_95",
        "var_95_decimal",
        "var_95_cf",
        "var_95_hist",
        "cvar_95",
        "cvar_95_decimal",
        "cvar_unstable",
        "cvar_reason",
        "coverage_ratio",
        "risk_basis",
    )
    result = {key: risk.get(key) for key in passthrough if key in risk}

    canonical = _finite(risk.get("enforced_flow_adjusted_dd_decimal"))
    flow_shadow = _finite(risk.get("flow_adjusted_current_dd_decimal"))
    clean_shadow = _finite(risk.get("clean_nav_current_dd_decimal"))
    synthetic_pct = _finite(risk.get("synthetic_current_dd"))
    result["drawdown_metrics"] = {
        "canonical_flow_adjusted": {
            "decimal": canonical,
            "stage": risk.get("enforced_drawdown_stage"),
            "authority": "enforced" if canonical is not None else "unavailable",
            "may_be_called_actual_dd": canonical is not None,
        },
        "flow_adjusted_shadow": {
            "decimal": flow_shadow,
            "authority": "shadow_only",
            "may_be_called_actual_dd": False,
        },
        "clean_nav_unadjusted_shadow": {
            "decimal": clean_shadow,
            "authority": "shadow_only",
            "may_be_called_actual_dd": False,
        },
        "synthetic_ex_ante_current_weights": {
            "decimal": round(synthetic_pct / 100.0, 8) if synthetic_pct is not None else None,
            "source_unit": "percent",
            "authority": "scenario_only_not_actual",
            "may_be_called_actual_dd": False,
        },
    }
    result["loss_guard"] = {
        "stage": risk.get("loss_guard_stage"),
        "daily_pnl_decimal": risk.get("daily_pnl_decimal"),
        "rolling_30_pnl_decimal": risk.get("rolling_30_pnl_decimal"),
        "metric_type": "realized_pnl_shock_not_drawdown",
    }
    result["metric_contract"] = (
        "Only drawdown_metrics.canonical_flow_adjusted with authority=enforced "
        "may be described as actual/current DD. All shadow and synthetic values "
        "must retain their labels and must not justify a DD gate."
    )
    return result


def normalize_stance_drawdown_language(synthesis: dict, risk: dict | None) -> dict:
    """Replace model-authored actual-DD claims with the canonical metric state."""
    if not isinstance(synthesis, dict):
        return synthesis
    reason = str(synthesis.get("stance_reason") or "")
    if not reason:
        return synthesis

    risk = risk if isinstance(risk, dict) else {}
    canonical = _finite(risk.get("enforced_flow_adjusted_dd_decimal"))
    replacement = (
        f"canonical flow-adjusted DD {canonical * 100:+.2f}%"
        if canonical is not None
        else "canonical flow-adjusted DD未確定"
    )
    normalized, actual_count = _ACTUAL_DD_RE.subn(replacement, reason)

    def _clean_replacement(match: re.Match[str]) -> str:
        value = match.group("value").replace("−", "-")
        return f"clean NAV shadow {value}%（自動ゲート非使用）"

    normalized, clean_count = _CLEAN_DD_RE.subn(_clean_replacement, normalized)
    if canonical is None:
        normalized = normalized.replace(
            "defensive強制条件はいずれも未該当",
            "DD由来のdefensive条件は未判定",
        )
        normalized = re.sub(
            r"現金(?P<cash>[^。]{0,30}?>3%)のため"
            r"aggressive昇格条件を実データで満たす",
            r"現金\g<cash>だが、aggressive昇格条件はcanonical DD欠損のため未確定",
            normalized,
        )
    guard_detail = synthesis.get("stance_guard_detail")
    if (
        synthesis.get("stance_guard_applied") is True
        and isinstance(guard_detail, dict)
        and guard_detail.get("downgraded_to")
    ):
        normalized = normalized.replace(
            "stance の格下げは行わない",
            "最終stanceは決定論的guard補正を優先",
        )
    if normalized == reason:
        return synthesis

    synthesis["stance_reason_original"] = reason
    synthesis["stance_reason"] = normalized
    synthesis["stance_reason_dd_normalized"] = True
    synthesis["stance_reason_dd_validation"] = {
        "canonical_flow_adjusted_dd_decimal": canonical,
        "canonical_authority": "enforced" if canonical is not None else "unavailable",
        "actual_dd_claims_replaced": actual_count,
        "clean_shadow_labels_replaced": clean_count,
        "validation_version": 1,
    }
    return synthesis


def _position_map(observation: dict | None) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    if not isinstance(observation, dict):
        return result
    denominator = _finite(observation.get("denominator_jpy"))
    for row in observation.get("positions") or []:
        if not isinstance(row, dict):
            continue
        ticker = str(row.get("canonical_instrument_id") or row.get("ticker") or "").upper()
        value = _finite(row.get("value_jpy"))
        weight = _finite(row.get("weight"))
        if not ticker or value is None or value < 0:
            continue
        if weight is None and denominator and denominator > 0:
            weight = value / denominator
        result[ticker] = {"value_jpy": value, "weight": weight or 0.0}
    return result


def _shock_from_warning(text: str) -> tuple[str, float] | None:
    match = _TWENTY_DAY_SHOCK_RE.search(text)
    if match:
        raw = match.group("shock").replace("−", "-").replace(" ", "")
        return f"{match.group('label').upper()} 20d", float(raw)
    match = _GENERIC_SHOCK_RE.search(text)
    if match:
        raw = match.group("shock").replace("−", "-").replace(" ", "")
        return "参照ショック", float(raw)
    return None


def normalize_risk_warning_claims(
    synthesis: dict,
    observation: dict | None,
) -> dict:
    """Recalculate loss claims from whole-portfolio structured positions.

    A warning containing a monetary estimated-loss claim is never passed
    through merely because it sounds plausible.  When its referenced holdings
    and shock can be resolved, the claim is regenerated deterministically.  If
    not, the amount is redacted and explicitly marked unverified.
    """
    if not isinstance(synthesis, dict):
        return synthesis
    warnings = synthesis.get("risk_warnings")
    if not isinstance(warnings, list):
        return synthesis

    positions = _position_map(observation)
    denominator = _finite((observation or {}).get("denominator_jpy")) if isinstance(observation, dict) else None
    normalized_warnings: list[Any] = []
    validations: list[dict] = []

    for index, raw in enumerate(warnings):
        if not isinstance(raw, str) or "推定損" not in raw:
            normalized_warnings.append(raw)
            continue
        referenced: list[str] = []
        # Match against known structured tickers.  LLM prose commonly omits a
        # separator ("NVDA14.5%"), so a generic ticker regex would greedily
        # consume the first digits of the weight.
        for ticker in positions:
            component = re.compile(
                rf"(?<![A-Z0-9.]){re.escape(ticker)}\s*"
                rf"\d+(?:\.\d+)?\s*%",
                re.IGNORECASE,
            )
            if component.search(raw):
                referenced.append(ticker)
        referenced.sort(key=lambda ticker: raw.upper().find(ticker))
        shock = _shock_from_warning(raw)

        if referenced and shock and denominator and denominator > 0:
            label, shock_pct = shock
            exposure = sum(positions[ticker]["value_jpy"] for ticker in referenced)
            concentration = exposure / denominator * 100.0
            estimated_loss = exposure * abs(shock_pct) / 100.0
            topic_match = re.match(r"\s*([^:：]{1,30}?集中)", raw)
            topic = topic_match.group(1) if topic_match else "集中リスク"
            component_text = " + ".join(
                f"{ticker} {positions[ticker]['weight'] * 100:.2f}%"
                for ticker in referenced
            )
            trailing = ""
            sentences = [part.strip() for part in raw.split("。") if part.strip()]
            if len(sentences) > 1 and "推定損" not in sentences[-1]:
                trailing = f"。{sentences[-1]}"
            normalized = (
                f"{topic}（whole_portfolio）: {component_text} = {concentration:.2f}%。"
                f"{label} {shock_pct:+.1f}%を同率適用した単純一次推定損失は"
                f"約¥{estimated_loss:,.0f}（ベータ・相関・税未調整）{trailing}"
            )
            normalized_warnings.append(normalized)
            validations.append({
                "index": index,
                "status": "recalculated",
                "original": raw,
                "normalized": normalized,
                "referenced_tickers": referenced,
                "denominator": "whole_portfolio",
                "denominator_jpy": round(denominator),
                "exposure_jpy": round(exposure),
                "concentration_pct": round(concentration, 4),
                "shock_label": label,
                "shock_pct": shock_pct,
                "estimated_loss_jpy": round(estimated_loss),
                "validation_version": 1,
            })
            continue

        redacted, count = _LOSS_CLAIM_RE.subn(
            "損失額は構造化データで検証できないため表示保留",
            raw,
        )
        if not count:
            redacted = "未検証の損失額を含むリスク警告のため、数値表示を保留"
        normalized_warnings.append(redacted)
        validations.append({
            "index": index,
            "status": "amount_redacted_unverified",
            "original": raw,
            "normalized": redacted,
            "referenced_tickers": referenced,
            "shock_resolved": bool(shock),
            "validation_version": 1,
        })

    synthesis["risk_warnings"] = normalized_warnings
    if validations:
        synthesis["risk_warning_claim_validation"] = validations
    return synthesis


def synthesis_for_display(analysis: dict | None) -> dict:
    """Return a read-only corrected synthesis for API/UI rendering.

    Historical artifacts remain immutable audit evidence.  This overlay also
    repairs analyses produced before these validators were deployed by using
    the structured policy audit fields already stored with each action.
    """
    analysis = analysis if isinstance(analysis, dict) else {}
    raw = analysis.get("synthesis")
    synthesis = deepcopy(raw) if isinstance(raw, dict) else {}
    risk = analysis.get("risk_snapshot")
    normalize_stance_drawdown_language(
        synthesis,
        risk if isinstance(risk, dict) else {},
    )
    normalize_risk_warning_claims(
        synthesis,
        synthesis.get("investment_policy_observation"),
    )
    try:
        from action_amounts import synchronize_persisted_resized_action_prose

        actions = synthesis.get("priority_actions")
        if isinstance(actions, list):
            synthesis["priority_actions"] = [
                synchronize_persisted_resized_action_prose(action)
                if isinstance(action, dict) else action
                for action in actions
            ]
    except Exception as exc:
        synthesis["display_prose_repair_error"] = f"{type(exc).__name__}: {exc}"
    return synthesis


def apply_execution_plan_display_conflicts(
    synthesis: dict,
    execution_plan: dict | None,
) -> dict:
    """Overlay newly-detectable opposite-plan reviews on cached actions."""
    if not isinstance(synthesis, dict) or not isinstance(execution_plan, dict):
        return synthesis
    actions = synthesis.get("priority_actions")
    if not isinstance(actions, list):
        return synthesis
    try:
        from execution_plan_engine import classify_candidate_against_plan
    except Exception:
        return synthesis

    updated: list[Any] = []
    for raw in actions:
        if not isinstance(raw, dict):
            updated.append(raw)
            continue
        action = dict(raw)
        try:
            decision = classify_candidate_against_plan(action, execution_plan)
        except Exception:
            updated.append(action)
            continue
        if decision.get("execution_plan_direction_conflict"):
            for key in (
                "execution_plan_direction_conflict",
                "execution_plan_requires_review",
                "execution_plan_conflict_item_ids",
                "execution_plan_conflict_reason",
            ):
                action[key] = decision.get(key)
            if str(action.get("execution_readiness") or "ready").lower() == "ready":
                action["execution_readiness"] = "review"
            reasons = [
                dict(row) for row in (action.get("execution_block_reasons") or [])
                if isinstance(row, dict)
            ]
            if not any(row.get("code") == "execution_plan_direction_conflict" for row in reasons):
                reasons.append({
                    "code": "execution_plan_direction_conflict",
                    "message": decision.get("execution_plan_conflict_reason"),
                    "plan_item_ids": decision.get("execution_plan_conflict_item_ids") or [],
                })
            action["execution_block_reasons"] = reasons
            action["display_plan_conflict_overlay"] = True
        updated.append(action)
    synthesis["priority_actions"] = updated
    return synthesis
