"""Regime shift detector for the ALMANAC catalyst observability layer.

Phase 2-E / Plan §5 step 11 / Round 6 C6-9.

**Phase 2 record-only — no downstream effect in MVP (Round 6 C6-8).**

When the market regime changes (e.g. ``bull_pullback`` → ``defensive``) the
historical track records that produced today's ``agent_reliability.json``
weights are partially invalidated — past wins in a bull regime don't prove an
agent will win in a bear regime.  This module detects those transitions and
records a cooldown window during which reliability weights *would* be halved.

**Invariants (Round 6 C6-9)**

- READ-ONLY of ``scenario_state.json`` and any external file.
- WRITE-ONLY of its own output files (``regime_shift_ledger.jsonl`` and
  ``regime_shift_report.json``).
- NEVER mutates ``agent_reliability.json`` or any other module's file.

**Output files**

- ``regime_shift_ledger.jsonl`` — strict append-only; one row per detected
  transition.  Written via
  :func:`almanac.observability.append_only_log.append_jsonl_safe`.
- ``regime_shift_report.json`` — daily snapshot; written atomically via
  ``.tmp`` + ``os.replace`` so a crashed run cannot corrupt the live file.

**Idempotency**

A same-day re-run that finds the regime unchanged since the last ledger row
does NOT append a duplicate row.

Public API
----------
- :class:`RegimeShift` — frozen dataclass for one detected transition.
- :class:`RegimeShiftReport` — frozen dataclass summary for a given day.
- :func:`classify_severity` — pure severity classifier.
- :func:`detect_shift` — pure shift detector.
- :func:`compute_active_multiplier` — pure multiplier from active shifts.
- :func:`run` — I/O orchestrator; the only function that touches disk.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "RegimeShift",
    "RegimeShiftReport",
    "classify_severity",
    "detect_shift",
    "compute_active_multiplier",
    "run",
]

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Regime taxonomy
# ---------------------------------------------------------------------------

#: Maps known regime IDs to their family.  Regimes not present here default
#: to the ``"unknown"`` family which triggers ``"major"`` severity
#: (conservative).
_REGIME_FAMILY: dict[str, str] = {
    "bull_pullback": "bull",
    "tech_boom": "bull",
    "fed_pivot": "bull",
    "war_end": "neutral",
    "defensive": "defensive",
    "risk_off": "defensive",
    "geopolitical_crisis": "defensive",
}

_UNKNOWN_FAMILY = "unknown"


def _family(regime: str | None) -> str:
    """Return the family for a regime string, or ``"unknown"`` if unrecognised."""
    if regime is None:
        return _UNKNOWN_FAMILY
    return _REGIME_FAMILY.get(regime, _UNKNOWN_FAMILY)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeShift:
    """One detected transition between market regimes.

    ``cooldown_until`` is inclusive — the weight multiplier of 0.5 applies
    on and through that date.
    """

    detected_at: str        # ISO datetime (UTC)
    from_regime: str | None  # None if no prior history
    to_regime: str
    severity: str           # "minor" | "major"
    cooldown_until: str     # ISO date (inclusive)


@dataclass(frozen=True)
class RegimeShiftReport:
    """Daily snapshot produced by :func:`run`."""

    as_of: str
    current_regime: str | None
    active_shifts: list[RegimeShift]      # shifts whose cooldown still applies
    historical_shifts: list[RegimeShift]  # all shifts in the ledger
    weight_multiplier: float              # 1.0 if no active shifts, else 0.5


# ---------------------------------------------------------------------------
# Pure rules
# ---------------------------------------------------------------------------


def classify_severity(
    from_regime: str | None,
    to_regime: str,
) -> str:
    """Classify the severity of a regime transition.

    Rules:

    - Same family (both ``"bull*"``, both ``"defensive*"`` etc.) → ``"minor"``
    - Cross-family (different families) → ``"major"``
    - Unknown family on either side → ``"major"`` (conservative)

    Parameters
    ----------
    from_regime:
        Previous regime ID, or ``None`` when there is no prior history.
    to_regime:
        The regime being transitioned into.

    Returns
    -------
    ``"minor"`` or ``"major"``.
    """
    f_family = _family(from_regime)
    t_family = _family(to_regime)
    if (
        f_family == _UNKNOWN_FAMILY
        or t_family == _UNKNOWN_FAMILY
        or f_family != t_family
    ):
        return "major"
    return "minor"


def detect_shift(
    previous_regime: str | None,
    current_regime: str,
    *,
    today: date,
    cooldown_days: int = 30,
) -> RegimeShift | None:
    """Detect a regime transition and return a :class:`RegimeShift` if one occurred.

    Returns ``None`` when ``previous_regime == current_regime`` (no change).

    Parameters
    ----------
    previous_regime:
        The last recorded ``to_regime`` from the ledger, or ``None`` if the
        ledger is empty (first ever run).
    current_regime:
        The regime derived from ``scenario_state.json`` for today.
    today:
        Calendar date of the run; used to compute ``cooldown_until``.
    cooldown_days:
        Number of calendar days (inclusive) for which the halved-weight
        multiplier applies.  Default 30.

    Returns
    -------
    :class:`RegimeShift` or ``None``.
    """
    if previous_regime == current_regime:
        return None
    severity = classify_severity(previous_regime, current_regime)
    cooldown_until = (today + timedelta(days=cooldown_days - 1)).isoformat()
    detected_at = datetime.now(timezone.utc).isoformat()
    return RegimeShift(
        detected_at=detected_at,
        from_regime=previous_regime,
        to_regime=current_regime,
        severity=severity,
        cooldown_until=cooldown_until,
    )


def compute_active_multiplier(
    shifts: list[RegimeShift],
    today: date,
) -> float:
    """Return the weight multiplier given current active shifts.

    Formula
    -------
    - Default: ``1.0``
    - If ANY shift in ``shifts`` has ``cooldown_until >= today``: ``0.5``

    MVP uses a flat 0.5 regardless of severity.  A future hook for
    ``"major"`` → 0.25 could be added here without changing the public
    contract.

    Parameters
    ----------
    shifts:
        Full list of :class:`RegimeShift` objects from the ledger.
    today:
        Calendar date used to check cooldown expiry.

    Returns
    -------
    ``0.5`` or ``1.0``.
    """
    today_iso = today.isoformat()
    for shift in shifts:
        if shift.cooldown_until >= today_iso:
            return 0.5
    return 1.0


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read every parseable JSON line from a JSONL file.  Missing file → ``[]``."""
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "%s:%d: malformed JSONL row, skipping", path, lineno
                )
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _row_to_shift(row: dict[str, Any]) -> RegimeShift | None:
    """Deserialise one ledger row to :class:`RegimeShift`.

    Returns ``None`` for rows missing required fields (defensive; a
    corrupted row must not crash the daily run).
    """
    try:
        return RegimeShift(
            detected_at=row["detected_at"],
            from_regime=row.get("from_regime"),  # may be None
            to_regime=row["to_regime"],
            severity=row["severity"],
            cooldown_until=row["cooldown_until"],
        )
    except (KeyError, TypeError):
        logger.warning("skipping malformed ledger row: %r", row)
        return None


