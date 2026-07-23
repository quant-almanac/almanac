"""Shared VIX classification helpers.

All producers should use these functions instead of hand-rolled thresholds so
``vix_state.json``, ``market_snapshot.json`` and API responses cannot disagree
for the same numeric VIX level.
"""

from __future__ import annotations

from math import inf

VIX_LEVELS: tuple[tuple[float, str], ...] = (
    (20.0, "CALM"),
    (30.0, "ELEVATED"),
    (40.0, "HIGH_FEAR"),
    (inf, "EXTREME"),
)

VIX_JA_LABELS = {
    "CALM": "CALM（落ち着き）",
    "ELEVATED": "ELEVATED（警戒）",
    "HIGH_FEAR": "HIGH_FEAR（高恐怖）",
    "EXTREME": "EXTREME_FEAR（パニック）",
    "UNKNOWN": "UNKNOWN（不明）",
}

VIX_MACRO_STATUS = {
    "CALM": "normal",
    "ELEVATED": "elevated",
    "HIGH_FEAR": "fear",
    "EXTREME": "capitulation",
    "UNKNOWN": "unknown",
}


def classify_vix(level: float | int | None) -> str:
    if level is None:
        return "UNKNOWN"
    try:
        value = float(level)
    except (TypeError, ValueError):
        return "UNKNOWN"
    for threshold, label in VIX_LEVELS:
        if value < threshold:
            return label
    return "EXTREME"


def format_vix_level_ja(level: float | int | None) -> str:
    return VIX_JA_LABELS[classify_vix(level)]


def vix_macro_status(level: float | int | None) -> str:
    return VIX_MACRO_STATUS[classify_vix(level)]
