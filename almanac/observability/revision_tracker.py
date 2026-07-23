"""Revision detection from news headlines (plan §5 step 6).

Background
----------

The catalyst layer needs to know which tickers had a recent earnings
guidance revision so ``earnings_revision_pullback`` candidates can be
scored. The MVP source is purely the existing
``news_signal_candidates.json`` (the daily news aggregator output)
matched against a small keyword list — yfinance recommendations / EPS
consensus diffs are recorded for posterity but **not** signalized until
Phase 2 (plan §5 step 6 c-d).

Surprise score and priced-in penalty (plan §6.7 / Round 4)
----------------------------------------------------------

A revision that everyone already mentioned is no edge; the score must
fade with prior mentions. We therefore maintain a small
``revision_mention_ledger.jsonl`` (append-only) recording every
(ticker, keyword, headline_hash, seen_at). When today's run sees a
match, it counts prior matches for the same ``event_key`` in the last 30
days to decay the surprise score. The exact formulas live in
:func:`compute_surprise_score` and :func:`compute_priced_in_penalty` so a
single reviewable function pins the policy.

Outputs
-------

- ``revision_state.json`` — snapshot rewritten atomically each run
  (plan §6.1 shape). One entry per ticker that had at least one match.
- ``revision_mention_ledger.jsonl`` — strict append-only ledger driving
  surprise / priced-in over time.
- ``revision_snapshots/{ticker}_{date}.json`` — current-price snapshots
  saved when the optional ``snapshot_provider`` is supplied. Used in
  Phase 2 to compute price-target / EPS diffs without needing yfinance
  historicals at compute time.

Pure / impure split
-------------------

Pure (no I/O, fully testable without fixtures):
  - :data:`REVISION_KEYWORDS`
  - :func:`match_headlines`
  - :func:`compute_surprise_score`
  - :func:`compute_priced_in_penalty`
  - :func:`build_ticker_entry`

I/O (only here do we touch disk / clock):
  - :func:`load_mention_ledger`
  - :func:`append_mention_ledger`
  - :func:`write_revision_state`
  - :func:`run`
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

__all__ = [
    "REVISION_KEYWORDS",
    "RevisionKeyword",
    "RevisionMatch",
    "TickerEntry",
    "match_headlines",
    "compute_surprise_score",
    "compute_priced_in_penalty",
    "build_ticker_entry",
    "load_mention_ledger",
    "append_mention_ledger",
    "write_revision_state",
    "run",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keyword catalogue
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevisionKeyword:
    """A regex pattern with the direction and locale it implies."""

    pattern: str
    direction: str  # "up" | "down"
    locale: str    # "ja" | "en"

    def compile(self) -> re.Pattern[str]:
        # IGNORECASE for English; harmless for Japanese (no case in CJK).
        return re.compile(self.pattern, re.IGNORECASE)


#: Ordered registry of keywords. New patterns should be added at the end so
#: existing tests pin the current set.
REVISION_KEYWORDS: tuple[RevisionKeyword, ...] = (
    # Japanese — upward
    RevisionKeyword(r"上方修正|増額修正|業績予想.{0,5}上方", "up", "ja"),
    # English — upward (covers "raises guidance", "raised full-year outlook")
    RevisionKeyword(
        r"(?:guidance raise|raised guidance|raises\s+(?:full[- ]year\s+)?(?:guidance|outlook|forecast)"
        r"|upward (?:guidance )?revision)",
        "up",
        "en",
    ),
    # Japanese — downward
    RevisionKeyword(r"下方修正|減額修正|業績予想.{0,5}下方", "down", "ja"),
    # English — downward
    RevisionKeyword(
        r"(?:guidance cut|cuts guidance|lowers\s+(?:full[- ]year\s+)?(?:guidance|outlook|forecast)"
        r"|downward (?:guidance )?revision)",
        "down",
        "en",
    ),
)


# Compiled lazily so tests can extend REVISION_KEYWORDS via monkeypatch
# without paying a compile cost on import.
def _compiled_keywords() -> list[tuple[RevisionKeyword, re.Pattern[str]]]:
    return [(kw, kw.compile()) for kw in REVISION_KEYWORDS]


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RevisionMatch:
    """A single keyword hit on one headline."""

    keyword: RevisionKeyword
    headline: str

    @property
    def headline_hash(self) -> str:
        """Stable hash so the ledger can dedupe repeated headlines."""
        return hashlib.sha256(self.headline.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TickerEntry:
    """One row of :func:`build_revision_state` output."""

    ticker: str
    direction: str  # "up" | "down" | "none"
    strength: float
    surprise_score: float
    priced_in_penalty: float
    first_seen_at: str | None
    prior_mentions_count: int
    sources: list[dict[str, Any]] = field(default_factory=list)
    last_event_date: str | None = None


def match_headlines(headlines: Iterable[str]) -> list[RevisionMatch]:
    """Return every revision keyword hit across ``headlines``.

    A headline matching multiple keywords (e.g. mixed Japanese / English
    framing) produces multiple matches. Direction conflict (one headline
    that contains both up and down patterns) is left to
    :func:`build_ticker_entry` to resolve — this function is pure search.
    """
    compiled = _compiled_keywords()
    out: list[RevisionMatch] = []
    for headline in headlines:
        if not headline:
            continue
        for kw, regex in compiled:
            if regex.search(headline):
                out.append(RevisionMatch(keyword=kw, headline=headline))
    return out


def compute_surprise_score(
    prior_mentions_count: int,
    days_since_first_seen: int,
    market_reaction_pct: float,
) -> float:
    """Plan §6.7 formula. Pure for review-ability.

    - ``prior_mentions_count`` decays the 0.5-weighted "novelty" term.
    - ``days_since_first_seen`` ≤ 2 gives the 0.3 "freshness" bonus.
    - ``market_reaction_pct`` is the cumulative % move since first seen;
      we credit 0.2 if the market is still <5% reactive.

    Clipped to ``[0, 1]``.
    """
    novelty = 0.5 * (1.0 / (1.0 + max(prior_mentions_count, 0)))
    freshness = 0.3 if days_since_first_seen <= 2 else 0.0
    reaction_room = 0.2 * max(0.0, 1.0 - abs(market_reaction_pct) / 0.05)
    raw = novelty + freshness + reaction_room
    return max(0.0, min(1.0, raw))


def compute_priced_in_penalty(
    prior_mentions_count: int,
    days_since_first_seen: int,
    market_reaction_pct: float,
) -> float:
    """Plan §6.7 formula. Clipped to ``[0, 0.6]``.

    The cap of 0.6 (not 1.0) keeps even very-known catalysts from being
    completely zero-weighted — a strong material can still run after the
    first wave, so we only penalize, never veto (Round 9 #7 reaffirmed).
    """
    n = 0.4 * min(max(prior_mentions_count, 0) / 10.0, 1.0)
    stale = 0.4 if days_since_first_seen > 7 else 0.0
    reacted = 0.2 if abs(market_reaction_pct) > 0.05 else 0.0
    raw = n + stale + reacted
    return max(0.0, min(0.6, raw))


def _resolve_direction(matches: list[RevisionMatch]) -> tuple[str, float]:
    """Reduce a bag of matches to a final direction + strength in [0, 1].

    Strength is the magnitude of the up-vs-down delta, normalized by the
    total match count, so unanimous up=1.0 / 3-up-1-down=0.5 / tie=0.0
    (which collapses direction to ``"none"``).
    """
    if not matches:
        return ("none", 0.0)
    up = sum(1 for m in matches if m.keyword.direction == "up")
    down = sum(1 for m in matches if m.keyword.direction == "down")
    total = up + down
    if total == 0:
        return ("none", 0.0)
    delta = up - down
    if delta == 0:
        return ("none", 0.0)
    direction = "up" if delta > 0 else "down"
    strength = abs(delta) / total
    return (direction, strength)


def build_ticker_entry(
    *,
    ticker: str,
    matches: list[RevisionMatch],
    prior_mentions_count: int,
    first_seen_at: str | None,
    today: date,
    market_reaction_pct: float = 0.0,
    last_event_date: str | None = None,
) -> TickerEntry:
    """Combine pure rules into one :class:`TickerEntry`.

    ``market_reaction_pct`` defaults to ``0`` so callers without a price
    feed (e.g. tests) get a sane surprise_score / priced_in_penalty pair
    without needing to fabricate prices.
    """
    direction, strength = _resolve_direction(matches)
    if first_seen_at is None:
        days_since = 0
    else:
        try:
            seen_date = datetime.fromisoformat(first_seen_at).date()
            days_since = max(0, (today - seen_date).days)
        except (TypeError, ValueError):
            days_since = 0
    surprise = compute_surprise_score(
        prior_mentions_count, days_since, market_reaction_pct
    )
    penalty = compute_priced_in_penalty(
        prior_mentions_count, days_since, market_reaction_pct
    )
    sources = [
        {
            "type": "news_keyword",
            "keyword": m.keyword.pattern,
            "direction": m.keyword.direction,
            "locale": m.keyword.locale,
            "headline": m.headline,
            "headline_hash": m.headline_hash,
            "as_of": today.isoformat(),
        }
        for m in matches
    ]
    return TickerEntry(
        ticker=ticker,
        direction=direction,
        strength=strength,
        surprise_score=surprise,
        priced_in_penalty=penalty,
        first_seen_at=first_seen_at,
        prior_mentions_count=prior_mentions_count,
        sources=sources,
        last_event_date=last_event_date,
    )


# ---------------------------------------------------------------------------
# I/O: mention ledger
# ---------------------------------------------------------------------------


def load_mention_ledger(
    path: Path | str,
    *,
    cutoff: date | None = None,
) -> list[dict[str, Any]]:
    """Load the append-only mention ledger.

    Rows older than ``cutoff`` are filtered out. ``cutoff`` defaults to
    ``today − 30 days`` so prior_mentions_count over the standard window
    works out of the box.
    """
    p = Path(path)
    if not p.exists():
        return []
    if cutoff is None:
        cutoff = date.today() - timedelta(days=30)
    rows: list[dict[str, Any]] = []
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skip malformed ledger row: %r", line[:120])
                continue
            seen_at = row.get("seen_at")
            if not seen_at:
                continue
            try:
                seen_date = datetime.fromisoformat(seen_at).date()
            except (TypeError, ValueError):
                continue
            if seen_date >= cutoff:
                rows.append(row)
    return rows


def append_mention_ledger(
    path: Path | str,
    *,
    ticker: str,
    keyword_pattern: str,
    direction: str,
    headline_hash: str,
    seen_at: str,
) -> None:
    """Append one mention row.

    Uses the shared :func:`almanac.observability.append_only_log.append_jsonl_safe`
    so it picks up the same fcntl + fsync guarantees as every other
    observability log.
    """
    from .append_only_log import append_jsonl_safe

    append_jsonl_safe(
        path,
        {
            "ticker": ticker,
            "keyword": keyword_pattern,
            "direction": direction,
            "headline_hash": headline_hash,
            "seen_at": seen_at,
        },
        fsync=True,
    )


def _count_prior(
    ledger: list[dict[str, Any]],
    *,
    ticker: str,
    today: date,
) -> tuple[int, str | None]:
    """Return ``(distinct_prior_mentions, first_seen_at)`` for *ticker*.

    "Prior" means strictly **before today** — yesterday's mention of the
    same headline is the canonical prior. If the cron is re-run within
    the same day, the same-day appends from the earlier run are also
    excluded so the count remains stable.

    **Codex Round 12 P2 #3**: previously counted raw rows per ticker, so
    a headline repeated in the ledger across 3 days inflated the prior
    count to 3 instead of 1, and same-day re-run duplicates double-
    counted themselves. We now collapse to distinct ``headline_hash``
    values so each catalyst event contributes a single prior regardless
    of how many ledger rows carry it. Rows without ``headline_hash`` are
    bucketed under a sentinel so legacy rows still count once.

    Rows with missing or malformed ``seen_at`` are skipped silently
    (the ledger has had multiple writers historically).
    """
    seen_hashes: set[str] = set()
    earliest: str | None = None
    for r in ledger:
        if r.get("ticker") != ticker:
            continue
        seen_at = r.get("seen_at")
        if not seen_at:
            continue
        try:
            seen_date = datetime.fromisoformat(seen_at).date()
        except (TypeError, ValueError):
            continue
        if seen_date >= today:
            continue
        # Legacy rows lacking ``headline_hash`` count once collectively
        # rather than being dropped — the alternative (silent loss)
        # would understate priors for tickers whose history pre-dates
        # the hash field.
        h = r.get("headline_hash") or "__legacy_no_hash__"
        seen_hashes.add(h)
        if earliest is None or seen_at < earliest:
            earliest = seen_at
    return (len(seen_hashes), earliest)


# ---------------------------------------------------------------------------
# I/O: revision_state.json
# ---------------------------------------------------------------------------


def write_revision_state(
    path: Path | str,
    *,
    as_of: str,
    entries: dict[str, TickerEntry],
) -> None:
    """Atomically rewrite ``revision_state.json`` (plan §6.1 shape).

    The file is a daily snapshot, not an append-only log, so a `.tmp`
    + ``os.replace`` keeps a crashed run from corrupting the live file.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "as_of": as_of,
        "tickers": {
            ticker: {
                "direction": entry.direction,
                "strength": entry.strength,
                "surprise_score": entry.surprise_score,
                "priced_in_penalty": entry.priced_in_penalty,
                "first_seen_at": entry.first_seen_at,
                "prior_mentions_count": entry.prior_mentions_count,
                "sources": entry.sources,
                "last_event_date": entry.last_event_date,
            }
            for ticker, entry in entries.items()
        },
    }
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, p)