def _shift_to_row(shift: RegimeShift) -> dict[str, Any]:
    """Serialise a :class:`RegimeShift` to a ledger row dict."""
    return {
        "row_id": str(uuid.uuid4()),
        "detected_at": shift.detected_at,
        "from_regime": shift.from_regime,
        "to_regime": shift.to_regime,
        "severity": shift.severity,
        "cooldown_until": shift.cooldown_until,
    }


def _shift_to_dict(shift: RegimeShift) -> dict[str, Any]:
    """Serialise a :class:`RegimeShift` to a plain dict for the report file."""
    return {
        "detected_at": shift.detected_at,
        "from_regime": shift.from_regime,
        "to_regime": shift.to_regime,
        "severity": shift.severity,
        "cooldown_until": shift.cooldown_until,
    }


def _derive_current_regime(scenario_state_path: Path) -> str | None:
    """Read ``scenario_state.json`` and return the highest-readiness scenario id.

    The file has shape::

        {
            "scenarios": {
                "<id>": {"readiness": 0.52, ...},
                ...
            },
            ...
        }

    Returns ``None`` when the file is missing, empty, or has no scenario with
    a parseable ``readiness`` value (graceful handling of empty/corrupt state).
    """
    if not scenario_state_path.exists():
        logger.warning("scenario_state.json not found at %s", scenario_state_path)
        return None
    try:
        with scenario_state_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("could not read scenario_state.json: %s", exc)
        return None
    scenarios = data.get("scenarios")
    if not scenarios or not isinstance(scenarios, dict):
        return None
    best_id: str | None = None
    best_readiness: float = -1.0
    for scenario_id, info in scenarios.items():
        if not isinstance(info, dict):
            continue
        readiness = info.get("readiness")
        if readiness is None:
            readiness = info.get("readiness_pct")  # forward-compat alias
        if not isinstance(readiness, (int, float)):
            continue
        if readiness > best_readiness:
            best_readiness = float(readiness)
            best_id = scenario_id
    return best_id


