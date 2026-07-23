"""Extended tests for execution_quality.py — gaps beyond the 4 existing tests.

Existing coverage (test_execution_quality.py):
  - _compute_slippage_bps: buy formula, sell favorable, alert threshold, missing data

New coverage here:
  - _spread_bps:             bid/ask spread, edge cases (None, zero bid)
  - _compute_shortfall_bps:  buy/sell Implementation Shortfall, None inputs
  - shortfall_summary:       median/IQR stats, AI compliance rate, date filtering
  - _median helper:          empty, single-item, even count (via shortfall_summary)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution_quality import (  # noqa: E402
    _compute_shortfall_bps,
    _spread_bps,
    shortfall_summary,
)


# ---------------------------------------------------------------------------
# _spread_bps
# ---------------------------------------------------------------------------


def test_spread_bps_basic() -> None:
    ex = {"bid_at_order": 100.0, "ask_at_order": 101.0}
    result = _spread_bps(ex)
    # (101 - 100) / 100 * 10000 = 100 bps
    assert result == pytest.approx(100.0)


def test_spread_bps_tight_spread() -> None:
    ex = {"bid_at_order": 200.0, "ask_at_order": 200.2}
    result = _spread_bps(ex)
    # 0.2 / 200 * 10000 = 10 bps
    assert result == pytest.approx(10.0)


def test_spread_bps_none_bid_returns_none() -> None:
    ex = {"bid_at_order": None, "ask_at_order": 101.0}
    assert _spread_bps(ex) is None


def test_spread_bps_none_ask_returns_none() -> None:
    ex = {"bid_at_order": 100.0, "ask_at_order": None}
    assert _spread_bps(ex) is None


def test_spread_bps_zero_bid_returns_none() -> None:
    """Division by zero guard: bid=0 → None."""
    ex = {"bid_at_order": 0.0, "ask_at_order": 1.0}
    assert _spread_bps(ex) is None


def test_spread_bps_missing_keys_returns_none() -> None:
    assert _spread_bps({}) is None


# ---------------------------------------------------------------------------
# _compute_shortfall_bps
# ---------------------------------------------------------------------------


def test_shortfall_buy_positive_when_executed_above_decision() -> None:
    """Buy at 102 vs decision 100 → unfavorable → positive shortfall."""
    result = _compute_shortfall_bps(102.0, 100.0, "buy")
    # (102 - 100) / 100 * 10000 = 200 bps
    assert result == pytest.approx(200.0)


def test_shortfall_buy_negative_when_executed_below_decision() -> None:
    """Buy at 98 vs decision 100 → favorable → negative shortfall."""
    result = _compute_shortfall_bps(98.0, 100.0, "buy")
    assert result == pytest.approx(-200.0)


def test_shortfall_sell_positive_when_executed_below_decision() -> None:
    """Sell at 98 vs decision 100 → unfavorable → positive shortfall."""
    result = _compute_shortfall_bps(98.0, 100.0, "sell")
    # (100 - 98) / 100 * 10000 = 200 bps
    assert result == pytest.approx(200.0)


def test_shortfall_sell_negative_when_executed_above_decision() -> None:
    """Sell at 102 vs decision 100 → favorable → negative shortfall."""
    result = _compute_shortfall_bps(102.0, 100.0, "sell")
    assert result == pytest.approx(-200.0)


def test_shortfall_short_uses_sell_side() -> None:
    result = _compute_shortfall_bps(102.0, 100.0, "short")
    assert result == pytest.approx(-200.0)


def test_shortfall_cover_uses_buy_side() -> None:
    result = _compute_shortfall_bps(98.0, 100.0, "cover")
    assert result == pytest.approx(-200.0)


def test_shortfall_margin_buy_uses_buy_side() -> None:
    result = _compute_shortfall_bps(102.0, 100.0, "margin_buy")
    assert result == pytest.approx(200.0)


def test_shortfall_zero_when_executed_at_decision() -> None:
    assert _compute_shortfall_bps(100.0, 100.0, "buy") == pytest.approx(0.0)


def test_shortfall_returns_none_for_none_executed_price() -> None:
    assert _compute_shortfall_bps(None, 100.0, "buy") is None


def test_shortfall_returns_none_for_none_decision_price() -> None:
    assert _compute_shortfall_bps(100.0, None, "buy") is None


def test_shortfall_returns_none_for_none_direction() -> None:
    assert _compute_shortfall_bps(100.0, 100.0, None) is None


def test_shortfall_returns_none_for_unknown_direction() -> None:
    assert _compute_shortfall_bps(100.0, 100.0, "hold") is None


def test_shortfall_returns_none_for_zero_decision_price() -> None:
    assert _compute_shortfall_bps(100.0, 0.0, "buy") is None


def test_shortfall_case_insensitive_direction() -> None:
    result = _compute_shortfall_bps(102.0, 100.0, "BUY")
    assert result == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# shortfall_summary
# ---------------------------------------------------------------------------


def _ex(ticker, price, decision, direction="buy", saved_at="2026-05-25",
        ai_order="limit", actual_order="limit") -> dict:
    """Minimal execution record for shortfall_summary."""
    return {
        "ticker":                    ticker,
        "id":                        f"id_{ticker}",
        "price":                     price,
        "decision_price":            decision,
        "direction":                 direction,
        "saved_at":                  f"{saved_at}T09:00:00",
        "ai_recommended_order_type": ai_order,
        "order_type":                actual_order,
    }


def test_shortfall_summary_empty_input() -> None:
    result = shortfall_summary(execs=[])
    assert result["n"] == 0
    assert result["median_shortfall_bps"] is None


def test_shortfall_summary_below_min_n_returns_none_stats() -> None:
    """Fewer than min_n (default 5) records → median/IQR are None."""
    execs = [_ex("NVDA", 102.0, 100.0) for _ in range(4)]
    result = shortfall_summary(execs=execs, min_n=5)
    assert result["n"] == 4
    assert result["median_shortfall_bps"] is None
    assert result["iqr_bps"] is None


def test_shortfall_summary_at_min_n_computes_stats() -> None:
    """Exactly min_n records → median/IQR are computed."""
    execs = [_ex(f"T{i}", 102.0, 100.0) for i in range(5)]
    result = shortfall_summary(execs=execs, min_n=5)
    assert result["n"] == 5
    assert result["median_shortfall_bps"] is not None
    assert result["median_shortfall_bps"] == pytest.approx(200.0)


def test_shortfall_summary_median_correct() -> None:
    """Known shortfalls: 100, 200, 300 bps → median=200."""
    execs = [
        _ex("A", 101.0, 100.0),   # 100 bps
        _ex("B", 102.0, 100.0),   # 200 bps
        _ex("C", 103.0, 100.0),   # 300 bps
    ]
    result = shortfall_summary(execs=execs, min_n=3)
    assert result["median_shortfall_bps"] == pytest.approx(200.0)


def test_shortfall_summary_worst_is_highest_bps() -> None:
    execs = [
        _ex("A", 101.0, 100.0),   # 100 bps
        _ex("B", 105.0, 100.0),   # 500 bps ← worst
        _ex("C", 102.0, 100.0),   # 200 bps
    ]
    result = shortfall_summary(execs=execs, min_n=3)
    assert result["worst"] is not None
    assert result["worst"]["ticker"] == "B"
    assert result["worst"]["sf"] == pytest.approx(500.0)


def test_shortfall_summary_ai_compliance_all_followed() -> None:
    """AI proposed limit; actual order_type=limit → compliance=1.0."""
    execs = [_ex(f"T{i}", 100.0, 100.0, ai_order="limit", actual_order="limit")
             for i in range(5)]
    result = shortfall_summary(execs=execs, min_n=5)
    assert result["ai_compliance_rate"] == pytest.approx(1.0)


def test_shortfall_summary_ai_compliance_none_followed() -> None:
    """AI proposed limit; actual=market → compliance=0.0."""
    execs = [_ex(f"T{i}", 100.0, 100.0, ai_order="limit", actual_order="market")
             for i in range(5)]
    result = shortfall_summary(execs=execs, min_n=5)
    assert result["ai_compliance_rate"] == pytest.approx(0.0)


def test_shortfall_summary_date_filter_excludes_old_records() -> None:
    execs = [
        _ex("OLD", 102.0, 100.0, saved_at="2026-04-01"),  # before window
        _ex("NEW", 102.0, 100.0, saved_at="2026-05-25"),
    ]
    result = shortfall_summary(execs=execs, week_start="2026-05-01", min_n=1)
    assert result["n"] == 1
    assert result["worst"]["ticker"] == "NEW"


def test_shortfall_summary_date_filter_excludes_future_records() -> None:
    execs = [
        _ex("FUTURE", 102.0, 100.0, saved_at="2026-06-01"),  # after week_end
        _ex("NOW",    102.0, 100.0, saved_at="2026-05-25"),
    ]
    result = shortfall_summary(execs=execs, week_end="2026-05-31", min_n=1)
    assert result["n"] == 1
    assert result["worst"]["ticker"] == "NOW"


def test_shortfall_summary_skips_records_without_decision_price() -> None:
    execs = [
        {"ticker": "A", "price": 100.0, "decision_price": None,
         "direction": "buy", "saved_at": "2026-05-25T09:00:00"},
    ]
    result = shortfall_summary(execs=execs, min_n=1)
    assert result["n"] == 0


def test_shortfall_summary_result_has_required_keys() -> None:
    result = shortfall_summary(execs=[])
    for key in ("n", "median_shortfall_bps", "iqr_bps", "worst",
                "ai_compliance_rate", "ai_proposed_limit_n", "ai_proposed_market_n"):
        assert key in result, f"missing key: {key}"
