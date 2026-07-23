"""Deterministic belief-invalidation rules (plan §5 step 2).

Background
----------

After the v2 migration (:mod:`almanac.migrations.agent_beliefs_v1_to_v2`)
each belief carries:

- ``base_conviction`` — set once at creation, never mutated.
- ``adjusted_conviction`` — runtime value = ``base + Σdelta`` over rows
  in ``belief_adjustments.jsonl`` matching this ``belief_id``.
- ``adjustment_log`` — list of adjustment row IDs (kept for forward
  reference / debug; the source of truth is ``belief_adjustments.jsonl``).

This module produces those ``belief_adjustments.jsonl`` rows by applying
three deterministic checks per belief:

1. **Holding-horizon expiry**  — once ``today`` is past ``expires_at``
   the belief no longer reflects a fresh thesis. ``delta = -10``.
2. **RSI overheat**  — RSI₁₄ > 75 on the primary ticker means the
   pullback we were waiting for is unlikely to happen soon. ``delta = -15``.
3. **MA20 break**  — price closes below the 20-day moving average,
   invalidating the technical setup. ``delta = -15``.

Each rule is a small pure function returning either an
:class:`InvalidationAdjustment` or ``None``. The orchestrator
:func:`evaluate_beliefs` composes them and is the single read-only
boundary between belief state and the deterministic policy.

Design choices
--------------

- **Pure-functional core.** No I/O, no clock, no network. The runner
  function :func:`apply_invalidations` is the only place that touches
  disk; it accepts injected callables for ``today`` and
  ``market_snapshot`` so tests can pin both.
- **Single delta per (belief, rule, day).** A belief that has already
  received an ``invalidation:rsi_overheat`` adjustment today is skipped
  so re-running the cron is safe. Idempotency is checked via the most-
  recent adjustment log row, not via a separate ledger.
- **Open-closed extension.** Adding a fourth rule means writing one
  pure function and registering it in :data:`RULES`. Existing tests
  pin the current three so a regression is caught immediately.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from .logs import write_belief_adjustment

__all__ = [
    "RULE_VERSION",
    "EXPIRY_DELTA",
    "RSI_OVERHEAT_DELTA",
    "MA20_BREAK_DELTA",
    "MarketIndicators",
    "InvalidationAdjustment",
    "check_expired",
    "check_rsi_overheat",
    "check_ma20_break",
    "RULES",
    "evaluate_belief",
    "evaluate_beliefs",
    "apply_invalidations",
]

logger = logging.getLogger(__name__)

#: String used in every adjustment ``rule_version`` field so future v2
#: rules can be distinguished from v1.
RULE_VERSION = "invalidation_rules:v1.0"

#: Delta applied when a belief has expired (negative — reduces conviction).
EXPIRY_DELTA = -10
#: Delta applied when RSI₁₄ on the primary ticker exceeds 75.
RSI_OVERHEAT_DELTA = -15
#: Delta applied when price closes below MA20.
MA20_BREAK_DELTA = -15


@dataclass(frozen=True)
class MarketIndicators:
    """Minimal market snapshot a belief check needs.

    All fields are optional because :func:`evaluate_belief` will skip the
    rule when the relevant indicator is missing, rather than fabricate an
    adjustment from incomplete data. This matches the plan rule "never
    invalidate on uncertainty" (R8 false-positive guard).
    """

    ticker: str
    #: Latest close.
    price: float | None = None
    #: 20-day simple moving average of close.
    ma20: float | None = None
    #: 14-period RSI.
    rsi_14: float | None = None


@dataclass(frozen=True)
class InvalidationAdjustment:
    """A single deterministic adjustment to be appended to the log."""

    belief_id: str
    ticker: str
    delta: int
    reason: str
    evidence: dict[str, Any]


# ---------------------------------------------------------------------------
# Pure rule functions
# ---------------------------------------------------------------------------


def check_expired(
    belief: dict[str, Any],
    today: date,
) -> InvalidationAdjustment | None:
    """Return an adjustment if the belief has passed ``expires_at``.

    Missing or malformed ``expires_at`` returns ``None`` rather than
    raising — agent_beliefs has had multiple writers historically and a
    bad timestamp must not crash the cron.
    """
    raw = belief.get("expires_at")
    if not raw:
        return None
    try:
        # ``fromisoformat`` accepts both ``YYYY-MM-DD`` and full ISO 8601
        # timestamps; production beliefs use the latter.
        expires = datetime.fromisoformat(raw).date()
    except (TypeError, ValueError):
        return None
    if today <= expires:
        return None
    return InvalidationAdjustment(
        belief_id=belief["id"],
        ticker=belief.get("ticker", "?"),
        delta=EXPIRY_DELTA,
        reason="invalidation:expired",
        evidence={
            "expires_at": raw,
            "today": today.isoformat(),
            "days_past_expiry": (today - expires).days,
        },
    )


def check_rsi_overheat(
    belief: dict[str, Any],
    market: MarketIndicators,
) -> InvalidationAdjustment | None:
    """Return an adjustment when RSI₁₄ on the belief's ticker > 75."""
    if market.rsi_14 is None or market.rsi_14 <= 75:
        return None
    return InvalidationAdjustment(
        belief_id=belief["id"],
        ticker=belief.get("ticker", "?"),
        delta=RSI_OVERHEAT_DELTA,
        reason="invalidation:rsi_overheat",
        evidence={"rsi_14": market.rsi_14, "threshold": 75},
    )


