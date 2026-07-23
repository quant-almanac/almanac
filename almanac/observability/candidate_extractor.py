"""Candidate extractor — READ-side adapter (plan Round 6 C6-1 / Round 11 #C).

This module is the **one and only** place in the codebase that knows the
schemas emitted by the existing producer surfaces:

- ``analyst/__init__.py``'s four Sonnet tiers (long / medium /
  short_positions / short_selling).
- ``analyst/__init__.py``'s Opus ``synthesis.priority_actions``.
- DeepSeek margin / short producers (slot reserved; payload schema is
  finalized when those modules ship in Phase 2).
- ``catalyst_layer.py`` (Week 2 producer — extractor included so the
  Phase 1-C boundary is complete now).

Downstream consumers (``agent_attribution_log``,
``catalyst_hypothesis_log``, ``recommendation_verifier``) see only the
unified ``candidate_packet`` schema (plan §6.9), so a future producer
rewrite is invisible past this module.

Stable hypothesis_id for legacy producers
-----------------------------------------

A Sonnet tier that keeps recommending the same ``(ticker, action_type,
horizon)`` across days is **one ongoing hypothesis**, not a fresh one per
day. The extractor therefore builds ``hypothesis_id`` from a stable
``source_event_id = f"sonnet:{source_agent}"`` (or
``f"deepseek:{source_agent}"``) so the Round 8 #1 / Round 9 #1 multi-day
join key keeps working for legacy producers that have no real
``source_event_id`` to give. catalyst_layer outputs supply a concrete
``source_event_id`` (news URL hash, scenario key, revision event) and
naturally bypass this fallback.

Producer agent role labels
--------------------------

The four Sonnet tiers and the Opus synthesizer each map to a canonical
``source_agent`` string used elsewhere (attribution log, reliability
tracking). Keep them in sync with the constants below if you add a tier.
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

from .ids import compute_hypothesis_id, new_row_id
from .signal_history import LEGACY_HYPOTHESIS_TYPE
from .status import CandidateStatus
from insider_restrictions import is_restricted_ticker

__all__ = [
    # Canonical agent name constants
    "AGENT_LONG_SONNET",
    "AGENT_MEDIUM_SONNET",
    "AGENT_SWING_SONNET",
    "AGENT_SHORT_SONNET",
    "AGENT_MARGIN_DEEPSEEK",
    "AGENT_SHORT_DEEPSEEK",
    "AGENT_CATALYST_LAYER",
    "AGENT_OPUS_FINAL",
    # Mapping helpers
    "infer_action_type",
    "infer_direction",
    "DEFAULT_HORIZON_DAYS",
    "final_action_hypothesis_identity",
    "final_action_hypothesis_id",
    # Per-producer extractors
    "extract_from_sonnet_tier",
    "extract_from_synthesis",
    "extract_from_deepseek_margin",
    "extract_from_deepseek_short",
    "extract_from_catalyst_layer",
    # Orchestrator
    "extract_all",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Canonical agent names (used in attribution_log + reliability tracking)
# ---------------------------------------------------------------------------

AGENT_LONG_SONNET = "long_sonnet"
AGENT_MEDIUM_SONNET = "medium_sonnet"
AGENT_SWING_SONNET = "swing_sonnet"           # = "short_positions" tier in legacy code
AGENT_SHORT_SONNET = "short_selling_sonnet"
AGENT_MARGIN_DEEPSEEK = "margin_deepseek"
AGENT_SHORT_DEEPSEEK = "short_deepseek"
AGENT_CATALYST_LAYER = "catalyst_layer"
AGENT_OPUS_FINAL = "opus_final"


# ---------------------------------------------------------------------------
# Default horizons per Sonnet tier (matches plan paper-trade windows)
# ---------------------------------------------------------------------------

DEFAULT_HORIZON_DAYS: dict[str, int] = {
    AGENT_LONG_SONNET: 20,
    AGENT_MEDIUM_SONNET: 10,
    AGENT_SWING_SONNET: 5,
    AGENT_SHORT_SONNET: 10,
    AGENT_MARGIN_DEEPSEEK: 10,
    AGENT_SHORT_DEEPSEEK: 10,
    AGENT_CATALYST_LAYER: 20,   # overridden by payload when present
    AGENT_OPUS_FINAL: 20,
}


# ---------------------------------------------------------------------------
# Action type & direction mapping
# ---------------------------------------------------------------------------

#: How legacy producer ``type`` strings map to candidate_packet action_type.
#: Plan §6.9 caps action_type at six values (no ``no_action`` per R11 #4).
_ACTION_TYPE_MAP: dict[str, str] = {
    # Long-direction openings
    "buy": "buy",
    "dca": "buy",            # dollar-cost-average is a buy variant
    "rebuy": "buy",
    "add": "buy",
    # Long-direction reductions
    "trim": "trim",
    "take_profit": "trim",   # partial reduction realizing gains
    "reduce": "trim",
    "rebalance": "trim",     # conservative: assume reducing the overweight side
    "sell": "sell",
    "exit": "sell",
    "close": "sell",
    "cover": "buy",          # closing a short = buy to cover
    # Margin / short
    "margin_buy": "margin_buy",
    "short": "short_sell",
    "short_sell": "short_sell",
    # Neutral
    "hold": "hold",
    "watch": "hold",
}


def infer_action_type(raw: str | None) -> str | None:
    """Map a legacy ``type`` value to the candidate_packet ``action_type``.

    Returns ``None`` for unrecognized strings so callers can skip the row
    rather than emit something the downstream schema validator would
    reject. ``no_action`` is intentionally unmapped — it belongs to
    ``portfolio_decision_state`` (Round 11 #4).
    """
    if raw is None:
        return None
    return _ACTION_TYPE_MAP.get(str(raw).strip().lower())


_DIRECTION_MAP: dict[str, str] = {
    "buy": "long",
    "margin_buy": "long",
    "short_sell": "short",
    "trim": "reduce",
    "sell": "reduce",
    "hold": "neutral",
}


def infer_direction(action_type: str) -> str:
    """Map ``action_type`` to ``direction`` (``long`` / ``short`` /
    ``reduce`` / ``neutral``). Falls back to ``neutral`` for safety."""
    return _DIRECTION_MAP.get(action_type, "neutral")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _legacy_source_event_id(source_agent: str) -> str:
    """Build a stable ``source_event_id`` for producers that lack one.

    The result is constant across days so :func:`compute_hypothesis_id`
    yields the same ``hypothesis_id`` for the same logical
    ``(ticker, action_type, horizon)`` tuple every time the producer
    reaffirms it — preserving multi-day attribution and opportunity-cost
    measurement (Round 8 #1).
    """
    if not source_agent:
        raise ValueError("source_agent must be non-empty")
    return f"legacy_producer:{source_agent}"


def final_action_hypothesis_identity(action: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the canonical Opus-final identity for one adopted action.

    This is the shared contract between the Opus final-action extractor and
    the runtime attribution writer.  Keeping both sides on this legacy
    producer namespace lets existing outcome rows join back to attribution
    rows by ``hypothesis_id``.
    """
    if not isinstance(action, Mapping):
        return None
    ticker = str(action.get("ticker") or "").strip()
    raw_action_type = action.get("type")
    if raw_action_type is None:
        raw_action_type = action.get("action_type")
    action_type = infer_action_type(raw_action_type)
    if not ticker or not action_type or is_restricted_ticker(ticker):
        return None
    hypothesis_type = LEGACY_HYPOTHESIS_TYPE
    horizon_days = DEFAULT_HORIZON_DAYS[AGENT_OPUS_FINAL]
    source_event_id = _legacy_source_event_id(AGENT_OPUS_FINAL)
    hypothesis_id = compute_hypothesis_id(
        ticker=ticker,
        action_type=action_type,
        hypothesis_type=hypothesis_type,
        horizon_days=horizon_days,
        source_event_id=source_event_id,
    )
    return {
        "hypothesis_id": hypothesis_id,
        "ticker": ticker,
        "action_type": action_type,
        "hypothesis_type": hypothesis_type,
        "horizon_days": horizon_days,
        "source_event_id": source_event_id,
    }


def final_action_hypothesis_id(action: Mapping[str, Any]) -> str:
    """Return the canonical ``hypothesis_id`` for one adopted Opus action."""
    identity = final_action_hypothesis_identity(action)
    if identity is None:
        raise ValueError("final action cannot be converted to a hypothesis identity")
    return str(identity["hypothesis_id"])


def _build_packet(
    *,
    ticker: str,
    action_type: str,
    hypothesis_type: str,
    horizon_days: int,
    source_event_id: str,
    source_agents: list[str],
    analysis_id: str,
    analysis_date: str,
    confidence_pct: int | None,
    evidence_summary: str | None,
    risk_flags: list[str] | None = None,
    constraints: list[str] | None = None,
    invalidation_summary: str | None = None,
    suggested_size: str | None = None,
    extracted_from: str | None = None,
    created_at: str | None = None,
    candidate_status: str = CandidateStatus.generated.value,
    expected_alpha_bps: float | None = None,
) -> dict[str, Any]:
    """Assemble a candidate_packet dict from validated inputs.

    The ``hypothesis_id`` is computed from the canonical 5-tuple, and a
    fresh ``row_id`` is allocated for each call so multiple extractors
    on the same day each contribute a distinct row even if they share
    the same ``hypothesis_id``.
    """
    if not ticker:
        raise ValueError("ticker must be non-empty")
    if not action_type:
        raise ValueError("action_type must be non-empty")
    hypothesis_id = compute_hypothesis_id(
        ticker=ticker,
        action_type=action_type,
        hypothesis_type=hypothesis_type,
        horizon_days=horizon_days,
        source_event_id=source_event_id,
    )
    return {
        "candidate_id": hypothesis_id,          # alias for §6.9 readers
        "hypothesis_id": hypothesis_id,
        "row_id": new_row_id(),
        "analysis_id": analysis_id,
        "analysis_date": analysis_date,
        "ticker": ticker,
        "action_type": action_type,
        "direction": infer_direction(action_type),
        "source_agents": list(source_agents),
        "hypothesis_type": hypothesis_type,
        "time_horizon_days": horizon_days,
        "expected_alpha_bps": expected_alpha_bps,
        "confidence_pct": confidence_pct,
        "evidence_summary": evidence_summary,
        "risk_flags": list(risk_flags) if risk_flags else [],
        "constraints": list(constraints) if constraints else [],
        "invalidation_summary": invalidation_summary,
        "suggested_size": suggested_size,
        "candidate_status": candidate_status,
        "extracted_from": extracted_from,
        "created_at": created_at,
    }


def _coerce_confidence(raw: Any) -> int | None:
    """Producers sometimes emit float or string; cap to ``[0, 100]``."""
    if raw is None:
        return None
    try:
        value = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, value))


