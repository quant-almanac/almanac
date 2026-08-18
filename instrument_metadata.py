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
    "1321.T": 1,
}

# Explicit metadata is required before a scheduled broad route can use a new
# instrument.  This is separate from illustrative/public portfolio holdings.
BROAD_EXECUTION_ALLOWLIST: dict[str, dict[str, object]] = {
    "VT": {"asset_class": "etf", "listing_currency": "USD", "broad_family": "global_all_country", "trading_unit": 1, "price_provider": "market_data"},
    "VTI": {"asset_class": "etf", "listing_currency": "USD", "broad_family": "us_broad", "trading_unit": 1, "price_provider": "market_data"},
    "VOO": {"asset_class": "etf", "listing_currency": "USD", "broad_family": "us_broad", "trading_unit": 1, "price_provider": "market_data"},
    "SPY": {"asset_class": "etf", "listing_currency": "USD", "broad_family": "us_broad", "trading_unit": 1, "price_provider": "market_data"},
    "1306.T": {"asset_class": "etf", "listing_currency": "JPY", "broad_family": "japan_broad", "trading_unit": 10, "price_provider": "market_data"},
    "1321.T": {"asset_class": "etf", "listing_currency": "JPY", "broad_family": "japan_broad", "trading_unit": 1, "price_provider": "market_data"},
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


def broad_execution_metadata(value: object) -> dict[str, object] | None:
    row = BROAD_EXECUTION_ALLOWLIST.get(canonical_ticker(value))
    return dict(row) if isinstance(row, dict) else None


def validate_broad_execution_allowlist(*, price_available: set[str] | None = None) -> list[dict[str, object]]:
    """Return completeness issues; callers must fail closed on any issue."""
    issues: list[dict[str, object]] = []
    required = {"asset_class", "listing_currency", "broad_family", "trading_unit", "price_provider"}
    for ticker, row in sorted(BROAD_EXECUTION_ALLOWLIST.items()):
        missing = sorted(key for key in required if not row.get(key))
        if missing:
            issues.append({"ticker": ticker, "code": "metadata_missing", "fields": missing})
        elif ticker.endswith(".T") and trading_unit_for_ticker(ticker) != row["trading_unit"]:
            issues.append({"ticker": ticker, "code": "trading_unit_mismatch"})
        elif price_available is not None and ticker not in price_available:
            issues.append({"ticker": ticker, "code": "price_provider_unavailable"})
    return issues


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