def check_ma20_break(
    belief: dict[str, Any],
    market: MarketIndicators,
) -> InvalidationAdjustment | None:
    """Return an adjustment when price closes below MA20."""
    if market.price is None or market.ma20 is None:
        return None
    if market.price >= market.ma20:
        return None
    return InvalidationAdjustment(
        belief_id=belief["id"],
        ticker=belief.get("ticker", "?"),
        delta=MA20_BREAK_DELTA,
        reason="invalidation:ma20_break",
        evidence={
            "price": market.price,
            "ma20": market.ma20,
            "below_ma20_pct": (market.price - market.ma20) / market.ma20,
        },
    )


#: Registry consulted by :func:`evaluate_belief`. Adding a fourth rule
#: means appending here and writing a single pure function.
RULES: tuple[str, ...] = (
    "expired",
    "rsi_overheat",
    "ma20_break",
)


# ---------------------------------------------------------------------------
# Composition
# ---------------------------------------------------------------------------


def evaluate_belief(
    belief: dict[str, Any],
    today: date,
    market: MarketIndicators | None,
) -> list[InvalidationAdjustment]:
    """Apply every rule to one belief, return zero or more adjustments.

    ``market`` may be ``None`` (e.g. belief about an asset class with no
    ticker); in that case only :func:`check_expired` is consulted.
    """
    out: list[InvalidationAdjustment] = []
    expired = check_expired(belief, today)
    if expired is not None:
        out.append(expired)
    if market is not None:
        rsi = check_rsi_overheat(belief, market)
        if rsi is not None:
            out.append(rsi)
        ma = check_ma20_break(belief, market)
        if ma is not None:
            out.append(ma)
    return out


def evaluate_beliefs(
    beliefs: Iterable[dict[str, Any]],
    today: date,
    market_snapshot: dict[str, MarketIndicators],
) -> list[InvalidationAdjustment]:
    """Evaluate every belief; flat-list the adjustments.

    Beliefs whose ticker is missing from ``market_snapshot`` are still
    evaluated for expiry (the only rule that does not need market data).
    """
    adjustments: list[InvalidationAdjustment] = []
    for belief in beliefs:
        if not isinstance(belief, dict) or "id" not in belief:
            # Defensive: skip records lacking the join key. The migration
            # already validated production data, so this should never fire
            # in practice — but a corrupted manual edit must not crash the
            # cron.
            logger.warning(
                "skipping invalid belief (missing id): %r",
                belief if isinstance(belief, dict) else type(belief).__name__,
            )
            continue
        ticker = belief.get("ticker")
        market = market_snapshot.get(ticker) if ticker else None
        adjustments.extend(evaluate_belief(belief, today, market))
    return adjustments


# ---------------------------------------------------------------------------
# Runner (the only function that touches disk)
# ---------------------------------------------------------------------------