# ---------------------------------------------------------------------------
# Per-producer extractors
# ---------------------------------------------------------------------------


def extract_from_sonnet_tier(
    tier_output: Mapping[str, Any] | None,
    *,
    source_agent: str,
    analysis_id: str,
    analysis_date: str,
    horizon_days: int | None = None,
    hypothesis_type: str = LEGACY_HYPOTHESIS_TYPE,
    extracted_from: str = "ai_portfolio_analysis.json",
) -> list[dict[str, Any]]:
    """Extract candidate packets from one Sonnet tier's output.

    ``tier_output`` is the dict produced by ``_analyze_long`` /
    ``_analyze_medium`` / ``_analyze_short_positions`` /
    ``_analyze_short_selling`` in :mod:`analyst.__init__` — it has the
    shape ``{overall_stance, priority_actions: [...], ...}``.

    Returns one packet per ``priority_actions`` entry whose ``type`` maps
    to a known ``action_type``. Rows with unmappable ``type`` (or missing
    ``ticker``) are silently skipped and logged at WARNING so the run
    completes even when one tier emits noise.
    """
    if not tier_output:
        return []
    actions = tier_output.get("priority_actions") or []
    if not isinstance(actions, list):
        logger.warning(
            "tier_output.priority_actions is %s, expected list; skipping",
            type(actions).__name__,
        )
        return []
    horizon = horizon_days or DEFAULT_HORIZON_DAYS.get(source_agent, 20)
    source_event_id = _legacy_source_event_id(source_agent)
    out: list[dict[str, Any]] = []
    for entry in actions:
        if not isinstance(entry, dict):
            continue
        ticker = entry.get("ticker")
        action_type = infer_action_type(entry.get("type"))
        if not ticker or not action_type or is_restricted_ticker(ticker):
            logger.warning(
                "skipping %s entry: ticker=%r type=%r action_type=%r",
                source_agent, ticker, entry.get("type"), action_type,
            )
            continue
        # Build a compact evidence summary from action+reason. We don't
        # truncate aggressively because Opus consumes this directly.
        evidence_parts = []
        for field in ("action", "reason"):
            value = entry.get(field)
            if value:
                evidence_parts.append(str(value).strip())
        evidence_summary = " | ".join(evidence_parts) if evidence_parts else None
        urgency = entry.get("urgency")
        risk_flags: list[str] = []
        if urgency:
            risk_flags.append(f"urgency:{urgency}")
        rank = entry.get("rank")
        if rank is not None:
            risk_flags.append(f"tier_rank:{rank}")
        packet = _build_packet(
            ticker=ticker,
            action_type=action_type,
            hypothesis_type=hypothesis_type,
            horizon_days=horizon,
            source_event_id=source_event_id,
            source_agents=[source_agent],
            analysis_id=analysis_id,
            analysis_date=analysis_date,
            confidence_pct=_coerce_confidence(entry.get("confidence_pct")),
            evidence_summary=evidence_summary,
            risk_flags=risk_flags,
            suggested_size=entry.get("amount_hint"),
            extracted_from=extracted_from,
            created_at=entry.get("created_at"),
        )
        out.append(packet)
    return out


