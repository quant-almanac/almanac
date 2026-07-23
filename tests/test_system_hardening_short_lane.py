from __future__ import annotations

import json

import pandas as pd
import pytest

import jp_loanability
from deterministic_disclosure_features import extract_deterministic_values
from disclosure_shadow_book import estimate_round_trip_cost_pct, simulate_shadow_book
from feature_validation import certify
from jp_dilution_parser import (
    PARSER_VERSION,
    parse_dilution_event,
    parse_going_concern_flag,
)
from sync_jp_loanable import parse_loanable_csv
from sync_jsf_lending import parse_jsf_csv


def test_dilution_parser_detects_event_and_explicit_ratio() -> None:
    flag, ratio = parse_dilution_event(
        "第三者割当による新株式発行のお知らせ 希薄化率は12.5%となる見込み"
    )
    assert flag is True
    assert ratio == 0.125
    assert PARSER_VERSION == "jp-dilution-1.0"


def test_dilution_parser_does_not_treat_dates_as_percentages() -> None:
    flag, ratio = parse_dilution_event("2026年6月12日 公募増資に関するお知らせ")
    assert flag is True
    assert ratio is None


def test_going_concern_is_high_precision_title_match() -> None:
    assert parse_going_concern_flag("継続企業の前提に関する注記について") is True
    assert parse_going_concern_flag("決算短信の一部訂正について") is False


def test_deterministic_features_wire_short_events() -> None:
    features, versions = extract_deterministic_values(
        {
            "title": "継続企業の前提に関する注記について",
            "body": "第三者割当を実施。希薄化率 8.0%",
        }
    )
    assert features["dilution_flag"] is True
    assert features["dilution_pct"] == 0.08
    assert features["going_concern_flag"] is True
    assert "jp-dilution-1.0" in versions


def _prices() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [100.0, 99.0, 98.0],
            "Close": [99.0, 98.0, 97.0],
        },
        index=pd.to_datetime(["2026-06-02", "2026-06-03", "2026-06-04"]),
    )


def _shadow_config() -> dict:
    return {
        "horizons": [1],
        "notional_jpy": 100_000,
        "thresholds": {
            "directional_score": 0.6,
            "directional_confidence": 0.7,
            "guidance_revision_pct": 0.1,
            "monthly_yoy_pct": 0.1,
            "insider_cluster_score": 3,
        },
        "cost_model": {
            "jp_spread_bps_each_side": {
                "notional_lte_100k": 20,
                "notional_lte_500k": 10,
                "larger": 5,
            },
            "us_commission_rate_each_side": 0.00495,
            "us_commission_cap_usd_each_side": 22,
            "us_spread_bps_each_side": 5,
            "rakuten_fx_spread_jpy_per_usd_each_side": 0.25,
            "jp_short": {
                "standard_borrow_rate_annual": 0.011,
                "reverse_daily_fee_buffer_annual": 0.01,
                "general_borrow_rate_annual_min": 0.014,
                "general_borrow_rate_annual_max": 0.039,
            },
        },
    }


def test_shadow_book_accepts_multiindex_parquet_columns() -> None:
    """data_fetcher stores yfinance ('Close','TICKER') MultiIndex in parquet.

    The parquet load path does not flatten it, so a real shadow-book run crashed
    with "price data needs Open and Close columns" while unit fixtures (flat
    columns) passed. simulate_shadow_book must collapse the MultiIndex.
    """
    idx = pd.to_datetime(["2026-06-02", "2026-06-03", "2026-06-04"])
    multi = pd.DataFrame(
        [[99.0, 100.0], [98.0, 99.0], [97.0, 98.0]],
        index=idx,
        columns=pd.MultiIndex.from_tuples([("Close", "1234.T"), ("Open", "1234.T")]),
    )
    multi.index.name = "Date"
    result = simulate_shadow_book(
        [{
            "feature_id": "f1",
            "ticker": "1234.T",
            "market": "JP",
            "publish_time": "2026-06-01T15:00:00+09:00",
            "directional_score": 0.9,
            "directional_confidence": 0.9,
        }],
        {"1234.T": multi},
        config=_shadow_config(),
    )
    assert result["trade_count"] == 1
    assert result["missing_price_tickers"] == []


