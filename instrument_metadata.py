"""Deterministic ticker aliases and exchange trading-unit metadata.

Only facts that are stable and required by execution sizing belong here.  In
particular, JPX ETFs do not share the 100-share unit used by ordinary Japanese
stocks.  The explicit overrides below are the instruments held by this
portfolio and are sourced from their issuer product pages.
"""
from __future__ import annotations

import re


_BARE_JPX_CODE = re.compile(r"^\d{4}$")
_JPX_CODE = re.compile(r"^[0-9A-Z]{4}$")
_AMBIGUOUS_BARE_ALPHANUMERIC = re.compile(r"^\d{3}[A-Z]$")
JPX_ALPHANUMERIC_CODES = {"285A"}

# JPX trading units (口).  Ordinary Japanese shares fall back to 100 shares.
# 1489 NEXT FUNDS Nikkei 225 High Dividend Yield Stock 50: 1 unit.
# 1306 NEXT FUNDS TOPIX: 10 units (after the 2026-04-01 split).
JPX_TRADING_UNITS: dict[str, int] = {
    "1489.T": 1,
    "1306.T": 10,
}


def canonical_ticker(value: object) -> str:
    """Return the canonical symbol used by local plan and execution records."""
    text = str(value or "").strip().upper()
    if _BARE_JPX_CODE.fullmatch(text) or text in JPX_ALPHANUMERIC_CODES:
        return f"{text}.T"
    for suffix in (".JPX", ".JP"):
        if text.endswith(suffix) and _JPX_CODE.fullmatch(text[: -len(suffix)]):
            return f"{text[: -len(suffix)]}.T"
    return text


def canonical_execution_ticker(value: object) -> str:
    """Canonicalize known JPX codes and reject unresolved bare JPX-like symbols."""
    raw = str(value or "").strip().upper()
    ticker = canonical_ticker(raw)
    if ticker == raw and _AMBIGUOUS_BARE_ALPHANUMERIC.fullmatch(raw):
        raise ValueError(
            f"{raw} はJPXコードか判定できません。.T/.JPXを付けるか銘柄マスターへ登録してください"
        )
    return ticker


def trading_unit_for_ticker(value: object) -> int:
    """Return the minimum regular-market quantity for a ticker."""
    ticker = canonical_ticker(value)
    if ticker in JPX_TRADING_UNITS:
        return JPX_TRADING_UNITS[ticker]
    if ticker.endswith(".T"):
        return 100
    return 1


def quantity_label_for_ticker(value: object) -> str:
    """Use 口 for known JPX ETFs and 株 for ordinary listed shares."""
    return "口" if canonical_ticker(value) in JPX_TRADING_UNITS else "株"


def jp_trading_unit_prompt() -> str:
    """Compact, deterministic sizing context for the final synthesis model."""
    rows = ", ".join(
        f"{ticker}={unit}口" for ticker, unit in sorted(JPX_TRADING_UNITS.items())
    )
    return (
        f"JPX ETF売買単位（公式商品仕様）: {rows}。"
        "これらを通常の日本株100株単元へ丸めてはならない。"
    )
