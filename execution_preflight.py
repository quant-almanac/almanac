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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from almanac.runtime_config import load_api_key
from risk_policy import (
    POLICY,
    RISK_POLICY_VERSION,
    classify_execution_risk,
    loss_guard_state,
    var_threshold_decimal,
)


PREFLIGHT_VERSION = "2026-08-v1"
PREFLIGHT_TTL_MINUTES = 60
PREFLIGHT_ACK_LOG = "execution_preflight_acknowledgements.jsonl"


def _canonical_json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def action_identity(payload: dict[str, Any]) -> dict[str, Any]:
    """Fields that cannot change after a user has reviewed the preflight."""
    position_keys = payload.get("execution_position_keys") or []
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


def _prospective_concentration(payload: dict[str, Any], base_dir: Path) -> float | None:
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
        return None
    try:
        guard = json.loads((base_dir / "guard_state.json").read_text(encoding="utf-8"))
        analysis = json.loads((base_dir / "ai_portfolio_analysis.json").read_text(encoding="utf-8"))
        holdings = json.loads((base_dir / "holdings.json").read_text(encoding="utf-8"))
        account = json.loads((base_dir / "account.json").read_text(encoding="utf-8"))
        if not all(isinstance(item, dict) for item in (guard, analysis, holdings, account)):
            return None
        total = _as_decimal(guard.get("portfolio_value")) or _as_decimal(analysis.get("portfolio_total"))
        if total is None or total <= 0:
            return None
        ticker = str(payload.get("ticker") or "")
        matching = [
            row for key, row in holdings.items()
            if isinstance(row, dict) and str(row.get("ticker") or key) == ticker
        ]
        existing = 0.0
        for row in matching:
            value = _as_decimal(row.get("current_value_jpy"))
            if value is None:
                value = _as_decimal(row.get("broker_position_value_jpy"))
            if value is None:
                return None
            existing += value
        currency = str(payload.get("currency") or "").upper()
        notional = quantity * price
        if currency == "USD":
            fx = _as_decimal(account.get("fx_rate_usdjpy"))
            if fx is None or not (50 < fx < 500):
                return None
            notional *= fx
        elif currency != "JPY":
            return None
        direction = str(payload.get("direction") or "").lower()
        if direction in {"buy", "margin_buy"}:
            value = existing + notional
            denominator = total
        elif direction == "short":
            # Gross exposure increases even if the short-sale cash is held.
            value = existing + notional
            denominator = total
        else:
            return existing / total
        return value / denominator if denominator > 0 else None
    except Exception:
        return None


def evaluate_preflight(payload: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    """Compute an execution-time decision without writing state."""
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
    concentration = _prospective_concentration(payload, base_dir)
    direction = str(payload.get("direction") or "").lower()
    risk_increasing = direction in {"buy", "margin_buy", "short"}
    decision = classify_execution_risk(
        daily_pnl_decimal=daily,
        rolling_30_pnl_decimal=rolling,
        var_1d_95_decimal=var,
        concentration_decimal=concentration,
        canonical_drawdown_decimal=drawdown,
        canonical_drawdown_stage=drawdown_stage,
        var_policy_threshold_decimal=var_threshold,
        risk_increasing=risk_increasing,
    )
    digest = action_digest(payload)
    metrics = {
        "daily_pnl_decimal": daily,
        "rolling_30_pnl_decimal": rolling,
        "var_1d_95_decimal": var,
        "var_policy_threshold_decimal": var_threshold,
        "var_source": var_source,
        "var_snapshot_as_of": var_snapshot_as_of,
        "prospective_concentration_decimal": concentration,
        "canonical_drawdown_decimal": drawdown,
        "canonical_drawdown_stage": drawdown_stage,
    }
    review_context = {
        "policy_version": RISK_POLICY_VERSION,
        "reason_codes": [str(item.get("code") or "") for item in decision["reasons"]],
        "hard_reason_codes": [str(item.get("code") or "") for item in decision["hard_reasons"]],
        "metrics": metrics,
    }
    token, expires_at = issue_preflight_token(
        digest=digest,
        disposition=decision["disposition"],
        review_context=review_context,
    )
    return {
        **decision,
        "policy_version": RISK_POLICY_VERSION,
        "preflight_version": PREFLIGHT_VERSION,
        "action_digest": digest,
        "preflight_token": token,
        "expires_at": expires_at,
        "metrics": metrics,
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
