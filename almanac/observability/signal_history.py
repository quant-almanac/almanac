"""Schema extension helpers for ``signal_history.json``.

Background
----------

``signal_history.json`` is the existing production log driving
:mod:`signal_tracker` (cron at 18:45). The MVP plan (§5 step 3) extends
the schema with five new fields so every record can join with the
catalyst observability layer:

- ``hypothesis_id`` — stable hash from
  :func:`almanac.observability.ids.compute_hypothesis_id`. Joins to
  ``catalyst_hypothesis_log.jsonl`` and ``agent_attribution_log.jsonl``.
- ``hypothesis_type`` — one of ``earnings_revision_pullback`` /
  ``bull_pullback`` / ``ipo_proxy`` / ``legacy``.
- ``horizon_days`` — intended holding horizon (``signal_tracker`` measures
  ``outcome_5d`` / ``outcome_10d`` today; extended to 3/20/60 in Phase 2).
- ``candidate_status`` (6-state enum, see :class:`CandidateStatus`).
- ``execution_state`` (6-state enum, see :class:`ExecutionState`).

Constraints
-----------

This module is **purely additive** — the existing ``signal_tracker`` cron
keeps working unchanged. Legacy records (those produced before the
migration) load with status fields defaulted to ``"legacy"`` so
``recommendation_verifier`` can group by hypothesis_type × candidate_status
× horizon without raising on missing keys (plan R18 — legacy compat).

Why a helper instead of mutating the file
-----------------------------------------

``signal_history.json`` is a single JSON list (not JSONL), so it cannot
sit behind :func:`append_jsonl_safe`. It is also rewritten in full by
:func:`signal_tracker.save_history` after each ``update_outcomes`` cycle,
which would clobber any mutation we made. The helper instead **decorates
records at read time** and provides a factory for writers (screeners,
catalyst_layer) to emit forward-compatible records.

API
---

- :func:`apply_legacy_defaults` — non-destructive: returns a copy with
  the five new fields populated for legacy records.
- :func:`read_history` — convenience: ``load + apply_legacy_defaults``
  in one call.
- :func:`make_record` — factory enforcing the extended schema for new
  writers. Rejects ``candidate_status="legacy"`` because a freshly
  emitted record should never claim legacy status.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from .status import CandidateStatus, ExecutionState

__all__ = [
    "EXTENDED_FIELDS",
    "LEGACY_HYPOTHESIS_TYPE",
    "SignalRecord",
    "apply_legacy_defaults",
    "make_record",
    "read_history",
]

#: Sentinel ``hypothesis_type`` for records that pre-date the migration.
LEGACY_HYPOTHESIS_TYPE = "legacy"

#: The five fields :func:`apply_legacy_defaults` will inject when absent.
#: Kept in module scope so tests can assert membership without duplicating
#: the list.
EXTENDED_FIELDS: tuple[str, ...] = (
    "hypothesis_id",
    "hypothesis_type",
    "horizon_days",
    "candidate_status",
    "execution_state",
)


class SignalRecord(TypedDict, total=False):
    """Canonical extended shape of a row in ``signal_history.json``.

    ``total=False`` because legacy records (37 in production at the time
    of writing) lack the extended fields until :func:`apply_legacy_defaults`
    runs. Downstream consumers should still call ``.get()`` for resilience.
    """

    # --- v1 fields (must remain untouched for cron compat) ---
    date: str
    ticker: str
    strategy: str
    signal: str
    confidence: float
    reason: str
    price_at_signal: float
    rsi: float
    volume_ratio: float
    mom_5d: float
    debate: Any
    regime: str
    outcome_5d: float | None
    outcome_10d: float | None
    # --- v2 extension (new) ---
    hypothesis_id: str | None
    hypothesis_type: str
    horizon_days: int | None
    candidate_status: str
    execution_state: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def apply_legacy_defaults(record: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy of *record* with the v2 fields populated.

    Behaviour:

    - Fields already present (any value, including ``None``) are
      left untouched.
    - Missing fields are filled with the legacy defaults:
      ``hypothesis_id=None``, ``hypothesis_type="legacy"``,
      ``horizon_days=None``, ``candidate_status="legacy"``,
      ``execution_state="legacy"``.
    - The input dict is never mutated; callers can rely on referential
      identity for the original.

    The function is the safe entry point for any consumer (verifier,
    weekly report, audit) that reads ``signal_history.json`` directly.
    """
    out = dict(record)  # shallow copy is enough; values are scalars / lists.
    defaults = {
        "hypothesis_id": None,
        "hypothesis_type": LEGACY_HYPOTHESIS_TYPE,
        "horizon_days": None,
        "candidate_status": CandidateStatus.legacy.value,
        "execution_state": ExecutionState.legacy.value,
    }
    for k, v in defaults.items():
        out.setdefault(k, v)
    return out


