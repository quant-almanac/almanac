"""Proxy mapping for the ALMANAC catalyst observability layer (plan §5 step 10).

Background
----------

Catalysts are often about entities that are not directly listed (e.g.
"OpenAI IPO filing", "SpaceX launch manifest"). The proxy mapper turns
such non-listed-entity catalysts into *listed ticker hypotheses* — the
actual stocks a portfolio can trade — by reasoning about ownership stakes,
supply-chain exposure, and thematic overlap.

This module is the Phase 2-D implementation referenced in Round 6 C6-5.

4-Layer Architecture (Round 6)
-------------------------------

- **L1 (deterministic)**: ``proxy_seed_map.json`` lookup — hand-curated
  lowercase-normalised key → list[ticker] mapping. Fast, auditable,
  zero-cost. Always consulted first.

- **L2 (LLM proposer)**: An :class:`LLMProvider` implementation proposes
  additional tickers given the entity name and context dict. In
  production this will be Sonnet; in tests it is stubbed via the
  Protocol. Real API calls are **never** made in this module.

- **L3 (LLM skeptic)**: A second pass through the same
  :class:`LLMProvider` filters dubious proposals from L2, returning
  only tickers the skeptic agrees are plausible proxies. This reduces
  hallucination propagation.

- **L4 (self-consistency)**: Runs L2+L3 ``self_consistency_n`` times
  independently, then intersects surviving ticker sets using a Jaccard
  threshold. Tickers that appear consistently across runs are kept;
  outliers are discarded. This layer runs only when
  ``self_consistency_n >= 2``.

**R6 C6-5 audit rule**: every :func:`propose_proxies` call's
input/output is appended to ``proxy_audit_log.jsonl`` via
:func:`almanac.observability.append_only_log.append_jsonl_safe` when
``audit_log_path`` is provided.

**R9 #3 append-only**: the audit log is write-only. No mutation API is
exposed. Status transitions enter the log as additional rows.

**NEVER make a real LLM API call** in this module. The
:class:`LLMProvider` Protocol is purely structural; real wiring of
Sonnet is a follow-up task out of scope here.

Behavior matrix
---------------

- ``llm is None`` → seed-only path, ``used_layers=["seed"]``, no L2-4.
- ``llm`` provided, ``self_consistency_n=1`` → seed + L2 + L3,
  ``used_layers=["seed", "llm_propose", "llm_skeptic"]``.
- ``llm`` provided, ``self_consistency_n >= 2`` → full 4-layer,
  ``used_layers`` includes ``"self_consistency"``.
- entity not in seed_map and ``llm is None`` → returns
  :class:`ProxyResult` with empty ``final_proxies``, NO error.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .append_only_log import append_jsonl_safe

__all__ = [
    "LLMProvider",
    "ProxyResult",
    "load_seed_map",
    "lookup_seed",
    "jaccard_intersection",
    "propose_proxies",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM provider Protocol (injected; no real API calls here)
# ---------------------------------------------------------------------------


@runtime_checkable
class LLMProvider(Protocol):
    """Structural interface for the two LLM passes in L2/L3.

    Implementations must be injected by the caller. Production wiring of
    real Sonnet is a follow-up task and is **out of scope** for this module.

    All implementations must be pure from this module's perspective — they
    receive a snapshot of the context and return a list of ticker strings.
    """

    def propose(self, entity: str, context: dict) -> list[str]:
        """L2: propose proxy tickers for *entity*.

        Parameters
        ----------
        entity : str
            Normalised entity name (already lower-stripped).
        context : dict
            Arbitrary caller-supplied context (news snippet, sector hint,
            etc.). Passed through unchanged so the implementation can use it.

        Returns
        -------
        list[str]
            Zero or more ticker symbols the model proposes as proxies.
        """
        ...

    def critique(self, proposal: list[str], context: dict) -> list[str]:
        """L3: filter *proposal* to plausible proxies only.

        Parameters
        ----------
        proposal : list[str]
            The tickers proposed by L2.
        context : dict
            Same context dict as was passed to :meth:`propose`.

        Returns
        -------
        list[str]
            Subset of *proposal* that the skeptic accepts.
        """
        ...


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProxyResult:
    """Complete output of one :func:`propose_proxies` invocation.

    All list fields are de-duplicated and order-stable.  ``final_proxies``
    is the authoritative result for downstream consumers.
    """

    entity: str
    """Normalised entity string that was looked up."""

    seed_proxies: list[str]
    """Tickers returned by L1 (seed map)."""

    llm_proposed: list[str]
    """Tickers proposed by L2 (LLM proposer). Empty when llm=None."""

    llm_filtered: list[str]
    """Tickers surviving L3 (LLM skeptic) from *llm_proposed*. Empty when
    llm=None or when self_consistency_n >= 2 (L4 supersedes)."""

    final_proxies: list[str]
    """Union of seed + LLM-vetted proxies, de-duplicated, order-stable."""

    jaccard_self_consistency: float | None
    """Mean pairwise Jaccard of L4 samples. None when L4 not run."""

    used_layers: list[str]
    """Ordered subset of ["seed", "llm_propose", "llm_skeptic",
    "self_consistency"] indicating which layers contributed."""


# ---------------------------------------------------------------------------
# L1 — seed map
# ---------------------------------------------------------------------------


def load_seed_map(path: Path | str) -> dict[str, list[str]]:
    """Load and return the seed proxy map from *path*.

    Keys are expected to be lowercase strings; values are lists of ticker
    symbols. The file must be valid JSON shaped as ``{str: [str, ...]}``.

    Parameters
    ----------
    path : Path or str
        Location of ``proxy_seed_map.json``.

    Returns
    -------
    dict[str, list[str]]
        Mapping from normalised entity key to list of proxy tickers.

    Raises
    ------
    ValueError
        If the file cannot be parsed or has an unexpected shape.
    """
    p = Path(path)
    try:
        with p.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p}: invalid JSON — {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p}: expected top-level JSON object, got {type(data).__name__}")
    # Validate that all values are lists of strings.
    for key, val in data.items():
        if not isinstance(val, list):
            raise ValueError(
                f"{p}: value for key {key!r} must be a list, got {type(val).__name__}"
            )
    return data  # type: ignore[return-value]


def lookup_seed(entity: str, seed_map: dict[str, list[str]]) -> list[str]:
    """Return the seed proxies for *entity* from *seed_map*.

    Entity normalisation: ``.lower().strip()``.  An entity absent from the
    map returns an empty list — no exception.

    Parameters
    ----------
    entity : str
        Raw entity string (case-insensitive, leading/trailing whitespace OK).
    seed_map : dict[str, list[str]]
        Seed proxy map as returned by :func:`load_seed_map`.

    Returns
    -------
    list[str]
        De-duplicated list of proxy tickers, or ``[]`` if not found.
    """
    normalised = entity.lower().strip()
    return list(seed_map.get(normalised, []))


# ---------------------------------------------------------------------------
# L4 — Jaccard self-consistency
# ---------------------------------------------------------------------------


def jaccard_intersection(
    samples: list[list[str]],
    threshold: float = 0.5,
) -> list[str]:
    """Compute the intersection of *samples* weighted by pairwise Jaccard.

    Algorithm
    ---------

    1. If ``samples`` is empty → return ``[]``.
    2. If ``samples`` has exactly 1 element → return that element unchanged
       (single-sample bypass; pairwise Jaccard is undefined with < 2 samples).
    3. For each pair ``(i, j)`` with ``i < j`` compute the Jaccard similarity
       of their ticker sets.
    4. Keep only tickers that appear in **every** sample whose pairwise Jaccard
       with at least one other sample meets the threshold.  Concretely: keep a
       ticker iff it appears in the intersection of all samples (strict
       intersection) AND the mean pairwise Jaccard across all pairs is >=
       *threshold*.  If the mean Jaccard is below *threshold* the intersection
       is still returned — the *threshold* gates whether we trust the run
       overall; the caller uses the scalar separately.

    The mean pairwise Jaccard is returned separately as
    ``jaccard_self_consistency`` on :class:`ProxyResult`; this function
    returns the **ticker list** formed by taking the set intersection across
    all samples.

    Parameters
    ----------
    samples : list[list[str]]
        Each inner list is the ``llm_filtered`` output from one L2+L3 run.
    threshold : float, default 0.5
        Jaccard similarity threshold used by the caller to decide whether
        to trust the consistency result.  This function always returns the
        intersection; the threshold is applied by :func:`propose_proxies`.

    Returns
    -------
    list[str]
        Order-stable intersection of all sample sets.
    """
    if not samples:
        return []
    if len(samples) == 1:
        return list(samples[0])

    # Compute set intersection across all samples preserving order from first.
    sets = [set(s) for s in samples]
    common = sets[0]
    for s in sets[1:]:
        common = common & s

    # Return tickers in the order they first appeared in samples[0].
    first_order = list(dict.fromkeys(samples[0]))  # dedup, preserve order
    return [t for t in first_order if t in common]


def _mean_pairwise_jaccard(samples: list[list[str]]) -> float:
    """Return the mean pairwise Jaccard similarity across *samples*.

    Returns 1.0 for a single sample (convention — perfectly self-consistent).
    Returns 0.0 if all samples are empty sets.
    """
    if len(samples) <= 1:
        return 1.0
    sets = [set(s) for s in samples]
    scores: list[float] = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            a, b = sets[i], sets[j]
            union = a | b
            if not union:
                scores.append(1.0)  # both empty → identical
            else:
                scores.append(len(a & b) / len(union))
    return sum(scores) / len(scores) if scores else 1.0


# ---------------------------------------------------------------------------
# Audit log writer
# ---------------------------------------------------------------------------


def _write_audit_row(
    audit_log_path: Path | str,
    *,
    entity: str,
    context: dict,
    seed_proxies: list[str],
    llm_proposed: list[str],
    llm_filtered: list[str],
    final_proxies: list[str],
    jaccard_self_consistency: float | None,
    used_layers: list[str],
) -> None:
    """Append one row to ``proxy_audit_log.jsonl`` (R6 C6-5).

    Uses :func:`almanac.observability.append_only_log.append_jsonl_safe`
    for multi-process safety. ``fsync=True`` to match the round-9 durability
    contract for observability logs.
    """
    row = {
        "row_id": str(uuid.uuid4()),
        "entity": entity,
        "context": context,
        "seed_proxies": seed_proxies,
        "llm_proposed": llm_proposed,
        "llm_filtered": llm_filtered,
        "final_proxies": final_proxies,
        "jaccard_self_consistency": jaccard_self_consistency,
        "used_layers": used_layers,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    append_jsonl_safe(audit_log_path, row, fsync=True)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def propose_proxies(
    entity: str,
    *,
    seed_map: dict[str, list[str]],
    llm: LLMProvider | None = None,
    self_consistency_n: int = 3,
    consistency_threshold: float = 0.5,
    audit_log_path: Path | str | None = None,
    context: dict | None = None,
) -> ProxyResult:
    """Map *entity* to listed proxy tickers using the 4-layer architecture.

    Parameters
    ----------
    entity : str
        Entity name to map (e.g. ``"OpenAI IPO filing"``). Must be non-empty.
        Leading/trailing whitespace is stripped; lookup is case-insensitive.
    seed_map : dict[str, list[str]]
        Pre-loaded seed map from :func:`load_seed_map`.
    llm : LLMProvider or None, default None
        Injected LLM provider.  When ``None`` only L1 runs.
    self_consistency_n : int, default 3
        Number of independent L2+L3 runs for L4. Must be >= 1.
        When == 1, L4 is skipped and L3's single-pass result is used.
    consistency_threshold : float, default 0.5
        Minimum mean pairwise Jaccard to accept L4 result.  When the mean
        Jaccard falls below this threshold, ``final_proxies`` will contain
        only ``seed_proxies`` (L4 result is untrusted).
    audit_log_path : Path or str or None, default None
        When provided, one row is appended per invocation (R6 C6-5).
    context : dict or None, default None
        Arbitrary caller-supplied context forwarded to the LLM provider.

    Returns
    -------
    ProxyResult
        See :class:`ProxyResult` for field-level semantics.

    Raises
    ------
    ValueError
        If *entity* is an empty string (after strip).
    """
    if not isinstance(entity, str) or not entity.strip():
        raise ValueError(
            f"entity must be a non-empty string; got {entity!r}"
        )

    # Normalise entity for all downstream use.
    normalised = entity.lower().strip()
    ctx = context or {}
    # Always include the entity in the LLM context. Callers may pass extra
    # hints, but they should not need to remember this required field.
    llm_ctx = {"entity": normalised, **ctx}

    # ------------------------------------------------------------------
    # L1 — deterministic seed lookup
    # ------------------------------------------------------------------
    seed_proxies = lookup_seed(normalised, seed_map)
    logger.debug("L1 seed lookup: entity=%r → %s", normalised, seed_proxies)

    # ------------------------------------------------------------------
    # Seed-only path (llm is None)
    # ------------------------------------------------------------------
    if llm is None:
        result = ProxyResult(
            entity=normalised,
            seed_proxies=seed_proxies,
            llm_proposed=[],
            llm_filtered=[],
            final_proxies=list(seed_proxies),
            jaccard_self_consistency=None,
            used_layers=["seed"],
        )
        if audit_log_path is not None:
            _write_audit_row(
                audit_log_path,
                entity=normalised,
                context=ctx,
                seed_proxies=result.seed_proxies,
                llm_proposed=result.llm_proposed,
                llm_filtered=result.llm_filtered,
                final_proxies=result.final_proxies,
                jaccard_self_consistency=result.jaccard_self_consistency,
                used_layers=result.used_layers,
            )
        return result

    # ------------------------------------------------------------------
    # L2 + L3 with optional L4
    # ------------------------------------------------------------------
    n_runs = max(1, self_consistency_n)
    all_filtered_samples: list[list[str]] = []
    # Keep one representative llm_proposed / llm_filtered for the result
    # (from the first run) for auditability.
    first_llm_proposed: list[str] = []
    first_llm_filtered: list[str] = []

    for run_idx in range(n_runs):
        # L2 — propose
        try:
            proposed = llm.propose(normalised, llm_ctx)
        except Exception as exc:  # noqa: BLE001 — provider may be unreliable
            logger.warning(
                "LLM proposer raised on run %d for entity %r: %s",
                run_idx, normalised, exc,
            )
            proposed = []

        # L3 — critique / filter
        if proposed:
            try:
                filtered = llm.critique(proposed, llm_ctx)
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "LLM skeptic raised on run %d for entity %r: %s",
                    run_idx, normalised, exc,
                )
                filtered = []
        else:
            filtered = []

        logger.debug(
            "L2/L3 run %d: entity=%r proposed=%s filtered=%s",
            run_idx, normalised, proposed, filtered,
        )

        all_filtered_samples.append(filtered)
        if run_idx == 0:
            first_llm_proposed = list(proposed)
            first_llm_filtered = list(filtered)

    # ------------------------------------------------------------------
    # L4 — self-consistency (only when n_runs >= 2)
    # ------------------------------------------------------------------
    if n_runs >= 2:
        jaccard = _mean_pairwise_jaccard(all_filtered_samples)
        if jaccard >= consistency_threshold:
            consistent_llm = jaccard_intersection(all_filtered_samples, consistency_threshold)
        else:
            # Low consistency → distrust LLM result; fall back to seed-only.
            logger.warning(
                "L4 self-consistency below threshold (%.3f < %.3f) for entity %r; "
                "LLM proxies discarded.",
                jaccard, consistency_threshold, normalised,
            )
            consistent_llm = []
        used_layers = ["seed", "llm_propose", "llm_skeptic", "self_consistency"]
    else:
        # Single run: L4 not applicable, use L3 output directly.
        jaccard = None
        consistent_llm = first_llm_filtered
        used_layers = ["seed", "llm_propose", "llm_skeptic"]

    # ------------------------------------------------------------------
    # Merge seed + LLM results, de-duplicate, preserve order (seed first).
    # ------------------------------------------------------------------
    seen: set[str] = set()
    final_proxies: list[str] = []
    for ticker in seed_proxies + consistent_llm:
        if ticker not in seen:
            seen.add(ticker)
            final_proxies.append(ticker)

    result = ProxyResult(
        entity=normalised,
        seed_proxies=list(seed_proxies),
        llm_proposed=first_llm_proposed,
        llm_filtered=first_llm_filtered,
        final_proxies=final_proxies,
        jaccard_self_consistency=jaccard,
        used_layers=used_layers,
    )

    if audit_log_path is not None:
        _write_audit_row(
            audit_log_path,
            entity=normalised,
            context=ctx,
            seed_proxies=result.seed_proxies,
            llm_proposed=result.llm_proposed,
            llm_filtered=result.llm_filtered,
            final_proxies=result.final_proxies,
            jaccard_self_consistency=result.jaccard_self_consistency,
            used_layers=result.used_layers,
        )

    return result
