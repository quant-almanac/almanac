"""Deterministic, execution-time risk confirmation for manually placed orders.

ALMANAC does not submit orders to a broker.  This module therefore protects the
last enforceable boundary: the UI's order-recording operation.  It never runs
an LLM and ``evaluate_preflight`` is read-only.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from almanac.runtime_config import load_api_key
from risk_policy import (
    POLICY,
    RISK_POLICY_VERSION,
    classify_execution_risk,
    concentration_limits,
    loss_guard_state,
    var_threshold_decimal,
)


PREFLIGHT_VERSION = "2026-08-v2"
PREFLIGHT_TTL_MINUTES = 60
PREFLIGHT_ACK_LOG = "execution_preflight_acknowledgements.jsonl"


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def action_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Fields that cannot change after a user has reviewed the preflight."""
    position_keys = payload.get("execution_position_keys") or []
    permission = payload.get("capital_deployment_permission")
    permission_id = (
        permission.get("permission_id") if isinstance(permission, dict) else None
    )
    return {
        "ticker": str(payload.get("ticker") or ""),
        "direction": str(payload.get("direction") or ""),
        "order_type": str(payload.get("order_type") or ""),
        "quantity": payload.get("quantity"),
        "price": payload.get("price"),
        "limit_price": payload.get("limit_price"),
        "currency": str(payload.get("currency") or ""),
        "account": str(payload.get("account") or ""),
        "execution_owner": str(payload.get("execution_owner") or ""),
        "execution_broker": str(payload.get("execution_broker") or ""),
        "execution_position_keys": sorted(str(key) for key in position_keys),
        "source": str(payload.get("source") or ""),
        "plan_item_id": str(payload.get("plan_item_id") or ""),
        "route_id": str(payload.get("route_id") or ""),
        "cash_wallet_key": str(payload.get("cash_wallet_key") or ""),
        "capital_deployment_permission_id": str(permission_id or ""),
        "analysis_id": str(payload.get("analysis_id") or ""),
        "action_state_id": str(payload.get("action_state_id") or ""),
    }


def action_digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(action_identity(payload)).encode("utf-8")).hexdigest()


def _token_secret() -> bytes:
    key = load_api_key()
    if not key:
        # The POST endpoint cannot be reached without an API key in production.
        # Explicitly refuse instead of silently issuing forgeable tokens.
        raise RuntimeError("ALMANAC API key is unavailable; preflight token cannot be issued")
    return key.encode("utf-8")