def _write_report_atomic(
    report_path: Path,
    report: RegimeShiftReport,
) -> None:
    """Atomically write the daily snapshot report.

    Uses ``.tmp`` + ``os.replace`` (mirrors
    ``revision_tracker.write_revision_state``).
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "as_of": report.as_of,
        "current_regime": report.current_regime,
        "weight_multiplier": report.weight_multiplier,
        "active_shifts": [_shift_to_dict(s) for s in report.active_shifts],
        "historical_shifts": [_shift_to_dict(s) for s in report.historical_shifts],
    }
    tmp = report_path.with_suffix(report_path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, report_path)


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run(
    *,
    scenario_state_path: Path | str,
    shift_ledger_path: Path | str,
    report_path: Path | str,
    today: date,
    cooldown_days: int = 30,
) -> RegimeShiftReport:
    """Detect regime shifts, update the ledger, write a daily report.

    Steps
    -----
    1. Read ``scenario_state.json`` → derive ``current_regime`` (highest
       ``readiness`` scenario id; ``None`` if file absent or empty).
    2. Read ``shift_ledger.jsonl`` (full history); empty file → ``[]``.
    3. Determine ``previous_regime`` from the most-recent ledger row's
       ``to_regime``; ``None`` if ledger is empty.
    4. Call :func:`detect_shift`; if non-``None`` append to ledger (strict
       append-only via :func:`~almanac.observability.append_only_log.append_jsonl_safe`).
    5. Compute active shifts (``cooldown_until >= today``).
    6. Compute weight multiplier from active shifts.
    7. Write ``regime_shift_report.json`` atomically.
    8. Return :class:`RegimeShiftReport`.

    **Idempotency**: if ``current_regime == previous_regime`` no ledger row
    is appended and the existing report is overwritten with a fresh
    ``as_of`` timestamp but the same data.

    Parameters
    ----------
    scenario_state_path:
        Path to ``scenario_state.json``.
    shift_ledger_path:
        Path to ``regime_shift_ledger.jsonl`` (append-only history).
    report_path:
        Path to ``regime_shift_report.json`` (atomic daily snapshot).
    today:
        Calendar date for this run.  Injected for testability.
    cooldown_days:
        Length of the cooldown window in calendar days (default 30).

    Returns
    -------
    :class:`RegimeShiftReport`
    """
    from .append_only_log import append_jsonl_safe

    scenario_state_path = Path(scenario_state_path)
    shift_ledger_path = Path(shift_ledger_path)
    report_path = Path(report_path)

    # Step 1: derive current regime
    current_regime = _derive_current_regime(scenario_state_path)

    # Step 2: load full ledger history
    raw_rows = _load_jsonl(shift_ledger_path)
    historical_shifts: list[RegimeShift] = []
    for row in raw_rows:
        shift = _row_to_shift(row)
        if shift is not None:
            historical_shifts.append(shift)

    # Step 3: determine previous regime (last to_regime in ledger)
    previous_regime: str | None = None
    if historical_shifts:
        previous_regime = historical_shifts[-1].to_regime

    # Step 4: detect shift; if found, append to ledger
    new_shift: RegimeShift | None = None
    if current_regime is not None:
        new_shift = detect_shift(
            previous_regime,
            current_regime,
            today=today,
            cooldown_days=cooldown_days,
        )
    if new_shift is not None:
        append_jsonl_safe(
            shift_ledger_path,
            _shift_to_row(new_shift),
            fsync=True,
        )
        historical_shifts = historical_shifts + [new_shift]

    # Step 5: compute active shifts (cooldown still in effect)
    today_iso = today.isoformat()
    active_shifts = [
        s for s in historical_shifts if s.cooldown_until >= today_iso
    ]

    # Step 6: compute multiplier
    multiplier = compute_active_multiplier(historical_shifts, today)

    # Step 7: write atomic report
    as_of = datetime.now(timezone.utc).isoformat()
    report = RegimeShiftReport(
        as_of=as_of,
        current_regime=current_regime,
        active_shifts=active_shifts,
        historical_shifts=historical_shifts,
        weight_multiplier=multiplier,
    )
    _write_report_atomic(report_path, report)

    return report
