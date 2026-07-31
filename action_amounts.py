"""Canonical quantity/notional presentation for investment actions.

LLM prose may contain both a quantity and a preformatted yen estimate. Once a
policy or deterministic sizing rule changes the quantity, that embedded money
claim becomes stale. Persist quantity and numeric notional separately; create
human-readable money only at a response/rendering boundary.
"""
from __future__ import annotations

import math
import re
from typing import Optional


_QUANTITY_RE = re.compile(
    r"(?P<quantity>[\d,]+(?:\.\d+)?)\s*(?P<unit>株|口|shares?)",
    re.IGNORECASE,
)


def _quantity_parts(raw: object) -> Optional[tuple[float, str]]:
    match = _QUANTITY_RE.search(str(raw or ""))
    if not match:
        return None
    try:
        numeric = float(match.group("quantity").replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(numeric) or numeric < 0:
        return None
    unit = match.group("unit").lower()
    return numeric, ("shares" if unit.startswith("share") else unit)


def canonical_quantity_hint(raw: object) -> Optional[str]:
    """Return ``quantity + unit`` for a quantity-based hint, else ``None``."""
    match = _QUANTITY_RE.search(str(raw or ""))
    parts = _quantity_parts(raw)
    if not match or parts is None:
        return None
    numeric, unit = parts
    quantity_text = (
        f"{int(numeric):,}" if numeric.is_integer()
        else f"{numeric:,.8f}".rstrip("0").rstrip(".")
    )
    return f"{quantity_text} shares" if unit == "shares" else f"{quantity_text}{unit}"


def rewrite_action_quantity(
    action_text: object,
    *,
    old_hint: object,
    new_hint: object,
) -> tuple[str, str]:
    """Rewrite one unambiguous order-quantity token in human-readable prose."""
    text = str(action_text or "")
    old_parts = _quantity_parts(old_hint)
    new_quantity = canonical_quantity_hint(new_hint)
    if not text or old_parts is None or new_quantity is None:
        return text, "missing"
    old_numeric, old_unit = old_parts
    candidates = [
        match for match in _QUANTITY_RE.finditer(text)
        if _quantity_parts(match.group(0)) == (old_numeric, old_unit)
    ]
    if not candidates:
        return text, "missing"
    if len(candidates) != 1:
        return text, "ambiguous"
    match = candidates[0]
    return text[:match.start()] + new_quantity + text[match.end():], "rewritten"


def canonicalize_action_amount_hint(action: dict, *, in_place: bool = False) -> dict:
    """Strip embedded prose/money from quantity-based ``amount_hint``."""
    out = action if in_place else dict(action)
    old = out.get("amount_hint")
    canonical = canonical_quantity_hint(old)
    if canonical is None or canonical == old:
        return out
    out["amount_hint"] = canonical
    out.setdefault("amount_hint_original", old)
    out["amount_hint_semantics"] = "quantity_only"
    return out


def render_action_amount(action: dict) -> dict:
    """Build response-only quantity and money text from structured fields."""
    quantity = canonical_quantity_hint(action.get("amount_hint"))
    try:
        notional = float(action.get("estimated_notional_jpy"))
    except (TypeError, ValueError):
        notional = math.nan
    return {
        "quantity": quantity,
        "notional_jpy": round(notional) if math.isfinite(notional) else None,
        "display": (
            f"{quantity} / 約¥{notional:,.0f}"
            if quantity and math.isfinite(notional)
            else quantity
            or (f"約¥{notional:,.0f}" if math.isfinite(notional) else None)
        ),
    }
