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
_YEN_CLAIM_RE = re.compile(
    r"(?P<whole>"
    r"(?:(?:約\s*)?[¥￥]\s*(?:約\s*)?)(?P<yen_number>[\d,.]+)"
    r"\s*(?P<yen_unit>万円|万|千円|円|[Kk])?(?:\s*相当)?"
    r"|"
    r"(?:約\s*)(?P<plain_number>[\d,.]+)\s*"
    r"(?P<plain_unit>万円|万|千円|円|[Kk])(?:\s*相当)?"
    r")"
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


def _yen_claim_value(match: re.Match[str]) -> float | None:
    raw = match.group("yen_number") or match.group("plain_number")
    unit = match.group("yen_unit") or match.group("plain_unit") or ""
    try:
        value = float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if unit in {"万円", "万"}:
        value *= 10_000
    elif unit == "千円" or unit.lower() == "k":
        value *= 1_000
    return value if math.isfinite(value) else None


def _rewrite_matching_notional_claims(
    text: object,
    *,
    old_notional_jpy: float | None,
    new_notional_jpy: float | None,
) -> tuple[str, int]:
    """Rewrite only yen claims that agree with the pre-resize notional."""
    raw = str(text or "")
    if (
        old_notional_jpy is None
        or new_notional_jpy is None
        or not math.isfinite(old_notional_jpy)
        or not math.isfinite(new_notional_jpy)
        or old_notional_jpy < 0
        or new_notional_jpy < 0
    ):
        return raw, 0
    tolerance = max(2_000.0, abs(old_notional_jpy) * 0.10)
    replacements = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal replacements
        claimed = _yen_claim_value(match)
        if claimed is None or abs(claimed - old_notional_jpy) > tolerance:
            return match.group(0)
        replacements += 1
        return f"約¥{new_notional_jpy:,.0f}"

    return _YEN_CLAIM_RE.sub(_replace, raw), replacements


def synchronize_resized_action_prose(
    action: dict,
    *,
    old_hint: object,
    new_hint: object,
    old_notional_jpy: float | None,
    new_notional_jpy: float | None,
) -> tuple[dict, dict[str, dict[str, str]]]:
    """Synchronize numeric prose after deterministic policy resizing.

    Structured ``amount_hint`` and ``estimated_notional_jpy`` remain the
    authority. Exact, unambiguous references to the pre-resize quantity and
    notional in ``action``/``reason`` are updated. Ambiguous quantity prose is
    left untouched and marked for readiness review rather than guessed.
    """
    out = dict(action)
    applied: dict[str, dict[str, str]] = {}
    statuses: dict[str, str] = {}
    money_rewrites = 0

    for field in ("action", "reason"):
        original = str(out.get(field) or "")
        if not original:
            if field == "action":
                statuses[field] = "missing"
                out["action_quantity_sync_failed"] = True
                out["prose_numeric_sync_failed"] = True
            continue
        quantity_text, quantity_status = rewrite_action_quantity(
            original,
            old_hint=old_hint,
            new_hint=new_hint,
        )
        statuses[field] = quantity_status
        new_quantity = canonical_quantity_hint(new_hint)
        if quantity_status == "ambiguous" or (
            field == "action"
            and quantity_status == "missing"
            and (not new_quantity or new_quantity not in original)
        ):
            out["action_quantity_sync_failed"] = True
            out["prose_numeric_sync_failed"] = True
        money_text, rewritten_money = _rewrite_matching_notional_claims(
            quantity_text,
            old_notional_jpy=old_notional_jpy,
            new_notional_jpy=new_notional_jpy,
        )
        money_rewrites += rewritten_money
        if money_text != original:
            applied[field] = {"from": original, "to": money_text}
            out[f"policy_resize_{field}_original"] = original
            out[field] = money_text

    if statuses:
        out["action_quantity_sync_status"] = statuses.get("action", "missing")
        out["reason_quantity_sync_status"] = statuses.get("reason", "missing")
    if applied:
        note = (
            f"policy縮小後の権威値: {canonical_quantity_hint(new_hint) or new_hint}"
            + (
                f" / 約¥{new_notional_jpy:,.0f}"
                if new_notional_jpy is not None and math.isfinite(new_notional_jpy)
                else ""
            )
        )
        reason = str(out.get("reason") or "").strip()
        if note not in reason:
            out["reason"] = f"{reason} / {note}" if reason else note
        out["prose_numeric_sync_status"] = "rewritten"
        out["prose_notional_claims_rewritten"] = money_rewrites
    elif out.get("prose_numeric_sync_failed"):
        out["prose_numeric_sync_status"] = "ambiguous"
    else:
        out["prose_numeric_sync_status"] = "not_present"
    return out, applied


def _finite_notional(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def synchronize_persisted_resized_action_prose(action: dict) -> dict:
    """Repair display prose in a previously persisted policy-resized action.

    Older artifacts already contain authoritative before/after values in
    ``policy_size_applied`` even when their free text is stale. Reusing those
    audit fields permits a read-only response overlay without rewriting the
    historical analysis file.
    """
    if not isinstance(action, dict):
        return action
    applied = action.get("policy_size_applied")
    if not isinstance(applied, dict):
        return dict(action)
    hint_change = applied.get("amount_hint")
    if not isinstance(hint_change, dict) or "from" not in hint_change:
        return dict(action)
    notional_change = applied.get("estimated_notional_jpy")
    if not isinstance(notional_change, dict):
        notional_change = applied.get("notional_jpy")
    old_notional = (
        notional_change.get("from") if isinstance(notional_change, dict) else None
    )
    new_notional = action.get("estimated_notional_jpy")
    if new_notional is None and isinstance(notional_change, dict):
        new_notional = notional_change.get("to")
    repaired, _ = synchronize_resized_action_prose(
        action,
        old_hint=hint_change.get("from"),
        new_hint=action.get("amount_hint") or hint_change.get("to"),
        old_notional_jpy=_finite_notional(old_notional),
        new_notional_jpy=_finite_notional(new_notional),
    )
    if repaired != action:
        repaired["display_prose_repaired_from_policy_audit"] = True
    return repaired


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
