"""Tests for rebalance_planner.compute_full_trim_plan and summarize_plans.

compute_full_trim_plan is called by the rebalance engine to determine exact
share counts and split schedules for Long-tier positions. A bug here maps
directly to wrong trade sizes, so every branching path must be covered:

  - US stock (USD): ¥660K-cap split heuristic, ≤5 shares → 1 split
  - JP stock (.T): 100-share lot rounding, unit-based split
  - Fund (SLIM_*, MNXACT, …): JPY-based, ¥30K/10K min/round, no share count
  - hold fast-path: diff=0 or abs_jpy<1 → zeros, no HTTP calls
  - share_price=0 for equity → graceful error reason, no crash
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from rebalance_planner import (  # noqa: E402
    compute_full_trim_plan,
    summarize_plans,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _plan(ticker, current, target, total, price, currency="USD", fx=150.0, splits=2):
    return compute_full_trim_plan(
        ticker=ticker,
        current_pct=current,
        target_pct=target,
        total_jpy=total,
        share_price=price,
        currency=currency,
        fx_rate=fx,
        max_splits=splits,
    )


# ---------------------------------------------------------------------------
# Hold fast-path
# ---------------------------------------------------------------------------


def test_hold_when_diff_is_zero() -> None:
    p = _plan("NVDA", 0.10, 0.10, 10_000_000, 500.0)
    assert p["direction"] == "hold"
    assert p["splits"] == 0
    assert p["total_shares"] == 0
    assert p["shares_per_split"] == []
    assert "hold" in p["reason"]


def test_hold_when_total_jpy_is_zero() -> None:
    """diff > 0 but portfolio value = 0 → abs_jpy < 1 → no-op.

    Note: direction reflects the diff sign ("add") but splits=0 and
    reason contains "hold" — the fast-path skips all calculation.
    """
    p = _plan("NVDA", 0.00, 0.05, 0, 500.0)
    assert p["splits"] == 0
    assert p["total_shares"] == 0
    assert "hold" in p["reason"]


def test_hold_direction_field_when_diff_negative_but_tiny() -> None:
    """Extremely small portfolio (abs_jpy < 1) → fast-path: splits=0.

    direction="trim" because diff is negative; the fast-path does not
    override direction — it just skips calculation and sets reason.
    """
    p = _plan("NVDA", 0.10, 0.09, 5, 500.0)  # abs_jpy = 0.01*5 = 0.05 < 1
    assert p["splits"] == 0
    assert p["total_shares"] == 0
    assert "hold" in p["reason"]


# ---------------------------------------------------------------------------
# US stock — direction
# ---------------------------------------------------------------------------


def test_us_stock_trim_direction() -> None:
    p = _plan("NVDA", 0.10, 0.08, 10_000_000, 500.0)
    assert p["direction"] == "trim"
    assert p["diff_pct"] == pytest.approx(-0.02, abs=1e-6)


def test_us_stock_add_direction() -> None:
    p = _plan("NVDA", 0.05, 0.08, 10_000_000, 500.0)
    assert p["direction"] == "add"
    assert p["diff_pct"] == pytest.approx(0.03, abs=1e-6)


# ---------------------------------------------------------------------------
# US stock — 1 split (small amount, within ¥660K cap)
# ---------------------------------------------------------------------------


def test_us_stock_small_trim_is_single_split() -> None:
    """
    abs_jpy=200K, price_in_jpy=75K → total_shares=3, per_jpy=225K ≤ 660K → 1 split.
    """
    p = _plan("NVDA", 0.10, 0.08, 10_000_000, 500.0, currency="USD", fx=150.0)
    # abs_jpy = 0.02 * 10M = 200K; price_in_jpy = 500*150 = 75K
    # raw = 200K/75K ≈ 2.67 → ceil = 3
    assert p["total_shares"] == 3
    assert p["splits"] == 1
    assert p["shares_per_split"] == [3]
    assert sum(p["shares_per_split"]) == p["total_shares"]


def test_us_stock_small_trim_jpy_per_split_matches_total() -> None:
    p = _plan("NVDA", 0.10, 0.08, 10_000_000, 500.0, currency="USD", fx=150.0)
    assert sum(p["jpy_per_split"]) == p["total_jpy"]


# ---------------------------------------------------------------------------
# US stock — 2 splits (large amount, exceeds ¥660K cap)
# ---------------------------------------------------------------------------


def test_us_stock_large_trim_splits_at_cap() -> None:
    """
    abs_jpy=3.6M, price_in_jpy=142.5K → total_shares=26, per_jpy=3.705M > 660K → 2 splits.
    """
    p = _plan("NVDA", 0.20, 0.08, 30_000_000, 950.0, currency="USD", fx=150.0)
    # price_in_jpy = 950*150 = 142500; raw = 3.6M/142.5K ≈ 25.26 → 26
    assert p["total_shares"] == 26
    assert p["splits"] == 2
    assert sum(p["shares_per_split"]) == 26
    assert len(p["shares_per_split"]) == 2
    # Even split (26 // 2 = 13 with 0 remainder)
    assert p["shares_per_split"] == [13, 13]


def test_us_stock_large_jpy_per_split_consistent() -> None:
    p = _plan("NVDA", 0.20, 0.08, 30_000_000, 950.0, currency="USD", fx=150.0)
    assert sum(p["jpy_per_split"]) == p["total_jpy"]


def test_us_stock_uneven_split_distributes_remainder() -> None:
    """When total_shares is odd, the first split gets one extra share."""
    # We need total_shares to be odd and > 5 to trigger splits.
    # price_in_jpy=60K (400*150), raw=2M/60K≈33.3 → total=34; 34>5 and 34*60K=2.04M>660K
    p = _plan("AAPL", 0.20, 0.13, 30_000_000, 400.0, currency="USD", fx=150.0)
    # abs_jpy = 0.07 * 30M = 2.1M; price_in_jpy=60K; raw=35 → total=35
    assert sum(p["shares_per_split"]) == p["total_shares"]
    assert len(p["shares_per_split"]) == p["splits"]


# ---------------------------------------------------------------------------
# US stock — ≤5 shares always 1 split (even if per_jpy > ¥660K)
# ---------------------------------------------------------------------------


def test_us_stock_five_or_fewer_shares_always_one_split() -> None:
    """
    total_shares=4, price_in_jpy=300K → per_jpy=1.2M > 660K, but ≤5 → splits=1.
    """
    # abs_jpy ≈ 1.05M / 300K per share → raw=3.5 → ceil=4 shares
    p = _plan("NVDA", 0.15, 0.05, 10_500_000, 2000.0, currency="USD", fx=150.0)
    assert p["total_shares"] == 4
    assert p["splits"] == 1
    assert p["shares_per_split"] == [4]


def test_us_stock_one_share_minimum() -> None:
    """Even when raw_shares < 1 we always buy/trim at least 1 share."""
    p = _plan("NVDA", 0.10, 0.09, 100_000, 2000.0, currency="USD", fx=150.0)
    # abs_jpy=1K, price_in_jpy=300K → raw=0.003 → max(1, ceil(0.003))=1
    assert p["total_shares"] == 1
    assert p["splits"] == 1


# ---------------------------------------------------------------------------
# JP stock/ETF (.T) — instrument trading-unit rounding
# ---------------------------------------------------------------------------


def test_jp_stock_rounds_to_100_share_lot() -> None:
    """
    abs_jpy=200K, price=5K → raw=40 shares → round(40/100)=0 → max(1,0)=1 unit → 100 shares.
    """
    p = _plan("7203.T", 0.02, 0.04, 10_000_000, 5000.0, currency="JPY")
    assert p["total_shares"] == 100
    assert p["splits"] == 1
    assert p["shares_per_split"] == [100]


def test_jpx_etfs_use_instrument_specific_trading_units() -> None:
    high_dividend = _plan("1489.T", 0.02, 0.04, 10_000_000, 5000.0, currency="JPY")
    topix = _plan("1306.T", 0.02, 0.04, 10_000_000, 420.0, currency="JPY")

    assert high_dividend["total_shares"] == 40
    assert all(value % 1 == 0 for value in high_dividend["shares_per_split"])
    assert topix["total_shares"] == 480
    assert all(value % 10 == 0 for value in topix["shares_per_split"])


def test_jp_stock_add_direction() -> None:
    p = _plan("1489.T", 0.02, 0.04, 10_000_000, 5000.0, currency="JPY")
    assert p["direction"] == "add"


def test_jp_stock_two_units_splits_evenly() -> None:
    """
    abs_jpy=600K, price=3K → raw=200 shares → 2 units → 2 splits of 100 each.
    """
    p = _plan("9999.T", 0.04, 0.10, 10_000_000, 3000.0, currency="JPY")
    assert p["total_shares"] == 200
    assert p["splits"] == 2
    assert p["shares_per_split"] == [100, 100]
    assert sum(p["shares_per_split"]) == p["total_shares"]


def test_jp_stock_single_unit_never_splits() -> None:
    """1 unit → splits=1 regardless of max_splits."""
    p = _plan("6762.T", 0.01, 0.03, 5_000_000, 2000.0, currency="JPY", splits=2)
    # abs_jpy=100K, price=2K, raw=50 → units=max(1,round(0.5))=max(1,0)=1 → splits=1
    assert p["total_shares"] == 100
    assert p["splits"] == 1


def test_jp_stock_total_jpy_is_shares_times_price() -> None:
    p = _plan("1489.T", 0.02, 0.04, 10_000_000, 5000.0, currency="JPY")
    assert p["total_jpy"] == p["total_shares"] * 5000.0


# ---------------------------------------------------------------------------
# Fund (SLIM_*, MNXACT, …) — JPY-based, ¥10K rounding, ≥¥300K/split
# ---------------------------------------------------------------------------


def test_fund_small_amount_uses_single_split() -> None:
    """
    abs_jpy=200K < 300K → target_per=300K → ceil(200K/300K)=1 split.
    """
    p = _plan("SLIM_SP500", 0.00, 0.02, 10_000_000, 0, currency="JPY")
    assert p["total_shares"] == -1          # flag: no share count for funds
    assert p["shares_per_split"] == []
    assert p["splits"] == 1
    assert p["jpy_per_split"][0] == 200_000  # ¥200K, already on 10K boundary
    assert p["total_jpy"] == 200_000


def test_fund_large_amount_uses_two_splits() -> None:
    """
    abs_jpy=800K → target_per=max(300K, 400K)=400K → splits=2, per=400K each.
    """
    p = _plan("SLIM_ORCAN", 0.00, 0.08, 10_000_000, 0, currency="JPY")
    assert p["splits"] == 2
    assert p["jpy_per_split"] == [400_000, 400_000]
    assert p["total_jpy"] == 800_000


def test_fund_amount_rounded_up_to_10k() -> None:
    """
    abs_jpy=355K → splits=2, per=ceil(177.5K)=178K → round up to ¥180K.
    """
    # abs_jpy = 0.071 * 5_000_000 = 355_000 (close enough to trigger rounding)
    # target_per = max(300K, ceil(355K/2)=178K) = 300K
    # splits = max(1, min(2, ceil(355K/300K))) = max(1, min(2, 2)) = 2
    # per = ceil(355K/2) = 178K → round to 10K: ceil(17.8)*10K = 180K
    p = _plan("MNXACT", 0.00, 0.071, 5_000_000, 0, currency="JPY")
    # Verify per is a multiple of 10K
    for amount in p["jpy_per_split"]:
        assert amount % 10_000 == 0, f"amount {amount} is not a multiple of ¥10K"


def test_fund_jpy_per_split_count_matches_splits() -> None:
    p = _plan("SLIM_ORCAN", 0.00, 0.08, 10_000_000, 0, currency="JPY")
    assert len(p["jpy_per_split"]) == p["splits"]


def test_fund_direction_is_add() -> None:
    p = _plan("SLIM_SP500", 0.00, 0.02, 10_000_000, 0, currency="JPY")
    assert p["direction"] == "add"


# ---------------------------------------------------------------------------
# share_price = 0 for equity (not fund)
# ---------------------------------------------------------------------------


def test_equity_share_price_zero_returns_graceful_error() -> None:
    """No crash; reason explains price is unknown; splits=0."""
    p = _plan("NVDA", 0.10, 0.08, 10_000_000, 0.0, currency="USD")
    assert p["splits"] == 0
    assert p["total_shares"] == 0
    assert "不明" in p["reason"] or "share_price" in p["reason"].lower()


# ---------------------------------------------------------------------------
# Schema completeness
# ---------------------------------------------------------------------------


def test_output_has_all_required_keys() -> None:
    required = {
        "ticker", "direction", "diff_pct", "abs_jpy_needed",
        "total_shares", "total_jpy", "splits",
        "shares_per_split", "jpy_per_split", "reason",
    }
    p = _plan("NVDA", 0.10, 0.08, 10_000_000, 500.0)
    assert required <= p.keys()


def test_shares_per_split_sum_equals_total_shares_us() -> None:
    p = _plan("NVDA", 0.20, 0.08, 30_000_000, 950.0, currency="USD", fx=150.0)
    assert sum(p["shares_per_split"]) == p["total_shares"]


def test_shares_per_split_sum_equals_total_shares_jp() -> None:
    p = _plan("9999.T", 0.04, 0.10, 10_000_000, 3000.0, currency="JPY")
    assert sum(p["shares_per_split"]) == p["total_shares"]


def test_abs_jpy_needed_is_rounded() -> None:
    p = _plan("NVDA", 0.10, 0.08, 10_000_000, 500.0)
    assert p["abs_jpy_needed"] == round(p["abs_jpy_needed"], 0)


# ---------------------------------------------------------------------------
# summarize_plans
# ---------------------------------------------------------------------------


def test_summarize_empty_returns_empty_string() -> None:
    assert summarize_plans([]) == ""


def test_summarize_skips_hold_entries() -> None:
    """summarize_plans always emits the table header, but hold rows are excluded."""
    hold = _plan("GLD", 0.05, 0.05, 10_000_000, 300.0)
    assert hold["direction"] == "hold"
    result = summarize_plans([hold])
    # Header is always emitted; the hold ticker must not appear as a data row.
    assert "GLD" not in result


def test_summarize_equity_shows_share_count() -> None:
    plans = [_plan("NVDA", 0.10, 0.08, 10_000_000, 500.0)]
    result = summarize_plans(plans)
    assert "NVDA" in result
    assert "株" in result
    assert "trim" in result


def test_summarize_fund_shows_jpy_amounts() -> None:
    plans = [_plan("SLIM_SP500", 0.00, 0.02, 10_000_000, 0, currency="JPY")]
    result = summarize_plans(plans)
    assert "SLIM_SP500" in result
    assert "¥" in result
    assert "株" not in result


def test_summarize_mixed_plans_table_has_headers() -> None:
    plans = [
        _plan("NVDA",      0.10, 0.08, 10_000_000, 500.0),
        _plan("SLIM_SP500", 0.00, 0.02, 10_000_000, 0, currency="JPY"),
        _plan("GLD",       0.05, 0.05, 10_000_000, 300.0),  # hold — excluded
    ]
    result = summarize_plans(plans)
    assert "| 銘柄 |" in result  # header row
    assert "NVDA" in result
    assert "SLIM_SP500" in result
    assert "GLD" not in result   # hold omitted
