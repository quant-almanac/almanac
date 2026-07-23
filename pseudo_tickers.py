"""Helpers for local portfolio identifiers that are not market-data tickers."""

from __future__ import annotations


PSEUDO_MARKET_TICKERS = frozenset({
    "SLIM_SP500",
    "SLIM_ORCAN",
    "MNXACT",
    "IFREE_FANGPLUS",
    "NOMURA_SEMI",
    "GS_MMF_USD",
    "CASH_JPY",
    "CASH_USD",
    "CASH_JPY_SBI",
    "CASH_JPY_SBI_WIFE",
    "WIFE_NISA_GROWTH",
    "WIFE_NISA_TSUMITATE",
    "AVGO_TOKU",
    "AVGO_IPPAN",
    "AVGO_特定",
    "AVGO_一般",
})


NON_EARNINGS_TICKERS = frozenset({
    *PSEUDO_MARKET_TICKERS,
    # ETFs / funds with valid prices but no issuer earnings calendar.
    "GLD",
    "IEV",
    "LIT",
    "ROBO",
    "XLF",
    "XLE",
    "XLI",
    "XLK",
    "XLP",
    "XLU",
    "XLV",
    "EWG",
    "EWJ",
    "EPOL",
    "EEM",
    "FXI",
    "ITA",
    "SMH",
    "SOXX",
    "SOXL",
    "SPY",
    "QQQ",
    "TQQQ",
    "TLT",
    "IWM",
    "VNQ",
})


def is_pseudo_market_ticker(ticker: str | None) -> bool:
    """Return True for internal identifiers that should not reach yfinance."""
    t = str(ticker or "").strip().upper()
    if not t:
        return False
    if t in PSEUDO_MARKET_TICKERS:
        return True
    if t.startswith(("CASH_", "WIFE_NISA")):
        return True
    if t.endswith("_WIFE"):
        return True
    if "MMF" in t:
        return True
    return False


def is_non_earnings_ticker(ticker: str | None) -> bool:
    """Return True for tickers that should not be sent to earnings APIs."""
    t = str(ticker or "").strip().upper()
    if not t:
        return False
    return t in NON_EARNINGS_TICKERS or is_pseudo_market_ticker(t)