def test_jp_short_cost_always_includes_borrow_cost() -> None:
    config = _shadow_config()
    long_cost = estimate_round_trip_cost_pct(
        market="JP", notional_jpy=100_000, config=config, direction=1, horizon_days=20
    )
    short_cost = estimate_round_trip_cost_pct(
        market="JP", notional_jpy=100_000, config=config, direction=-1, horizon_days=20
    )
    assert short_cost > long_cost


def test_shadow_short_is_untradeable_when_loanability_unknown(monkeypatch) -> None:
    monkeypatch.setattr(
        jp_loanability,
        "evaluate_short_tradeability",
        lambda _ticker: {
            "loanable": None,
            "loan_ratio": None,
            "reverse_daily_fee": False,
            "untradeable": True,
            "reasons": ["loanable_not_confirmed"],
        },
    )
    result = simulate_shadow_book(
        [{
            "feature_id": "f1",
            "ticker": "1234.T",
            "market": "JP",
            "publish_time": "2026-06-01T15:00:00+09:00",
            "dilution_flag": True,
        }],
        {"1234.T": _prices()},
        config=_shadow_config(),
    )
    assert result["trade_count"] == 1
    assert result["tradeable_trade_count"] == 0
    assert result["trades"][0]["excluded_from_certify"] is True


def test_us_short_is_untradeable_until_explicitly_enabled() -> None:
    """米株の direction=-1 は us_short_enabled=False の間は執行不能扱い (review P2)。

    楽天での米株空売りの可否・コストは未確認。JP の loanable_not_confirmed と同じ
    保守側デフォルトで、確認前の US 売りがロング同等コストでシャドー損益を
    水増しすることを防ぐ。
    """
    row = {
        "feature_id": "f-us-short",
        "ticker": "AAPL",
        "market": "US",
        "publish_time": "2026-06-01T15:00:00+09:00",
        "directional_score": -0.9,
        "directional_confidence": 0.9,
    }
    result = simulate_shadow_book(
        [row],
        {"AAPL": _prices()},
        config=_shadow_config(),  # us_short_enabled 未指定 = False がデフォルト
    )
    assert result["trade_count"] == 1
    assert result["tradeable_trade_count"] == 0
    trade = result["trades"][0]
    assert trade["untradeable"] is True
    assert "us_short_not_enabled" in trade["untradeable_reasons"]
    assert trade["excluded_from_certify"] is True
    # Opt-in flips it back to a simulated trade.
    enabled = simulate_shadow_book(
        [row],
        {"AAPL": _prices()},
        config={**_shadow_config(), "us_short_enabled": True},
    )
    assert enabled["tradeable_trade_count"] == 1


def test_loanable_and_jsf_parsers_fail_closed_on_unknown_headers() -> None:
    parsed = parse_loanable_csv("銘柄コード,貸借区分\n1234,貸借\n5678,非貸借\n")
    assert parsed == {"1234.T": True, "5678.T": False}
    jsf = parse_jsf_csv(
        "ticker,loan_ratio,reverse_daily_fee\n1234.T,1.1,active\n"
    )
    assert jsf["1234.T"] == {"loan_ratio": 1.1, "reverse_daily_fee": True}
    with pytest.raises(ValueError, match="unrecognized"):
        parse_loanable_csv("foo,bar\n1,2\n")


def test_short_certify_uses_higher_cost_and_excludes_untradeable() -> None:
    panel = [
        {
            "date": "2026-06-01",
            "ticker": "A",
            "feature": -1.0,
            "fwd_return": -0.02,
            "direction": "short",
            "compute_time": "2026-06-01T00:00:00+00:00",
            "untradeable": False,
        },
        {
            "date": "2026-06-01",
            "ticker": "B",
            "feature": -0.5,
            "fwd_return": -0.01,
            "direction": "short",
            "compute_time": "2026-06-01T00:00:00+00:00",
            "untradeable": True,
        },
    ]
    result = certify(
        panel,
        feature_name="dilution_pct",
        n_trials=1,
        min_compute_time="2026-06-01T00:00:00+00:00",
        outcome_horizon_days=5,
        placebo_panel=[],
        direction="short",
    )
    assert result["effective_cost_bps"] == 30.0
    assert result["n_untradeable_excluded"] == 1


def test_universe_has_explicit_unknown_loanability_for_every_ticker() -> None:
    universe = json.loads(open("disclosure_universe_jp.json", encoding="utf-8").read())
    assert set(universe["loanable_by_ticker"]) == set(universe["tickers"])
