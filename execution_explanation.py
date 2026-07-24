"""Deterministic cleanup for user-facing order explanations."""
from __future__ import annotations

import math
import re


_FORMULA_RE = re.compile(r"(?:\blimit\s*=|\bmin\s*\(|\bmax\s*\()", re.IGNORECASE)
_BID_VALUE_RE = re.compile(r"\bbid\s*[$¥￥]?\s*(\d+(?:\.\d+)?)", re.IGNORECASE)


def _number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _price_text(value: float, ticker: str) -> str:
    if ticker.upper().endswith(".T"):
        return f"¥{value:,.0f}"
    return f"${value:,.2f}"


def normalize_execution_explanation(action: dict) -> dict:
    """Make prose describe the persisted order instead of an LLM equation.

    The exact formula used by an LLM is not an executable contract and can
    contradict ``limit_price``.  We retain qualitative text but replace that
    pseudo-equation with the actual structured values.  Likewise, a number is
    called ``bid`` only when it matches the captured bid quote.
    """
    reason = str(action.get("execution_reason") or "").strip()
    if not reason:
        return action
    ticker = str(action.get("ticker") or "")
    limit_price = _number(action.get("limit_price"))
    decision_price = _number(action.get("decision_price"))
    original = reason

    formula_match = _FORMULA_RE.search(reason)
    if limit_price is not None and formula_match:
        sentence_start = reason.rfind("。", 0, formula_match.start()) + 1
        sentence_end = reason.find("。", formula_match.end())
        prefix = reason[:sentence_start]
        suffix = reason[sentence_end + 1:] if sentence_end >= 0 else ""
        contract = f"実注文は指値{_price_text(limit_price, ticker)}"
        if decision_price is not None:
            contract += f"（判断値{_price_text(decision_price, ticker)}）"
        reason = f"{prefix}{contract}。{suffix}"

    quote_bid = _number(action.get("quote_bid"))

    def replace_bid(match: re.Match[str]) -> str:
        stated = _number(match.group(1))
        if stated is None or (quote_bid is not None and math.isclose(stated, quote_bid, rel_tol=0, abs_tol=0.01)):
            return match.group(0)
        if decision_price is not None and math.isclose(stated, decision_price, rel_tol=0, abs_tol=0.01):
            return f"判断値{match.group(1)}"
        return f"参照値{match.group(1)}"

    reason = _BID_VALUE_RE.sub(replace_bid, reason)
    if reason == original:
        return action
    updated = dict(action)
    updated["execution_reason"] = reason
    updated["execution_reason_normalized"] = True
    updated["execution_reason_original"] = original
    return updated