def _sign(encoded: str) -> str:
    return hmac.new(_token_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()


def issue_preflight_token(
    *,
    digest: str,
    disposition: str,
    review_context: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> tuple[str, str]:
    now = now or datetime.now(timezone.utc)
    expires = now + timedelta(minutes=PREFLIGHT_TTL_MINUTES)
    claims = {
        "v": PREFLIGHT_VERSION,
        "digest": digest,
        "disposition": disposition,
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "review_context": review_context or {},
    }
    encoded = base64.urlsafe_b64encode(_canonical_json(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{encoded}.{_sign(encoded)}", claims["expires_at"]


def validate_preflight_token(token: str | None, *, digest: str, now: datetime | None = None) -> dict[str, Any]:
    if not token or "." not in token:
        raise ValueError("preflight token is required")
    encoded, signature = token.rsplit(".", 1)
    if not hmac.compare_digest(signature, _sign(encoded)):
        raise ValueError("preflight token signature is invalid")
    try:
        raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ValueError("preflight token is malformed") from exc
    if claims.get("v") != PREFLIGHT_VERSION or claims.get("digest") != digest:
        raise ValueError("action identity does not match the reviewed preflight")
    try:
        expires = datetime.fromisoformat(str(claims["expires_at"]))
    except Exception as exc:
        raise ValueError("preflight token expiry is invalid") from exc
    now = now or datetime.now(timezone.utc)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        raise ValueError("preflight token has expired")
    return claims


def _as_decimal(value: Any) -> float | None:
    """Accept only a documented decimal field; never infer a unit by size."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _guard_metrics(base_dir: Path) -> tuple[float | None, float | None]:
    path = base_dir / "guard_state.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    return _as_decimal(raw.get("daily_pnl_pct")), _as_decimal(raw.get("monthly_pnl_pct"))


def _current_short_positions(base_dir: Path) -> int | None:
    """Read the EOD canonical short count used by the behavioral guard."""
    try:
        raw = json.loads((base_dir / "guard_state.json").read_text(encoding="utf-8"))
        value = raw.get("short_positions") if isinstance(raw, dict) else None
        if value is None:
            return None
        count = int(value)
        return count if count >= 0 else None
    except (FileNotFoundError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _analysis_risk_metrics(base_dir: Path) -> tuple[float | None, str | None, str | None]:
    """Read explicitly named decimal metrics from the latest analysis snapshot."""
    try:
        raw = json.loads((base_dir / "ai_portfolio_analysis.json").read_text(encoding="utf-8"))
        risk = (
            raw.get("risk_snapshot") or raw.get("risk")
            if isinstance(raw, dict)
            else None
        )
        if not isinstance(risk, dict):
            return None, None, None
        var = _as_decimal(risk.get("var_95_decimal"))
        source = str(risk.get("source") or "").strip() or None
        snapshot_as_of = str(risk.get("snapshot_as_of") or raw.get("as_of") or "").strip() or None
        return var, source, snapshot_as_of
    except Exception:
        return None, None, None


def _promoted_drawdown_metrics(base_dir: Path) -> tuple[float | None, str | None]:
    """Read the live promoted DD controller, never a stale analysis copy."""
    try:
        state = json.loads((base_dir / "drawdown_state.json").read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(state, dict) or state.get("enforcement_enabled") is not True:
        return None, None
    drawdown = _as_decimal(state.get("last_drawdown_decimal"))
    stage = str(state.get("dd_state") or "").strip() or None
    return drawdown, stage


def _current_var_threshold(base_dir: Path, *, loss_guard_stage: str) -> float:
    """Resolve the fixed 1.2/1.4/1.6% budget from current deterministic state."""
    try:
        regime = json.loads((base_dir / "regime_state.json").read_text(encoding="utf-8"))
        regime = regime if isinstance(regime, dict) else {}
    except Exception:
        regime = {}
    try:
        vix_state = json.loads((base_dir / "vix_state.json").read_text(encoding="utf-8"))
        vix_node = vix_state.get("vix") if isinstance(vix_state, dict) else None
        vix = _as_decimal(vix_node.get("level")) if isinstance(vix_node, dict) else None
    except Exception:
        vix = None
    label = str(regime.get("regime") or "")
    upper = label.upper()
    bull = "強気" in label or "BULL" in upper
    stressed = (
        "弱気" in label
        or "BEAR" in upper
        or "DEFENSIVE" in upper
        or "STRESS" in upper
        or loss_guard_stage in {"daily_block", "stage_1", "stage_2", "stage_3"}
        or (vix is not None and vix >= 30)
    )
    return var_threshold_decimal(bull=bull, vix=vix, stressed=stressed)


def _investment_policy_snapshot(
    payload: dict[str, Any], base_dir: Path,
) -> dict[str, Any] | None:
    inline_observation = payload.get("investment_policy_observation")
    if isinstance(inline_observation, dict):
        try:
            inline_denominator = float(inline_observation.get("denominator_jpy"))
        except (TypeError, ValueError):
            inline_denominator = 0.0
        if math.isfinite(inline_denominator) and inline_denominator > 0:
            canonical = _canonical_json(inline_observation)
            return {
                "analysis_id": str(payload.get("analysis_id") or "") or None,
                "as_of": payload.get("analysis_as_of"),
                "denominator_jpy": inline_denominator,
                "observation": inline_observation,
                "snapshot_hash": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            }
    try:
        analysis = json.loads((base_dir / "ai_portfolio_analysis.json").read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(analysis, dict):
        return None
    synthesis = analysis.get("synthesis")
    if not isinstance(synthesis, dict):
        return None
    requested_analysis_id = str(payload.get("analysis_id") or "").strip()
    snapshot_analysis_id = str(synthesis.get("analysis_id") or "").strip()
    if requested_analysis_id and requested_analysis_id != snapshot_analysis_id:
        return None
    observation = synthesis.get("investment_policy_observation")
    if not isinstance(observation, dict):
        return None
    try:
        denominator = float(observation.get("denominator_jpy"))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(denominator) or denominator <= 0:
        return None
    canonical = _canonical_json(observation)
    return {
        "analysis_id": snapshot_analysis_id or None,
        "as_of": analysis.get("as_of"),
        "denominator_jpy": denominator,
        "observation": observation,
        "snapshot_hash": "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _prospective_concentration_observation(
    payload: dict[str, Any], base_dir: Path,
) -> dict[str, Any]:
    """Estimate concentration from persisted facts without refreshing prices/FX.

    This intentionally returns ``None`` for missing inputs rather than guessing
    currency, quantity or portfolio value.  The caller then asks for visible
    human confirmation rather than manufacturing a false precise percentage.
    In particular, this path must not call ``build_portfolio_snapshot()`` or
    ``get_fx_rate_cached()`` because both can refresh ``account.json`` and
    violate the preflight endpoint's read-only contract.
    """
    quantity = _as_decimal(payload.get("quantity"))
    price = _as_decimal(payload.get("price"))
    if quantity is None or price is None or quantity <= 0 or price < 0:
        return {"ratio": None, "status": "quantity_or_price_unresolved"}
    try:
        holdings = json.loads((base_dir / "holdings.json").read_text(encoding="utf-8"))
        account = json.loads((base_dir / "account.json").read_text(encoding="utf-8"))
        snapshot = _investment_policy_snapshot(payload, base_dir)
        if not isinstance(holdings, dict) or not isinstance(account, dict) or snapshot is None:
            return {"ratio": None, "status": "current_policy_snapshot_unavailable"}
        total = float(snapshot["denominator_jpy"])
        from instrument_metadata import canonical_ticker

        ticker = canonical_ticker(payload.get("ticker"))
        matching = [
            row for key, row in holdings.items()
            if isinstance(row, dict) and canonical_ticker(row.get("ticker") or key) == ticker
        ]
        shares = 0.0
        for row in matching:
            value = _as_decimal(row.get("shares"))
            if value is None:
                value = _as_decimal(row.get("broker_quantity"))
            if value is None or value < 0:
                return {"ratio": None, "status": "holding_quantity_unavailable"}
            shares += value
        currency = str(payload.get("currency") or "").upper()
        multiplier = 1.0
        if currency == "USD":
            fx = _as_decimal(account.get("fx_rate_usdjpy"))
            if fx is None or not (50 < fx < 500):
                return {"ratio": None, "status": "fx_unavailable"}
            multiplier = fx
        elif currency != "JPY":
            return {"ratio": None, "status": "currency_unresolved"}
        existing = shares * price * multiplier
        notional = quantity * price * multiplier
        direction = str(payload.get("direction") or "").lower()
        if direction in {"buy", "margin_buy"}:
            value = existing + notional
        elif direction == "short":
            # Gross exposure increases even if the short-sale cash is held.
            value = existing + notional
        else:
            value = max(0.0, existing - notional)
        return {
            "ratio": value / total,
            "status": "ok",
            "valuation_source": "household_quantity_x_execution_price_x_current_fx",
            "existing_quantity": shares,
            "existing_value_jpy": round(existing),
            "proposed_notional_jpy": round(notional),
            "denominator_jpy": round(total),
            "analysis_id": snapshot.get("analysis_id"),
            "as_of": snapshot.get("as_of"),
            "snapshot_hash": snapshot.get("snapshot_hash"),
        }
    except Exception:
        return {"ratio": None, "status": "valuation_error"}


def _prospective_concentration(payload: dict[str, Any], base_dir: Path) -> float | None:
    observation = _prospective_concentration_observation(payload, base_dir)
    return _as_decimal(observation.get("ratio"))


def _broad_concentration_context(payload: dict[str, Any], base_dir: Path) -> dict[str, Any]:
    """Resolve broad limits only with complete long-family evidence."""
    try:
        from instrument_metadata import broad_execution_metadata, canonical_ticker

        ticker = canonical_ticker(payload.get("ticker"))
        metadata = broad_execution_metadata(ticker)
        tier = str(payload.get("execution_investment_type") or payload.get("investment_type") or payload.get("tier") or "").lower()
        if not metadata or tier != "long":
            return concentration_limits(investment_type=tier)
        snapshot = _investment_policy_snapshot(payload, base_dir)
        if snapshot is None:
            return concentration_limits(investment_type=None)
        family = str(metadata["broad_family"])
        positions = (snapshot["observation"].get("positions") or [])
        matching = [row for row in positions if isinstance(row, dict) and canonical_ticker(row.get("canonical_instrument_id")) == ticker]
        # A mixed tier must retain the strictest ordinary cap.
        if any(str(row.get("cap_basis_tier") or row.get("dominant_tier") or "long").lower() != "long" for row in matching):
            return concentration_limits(investment_type="medium")
        total = _as_decimal(snapshot.get("denominator_jpy"))
        if total is None or total <= 0:
            return concentration_limits(investment_type=None)
        currency = str(payload.get("currency") or "").upper()
        quantity = _as_decimal(payload.get("quantity"))
        price = _as_decimal(payload.get("price") or payload.get("decision_price") or payload.get("limit_price"))
        if total is None or total <= 0 or quantity is None or quantity <= 0 or price is None or price < 0:
            return concentration_limits()
        notional = quantity * price
        currency = str(payload.get("currency") or "").upper()
        if currency == "USD":
            account = json.loads((base_dir / "account.json").read_text(encoding="utf-8"))
            fx = _as_decimal(account.get("fx_rate_usdjpy")) if isinstance(account, dict) else None
            if fx is None or not 50 < fx < 500:
                return concentration_limits()
            notional *= fx
        elif currency != "JPY":
            return concentration_limits()
        family = str(metadata["broad_family"])
        family_value = 0.0
        for row in positions:
            if not isinstance(row, dict):
                continue
            row_meta = broad_execution_metadata(row.get("canonical_instrument_id"))
            if not row_meta or row_meta.get("broad_family") != family:
                continue
            value = _as_decimal(row.get("value_jpy"))
            if value is None:
                return concentration_limits()
            family_value += value
        limits = concentration_limits(broad_family=family, investment_type="long")
        return {
            **limits,
            "family_concentration_decimal": (family_value + notional) / total,
            "family_valuation_source": "current_investment_policy_observation",
            "valuation_as_of": snapshot.get("as_of"),
            "valuation_snapshot_hash": snapshot.get("snapshot_hash"),
        }
    except Exception:
        return concentration_limits()


def _scheduled_broad_permission_assessment(
    payload: dict[str, Any],
    *,
    canonical_drawdown_stage: str | None,
    proposed_notional_jpy: Any,
) -> dict[str, Any]:
    source = str(payload.get("source") or "").strip().lower()
    direction = str(payload.get("direction") or "").strip().lower()
    stage = str(canonical_drawdown_stage or "").strip()
    blocking_stages = {"block", "derisk_review", "freeze", "objective_breach"}
    if source != "scheduled_broad_deployment" or direction not in {"buy", "margin_buy"}:
        return {"required": False, "valid": None, "stage": stage or None}
    if stage not in blocking_stages:
        return {"required": False, "valid": None, "stage": stage or None}
    valid = False
    if stage in {"block", "derisk_review"}:
        try:
            from capital_deployment import validate_scheduled_broad_permission

            valid = validate_scheduled_broad_permission(
                payload,
                canonical_dd_stage=stage,
                requested_notional_jpy=proposed_notional_jpy,
            )
        except Exception:
            valid = False
    return {
        "required": True,
        "valid": bool(valid),
        "stage": stage,
        "permission_id": str(
            ((payload.get("capital_deployment_permission") or {}).get("permission_id"))
            if isinstance(payload.get("capital_deployment_permission"), dict)
            else ""
        ) or None,
    }


def evaluate_preflight_decision(payload: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    """Compute the token-free execution decision without writing state."""
    daily, rolling = _guard_metrics(base_dir)
    loss = loss_guard_state(
        daily_pnl_decimal=daily,
        rolling_30_pnl_decimal=rolling,
    )
    var, var_source, var_snapshot_as_of = _analysis_risk_metrics(base_dir)
    drawdown, drawdown_stage = _promoted_drawdown_metrics(base_dir)
    var_threshold = _current_var_threshold(
        base_dir, loss_guard_stage=str(loss.get("loss_guard_stage") or ""),
    )
    # Keep the scalar helper as the decision seam.  Besides preserving the
    # long-standing test/consumer contract, this lets an operator replace the
    # calculator without silently changing the audit metadata schema.
    concentration = _prospective_concentration(payload, base_dir)
    concentration_observation = _prospective_concentration_observation(payload, base_dir)
    if concentration != _as_decimal(concentration_observation.get("ratio")):
        concentration_observation = {
            **concentration_observation,
            "ratio": concentration,
            "status": "decision_seam_override",
        }
    concentration_context = _broad_concentration_context(payload, base_dir)
    direction = str(payload.get("direction") or "").lower()
    risk_increasing = direction in {"buy", "margin_buy", "short"}
    short_positions = _current_short_positions(base_dir)
    scheduled_broad_permission = _scheduled_broad_permission_assessment(
        payload,
        canonical_drawdown_stage=drawdown_stage,
        proposed_notional_jpy=concentration_observation.get("proposed_notional_jpy"),
    )
    decision = classify_execution_risk(
        daily_pnl_decimal=daily,
        rolling_30_pnl_decimal=rolling,
        var_1d_95_decimal=var,
        concentration_decimal=concentration,
        canonical_drawdown_decimal=drawdown,
        canonical_drawdown_stage=drawdown_stage,
        var_policy_threshold_decimal=var_threshold,
        risk_increasing=risk_increasing,
        action_direction=direction,
        current_short_positions=short_positions,
        concentration_caution_decimal=concentration_context.get("caution_decimal"),
        concentration_cap_decimal=concentration_context.get("cap_decimal"),
        family_concentration_decimal=concentration_context.get("family_concentration_decimal"),
        family_concentration_cap_decimal=concentration_context.get("family_cap_decimal"),
    )
    if (
        scheduled_broad_permission.get("required") is True
        and scheduled_broad_permission.get("valid") is not True
    ):
        hard_reasons = list(decision.get("hard_reasons") or [])
        hard_reasons.append({
            "code": "scheduled_broad_permission_invalid",
            "message": "The scheduled broad permission no longer matches the current order, route, or plan.",
        })
        decision = {
            **decision,
            "disposition": "hard_reject",
            "hard_reasons": hard_reasons,
        }
    digest = action_digest(payload)
    metrics = {
        "daily_pnl_decimal": daily,
        "rolling_30_pnl_decimal": rolling,
        "var_1d_95_decimal": var,
        "var_policy_threshold_decimal": var_threshold,
        "var_source": var_source,
        "var_snapshot_as_of": var_snapshot_as_of,
        "prospective_concentration_decimal": concentration,
        "concentration_valuation": concentration_observation,
        "concentration_assessment": concentration_context,
        "canonical_drawdown_decimal": drawdown,
        "canonical_drawdown_stage": drawdown_stage,
        "current_short_positions": short_positions,
        "scheduled_broad_permission": scheduled_broad_permission,
    }
    return {
        **decision,
        "policy_version": RISK_POLICY_VERSION,
        "preflight_version": PREFLIGHT_VERSION,
        "action_digest": digest,
        "metrics": metrics,
    }


def evaluate_preflight(payload: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    """Compute an execution-time decision and bind it to an expiring token."""
    result = evaluate_preflight_decision(payload, base_dir=base_dir)
    review_context = {
        "policy_version": RISK_POLICY_VERSION,
        "reason_codes": [str(item.get("code") or "") for item in result["reasons"]],
        "hard_reason_codes": [str(item.get("code") or "") for item in result["hard_reasons"]],
        "metrics": result["metrics"],
    }
    token, expires_at = issue_preflight_token(
        digest=result["action_digest"],
        disposition=result["disposition"],
        review_context=review_context,
    )
    return {
        **result,
        "preflight_token": token,
        "expires_at": expires_at,
    }


def append_acknowledgement(
    *,
    base_dir: Path,
    token: str,
    digest: str,
    acknowledgement_reason: str,
) -> dict[str, Any]:
    """Append one explicit human acknowledgement; never modify the preflight."""
    claims = validate_preflight_token(token, digest=digest)
    if claims.get("disposition") != "confirmation_required":
        raise ValueError("this preflight does not require a human acknowledgement")
    reason = str(acknowledgement_reason or "").strip()
    if not reason:
        raise ValueError("acknowledgement_reason is required")
    row = {
        "at": datetime.now(timezone.utc).isoformat(),
        "action_digest": digest,
        "preflight_token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "expires_at": claims.get("expires_at"),
        "reason": reason,
        "policy_version": RISK_POLICY_VERSION,
        "review_context": claims.get("review_context") or {},
    }
    with (base_dir / PREFLIGHT_ACK_LOG).open("a", encoding="utf-8") as handle:
        handle.write(_canonical_json(row) + "\n")
    return row


def has_acknowledgement(*, base_dir: Path, token: str, digest: str) -> bool:
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    path = base_dir / PREFLIGHT_ACK_LOG
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("action_digest") == digest and row.get("preflight_token_sha256") == token_hash:
                return True
    except FileNotFoundError:
        return False
    except Exception:
        return False
    return False
