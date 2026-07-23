"""Registry helpers for signal-producing and measurement-only lanes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

VALID_STATUSES = {"measured", "display_only", "retired"}
REQUIRED_LANES = {
    "sonnet_long",
    "sonnet_medium",
    "sonnet_swing",
    "sonnet_short",
    "opus_final",
    "screener_short",
    "screener_short_overheat",
    "screener_short_event",
    "screener_short_bear",
    "screener_margin_long",
    "screener_pair",
    "screener_squeeze",
    "disclosure_deterministic",
    "revision_tracker",
    "scenario_monitor",
    "proxy_mapper",
    "ipo_watch",
    "news_topic",
    "social_topic",
    "geopolitical",
    "jp_dilution",
    "jp_going_concern",
    "signal_tracker",
}


def load_lane_registry(path: Path | str) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    lanes = raw.get("lanes") if isinstance(raw, dict) else None
    if not isinstance(lanes, list):
        raise ValueError("lane_registry.json must contain a lanes list")
    return [lane for lane in lanes if isinstance(lane, dict)]


def validate_lane_registry(path: Path | str) -> list[str]:
    errors: list[str] = []
    lanes = load_lane_registry(path)
    names = {str(lane.get("name") or "") for lane in lanes}
    for missing in sorted(REQUIRED_LANES - names):
        errors.append(f"missing lane: {missing}")
    for lane in lanes:
        name = str(lane.get("name") or "")
        if lane.get("status") not in VALID_STATUSES:
            errors.append(f"{name}: invalid status")
        if not lane.get("measurement_path"):
            errors.append(f"{name}: missing measurement_path")
        if "final_outcome_date" not in lane:
            errors.append(f"{name}: missing final_outcome_date")
    return errors