def _read_today_adjustments(
    adjustments_path: Path | str,
    today: date,
) -> set[tuple[str, str]]:
    """Return ``{(belief_id, reason)}`` already applied today.

    Used by :func:`apply_invalidations` to enforce the "one delta per
    (belief, rule, day)" contract its docstring promises. Codex P1
    review (Round 12) found the runner was re-applying the same delta
    on every cron tick because this filter was missing.

    Rows with missing or malformed ``applied_at`` are skipped silently —
    a corrupted log row must not block today's evaluation.
    """
    p = Path(adjustments_path)
    if not p.exists():
        return set()
    seen: set[tuple[str, str]] = set()
    with p.open("r", encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping malformed belief_adjustments row")
                continue
            applied_at = row.get("applied_at")
            if not applied_at:
                continue
            try:
                d = datetime.fromisoformat(applied_at).date()
            except (TypeError, ValueError):
                continue
            if d != today:
                continue
            belief_id = row.get("belief_id")
            reason = row.get("reason")
            if belief_id and reason:
                seen.add((belief_id, reason))
    return seen


def apply_invalidations(
    beliefs_path: Path | str,
    adjustments_path: Path | str,
    *,
    today: date,
    market_snapshot: dict[str, MarketIndicators],
    fsync: bool = True,
) -> list[str]:
    """Read beliefs, evaluate rules, append adjustments. Return row_ids.

    The function is intentionally narrow — it does not fetch market data
    (caller injects ``market_snapshot``) and it does not modify
    ``agent_beliefs.json``. The downstream consumer that reads beliefs
    rebuilds ``adjusted_conviction = base + Σdelta`` at synthesis time
    (plan Round 3 architecture).

    **Idempotency (Codex Round 12 P1 #1)**: an adjustment already written
    for the same ``(belief_id, reason)`` on ``today`` is skipped so
    re-running the cron does not stack duplicate ``-10/-15`` deltas onto
    the same belief. The existing ``belief_adjustments.jsonl`` is scanned
    once at entry; new rows are appended only for adjustments not yet
    seen today. Returns ``row_ids`` for **newly written** rows only —
    skipped duplicates are not included.
    """
    beliefs_path = Path(beliefs_path)
    with beliefs_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict) or "beliefs" not in payload:
        raise ValueError(
            f"{beliefs_path}: expected {{beliefs: [...]}} shape; "
            "is the v1→v2 migration done?"
        )
    seen_today = _read_today_adjustments(adjustments_path, today)
    adjustments = evaluate_beliefs(payload["beliefs"], today, market_snapshot)
    # Pin ``applied_at`` to the caller's ``today`` rather than wall-clock
    # ``_now_utc()`` so the per-day idempotency lookup is reliable when
    # back-fill / tests inject a fixed date that differs from the host
    # clock (the bug surfaced in Codex Round 12 P1 #1 regression tests).
    applied_at_iso = f"{today.isoformat()}T00:00:00+00:00"
    row_ids: list[str] = []
    for adj in adjustments:
        if (adj.belief_id, adj.reason) in seen_today:
            logger.debug(
                "idempotency skip: %s already applied to %s today",
                adj.reason, adj.belief_id,
            )
            continue
        row_id = write_belief_adjustment(
            adjustments_path,
            belief_id=adj.belief_id,
            ticker=adj.ticker,
            delta=adj.delta,
            reason=adj.reason,
            evidence=adj.evidence,
            rule_version=RULE_VERSION,
            applied_at=applied_at_iso,
            fsync=fsync,
        )
        # Record the freshly-written adjustment so subsequent rules in
        # this same call don't redundantly re-write either.
        seen_today.add((adj.belief_id, adj.reason))
        row_ids.append(row_id)
    return row_ids


# ---------------------------------------------------------------------------
# Optional injection point for callers that want to supply ``today`` via
# a factory (useful for back-fill runs against historical snapshots).
# ---------------------------------------------------------------------------


def make_apply(
    today_fn: Callable[[], date],
    snapshot_fn: Callable[[Iterable[str]], dict[str, MarketIndicators]],
) -> Callable[[Path | str, Path | str], list[str]]:
    """Curry :func:`apply_invalidations` with date and snapshot providers.

    The production runner wires this to ``datetime.utcnow().date`` and a
    yfinance-backed snapshot fetcher; tests wire it to fixed dates and
    handcrafted snapshots.
    """
    def _run(
        beliefs_path: Path | str,
        adjustments_path: Path | str,
        *,
        fsync: bool = True,
    ) -> list[str]:
        beliefs_path = Path(beliefs_path)
        with beliefs_path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        tickers = {
            b.get("ticker") for b in payload.get("beliefs", []) if b.get("ticker")
        }
        snapshot = snapshot_fn(tickers)
        return apply_invalidations(
            beliefs_path,
            adjustments_path,
            today=today_fn(),
            market_snapshot=snapshot,
            fsync=fsync,
        )

    return _run