def extract_from_synthesis(
    synthesis: Mapping[str, Any] | None,
    *,
    analysis_id: str,
    analysis_date: str,
    extracted_from: str = "ai_portfolio_analysis.json#synthesis",
) -> list[dict[str, Any]]:
    """Extract candidate packets from the Opus synthesis output.

    ``synthesis.priority_actions`` is the final adopted list; every entry
    becomes a candidate packet with ``source_agents=[AGENT_OPUS_FINAL]``
    and ``candidate_status=adopted`` since these are the actions the
    portfolio decision actually committed to.
    """
    if not synthesis:
        return []
    actions = synthesis.get("priority_actions") or []
    if not isinstance(actions, list):
        return []
    out: list[dict[str, Any]] = []
    for entry in actions:
        if not isinstance(entry, dict):
            continue
        identity = final_action_hypothesis_identity(entry)
        if identity is None:
            continue
        evidence_parts = [str(entry[k]).strip() for k in ("action", "reason") if entry.get(k)]
        packet = _build_packet(
            ticker=identity["ticker"],
            action_type=identity["action_type"],
            hypothesis_type=identity["hypothesis_type"],
            horizon_days=identity["horizon_days"],
            source_event_id=identity["source_event_id"],
            source_agents=[AGENT_OPUS_FINAL],
            analysis_id=analysis_id,
            analysis_date=analysis_date,
            confidence_pct=_coerce_confidence(entry.get("confidence_pct")),
            evidence_summary=" | ".join(evidence_parts) if evidence_parts else None,
            risk_flags=[f"urgency:{entry['urgency']}"] if entry.get("urgency") else [],
            suggested_size=entry.get("amount_hint"),
            extracted_from=extracted_from,
            candidate_status=CandidateStatus.adopted.value,
        )
        out.append(packet)
    return out


