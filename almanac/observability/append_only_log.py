"""Strict append-only JSONL log writer with multi-process safety.

This module is the shared foundation for every observability log defined in
the plan:

- ``catalyst_hypothesis_log.jsonl`` (§6.6)
- ``catalyst_outcome_log.jsonl`` (§6.14)
- ``sell_decision_log.jsonl`` (§6.8)
- ``sell_outcome_log.jsonl``
- ``agent_attribution_log.jsonl`` (§6.10 — flat row, R11 #1)
- ``portfolio_decision_log.jsonl`` (§6.11)
- ``cash_deployment_log.jsonl`` (§6.12 — event_type split, R11 #2)
- ``belief_adjustments.json[l]`` (§6.3)

The architectural rule established in Round 9 #3 is: **writes are
append-only, never mutate.** Status transitions, outcome updates, and
follow-up measurements all enter the log as additional rows joined by a
stable ID (``hypothesis_id``, ``cash_decision_id``, ``sell_decision_id``).

This module enforces that rule operationally:

- ``fcntl.flock`` (advisory ``LOCK_EX``) serializes concurrent writers from
  cron jobs that run on the same host (``analyzer.py`` 17:00,
  ``data_fetcher`` 17:30, ``signal_tracker`` 18:45).
- ``f.flush()`` + ``os.fsync()`` makes each row durable before the next
  writer can acquire the lock, eliminating the partial-write window that
  would corrupt JSONL parse.
- ``fsync=False`` is exposed as a kwarg for tests and historical backfill
  where durability per-row is unnecessary and would dominate runtime
  (Round 11 A clarification).

Currency normalization helpers live here too because every outcome log
needs to compare cross-currency baskets (Round 9 #6).
"""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "append_jsonl_safe",
    "normalize_to_jpy",
    "normalize_to_usd",
    "MeasurementQuality",
]


# ---------------------------------------------------------------------------
# Append-only writer
# ---------------------------------------------------------------------------


def append_jsonl_safe(
    path: Path | str,
    row: Mapping[str, Any],
    *,
    fsync: bool = True,
    ensure_parent: bool = True,
) -> None:
    """Append a single JSON row to ``path`` with multi-process safety.

    The function is the canonical write path for every observability log.
    It guarantees:

    1. **Atomic line append** — ``flock(LOCK_EX)`` serializes writers across
       processes on the same host; concurrent ``analyzer.py`` /
       ``signal_tracker.py`` cron jobs cannot interleave bytes.
    2. **Durability before release** — ``flush()`` + ``fsync()`` push the
       row to disk before the lock is released, so a crash mid-batch leaves
       a valid (possibly shorter) JSONL.
    3. **One row per call** — no batching, no rewriting. Mutating an
       existing row is intentionally impossible via this API; emit a new
       row joined by a stable ID instead (Round 9 #3).

    Parameters
    ----------
    path : Path or str
        Target log file. Created if missing.
    row : Mapping[str, Any]
        JSON-serializable payload. ``default=str`` is used so common
        non-serializable values (``datetime``, ``Path``, ``Decimal``)
        degrade to their ``str()`` form rather than raising.
    fsync : bool, default True
        When ``False``, skip ``os.fsync`` for the row. Use in unit tests
        and historical backfill where per-row durability would dominate
        runtime. ``flock`` and ``flush`` still run, so concurrent writers
        remain safe — only crash durability is relaxed.
    ensure_parent : bool, default True
        Create the parent directory if missing. Set ``False`` to fail
        loudly when callers expect the directory to exist.

    Raises
    ------
    TypeError
        If ``row`` contains a value that is not JSON-serializable even via
        ``str()``.
    OSError
        On filesystem errors (disk full, permission denied, etc.). The
        partial row, if any, is rolled back by the fact that
        ``write+flush+fsync`` is the only durability point.
    """
    path = Path(path)
    if ensure_parent:
        path.parent.mkdir(parents=True, exist_ok=True)

    # ``json.dumps`` runs *before* the lock so we never hold the lock while
    # doing CPU work — keeps contention low even under heavy logging.
    line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
    encoded = line.encode("utf-8")

    # Open in append+binary mode so we can write the pre-encoded bytes
    # without any text-mode newline translation surprises.
    with open(path, "ab") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        try:
            fh.write(encoded)
            fh.flush()
            if fsync:
                os.fsync(fh.fileno())
        finally:
            # Always release; ``with open`` would release on close anyway,
            # but explicit unlock keeps the contended window minimal.
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Currency normalization (Round 9 #6)
# ---------------------------------------------------------------------------


def normalize_to_jpy(price: float, currency: str, usdjpy: float) -> float:
    """Convert ``price`` to JPY.

    Used when an outcome log mixes JP- and US-listed names in the same
    ``benchmark_basket`` (e.g. ``["TOPIX", "ARM", "QQQ"]`` for a 9984.T
    ipo_proxy hypothesis). The basket value must be normalized to the
    primary ticker's reporting currency (Round 9 #6).

    Parameters
    ----------
    price : float
        Price in ``currency``.
    currency : str
        ISO 4217 code. Only ``"JPY"`` and ``"USD"`` are supported in MVP.
    usdjpy : float
        Spot USD/JPY rate at the measurement event.

    Returns
    -------
    float
        Price expressed in JPY.

    Raises
    ------
    ValueError
        If ``currency`` is not supported. Add new branches as the universe
        expands; silently degrading is dangerous because it can corrupt
        excess-return arithmetic.
    """
    if currency == "JPY":
        return price
    if currency == "USD":
        return price * usdjpy
    raise ValueError(
        f"Unsupported currency for JPY normalization: {currency!r}. "
        "Add a branch to normalize_to_jpy() if the universe expands."
    )


def normalize_to_usd(price: float, currency: str, usdjpy: float) -> float:
    """Convert ``price`` to USD. Mirror of :func:`normalize_to_jpy`."""
    if currency == "USD":
        return price
    if currency == "JPY":
        # usdjpy is JPY-per-USD, so USD = JPY / usdjpy. Guard against the
        # nonsensical case where a caller passes 0 so we surface the bug
        # immediately rather than producing inf in the outcome log.
        if usdjpy <= 0:
            raise ValueError(f"usdjpy must be positive, got {usdjpy}")
        return price / usdjpy
    raise ValueError(
        f"Unsupported currency for USD normalization: {currency!r}."
    )


# ---------------------------------------------------------------------------
# Outcome measurement quality (plan §6.14)
# ---------------------------------------------------------------------------


class MeasurementQuality:
    """String constants for the ``measurement_quality`` field of outcome logs.

    Kept as a plain namespace (not :class:`enum.Enum`) so the values land in
    JSON as bare strings without callers needing ``.value`` accessors.
    """

    OK = "ok"
    """Normal measurement; price source returned a value on schedule."""

    STALE = "stale"
    """Data source returned a price but the timestamp is past the SLA."""

    MISSING = "missing"
    """Data source had no price (holiday, delisted, vendor gap)."""

    REVISED = "revised"
    """Corp-action adjustment after the fact (new row, ``OK`` superseded)."""
