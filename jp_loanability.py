"""Local, fail-safe JP short tradeability checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).parent


def _load(path: Path | str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def evaluate_short_tradeability(
    ticker: str,
    *,
    universe: dict[str, Any] | None = None,
    jsf_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    universe = universe or _load(BASE_DIR / "disclosure_universe_jp.json")
    jsf_state = jsf_state or _load(BASE_DIR / "data" / "jsf_lending_state.json")
    loanable = (universe.get("loanable_by_ticker") or {}).get(ticker)
    state = (jsf_state.get("tickers") or {}).get(ticker) or {}
    ratio = state.get("loan_ratio")
    reverse_daily_fee = bool(state.get("reverse_daily_fee"))
    reasons: list[str] = []
    if loanable is not True:
        reasons.append("loanable_not_confirmed")
    try:
        if ratio is not None and float(ratio) < 1.2:
            reasons.append("loan_ratio_below_1_2")
    except (TypeError, ValueError):
        reasons.append("invalid_loan_ratio")
    if reverse_daily_fee:
        reasons.append("reverse_daily_fee_active")
    return {
        "ticker": ticker,
        "loanable": loanable,
        "loan_ratio": ratio,
        "reverse_daily_fee": reverse_daily_fee,
        "untradeable": bool(reasons),
        "reasons": reasons,
    }