def extract_from_deepseek_margin(
    payload: Mapping[str, Any] | None,
    *,
    analysis_id: str,
    analysis_date: str,
    extracted_from: str = "margin_long_state.json",
) -> list[dict[str, Any]]:
    """Extract candidate packets from the DeepSeek margin-long producer.

    The exact wire schema of the DeepSeek margin output is still being
    finalized in Phase 2, so we accept the documented surface
    (``candidates`` list with ``{ticker, action, confidence, reason}``)
    and skip anything that does not parse. The interface keeps the call
    site stable so the production wiring is a one-liner once the schema
    is locked.
    """
    return _extract_from_deepseek_like(
        payload,
        source_agent=AGENT_MARGIN_DEEPSEEK,
        analysis_id=analysis_id,
        analysis_date=analysis_date,
        default_action_type="margin_buy",
        extracted_from=extracted_from,
    )


def extract_from_deepseek_short(
    payload: Mapping[str, Any] | None,
    *,
    analysis_id: str,
    analysis_date: str,
    extracted_from: str = "short_state.json",
) -> list[dict[str, Any]]:
    """Extract candidate packets from the DeepSeek short-sell producer."""
    return _extract_from_deepseek_like(
        payload,
        source_agent=AGENT_SHORT_DEEPSEEK,
        analysis_id=analysis_id,
        analysis_date=analysis_date,
        default_action_type="short_sell",
        extracted_from=extracted_from,
    )


def _extract_from_deepseek_like(
    payload: Mapping[str, Any] | None,
    *,
    source_agent: str,
    analysis_id: str,
    analysis_date: str,
    default_action_type: str,
    extracted_from: str,
) -> list[dict[str, Any]]:
    """Shared implementation for both DeepSeek extractors."""
    if not payload:
        return []
    candidates = payload.get("candidates") or []
    if not isinstance(candidates, list):
        return []
    horizon = DEFAULT_HORIZON_DAYS[source_agent]
    source_event_id = _legacy_source_event_id(source_agent)
    out: list[dict[str, Any]] = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        ticker = c.get("ticker")
        if not ticker or is_restricted_ticker(ticker):
            continue
        action_type = infer_action_type(c.get("action")) or default_action_type
        if action_type not in _DIRECTION_MAP:
            continue
        out.append(
            _build_packet(
                ticker=ticker,
                action_type=action_type,
                hypothesis_type=LEGACY_HYPOTHESIS_TYPE,
                horizon_days=horizon,
                source_event_id=source_event_id,
                source_agents=[source_agent],
                analysis_id=analysis_id,
                analysis_date=analysis_date,
                confidence_pct=_coerce_confidence(c.get("confidence") or c.get("confidence_pct")),
                evidence_summary=c.get("reason"),
                risk_flags=list(c.get("risk_flags") or []),
                constraints=list(c.get("constraints") or []),
                suggested_size=c.get("amount_hint") or c.get("suggested_size"),
                extracted_from=extracted_from,
            )
        )
    return out


