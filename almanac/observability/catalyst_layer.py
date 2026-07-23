"""Catalyst layer — top-level orchestrator that integrates every Phase 1/2 module.

Architecture (plan §4)
----------------------

The catalyst_layer is the **public-facing entry point** that ``analyzer.py`` /
the Opus prompt-injection pipeline calls to get a ranked list of investment
hypotheses for a given trading day.

It integrates four distinct input sources:

1. **revision_tracker** output (``revision_state.json``) — tickers with
   upward/downward earnings guidance revisions + ``surprise_score`` +
   ``priced_in_penalty`` computed by :mod:`almanac.observability.revision_tracker`.

2. **scenario_state.json** — active macro scenarios (readiness ≥ 60%) produced
   by the scenario engine. Scenarios with ``enabled_for_decision=true`` generate
   hypotheses for the decision pipeline; ``observe_only=true`` scenarios are
   generated for measurement only and excluded from the returned top-list.

3. **proxy_mapper** output (``proxy_seed_map.json``) — non-listed entity →
   listed ticker mappings. When a news entity detected today matches a seed in
   the map, one ``ipo_proxy`` hypothesis is emitted per listed proxy ticker.

4. **legacy producers** (``ai_portfolio_analysis.json`` via
   :func:`almanac.observability.candidate_extractor.extract_all`) — the four
   Sonnet tiers, Opus synthesis, and DeepSeek specialists.  The extractor is
   the adapter that normalises the heterogeneous producer schemas.

Synthesis flow
--------------

::

    synthesize_from_revision_state()   ──┐
    synthesize_from_active_scenarios() ──┤  dedupe_by_hypothesis_id()
    synthesize_from_proxy_predictions()──┤       ↓
    synthesize_from_legacy_producers() ──┘  rank_by_catalyst_score()
                                                 ↓
                                          top_n selection
                                                 ↓
                                    write_catalyst_hypothesis_generated()
                                         (append-only, for ALL hypotheses)
                                                 ↓
                                          CatalystOutput returned

The module never touches ``analyzer.py`` internals; prompt injection is the
caller's responsibility.

Locked invariants
-----------------

- Round 4 C4-3: ``priced_in_penalty`` caps at 0.6 (consumed from revision_tracker;
  never re-capped here since the producer already enforces it).
- Round 8 #1 / Round 9 #1: ``hypothesis_id`` is date-independent.
- Round 9 #3: ``write_catalyst_hypothesis_generated`` is the only write path.
- Round 9 #6: ``benchmark_basket`` + ``benchmark_currency_normalized_to`` required.
- Round 11 #4: no ``no_action`` action_type.
- Round 11 #C: ``enabled_for_decision=false`` scenarios are skipped except when
  ``observe_only=true``; observe-only hypotheses are logged for measurement and
  excluded from decision top-lists.
- Round 12 P1 #2: explicit currency on every hypothesis.
- Codex Round 12 P2 #4: ``primary_ticker`` kwarg on writer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ids import compute_hypothesis_id
from .logs import write_catalyst_hypothesis_filtered, write_catalyst_hypothesis_generated
from .status import CandidateStatus

__all__ = [
    # Data classes
    "CatalystHypothesis",
    "CatalystOutput",
    # Pure scoring
    "compute_catalyst_score",
    # Pure synthesizers
    "synthesize_from_revision_state",
    "synthesize_from_active_scenarios",
    "synthesize_from_proxy_predictions",
    "synthesize_from_legacy_producers",
    "synthesize_from_disclosure_features",
    "disclosure_directional_value",
    "disclosure_hypothesis_id",
    # Pure post-processing
    "dedupe_by_hypothesis_id",
    "rank_by_catalyst_score",
    # Prompt helpers
    "compact_for_opus",
    # I/O orchestrator
    "run",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Minimum scenario readiness fraction to emit a hypothesis (60% per plan).
_DEFAULT_MIN_READINESS: float = 0.60

#: Default horizon in days per source (calendar days).
_HORIZON_REVISION: int = 10
_HORIZON_SCENARIO: int = 20
_HORIZON_PROXY: int = 20
_HORIZON_DISCLOSURE: int = 20

#: Default surprise_score when revision data is absent.
_DEFAULT_SURPRISE_SCORE: float = 0.5

#: Benchmark basket defaults per currency region.
_BENCHMARK_JP = (["TOPIX"], [1.0], "JPY")
_BENCHMARK_US = (["QQQ"], [1.0], "USD")

#: Missing price marker used when no live price provider is wired.  Do not use
#: ``0.0`` here: outcome writers reject zero reference prices, and a zero row
#: looks like real data. ``null`` makes the measurement gap explicit.
_MISSING_PRICE: float | None = None
_PLACEHOLDER_FX: float = 1.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalystHypothesis:
    """One unified hypothesis row — schema feeds catalyst_hypothesis_log + Opus prompt.

    All fields are immutable so the object can be used as a dict key or in a
    set when deduplicating by ``hypothesis_id``.
    """

    hypothesis_id: str
    """Stable date-independent hash — the cross-day join key."""

    ticker: str
    """Primary ticker in exchange notation (e.g. ``NVDA``, ``9984.T``)."""

    hypothesis_type: str
    """Playbook label:
    ``earnings_revision_pullback`` | ``bull_pullback`` | ``ipo_proxy`` | ``legacy_*``
    """

    candidate_status: str
    """A :class:`~almanac.observability.status.CandidateStatus` value."""

    catalyst_score: float
    """Composite score in [0, 1] — higher = stronger hypothesis."""

    scenario_readiness: float
    """Fraction [0, 1]; 0 when no scenario contributed."""

    priced_in_penalty: float
    """Penalty applied to the score [0, 0.6]; 0 when revision data is absent."""

    surprise_score: float
    """How novel/fresh the catalyst is [0, 1]; defaults to 0.5 for non-revision sources."""

    conviction_at_generation: int
    """Agent conviction at the time the hypothesis was generated [0, 100]."""

    gross_expected_return_bps: float
    """MVP estimate: ``conviction * 5``.  Phase 2 will refine via EV model."""

    proxy_tickers: list[str]
    """For ``ipo_proxy`` hypotheses, the listed proxies; empty for others."""

    non_listed_entity: str | None
    """For ``ipo_proxy`` hypotheses, the unmapped entity name; ``None`` otherwise."""

    evidence_summary: str
    """Human-readable evidence text for Opus prompt injection."""

    source_event_id: str
    """Stable event identifier passed to ``compute_hypothesis_id``."""

    horizon_days: int
    """Intended holding horizon in calendar days."""

    primary_source_agent: str
    """Which module originated this hypothesis."""

    action_type: str = "buy"
    """``buy`` | ``trim`` | ``sell`` | ``hold`` | ``margin_buy`` | ``short_sell``."""

    currency: str = "USD"
    """ISO currency code for the primary ticker."""

    observe_only: bool = False
    """True when the source is observational and must not be injected for decisions."""

    invalidates_if: str = ""
    """One-line condition under which the hypothesis should be abandoned.

    Used by :func:`compact_for_opus` to surface the invalidation criterion to
    Opus and by the Evidence Sufficiency Gate (C6-5 / C7-4) to check that a
    caller-supplied condition is present.  Empty string → ESG will mark this
    hypothesis ``not_injected``.

    Examples:
        ``"MA20 break / RSI>75 / 20営業日経過"``
        ``"IPO delay / proxy_audit Jaccard <0.5"``
        ``"Scenario readiness < 60%"``
    """

    event_at: str | None = None
    """Forward-outcome origin written to the catalyst log as ``event_at``.

    For disclosure features this is the feature's ``compute_time`` — when the
    signal became available to us — so outcome measurement starts from a real,
    no-look-ahead t0 rather than the catalyst run date. ``None`` lets the log
    writer default to ``now()`` for sources with no distinct event time.
    """

    human_execution_only: bool = True
    """True means the hypothesis may be reviewed but never auto-ordered."""

    execution_cost_model: dict[str, Any] = field(default_factory=dict)
    """Optional execution-cost metadata, especially for short hypotheses."""

    tradeability: dict[str, Any] = field(default_factory=dict)
    """Optional borrow/loanability/squeeze status.  Fail-closed when unknown."""

    risk_controls: dict[str, Any] = field(default_factory=dict)
    """Optional size caps, stops, and other safety controls for risky hypotheses."""


@dataclass(frozen=True)
class CatalystOutput:
    """Top-level return type — what an ``analyzer.py``-side caller actually gets."""

    as_of: str
    """ISO-8601 datetime of this synthesis run."""

    n_hypotheses_total: int
    """Total number of hypotheses after dedup."""

    n_hypotheses_top: int
    """``min(top_n, n_hypotheses_total)``."""

    top: list[CatalystHypothesis]
    """Top-ranked hypotheses (len == n_hypotheses_top), ordered by catalyst_score desc."""

    by_type: dict[str, int]
    """hypothesis_type → count in the full list."""

    all_hypotheses: list[CatalystHypothesis]
    """All hypotheses ranked by catalyst_score desc."""


# ---------------------------------------------------------------------------
# Pure scoring
# ---------------------------------------------------------------------------


def compute_catalyst_score(
    *,
    base_conviction: int,
    scenario_readiness: float,
    surprise_score: float,
    priced_in_penalty: float,
    freshness_bonus: float = 0.0,
) -> float:
    """Compose inputs into a single [0, 1] catalyst score.

    Formula (Round 4 + Round 9)::

        raw = (base_conviction/100) * 0.5
              + scenario_readiness * 0.3
              + surprise_score * 0.2
              + freshness_bonus
        score = clip(raw * (1 - priced_in_penalty), 0, 1)

    Parameters
    ----------
    base_conviction:
        Agent conviction in [0, 100].
    scenario_readiness:
        Macro scenario readiness fraction in [0, 1].
    surprise_score:
        Novelty / freshness of the catalyst in [0, 1].
    priced_in_penalty:
        How much the catalyst is already priced in [0, 0.6].
    freshness_bonus:
        Optional additional bonus in [0, 0.2] for very recent events.
    """
    raw = (
        (base_conviction / 100.0) * 0.5
        + scenario_readiness * 0.3
        + surprise_score * 0.2
        + freshness_bonus
    )
    score = raw * (1.0 - priced_in_penalty)
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_jp_ticker(ticker: str) -> bool:
    """Return True when the ticker looks like a Tokyo-listed security."""
    return ticker.endswith(".T") or (ticker.isdigit() and len(ticker) == 4)


def _ticker_currency(ticker: str) -> str:
    """Infer ISO currency from ticker notation."""
    return "JPY" if _is_jp_ticker(ticker) else "USD"


def _benchmark_for(ticker: str) -> tuple[list[str], list[float], str]:
    """Return ``(basket, weights, currency_normalized_to)`` for a ticker."""
    if _is_jp_ticker(ticker):
        return _BENCHMARK_JP[0][:], _BENCHMARK_JP[1][:], _BENCHMARK_JP[2]
    return _BENCHMARK_US[0][:], _BENCHMARK_US[1][:], _BENCHMARK_US[2]


def _normalize_entity(entity: str) -> str:
    """Lowercase-strip an entity name for stable source_event_id construction."""
    return entity.strip().lower().replace(" ", "_")


def _entity_hash(entity: str) -> str:
    """Stable 8-char hash for an entity string."""
    return hashlib.sha256(entity.encode("utf-8")).hexdigest()[:8]


def _scenario_hypothesis_type(scenario_id: str, scenario: dict[str, Any]) -> str:
    """Return a stable hypothesis_type for a scenario row.

    Older test fixtures and the new ``bull_pullback`` playbook carry an explicit
    ``hypothesis_type``. Production macro scenarios such as ``war_end`` usually
    do not, so defaulting every row to ``bull_pullback`` mislabels unrelated
    macro playbooks. Use a scenario-scoped type instead.
    """
    explicit = scenario.get("hypothesis_type")
    if explicit:
        return str(explicit)
    norm = "".join(
        ch if ch.isalnum() else "_"
        for ch in str(scenario_id or "").strip().lower()
    ).strip("_")
    if not norm:
        return "scenario_unknown"
    if norm.startswith("bull_pullback"):
        return "bull_pullback"
    return f"scenario_{norm}"


def _infer_scenario_action_type(entry: dict[str, Any] | None) -> str:
    """Infer candidate action_type from a scenario recommended_action row."""
    if not isinstance(entry, dict):
        return "buy"
    raw = " ".join(
        str(entry.get(k) or "")
        for k in ("action_type", "type", "action", "reason")
    ).lower()
    if any(tok in raw for tok in ("trim", "take_profit", "profit", "利確", "一部売", "削減")):
        return "trim"
    if any(tok in raw for tok in ("sell", "売却", "全売", "撤退")):
        return "sell"
    if any(tok in raw for tok in ("short_sell", "short", "空売")):
        return "short_sell"
    if any(tok in raw for tok in ("margin_buy", "信用買")):
        return "margin_buy"
    return "buy"


# ---------------------------------------------------------------------------
# Pure synthesizers
# ---------------------------------------------------------------------------


def synthesize_from_revision_state(
    revision_state: dict[str, Any],
    *,
    analysis_id: str,
    analysis_date: str,
) -> list[CatalystHypothesis]:
    """Build hypotheses from ``revision_state.json`` output.

    One hypothesis is emitted per ticker entry.

    - ``direction=up`` → ``action_type=buy``, ``hypothesis_type=earnings_revision_pullback``
    - ``direction=down`` → ``action_type=trim``, ``hypothesis_type=earnings_revision_pullback``
    - ``direction=none`` → skipped (no actionable signal)

    ``source_event_id = f"revision:{ticker}:{direction}"`` so day-to-day
    revision signals on the same ticker share the same ``hypothesis_id``
    (Round 8 #1).
    """
    if not revision_state:
        return []
    tickers_dict = revision_state.get("tickers") or {}
    if not isinstance(tickers_dict, dict):
        logger.warning("revision_state.tickers is not a dict; skipping")
        return []

    hypotheses: list[CatalystHypothesis] = []
    for ticker, entry in tickers_dict.items():
        if not isinstance(entry, dict):
            continue
        direction = entry.get("direction", "none")
        if direction == "none":
            continue
        if direction == "up":
            action_type = "buy"
        elif direction == "down":
            action_type = "trim"
        else:
            logger.warning("unknown revision direction %r for %s; skipping", direction, ticker)
            continue

        surprise_score = float(entry.get("surprise_score") or _DEFAULT_SURPRISE_SCORE)
        priced_in_penalty = float(entry.get("priced_in_penalty") or 0.0)
        strength = float(entry.get("strength") or 0.5)
        # Map strength [0,1] → conviction [0,100]
        conviction = int(round(strength * 100))

        source_event_id = f"revision:{ticker}:{direction}"
        hypothesis_id = compute_hypothesis_id(
            ticker=ticker,
            action_type=action_type,
            hypothesis_type="earnings_revision_pullback",
            horizon_days=_HORIZON_REVISION,
            source_event_id=source_event_id,
        )

        catalyst_score = compute_catalyst_score(
            base_conviction=conviction,
            scenario_readiness=0.0,
            surprise_score=surprise_score,
            priced_in_penalty=priced_in_penalty,
        )

        sources = entry.get("sources") or []
        evidence_parts = []
        for src in sources[:3]:
            headline = src.get("headline")
            if headline:
                evidence_parts.append(headline[:120])
        evidence_summary = (
            f"Revision {direction}: " + " | ".join(evidence_parts)
            if evidence_parts
            else f"Earnings guidance revision ({direction}) detected for {ticker}"
        )

        hypotheses.append(
            CatalystHypothesis(
                hypothesis_id=hypothesis_id,
                ticker=ticker,
                hypothesis_type="earnings_revision_pullback",
                candidate_status=CandidateStatus.generated.value,
                catalyst_score=catalyst_score,
                scenario_readiness=0.0,
                priced_in_penalty=priced_in_penalty,
                surprise_score=surprise_score,
                conviction_at_generation=conviction,
                gross_expected_return_bps=float(conviction * 5),
                proxy_tickers=[],
                non_listed_entity=None,
                evidence_summary=evidence_summary,
                source_event_id=source_event_id,
                horizon_days=_HORIZON_REVISION,
                primary_source_agent="revision_tracker",
                action_type=action_type,
                currency=_ticker_currency(ticker),
                invalidates_if="MA20 break / RSI>75 / 20営業日経過",
            )
        )
    return hypotheses


def synthesize_from_active_scenarios(
    scenario_state: dict[str, Any],
    *,
    analysis_id: str,
    analysis_date: str,
    min_readiness: float = _DEFAULT_MIN_READINESS,
) -> list[CatalystHypothesis]:
    """Build hypotheses from active macro scenarios.

    Filtering rules (Round 11 #C):
    - Scenarios with ``enabled_for_decision=false`` are skipped unless they are
      also ``observe_only=true``.
    - Scenarios with ``readiness_pct < min_readiness * 100`` are skipped.
    - Scenarios with ``observe_only=true`` are included in ``all_hypotheses``
      and the log, but flagged so the decision top-list can exclude them.

    ``source_event_id = f"scenario:{scenario_id}"`` for id stability.
    """
    if not scenario_state:
        return []

    # Normalize the ``scenarios`` field to a list of ``(scenario_id, data)``
    # tuples. Production ``scenario_state.json`` stores it as a dict keyed by
    # scenario id; legacy tests (and some early prototypes) used a flat list
    # of entries each carrying its own ``id``. Accept both — the rest of the
    # function then operates on a uniform iterable.
    raw_scenarios = scenario_state.get("scenarios")
    entries: list[tuple[str, dict]] = []
    if isinstance(raw_scenarios, dict):
        for sid, item in raw_scenarios.items():
            if isinstance(item, dict):
                entries.append((str(sid), item))
    elif isinstance(raw_scenarios, list):
        for item in raw_scenarios:
            if not isinstance(item, dict):
                continue
            sid = item.get("id") or item.get("scenario_id") or ""
            entries.append((str(sid), item))
    elif raw_scenarios is None:
        return []
    else:
        logger.warning(
            "scenario_state.scenarios is %s, expected dict or list; skipping",
            type(raw_scenarios).__name__,
        )
        return []

    hypotheses: list[CatalystHypothesis] = []
    for scenario_id, scenario in entries:
        observe_only = bool(scenario.get("observe_only", False))

        # Round 11 #C: skip disabled scenarios unless they are explicitly
        # observe-only. The flag lives on the
        # *playbook* definition in scenario_playbook.json — when the state
        # file mirrors it we honour it; absent flag defaults to enabled.
        if not scenario.get("enabled_for_decision", True) and not observe_only:
            logger.debug("skipping scenario %r: enabled_for_decision=false", scenario_id)
            continue

        # Accept either ``readiness`` (fraction in [0, 1], production shape)
        # or ``readiness_pct`` (0..100, legacy shape). Coerce to a fraction.
        if "readiness" in scenario:
            try:
                scenario_readiness = float(scenario["readiness"])
            except (TypeError, ValueError):
                scenario_readiness = 0.0
            # Defensive: if the producer mistakenly wrote a percent here,
            # clamp into [0, 1] rather than over-weight the candidate.
            if scenario_readiness > 1.0:
                scenario_readiness /= 100.0
        else:
            try:
                scenario_readiness = float(scenario.get("readiness_pct") or 0.0) / 100.0
            except (TypeError, ValueError):
                scenario_readiness = 0.0
        scenario_readiness = max(0.0, min(1.0, scenario_readiness))

        if scenario_readiness < min_readiness:
            logger.debug(
                "skipping scenario %r: readiness=%.2f < %.2f",
                scenario_id, scenario_readiness, min_readiness,
            )
            continue

        if not scenario_id:
            logger.warning("scenario entry missing id; skipping: %r", scenario)
            continue

        conviction = scenario.get("conviction") or int(round(scenario_readiness * 80))
        conviction = max(0, min(100, int(conviction)))

        # Extract ticker/action pairs. Three sources, in priority order:
        #   1. explicit flat ``tickers`` / ``target_tickers`` (test fixtures)
        #   2. ``recommended_actions`` nested phase dict (production shape):
        #      ``{"phase_1": [{"ticker": "SOXL", ...}, ...], "phase_2": [...]}``
        #   3. fallback to ``primary_ticker`` or the sentinel ``"MARKET"`` so
        #      the scenario is still visible in the hypothesis log.
        ticker_actions: list[tuple[str, str, str | None]] = []
        flat_tickers: list[str] = list(
            scenario.get("tickers") or scenario.get("target_tickers") or []
        )
        for ticker in flat_tickers:
            ticker_actions.append((str(ticker), "buy", None))

        if not ticker_actions:
            recommended = scenario.get("recommended_actions")
            if isinstance(recommended, dict):
                seen: set[tuple[str, str]] = set()
                seen_tickers: set[str] = set()
                for phase_name, phase_value in recommended.items():
                    if phase_name == "sell_on_trigger":
                        continue
                    if not isinstance(phase_value, list):
                        continue
                    for entry in phase_value:
                        if not isinstance(entry, dict):
                            continue
                        t = entry.get("ticker")
                        action_type = _infer_scenario_action_type(entry)
                        key = (str(t), action_type)
                        if t and key not in seen:
                            seen.add(key)
                            seen_tickers.add(str(t))
                            ticker_actions.append(
                                (str(t), action_type, str(entry.get("reason") or "") or None)
                            )
                triggers = recommended.get("sell_on_trigger")
                if isinstance(triggers, list):
                    for trigger in triggers:
                        t = str(trigger) if trigger else ""
                        if not t or t in seen_tickers:
                            continue
                        key = (t, "sell")
                        if key in seen:
                            continue
                        seen.add(key)
                        seen_tickers.add(t)
                        ticker_actions.append((t, "sell", "sell_on_trigger"))
            elif isinstance(recommended, list):
                # Some prototypes flattened phases into a single list.
                seen: set[tuple[str, str]] = set()
                for entry in recommended:
                    if isinstance(entry, dict) and entry.get("ticker"):
                        action_type = _infer_scenario_action_type(entry)
                        key = (str(entry["ticker"]), action_type)
                        if key not in seen:
                            seen.add(key)
                            ticker_actions.append(
                                (
                                    str(entry["ticker"]),
                                    action_type,
                                    str(entry.get("reason") or "") or None,
                                )
                            )
        if not ticker_actions:
            ticker_actions = [(str(scenario.get("primary_ticker") or "MARKET"), "buy", None)]

        hypothesis_type = _scenario_hypothesis_type(scenario_id, scenario)

        evidence_summary = (
            scenario.get("description")
            or scenario.get("name")
            or f"Active scenario: {scenario_id}"
        )
        invalidates_if = (
            scenario.get("invalidation_condition")
            or f"Scenario readiness < {min_readiness * 100:.0f}%"
        )

        for ticker, action_type, action_reason in ticker_actions:
            source_event_id = f"scenario:{scenario_id}"
            hypothesis_id = compute_hypothesis_id(
                ticker=ticker,
                action_type=action_type,
                hypothesis_type=hypothesis_type,
                horizon_days=_HORIZON_SCENARIO,
                source_event_id=source_event_id,
            )
            catalyst_score = compute_catalyst_score(
                base_conviction=conviction,
                scenario_readiness=scenario_readiness,
                surprise_score=_DEFAULT_SURPRISE_SCORE,
                priced_in_penalty=0.0,
            )
            hypotheses.append(
                CatalystHypothesis(
                    hypothesis_id=hypothesis_id,
                    ticker=ticker,
                    hypothesis_type=hypothesis_type,
                    candidate_status=CandidateStatus.generated.value,
                    catalyst_score=catalyst_score,
                    scenario_readiness=scenario_readiness,
                    priced_in_penalty=0.0,
                    surprise_score=_DEFAULT_SURPRISE_SCORE,
                    conviction_at_generation=conviction,
                    gross_expected_return_bps=float(conviction * 5),
                    proxy_tickers=[],
                    non_listed_entity=None,
                    evidence_summary=(
                        f"{evidence_summary}: {action_reason}"
                        if action_reason else evidence_summary
                    ),
                    source_event_id=source_event_id,
                    horizon_days=_HORIZON_SCENARIO,
                    primary_source_agent=f"scenario:{scenario_id}",
                    action_type=action_type,
                    currency=_ticker_currency(ticker),
                    observe_only=observe_only,
                    invalidates_if=invalidates_if,
                )
            )
    return hypotheses


def synthesize_from_proxy_predictions(
    proxy_seed_map: dict[str, list[str]],
    news_entities: list[str],
    *,
    analysis_id: str,
    analysis_date: str,
) -> list[CatalystHypothesis]:
    """Build ``ipo_proxy`` hypotheses when detected news entities match the seed map.

    For each entity in ``news_entities`` that appears as a key in
    ``proxy_seed_map``, one hypothesis is emitted **per listed proxy ticker**
    in the mapped value list.

    ``source_event_id = f"proxy:{entity_normalized}"`` so the id is stable
    across days for the same entity.
    """
    if not proxy_seed_map or not news_entities:
        return []

    # Normalise seed map keys for case-insensitive matching
    normalised_map: dict[str, tuple[str, list[str]]] = {}
    for entity, tickers in proxy_seed_map.items():
        norm = _normalize_entity(entity)
        normalised_map[norm] = (entity, list(tickers))

    hypotheses: list[CatalystHypothesis] = []
    for entity in news_entities:
        norm = _normalize_entity(entity)
        if norm not in normalised_map:
            continue
        original_entity, proxy_tickers = normalised_map[norm]
        if not proxy_tickers:
            continue

        source_event_id = f"proxy:{norm}"
        conviction = 50  # neutral default for proxy hypotheses
        evidence_summary = (
            f"Non-listed entity '{original_entity}' detected in news; "
            f"proxy tickers: {', '.join(proxy_tickers)}"
        )
        invalidates_if = (
            f"'{original_entity}' event cancelled / IPO delay / proxy_audit Jaccard <0.5"
        )

        for proxy_ticker in proxy_tickers:
            hypothesis_id = compute_hypothesis_id(
                ticker=proxy_ticker,
                action_type="buy",
                hypothesis_type="ipo_proxy",
                horizon_days=_HORIZON_PROXY,
                source_event_id=source_event_id,
            )
            catalyst_score = compute_catalyst_score(
                base_conviction=conviction,
                scenario_readiness=0.0,
                surprise_score=_DEFAULT_SURPRISE_SCORE,
                priced_in_penalty=0.0,
            )
            hypotheses.append(
                CatalystHypothesis(
                    hypothesis_id=hypothesis_id,
                    ticker=proxy_ticker,
                    hypothesis_type="ipo_proxy",
                    candidate_status=CandidateStatus.generated.value,
                    catalyst_score=catalyst_score,
                    scenario_readiness=0.0,
                    priced_in_penalty=0.0,
                    surprise_score=_DEFAULT_SURPRISE_SCORE,
                    conviction_at_generation=conviction,
                    gross_expected_return_bps=float(conviction * 5),
                    proxy_tickers=list(proxy_tickers),
                    non_listed_entity=original_entity,
                    evidence_summary=evidence_summary,
                    source_event_id=source_event_id,
                    horizon_days=_HORIZON_PROXY,
                    primary_source_agent="proxy_mapper",
                    action_type="buy",
                    currency=_ticker_currency(proxy_ticker),
                    invalidates_if=invalidates_if,
                )
            )
    return hypotheses


def synthesize_from_legacy_producers(
    candidate_packets: list[dict[str, Any]],
    *,
    analysis_id: str,
    analysis_date: str,
) -> list[CatalystHypothesis]:
    """Convert ``candidate_extractor.extract_all`` output to CatalystHypothesis objects.

    Legacy producers supply a real ``hypothesis_id`` (built by ``_build_packet``
    in ``candidate_extractor``), so we reuse it directly rather than
    recomputing — preserving the multi-day join key (Round 8 #1).
    """
    if not candidate_packets:
        return []

    hypotheses: list[CatalystHypothesis] = []
    for packet in candidate_packets:
        if not isinstance(packet, dict):
            continue
        ticker = packet.get("ticker")
        hypothesis_id = packet.get("hypothesis_id")
        action_type = packet.get("action_type")
        hypothesis_type = packet.get("hypothesis_type") or "legacy"
        source_event_id = packet.get("source_event_id") or ""
        if not ticker or not hypothesis_id or not action_type:
            logger.warning(
                "legacy packet missing required fields; skipping ticker=%r", ticker
            )
            continue

        confidence_pct = packet.get("confidence_pct") or 50
        conviction = max(0, min(100, int(confidence_pct)))
        evidence_summary = packet.get("evidence_summary") or f"Legacy recommendation for {ticker}"
        candidate_status = packet.get("candidate_status") or CandidateStatus.generated.value
        horizon_days = int(packet.get("time_horizon_days") or _HORIZON_PROXY)
        source_agents = packet.get("source_agents") or []
        primary_source_agent = (
            f"legacy_producer:{source_agents[0]}" if source_agents else "legacy_producer"
        )
        invalidates_if = packet.get("invalidation_summary") or ""
        risk_controls = dict(packet.get("risk_controls") or {})
        execution_cost_model = dict(packet.get("execution_cost_model") or {})
        tradeability = dict(packet.get("tradeability") or {})

        catalyst_score = compute_catalyst_score(
            base_conviction=conviction,
            scenario_readiness=0.0,
            surprise_score=_DEFAULT_SURPRISE_SCORE,
            priced_in_penalty=0.0,
        )

        hypotheses.append(
            CatalystHypothesis(
                hypothesis_id=hypothesis_id,
                ticker=ticker,
                hypothesis_type=hypothesis_type,
                candidate_status=candidate_status,
                catalyst_score=catalyst_score,
                scenario_readiness=0.0,
                priced_in_penalty=0.0,
                surprise_score=_DEFAULT_SURPRISE_SCORE,
                conviction_at_generation=conviction,
                gross_expected_return_bps=float(conviction * 5),
                proxy_tickers=[],
                non_listed_entity=None,
                evidence_summary=evidence_summary,
                source_event_id=source_event_id,
                horizon_days=horizon_days,
                primary_source_agent=primary_source_agent,
                action_type=action_type,
                currency=_ticker_currency(ticker),
                observe_only=bool(packet.get("observe_only", False)),
                invalidates_if=invalidates_if,
                human_execution_only=bool(packet.get("human_execution_only", True)),
                execution_cost_model=execution_cost_model,
                tradeability=tradeability,
                risk_controls=risk_controls,
            )
        )
    return hypotheses


# ---------------------------------------------------------------------------
# Dedup + ranking (pure)
# ---------------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    """Coerce ``value`` to float, returning ``None`` on failure.

    Used so a single malformed store row (a string score from a migration, manual
    injection, or an older schema) skips itself instead of crashing the run.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _disclosure_action_type(ds: float, row: dict[str, Any] | None = None) -> str:
    """Map a disclosure direction to an action type.

    Generic negative disclosure remains ``trim`` for backward compatibility.
    Deterministic catalyst events (dilution / going-concern) are different: they
    are pre-registered as a short-side, observe-only measurement lane (the
    ``event`` short lane), not as a holdings trim.
    """
    if row and ds < 0 and (
        row.get("dilution_flag") is True or row.get("going_concern_flag") is True
    ):
        return "short_sell"
    return "buy" if ds > 0 else "trim"


def disclosure_directional_value(row: dict[str, Any]) -> float | None:
    """Return a signed measurement direction for AI or deterministic features."""

    directional = _safe_float(row.get("directional_score"))
    if directional not in (None, 0):
        return directional
    for name in ("guidance_revision_pct", "monthly_yoy_pct"):
        value = _safe_float(row.get(name))
        if value not in (None, 0):
            return value
    insider = _safe_float(row.get("insider_cluster_score"))
    if insider is not None and insider > 0:
        return min(insider / 3.0, 1.0)
    if row.get("activist_flag") is True:
        return 1.0
    if row.get("going_concern_flag") is True:
        return -1.0
    if row.get("dilution_flag") is True:
        dilution = _safe_float(row.get("dilution_pct"))
        return -min(max(dilution or 0.5, 0.1), 1.0)
    return None


def disclosure_hypothesis_id(
    ticker: str | None,
    directional_score: Any,
    source_event_id: str | None,
    *,
    model_id: str | None = None,
    prompt_version: str | None = None,
    feature_schema_version: str | None = None,
    action_type: str | None = None,
) -> str | None:
    """Stable ``hypothesis_id`` for a disclosure feature row.

    Single source of truth shared by :func:`synthesize_from_disclosure_features`
    (which emits the hypothesis) and the Phase-1 panel-assembly join (which must
    recover the same id to attach realized outcomes). Extractor-version fields
    are part of the identity so an outcome generated from an older prompt/model
    cannot be joined to a newer feature value for the same disclosure.

    Rows without any version metadata retain the legacy identity for backwards
    compatibility. Versioned rows never fall back to that legacy id during
    panel assembly because an ambiguous join is worse than a missing outcome.
    Returns ``None`` when the row has no actionable direction or stable anchor.
    """
    ds = _safe_float(directional_score)
    if ds is None or ds == 0 or not ticker or not source_event_id:
        return None
    version_parts = (model_id, prompt_version, feature_schema_version)
    if any(value is not None for value in version_parts):
        version_anchor = json.dumps(
            [value or "" for value in version_parts],
            ensure_ascii=True,
            separators=(",", ":"),
        )
        version_hash = hashlib.sha256(version_anchor.encode("utf-8")).hexdigest()[:16]
        source_event_id = f"{source_event_id}|extractor:{version_hash}"
    return compute_hypothesis_id(
        ticker=ticker,
        action_type=action_type or _disclosure_action_type(ds),
        hypothesis_type="disclosure_catalyst",
        horizon_days=_HORIZON_DISCLOSURE,
        source_event_id=source_event_id,
    )


def _short_execution_metadata(
    ticker: str,
    row: dict[str, Any],
    *,
    horizon_days: int,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Return cost/tradeability/risk metadata for an observe-only short.

    Missing broker or borrow data must not suppress the hypothesis entirely; it
    should be measured and reviewed, but marked untradeable/certify-excluded so
    it cannot masquerade as a live-ready signal.
    """
    market = str(row.get("market") or ("JP" if ticker.endswith(".T") else "US")).upper()
    cost_model: dict[str, Any] = {
        "model": "disclosure_shadow_book",
        "market": market,
        "direction": "short",
        "horizon_days": int(horizon_days),
        "available": False,
    }
    tradeability: dict[str, Any] = {
        "ticker": ticker,
        "market": market,
        "untradeable": True,
        "reasons": ["short_tradeability_not_checked"],
        "excluded_from_certify": True,
    }
    risk_controls: dict[str, Any] = {
        "observe_only_first": True,
        "human_execution_only": True,
        "requires_borrow_confirmed": True,
        "requires_squeeze_guard": True,
        "excluded_from_certify_if_untradeable": True,
    }

    try:
        from disclosure_shadow_book import estimate_round_trip_cost_pct, load_config

        cfg = load_config()
        notional_jpy = float(row.get("notional_jpy") or cfg.get("notional_jpy") or 100_000)
        cost_model.update({
            "available": True,
            "notional_jpy": round(notional_jpy),
        })
        if market == "JP":
            standard = estimate_round_trip_cost_pct(
                market="JP",
                notional_jpy=notional_jpy,
                config=cfg,
                direction=-1,
                horizon_days=horizon_days,
                short_credit_type="standard",
            )
            general = estimate_round_trip_cost_pct(
                market="JP",
                notional_jpy=notional_jpy,
                config=cfg,
                direction=-1,
                horizon_days=horizon_days,
                short_credit_type="general",
            )
            short_cfg = ((cfg.get("cost_model") or {}).get("jp_short") or {})
            cost_model.update({
                "standard_credit": {
                    "round_trip_cost_pct": round(float(standard), 6),
                    "borrow_rate_annual": short_cfg.get("standard_borrow_rate_annual"),
                    "reverse_daily_fee_buffer_annual": short_cfg.get("reverse_daily_fee_buffer_annual"),
                },
                "general_credit": {
                    "round_trip_cost_pct": round(float(general), 6),
                    "borrow_rate_annual_min": short_cfg.get("general_borrow_rate_annual_min"),
                    "borrow_rate_annual_max": short_cfg.get("general_borrow_rate_annual_max"),
                },
            })
        else:
            cost_model["round_trip_cost_pct"] = round(float(estimate_round_trip_cost_pct(
                market=market,
                notional_jpy=notional_jpy,
                config=cfg,
                direction=-1,
                horizon_days=horizon_days,
            )), 6)
    except Exception as exc:  # noqa: BLE001 — metadata is best-effort/fail-closed.
        cost_model["error"] = str(exc)[:160]

    try:
        if market == "JP":
            from jp_loanability import evaluate_short_tradeability

            checked = evaluate_short_tradeability(ticker)
            tradeability.update(checked)
            tradeability["market"] = market
        else:
            # US short availability/cost is not broker-certified in this system.
            tradeability.update({
                "untradeable": True,
                "reasons": ["us_short_not_enabled"],
            })
    except Exception as exc:  # noqa: BLE001
        tradeability.update({
            "untradeable": True,
            "reasons": ["short_tradeability_check_failed"],
            "error": str(exc)[:160],
        })

    tradeability["excluded_from_certify"] = bool(tradeability.get("untradeable"))
    return cost_model, tradeability, risk_controls


def synthesize_from_disclosure_features(
    features: list[dict[str, Any]],
    *,
    analysis_id: str,
    analysis_date: str,
) -> list[CatalystHypothesis]:
    """Build **observe_only** hypotheses from stored public-disclosure features.

    Phase 0: every hypothesis is ``observe_only=True`` so it is logged and
    outcome-measured but NEVER injected into the Opus prompt or the decision
    ``top`` (enforced structurally by :func:`compact_for_opus` and :func:`run`).
    Promotion to a decision input happens only after the Phase-1 validation
    harness certifies a feature — never here.

    Each row from :mod:`almanac.observability.disclosure_features` becomes one
    hypothesis:

    - ``directional_score > 0`` → ``action_type=buy``; ``< 0`` → ``trim``;
      ``None`` / ``0`` (no actionable direction) → skipped.
    - ``conviction`` = ``|directional_score| × (directional_confidence or 1) × 100``.
    - ``surprise_score`` ← ``catalyst_specificity`` (specific catalysts are novel).
    - ``priced_in_penalty`` ← ``crowding_hype_score`` (already-discussed = priced in).
    - ``source_event_id`` reuses the feature's stable id (EDGAR accession / EDINET
      docID / TDnet id) so the same disclosure shares one ``hypothesis_id`` across
      days (Round 8 #1).
    """
    hypotheses: list[CatalystHypothesis] = []
    for row in features or []:
        if not isinstance(row, dict):
            continue
        # Robust against non-numeric values: an append-only store accumulates rows
        # from future migrations, manual injection, or older schemas. A single bad
        # row must skip itself, never crash the whole catalyst run.
        ds = disclosure_directional_value(row)
        if ds is None or ds == 0:
            continue  # missing / non-numeric / neutral → no actionable direction
        ticker = row.get("ticker")
        source_event_id = row.get("source_event_id")
        if not ticker or not source_event_id:
            continue

        action_type = _disclosure_action_type(ds, row)
        conf_mult = _safe_float(row.get("directional_confidence"))
        conf_mult = conf_mult if conf_mult is not None else 1.0
        conviction = max(0, min(100, int(round(abs(ds) * conf_mult * 100))))

        specificity = _safe_float(row.get("catalyst_specificity"))
        surprise_score = (
            specificity if specificity is not None else _DEFAULT_SURPRISE_SCORE
        )
        crowding = _safe_float(row.get("crowding_hype_score"))
        priced_in_penalty = min(crowding, 0.6) if crowding is not None else 0.0

        catalyst_score = compute_catalyst_score(
            base_conviction=conviction,
            scenario_readiness=0.0,
            surprise_score=surprise_score,
            priced_in_penalty=priced_in_penalty,
        )

        hypothesis_id = disclosure_hypothesis_id(
            ticker,
            ds,
            source_event_id,
            model_id=row.get("model_id"),
            prompt_version=row.get("prompt_version"),
            feature_schema_version=row.get("feature_schema_version"),
            action_type=action_type,
        )

        summary = (row.get("summary") or "").strip()
        evidence_summary = (
            summary[:120]
            if summary
            else f"Public disclosure feature ({action_type}) for {ticker}"
        )

        execution_cost_model: dict[str, Any] = {}
        tradeability: dict[str, Any] = {}
        risk_controls: dict[str, Any] = {}
        if action_type == "short_sell":
            execution_cost_model, tradeability, risk_controls = _short_execution_metadata(
                str(ticker),
                row,
                horizon_days=_HORIZON_DISCLOSURE,
            )
            # 3レーン分離(Step D): catalyst 起点の short は event レーン。
            # outcome 計測/昇格をレーン別に分離できるよう risk_controls に記録する。
            try:
                from short_universe import classify_short_lane
                risk_controls = {
                    **risk_controls,
                    "short_lane": classify_short_lane(row) or "event",
                }
            except Exception:
                risk_controls = {**risk_controls, "short_lane": "event"}

        hypotheses.append(
            CatalystHypothesis(
                hypothesis_id=hypothesis_id,
                ticker=ticker,
                hypothesis_type="disclosure_catalyst",
                candidate_status=CandidateStatus.generated.value,
                catalyst_score=catalyst_score,
                scenario_readiness=0.0,
                priced_in_penalty=priced_in_penalty,
                surprise_score=surprise_score,
                conviction_at_generation=conviction,
                gross_expected_return_bps=float(conviction * 5),
                proxy_tickers=[],
                non_listed_entity=None,
                evidence_summary=evidence_summary,
                source_event_id=source_event_id,
                horizon_days=_HORIZON_DISCLOSURE,
                primary_source_agent="disclosure_feature",
                action_type=action_type,
                currency=_ticker_currency(ticker),
                observe_only=True,  # Phase 0: never injected for decisions
                invalidates_if=(
                    "開示が訂正/否定された / 20営業日経過 / 反応が完全に織り込み済み"
                ),
                # Forward-outcome origin = when the feature became available to us
                # (compute_time), NOT publish_time. Falling back to publish_time
                # would re-introduce look-ahead (we did not have the feature then).
                event_at=row.get("compute_time"),
                human_execution_only=True,
                execution_cost_model=execution_cost_model,
                tradeability=tradeability,
                risk_controls=risk_controls,
            )
        )
    return hypotheses


def dedupe_by_hypothesis_id(
    hypotheses: list[CatalystHypothesis],
) -> list[CatalystHypothesis]:
    """Deduplicate by ``hypothesis_id``, keeping the higher-score row.

    When two sources produce the same ``hypothesis_id`` (e.g. revision_tracker
    AND legacy long_sonnet both flag NVDA for a buy), we keep the row with the
    higher ``catalyst_score`` and merge their ``primary_source_agent`` strings
    into a slash-joined combined value.

    Input order is preserved among non-duplicate rows so callers that care about
    insertion order (e.g. tests asserting first-seen wins on tie) get predictable
    behaviour.
    """
    seen: dict[str, CatalystHypothesis] = {}
    for h in hypotheses:
        existing = seen.get(h.hypothesis_id)
        if existing is None:
            seen[h.hypothesis_id] = h
        else:
            # Merge source agents
            agents = f"{existing.primary_source_agent}/{h.primary_source_agent}"
            # Keep the higher-score row; replace source agent with merged value
            if h.catalyst_score >= existing.catalyst_score:
                winner = h
            else:
                winner = existing
            # Rebuild with merged agent string
            seen[h.hypothesis_id] = CatalystHypothesis(
                hypothesis_id=winner.hypothesis_id,
                ticker=winner.ticker,
                hypothesis_type=winner.hypothesis_type,
                candidate_status=winner.candidate_status,
                catalyst_score=winner.catalyst_score,
                scenario_readiness=winner.scenario_readiness,
                priced_in_penalty=winner.priced_in_penalty,
                surprise_score=winner.surprise_score,
                conviction_at_generation=winner.conviction_at_generation,
                gross_expected_return_bps=winner.gross_expected_return_bps,
                proxy_tickers=winner.proxy_tickers,
                non_listed_entity=winner.non_listed_entity,
                evidence_summary=winner.evidence_summary,
                source_event_id=winner.source_event_id,
                horizon_days=winner.horizon_days,
                primary_source_agent=agents,
                action_type=winner.action_type,
                currency=winner.currency,
                observe_only=winner.observe_only,
                invalidates_if=winner.invalidates_if,
                event_at=winner.event_at,
                human_execution_only=winner.human_execution_only,
                execution_cost_model=winner.execution_cost_model,
                tradeability=winner.tradeability,
                risk_controls=winner.risk_controls,
            )
    return list(seen.values())


def rank_by_catalyst_score(
    hypotheses: list[CatalystHypothesis],
) -> list[CatalystHypothesis]:
    """Return hypotheses sorted by ``catalyst_score`` descending (stable)."""
    return sorted(hypotheses, key=lambda h: h.catalyst_score, reverse=True)


# ---------------------------------------------------------------------------
# I/O: log writer helper
# ---------------------------------------------------------------------------


def _write_hypothesis_to_log(
    h: CatalystHypothesis,
    *,
    log_path: Path,
    analysis_id: str,
    analysis_date: str,
    fsync: bool = True,
) -> None:
    """Write one hypothesis to the catalyst_hypothesis_log via the approved writer."""
    basket, weights, currency_normalized = _benchmark_for(h.ticker)
    try:
        write_catalyst_hypothesis_generated(
            log_path,
            hypothesis_id=h.hypothesis_id,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
            hypothesis_type=h.hypothesis_type,
            primary_ticker=h.ticker,
            catalyst_score=h.catalyst_score,
            scenario_readiness=h.scenario_readiness,
            priced_in_penalty=h.priced_in_penalty,
            surprise_score=h.surprise_score,
            gross_expected_return_bps=h.gross_expected_return_bps,
            conviction_at_generation=h.conviction_at_generation,
            price_at_event=_MISSING_PRICE,
            benchmark_basket=basket,
            benchmark_weights=weights,
            benchmark_currency_normalized_to=currency_normalized,
            benchmark_price_at_event={b: _MISSING_PRICE for b in basket},
            usdjpy_at_event=_PLACEHOLDER_FX,
            proxy_tickers=h.proxy_tickers if h.proxy_tickers else None,
            non_listed_entity=h.non_listed_entity,
            observe_only=h.observe_only,
            source_event_id=h.source_event_id,
            primary_source_agent=h.primary_source_agent,
            action_type=h.action_type,
            horizon_days=h.horizon_days,
            event_at=h.event_at,
            fsync=fsync,
        )
    except Exception:
        logger.exception(
            "Failed to write hypothesis %s (%s) to log", h.hypothesis_id, h.ticker
        )


# ---------------------------------------------------------------------------
# I/O orchestrator
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Evidence Sufficiency Gate (C6-5 / C7-4)
# ---------------------------------------------------------------------------


def _evidence_sufficiency_check(h: CatalystHypothesis) -> list[str]:
    """Return a list of *missing* logical field names that fail the Evidence Sufficiency Gate.

    The gate (Round 6 C6-5 + Round 7 C7-4) requires three fields to be present
    on every hypothesis before it can be injected into the Opus prompt:

    1. ``source_event`` — a non-empty ``source_event_id``.
    2. ``time_horizon_days`` — ``horizon_days > 0``.
    3. ``invalidation`` — a non-empty ``invalidates_if`` condition.

    **Legacy producer exemption**: hypotheses with ``hypothesis_type`` starting
    with ``"legacy"`` are exempt from **all** ESG checks.  The existing
    Sonnet/DeepSeek tier tool schemas are intentionally not modified (plan C6-5:
    「既存 Sonnet tier の submit_analysis tool schema は触らない（過剰保守化回避）」).
    Those producers don't supply ``source_event_id`` or ``invalidates_if`` by
    design; gating them out would silently discard every legacy signal.

    An empty list means the hypothesis **passes** the gate.  A non-empty list
    contains the logical field names (matching §6.13 schema) that are absent.
    """
    # Legacy producers bypass the gate entirely — their schema is intentionally
    # not modified, so they will always be missing source_event / invalidation.
    if h.hypothesis_type.startswith("legacy"):
        return []

    missing: list[str] = []
    if not h.source_event_id:
        missing.append("source_event")
    if not h.horizon_days or h.horizon_days <= 0:
        missing.append("time_horizon_days")
    if not h.invalidates_if:
        missing.append("invalidation")
    return missing


# ---------------------------------------------------------------------------
# Opus prompt helper
# ---------------------------------------------------------------------------


def compact_for_opus(
    output: "CatalystOutput",
    *,
    scenario_readiness: float = 0.0,
    max_items: int = 3,
    min_score_multiplier: float = 1.2,
) -> str:
    """Format top catalyst hypotheses as a compact block for Opus prompt injection.

    Decision hypotheses satisfying **all** of the following are included:

    - ``observe_only=False``
    - ``catalyst_score > scenario_readiness * min_score_multiplier``

    AI autonomy v2 also includes high-scoring ``observe_only=True`` non-disclosure
    hypotheses from ``all_hypotheses`` as capped review candidates. Raw
    disclosure features remain outside this compact block and are surfaced
    through the disclosure brief / information-lane verdict path. Review
    candidates are not inserted into ``CatalystOutput.top`` and must only become
    actions if the final synthesis emits a new ``provisional_decision`` action
    with ``source_observe_only=true`` and the downstream post-filter cap passes.

    Returns ``""`` when no hypothesis qualifies — zero output is explicitly
    allowed per plan §6.4 (「0 件出力を許容する」).

    Parameters
    ----------
    output:
        Return value of :func:`run`.
    scenario_readiness:
        Current global scenario readiness fraction in ``[0, 1]``.  Pass
        ``0.0`` (default) to admit all non-trivial hypotheses regardless of
        macro state.
    max_items:
        Hard cap — plan §5 step 10 permits 「通常 0-2 件、最大 3 件」.
    min_score_multiplier:
        Admission requires ``catalyst_score > scenario_readiness × this``.
        Defaults to 1.2 per plan §6.4.

    Output format (§6.4)::

        【触媒予測 (Catalyst Hypotheses)】
        - [TYPE] TICKER (conv=N, hor=Nd, score=N.NN): EVIDENCE_ONE_LINE
          invalidates_if: CONDITION_ONE_LINE
          proxy_for: NON_LISTED_ENTITY  ← only when applicable
    """
    threshold = scenario_readiness * min_score_multiplier
    decision_candidates = [
        h for h in output.top
        if not h.observe_only and h.catalyst_score > threshold
    ]
    review_candidates = [
        h for h in getattr(output, "all_hypotheses", [])
        if h.observe_only
        and h.hypothesis_type != "disclosure_catalyst"
        and h.catalyst_score > threshold
    ]
    # Reserve at least one prompt slot for observe-only review candidates when
    # they exist.  Otherwise high-scoring active scenarios can fill the small
    # top-N block and make measured-but-not-yet-promoted lanes invisible to the
    # final synthesis (e.g. japan_standalone_bull → JP ETF review).
    if review_candidates and max_items >= 2:
        ordered_candidates = (
            decision_candidates[: max_items - 1]
            + review_candidates[:1]
            + decision_candidates[max_items - 1 :]
            + review_candidates[1:]
        )
    else:
        ordered_candidates = decision_candidates + review_candidates

    candidates = []
    seen: set[tuple[str, str]] = set()
    for h in ordered_candidates:
        key = (h.hypothesis_id, h.ticker)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(h)
        if len(candidates) >= max_items:
            break

    if not candidates:
        return ""

    lines = ["【触媒予測 (Catalyst Hypotheses)】"]
    if any(h.observe_only for h in candidates):
        lines.append(
            "※ [OBSERVE-ONLY REVIEW] はAI文脈用。priority_actions化する場合は "
            "source_observe_only=true / provisional_decision=true / source_lane / ai_override_reason を付け、"
            "後段capを通すこと。生の observe_only=true action は不可。"
        )
    for h in candidates:
        type_tag = h.hypothesis_type.replace("_", "-").upper()
        if h.observe_only:
            type_tag = f"{type_tag} OBSERVE-ONLY REVIEW"
        score_str = f"{h.catalyst_score:.2f}"
        evidence = h.evidence_summary[:120].rstrip()
        lines.append(
            f"- [{type_tag}] {h.ticker} "
            f"(conv={h.conviction_at_generation}, hor={h.horizon_days}d, score={score_str}): "
            f"{evidence}"
        )
        if h.observe_only:
            lines.append(
                f"  source_observe_only: true / source_lane: {h.primary_source_agent or h.hypothesis_type}"
            )
        if h.invalidates_if:
            lines.append(f"  invalidates_if: {h.invalidates_if}")
        if h.human_execution_only:
            lines.append("  execution: human_execution_only")
        if h.risk_controls:
            controls = json.dumps(h.risk_controls, ensure_ascii=False, sort_keys=True)
            lines.append(f"  risk_controls: {controls[:240]}")
        if h.execution_cost_model:
            cost = json.dumps(h.execution_cost_model, ensure_ascii=False, sort_keys=True)
            lines.append(f"  cost_model: {cost[:240]}")
        if h.tradeability:
            trade = json.dumps(h.tradeability, ensure_ascii=False, sort_keys=True)
            lines.append(f"  tradeability: {trade[:240]}")
        if h.non_listed_entity:
            lines.append(f"  proxy_for: {h.non_listed_entity}")
    return "\n".join(lines)


def _load_json_file(path: Path | str | None, label: str) -> dict[str, Any]:
    """Load a JSON file gracefully, returning {} on any error."""
    if path is None:
        return {}
    p = Path(path)
    if not p.exists():
        logger.debug("%s not found: %s", label, p)
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            logger.warning("%s is not a dict (%s); ignoring", label, type(data).__name__)
            return {}
        return data
    except Exception:
        logger.exception("Failed to load %s from %s", label, p)
        return {}


def run(
    *,
    revision_state_path: Path | str | None = None,
    scenario_state_path: Path | str | None = None,
    proxy_seed_map_path: Path | str | None = None,
    legacy_analysis_path: Path | str | None = None,
    disclosure_features_path: Path | str | None = None,
    screener_payloads: dict[str, dict[str, Any]] | None = None,
    news_entities: list[str] | None = None,
    catalyst_log_path: Path | str | None = None,
    analysis_id: str,
    analysis_date: str,
    top_n: int = 10,
    write_log: bool = True,
) -> CatalystOutput:
    """Run the full catalyst synthesis pipeline and return a :class:`CatalystOutput`.

    Parameters
    ----------
    revision_state_path:
        Path to ``revision_state.json`` produced by revision_tracker. ``None``
        or missing file → revision source contributes zero hypotheses.
    scenario_state_path:
        Path to ``scenario_state.json``. ``None`` or missing → zero hypotheses.
    proxy_seed_map_path:
        Path to ``proxy_seed_map.json``. ``None`` or missing → zero hypotheses.
    legacy_analysis_path:
        Path to ``ai_portfolio_analysis.json``. Parsed via
        :func:`almanac.observability.candidate_extractor.extract_all`. ``None``
        or missing → zero hypotheses.
    news_entities:
        List of entity names detected in today's news (from the NER pipeline).
        Used to match against ``proxy_seed_map``. Defaults to ``[]``.
    catalyst_log_path:
        Where to append ``catalyst_hypothesis_log.jsonl`` rows. Required when
        ``write_log=True``; ignored otherwise.
    analysis_id:
        UUID for this ``analyzer.py`` run (from ``new_analysis_id()``).
    analysis_date:
        ISO date string for this run (``YYYY-MM-DD``).
    top_n:
        How many hypotheses to include in ``CatalystOutput.top``.
    write_log:
        When ``True`` and ``catalyst_log_path`` is provided, writes **all**
        hypotheses (not just top_n) to the log.
    """
    from datetime import datetime, timezone

    as_of = datetime.now(timezone.utc).isoformat()

    # --- Load inputs ---
    revision_state = _load_json_file(revision_state_path, "revision_state")
    scenario_state = _load_json_file(scenario_state_path, "scenario_state")
    proxy_seed_map_raw = _load_json_file(proxy_seed_map_path, "proxy_seed_map")
    legacy_analysis = _load_json_file(legacy_analysis_path, "legacy_analysis")

    # Disclosure features (Phase 0): JSONL store, not a single JSON object, so it
    # uses its own reader. These become observe_only hypotheses (measurement only).
    disclosure_features: list[dict[str, Any]] = []
    if disclosure_features_path is not None:
        from .disclosure_features import read_features as _read_disclosure_features
        disclosure_features = _read_disclosure_features(disclosure_features_path)

    # proxy_seed_map values should be lists of strings
    proxy_seed_map: dict[str, list[str]] = {}
    for entity, tickers in proxy_seed_map_raw.items():
        if isinstance(tickers, list):
            proxy_seed_map[entity] = [str(t) for t in tickers]
        else:
            logger.warning("proxy_seed_map[%r] is not a list; skipping", entity)

    # --- Extract legacy candidates ---
    candidate_packets: list[dict[str, Any]] = []
    if legacy_analysis:
        from .candidate_extractor import extract_all as _extract_all
        long_tier = (
            legacy_analysis.get("long_analysis")
            or legacy_analysis.get("long_tier")
            or legacy_analysis.get("long")
        )
        medium_tier = (
            legacy_analysis.get("medium_analysis")
            or legacy_analysis.get("medium_tier")
            or legacy_analysis.get("medium")
        )
        swing_tier = (
            legacy_analysis.get("short_positions_analysis")
            or legacy_analysis.get("swing_tier")
            or legacy_analysis.get("swing")
        )
        short_tier = (
            legacy_analysis.get("short_selling_analysis")
            or legacy_analysis.get("short_tier")
            or legacy_analysis.get("short_selling")
        )
        synthesis = legacy_analysis.get("synthesis")
        candidate_packets = _extract_all(
            analysis_id=analysis_id,
            analysis_date=analysis_date,
            long_tier=long_tier,
            medium_tier=medium_tier,
            swing_tier=swing_tier,
            short_tier=short_tier,
            synthesis=synthesis,
        )
    if screener_payloads:
        from .screener_hypotheses import extract_screener_packets
        candidate_packets.extend(
            extract_screener_packets(
                screener_payloads,
                analysis_date=analysis_date,
            )
        )

    # --- Synthesize from each source ---
    all_raw: list[CatalystHypothesis] = []
    all_raw.extend(
        synthesize_from_revision_state(
            revision_state,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    all_raw.extend(
        synthesize_from_active_scenarios(
            scenario_state,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    all_raw.extend(
        synthesize_from_proxy_predictions(
            proxy_seed_map,
            news_entities or [],
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    all_raw.extend(
        synthesize_from_legacy_producers(
            candidate_packets,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )
    all_raw.extend(
        synthesize_from_disclosure_features(
            disclosure_features,
            analysis_id=analysis_id,
            analysis_date=analysis_date,
        )
    )

    # S8: every producer, including revision/scenario/proxy lanes, converges here.
    from insider_restrictions import is_restricted_ticker
    all_raw = [h for h in all_raw if not is_restricted_ticker(h.ticker)]

    # --- Dedup + rank ---
    deduped = dedupe_by_hypothesis_id(all_raw)
    ranked = rank_by_catalyst_score(deduped)

    # --- Evidence Sufficiency Gate (C6-5 / C7-4) ---
    # Split hypotheses into those that pass the gate and those that don't.
    # Filtered ones are logged with candidate_status="not_injected" and
    # filter_reason/missing_fields; they are excluded from CatalystOutput.
    passed: list[CatalystHypothesis] = []
    filtered_by_esg: list[tuple[CatalystHypothesis, list[str]]] = []
    for h in ranked:
        missing = _evidence_sufficiency_check(h)
        if missing:
            filtered_by_esg.append((h, missing))
        else:
            passed.append(h)

    if write_log and catalyst_log_path is not None:
        log_path = Path(catalyst_log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        # Write ESG-filtered first (they are not in CatalystOutput)
        for h, missing_fields in filtered_by_esg:
            try:
                write_catalyst_hypothesis_filtered(
                    log_path,
                    hypothesis_id=h.hypothesis_id,
                    analysis_id=analysis_id,
                    analysis_date=analysis_date,
                    filter_reason="evidence_sufficiency_gate",
                    missing_fields=missing_fields,
                    fsync=False,
                )
            except Exception:
                logger.exception(
                    "Failed to write ESG-filtered hypothesis %s (%s)",
                    h.hypothesis_id, h.ticker,
                )
        # Write ALL passed hypotheses (Round 9 #3: append-only)
        for h in passed:
            _write_hypothesis_to_log(
                h,
                log_path=log_path,
                analysis_id=analysis_id,
                analysis_date=analysis_date,
                fsync=False,  # batch; caller can fsync the file separately
            )

    # --- Build output (only from passed hypotheses) ---
    top = [h for h in passed if not h.observe_only][:top_n]
    by_type: dict[str, int] = {}
    for h in passed:
        by_type[h.hypothesis_type] = by_type.get(h.hypothesis_type, 0) + 1

    return CatalystOutput(
        as_of=as_of,
        n_hypotheses_total=len(passed),
        n_hypotheses_top=len(top),
        top=top,
        by_type=by_type,
        all_hypotheses=passed,
    )
