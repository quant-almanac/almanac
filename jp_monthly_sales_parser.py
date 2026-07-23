"""Deterministic monthly-sales YoY parser for Japanese disclosures."""

from __future__ import annotations

import re
import unicodedata
from typing import Optional

PARSER_VERSION = "jp-monthly-sales-1.0"


def parse_monthly_yoy_pct(text: str) -> Optional[float]:
    """Return monthly YoY change as a ratio (12.3% growth -> ``0.123``)."""

    normalized = unicodedata.normalize("NFKC", text or "").replace("％", "%")
    patterns = (
        r"(?:前年同月比|前年比|既存店前年比)[^0-9+\-△▲]{0,20}([+\-△▲]?\d+(?:\.\d+)?)\s*%",
        r"([+\-]?\d+(?:\.\d+)?)\s*%\s*(?:増|減)[^。\n]{0,20}(?:前年同月|前年比)",
    )
    for pattern in patterns:
        match = re.search(pattern, normalized)
        if not match:
            continue
        raw = match.group(1)
        negative = raw.startswith(("-", "△", "▲")) or "減" in match.group(0)
        try:
            value = float(raw.lstrip("+-△▲"))
        except ValueError:
            continue
        context = normalized[match.start():match.end() + 2]
        negative = negative or "減" in context
        if raw.startswith(("+", "-", "△", "▲")) or "増" in context or "減" in context:
            return (-value if negative else value) / 100.0
        # No sign / 増減 word. A value >= 50 is an index ("前年同月比 112.3%" = +12.3%);
        # a small bare percentage ("前年比 8.5%") is a delta, conventionally positive.
        # Without this split, "前年比 8.5%" would be mis-read as an index = -91.5%.
        if value >= 50.0:
            return value / 100.0 - 1.0
        return value / 100.0
    return None