def read_history(path: Path | str) -> list[dict[str, Any]]:
    """Load ``signal_history.json`` and decorate every record.

    Returns an empty list when the file is missing — matches the behaviour
    of :func:`signal_tracker.load_history` so callers can substitute it
    without conditional checks.
    """
    p = Path(path)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    if not isinstance(raw, list):
        raise ValueError(
            f"{p}: expected a JSON list at the top level, got "
            f"{type(raw).__name__}"
        )
    return [apply_legacy_defaults(r) for r in raw]


def make_record(
    *,
    # --- v1 required fields (replicated so writers fail fast on missing) ---
    date: str,
    ticker: str,
    strategy: str,
    signal: str,
    confidence: float,
    reason: str,
    price_at_signal: float,
    # --- v1 optional indicator fields ---
    rsi: float | None = None,
    volume_ratio: float | None = None,
    mom_5d: float | None = None,
    debate: Any = None,
    regime: str | None = None,
    # --- v2 extension (new) ---
    hypothesis_id: str | None = None,
    hypothesis_type: str = LEGACY_HYPOTHESIS_TYPE,
    horizon_days: int | None = None,
    candidate_status: CandidateStatus | str = CandidateStatus.generated,
    execution_state: ExecutionState | str = ExecutionState.not_ordered,
) -> dict[str, Any]:
    """Build a forward-compatible record dict for :mod:`signal_tracker`.

    Validates ``candidate_status`` / ``execution_state`` so a typo cannot
    enter the history file. Rejects ``candidate_status="legacy"`` because a
    freshly emitted record claiming legacy status is almost certainly a
    caller bug — legacy is for back-fill of pre-migration rows only.

    Returns a plain dict (not :class:`SignalRecord`) so callers can pass it
    straight to :func:`signal_tracker.save_history`.
    """
    cs = _coerce_enum(candidate_status, CandidateStatus, "candidate_status")
    es = _coerce_enum(execution_state, ExecutionState, "execution_state")
    if cs == CandidateStatus.legacy.value:
        raise ValueError(
            "candidate_status='legacy' is reserved for pre-migration rows; "
            "new records must use a production status"
        )
    if es == ExecutionState.legacy.value:
        raise ValueError(
            "execution_state='legacy' is reserved for pre-migration rows; "
            "new records must use a production state"
        )

    record: dict[str, Any] = {
        "date": date,
        "ticker": ticker,
        "strategy": strategy,
        "signal": signal,
        "confidence": confidence,
        "reason": reason,
        "price_at_signal": price_at_signal,
        "rsi": rsi,
        "volume_ratio": volume_ratio,
        "mom_5d": mom_5d,
        "debate": debate,
        "regime": regime,
        # Outcomes are filled by signal_tracker.update_outcomes cron later.
        "outcome_5d": None,
        "outcome_10d": None,
        # v2 extension
        "hypothesis_id": hypothesis_id,
        "hypothesis_type": hypothesis_type,
        "horizon_days": horizon_days,
        "candidate_status": cs,
        "execution_state": es,
    }
    return record


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _coerce_enum(value: Any, expected: type, field: str) -> str:
    """Accept enum member or string; return the canonical string value.

    Mirrors :func:`almanac.observability.logs._coerce_status` but raises a
    field-aware error message so the call site is obvious in tracebacks.
    """
    if isinstance(value, expected):
        return value.value
    if isinstance(value, str):
        try:
            return expected(value).value
        except ValueError as exc:
            raise ValueError(
                f"{field}={value!r} is not a valid {expected.__name__}"
            ) from exc
    raise TypeError(
        f"{field} must be {expected.__name__} or str, "
        f"got {type(value).__name__}"
    )