def extract_from_catalyst_layer(
    payload: Mapping[str, Any] | None,
    *,
    analysis_id: str,
    analysis_date: str,
    extracted_from: str = "catalyst_layer.compact",
) -> list[dict[str, Any]]:
    """Extract candidate packets from the catalyst_layer compact output.

    Unlike legacy producers, catalyst_layer supplies an explicit
    ``source_event_id`` (news URL hash / scenario key / revision event)
    and a real ``hypothesis_type``, so we don't fall back to the
    legacy synthesis. Rows missing those keys are skipped — emitting a
    fabricated id would corrupt the cross-day join.
    """
    if not payload:
        return []
    hypotheses = payload.get("hypotheses") or payload.get("candidates") or []
    if not isinstance(hypotheses, list):
        return []
    out: list[dict[str, Any]] = []
    for h in hypotheses:
        if not isinstance(h, dict):
            continue
        ticker = h.get("primary_ticker") or h.get("ticker")
        action_type = infer_action_type(h.get("action_type"))
        hypothesis_type = h.get("hypothesis_type")
        source_event_id = h.get("source_event_id")
        if (
            not ticker
            or is_restricted_ticker(ticker)
            or not action_type
            or not hypothesis_type
            or not source_event_id
        ):
            logger.warning(
                "catalyst_layer entry missing required fields; "
                "ticker=%r action_type=%r hypothesis_type=%r source_event_id=%r",
                ticker, action_type, hypothesis_type, source_event_id,
            )
            continue
        horizon = int(h.get("time_horizon_days") or DEFAULT_HORIZON_DAYS[AGENT_CATALYST_LAYER])
        out.append(
            _build_packet(
                ticker=ticker,
                action_type=action_type,
                hypothesis_type=hypothesis_type,
                horizon_days=horizon,
                source_event_id=source_event_id,
                source_agents=list(h.get("source_agents") or [AGENT_CATALYST_LAYER]),
                analysis_id=analysis_id,
                analysis_date=analysis_date,
                confidence_pct=_coerce_confidence(h.get("confidence_pct")),
                evidence_summary=h.get("evidence_summary"),
                risk_flags=list(h.get("risk_flags") or []),
                constraints=list(h.get("constraints") or []),
                invalidation_summary=h.get("invalidation_summary"),
                suggested_size=h.get("suggested_size"),
                extracted_from=extracted_from,
                expected_alpha_bps=h.get("expected_alpha_bps"),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def extract_all(
    *,
    analysis_id: str,
    analysis_date: str,
    long_tier: Mapping[str, Any] | None = None,
    medium_tier: Mapping[str, Any] | None = None,
    swing_tier: Mapping[str, Any] | None = None,
    short_tier: Mapping[str, Any] | None = None,
    synthesis: Mapping[str, Any] | None = None,
    margin_deepseek: Mapping[str, Any] | None = None,
    short_deepseek: Mapping[str, Any] | None = None,
    catalyst_layer: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Run every extractor and concatenate the candidate packets.

    Order of producers matches the natural flow of
    ``analyzer.py``: tier-level Sonnets first, then synthesis (Opus
    final), then DeepSeek specialists, then catalyst_layer. The order
    only affects test predictability — downstream consumers join on
    ``hypothesis_id`` and never assume row order.
    """
    out: list[dict[str, Any]] = []
    out.extend(
        extract_from_sonnet_tier(
            long_tier,
            source_agent=AGENT_LONG_SONNET,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_sonnet_tier(
            medium_tier,
            source_agent=AGENT_MEDIUM_SONNET,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_sonnet_tier(
            swing_tier,
            source_agent=AGENT_SWING_SONNET,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_sonnet_tier(
            short_tier,
            source_agent=AGENT_SHORT_SONNET,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_synthesis(
            synthesis,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_deepseek_margin(
            margin_deepseek,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_deepseek_short(
            short_deepseek,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    out.extend(
        extract_from_catalyst_layer(
            catalyst_layer,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    return out