def _maybe_snapshot_price(
    snapshot_dir: Path | None,
    *,
    ticker: str,
    today: date,
    snapshot_provider: Callable[[str], dict[str, Any] | None] | None,
) -> None:
    """Save the current-price snapshot when both dir and provider exist.

    Failure to snapshot is logged but never raised — the regex output is
    the MVP signal; snapshots are forward-looking data collection only.
    """
    if snapshot_dir is None or snapshot_provider is None:
        return
    try:
        snap = snapshot_provider(ticker)
    except Exception as exc:  # noqa: BLE001 — provider may be unreliable
        logger.warning("snapshot_provider(%s) raised: %s", ticker, exc)
        return
    if not snap:
        return
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    out = snapshot_dir / f"{ticker}_{today.isoformat()}.json"
    out.write_text(
        json.dumps(snap, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run(
    *,
    news_path: Path | str,
    state_path: Path | str,
    ledger_path: Path | str,
    today: date,
    snapshot_dir: Path | str | None = None,
    snapshot_provider: Callable[[str], dict[str, Any] | None] | None = None,
    market_reaction_provider: Callable[[str, date], float] | None = None,
) -> dict[str, TickerEntry]:
    """End-to-end MVP run. Returns the entries written.

    Steps:

    1. Load ``news_signal_candidates.json`` (the daily news aggregator
       output).
    2. For each candidate, regex-match headlines via :func:`match_headlines`.
    3. Pull prior mention counts from the 30-day ledger.
    4. Append new mentions to the ledger.
    5. Build :class:`TickerEntry` per ticker that had at least one match.
    6. Atomically write ``revision_state.json``.
    7. (Optional) snapshot current prices for Phase 2 diff use.
    """
    news_path = Path(news_path)
    with news_path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    candidates = payload.get("candidates", [])
    if not isinstance(candidates, list):
        raise ValueError(f"{news_path}: 'candidates' must be a list")

    ledger = load_mention_ledger(ledger_path, cutoff=today - timedelta(days=30))

    # Codex Round 12 P2 #3: same-day re-runs must not append duplicate
    # mentions to the ledger. Collect today's already-recorded
    # ``(ticker, headline_hash)`` keys up front so the per-row guard below
    # is a constant-time set lookup.
    today_already_seen: set[tuple[str, str]] = set()
    for row in ledger:
        seen_at = row.get("seen_at")
        if not seen_at:
            continue
        try:
            if datetime.fromisoformat(seen_at).date() != today:
                continue
        except (TypeError, ValueError):
            continue
        tkr = row.get("ticker")
        hash_ = row.get("headline_hash")
        if tkr and hash_:
            today_already_seen.add((tkr, hash_))

    snapshot_path = Path(snapshot_dir) if snapshot_dir else None

    entries: dict[str, TickerEntry] = {}
    as_of_iso = datetime.now(timezone.utc).isoformat()
    today_iso = today.isoformat()

    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        ticker = cand.get("ticker")
        if not ticker:
            continue
        headlines = cand.get("top_headlines") or []
        matches = match_headlines(headlines)
        if not matches:
            continue

        # Per-headline dedupe so a single headline counts once even when
        # multiple keywords match it. Use first match per headline.
        seen_hashes: set[str] = set()
        unique_matches: list[RevisionMatch] = []
        for m in matches:
            if m.headline_hash in seen_hashes:
                continue
            seen_hashes.add(m.headline_hash)
            unique_matches.append(m)

        prior_count, first_seen = _count_prior(
            ledger,
            ticker=ticker,
            today=today,
        )
        market_reaction = 0.0
        if market_reaction_provider is not None and first_seen:
            try:
                first_seen_date = datetime.fromisoformat(first_seen).date()
                market_reaction = market_reaction_provider(ticker, first_seen_date)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "market_reaction_provider(%s) raised: %s", ticker, exc
                )
                market_reaction = 0.0

        entry = build_ticker_entry(
            ticker=ticker,
            matches=unique_matches,
            prior_mentions_count=prior_count,
            first_seen_at=first_seen,
            today=today,
            market_reaction_pct=market_reaction,
            last_event_date=cand.get("last_article_at"),
        )
        entries[ticker] = entry

        # Append today's mentions to the ledger BEFORE writing state so
        # tomorrow's run will see them as prior mentions. Skip duplicates
        # that an earlier run today already wrote (Codex Round 12 P2 #3).
        for m in unique_matches:
            dedup_key = (ticker, m.headline_hash)
            if dedup_key in today_already_seen:
                continue
            append_mention_ledger(
                ledger_path,
                ticker=ticker,
                keyword_pattern=m.keyword.pattern,
                direction=m.keyword.direction,
                headline_hash=m.headline_hash,
                seen_at=today_iso,
            )
            today_already_seen.add(dedup_key)

        _maybe_snapshot_price(
            snapshot_path,
            ticker=ticker,
            today=today,
            snapshot_provider=snapshot_provider,
        )

    write_revision_state(state_path, as_of=as_of_iso, entries=entries)
    return entries
