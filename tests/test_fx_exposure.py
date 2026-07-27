"""Stage 7A: economic exposure resolver + 4量分離 (fx_exposure.py)。

背景: currency_breakdown (portfolio_manager.py) は取引通貨の単純合計
(`p['currency']=='USD'`) で USD/JPY 比率を出しており、2634.T (JPY建て
だが円ヘッジ済みS&P500=経済的にはUSD) や IEV (USD建てだが欧州株=
経済通貨はEUR) のような look-through 不一致を扱えない。本テストは
instrument_master 経由の解決・単一株のtrivialケース・未登録fundの
fail-closed (unknown) 動作を検証する。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import fx_exposure as fx  # noqa: E402
from position_identity import PositionIdentity  # noqa: E402


def _pos(ticker):
    return PositionIdentity("husband", "rakuten", "general", ticker)


# ---------------------------------------------------------------------------
# instrument_master 経由の解決
# ---------------------------------------------------------------------------


def test_hedged_jpy_fund_resolves_to_zero_net_usd_exposure():
    """本題: 2634.T (JPY建て・円ヘッジ済みS&P500) は gross は USD だが
    ヘッジ比率100%のため net USD exposure はほぼゼロになる。"""
    e = fx.resolve_economic_exposure(
        position=_pos('2634.T'), market_value_jpy=1_000_000, listing_currency='JPY',
    )
    assert e.exposure_source == 'instrument_master'
    assert e.gross_usd_exposure_jpy == pytest.approx(1_000_000)
    assert e.embedded_hedge_notional_jpy == pytest.approx(1_000_000)
    assert e.net_usd_exposure_jpy == pytest.approx(0)


def test_unhedged_jpy_fund_keeps_full_net_usd_exposure():
    """1655.T (JPY建て・無ヘッジS&P500) は gross=net (ヘッジ無し)。"""
    e = fx.resolve_economic_exposure(
        position=_pos('1655.T'), market_value_jpy=1_000_000, listing_currency='JPY',
    )
    assert e.gross_usd_exposure_jpy == pytest.approx(1_000_000)
    assert e.embedded_hedge_notional_jpy == pytest.approx(0)
    assert e.net_usd_exposure_jpy == pytest.approx(1_000_000)


def test_ietf_with_non_usd_economic_currency_is_not_counted_as_usd():
    """本題: IEV は USD建てだが経済通貨はEUR。gross_usd_exposure に
    算入してはならない (取引通貨だけ見た集計の誤りを再現しない)。"""
    e = fx.resolve_economic_exposure(
        position=_pos('IEV'), market_value_jpy=500_000, listing_currency='USD',
    )
    assert e.exposure_source == 'instrument_master'
    assert e.gross_usd_exposure_jpy == 0.0  # EUR エクスポージャーであり USD ではない


def test_leveraged_etn_still_resolves_but_is_flagged_leveraged():
    e = fx.resolve_economic_exposure(
        position=_pos('2040.T'), market_value_jpy=100_000, listing_currency='JPY',
    )
    assert fx.INSTRUMENT_MASTER['2040.T'].is_leveraged_or_inverse is True
    assert e.exposure_source == 'instrument_master'


# ---------------------------------------------------------------------------
# 単一株の trivial ケース
# ---------------------------------------------------------------------------


def test_single_stock_usd_listing_is_full_usd_exposure():
    e = fx.resolve_economic_exposure(
        position=_pos('AAPL'), market_value_jpy=2_000_000, listing_currency='USD', is_fund=False,
    )
    assert e.exposure_source == 'single_stock'
    assert e.gross_usd_exposure_jpy == pytest.approx(2_000_000)
    assert e.net_usd_exposure_jpy == pytest.approx(2_000_000)
    assert e.embedded_hedge_notional_jpy == 0.0


def test_single_stock_jpy_listing_has_zero_usd_exposure():
    e = fx.resolve_economic_exposure(
        position=_pos('9432.T'), market_value_jpy=800_000, listing_currency='JPY', is_fund=False,
    )
    assert e.exposure_source == 'single_stock'
    assert e.gross_usd_exposure_jpy == 0.0


# ---------------------------------------------------------------------------
# 未登録 fund は fail-closed で unknown
# ---------------------------------------------------------------------------


def test_unregistered_fund_fails_closed_to_unknown():
    """本題: instrument_master に無い fund/ETF を「上場通貨=経済通貨」と
    憶測しない。0ではなく unknown として明示する。"""
    e = fx.resolve_economic_exposure(
        position=_pos('SLIM_SP500'), market_value_jpy=1_500_000, listing_currency='JPY', is_fund=True,
    )
    assert e.exposure_source == 'unknown'
    assert e.gross_usd_exposure_jpy is None
    assert e.embedded_hedge_notional_jpy is None
    assert e.net_usd_exposure_jpy is None


def test_is_fund_flag_is_caller_responsibility_not_guessed_from_ticker():
    """ticker文字列から fund かどうかを推測しない — is_fund は呼び出し側が
    明示する契約であることを、同じ ticker で挙動が変わることで示す。"""
    as_stock = fx.resolve_economic_exposure(
        position=_pos('XYZ123'), market_value_jpy=100_000, listing_currency='USD', is_fund=False,
    )
    as_fund = fx.resolve_economic_exposure(
        position=_pos('XYZ123'), market_value_jpy=100_000, listing_currency='USD', is_fund=True,
    )
    assert as_stock.exposure_source == 'single_stock'
    assert as_fund.exposure_source == 'unknown'


# ---------------------------------------------------------------------------
# summarize_fx_exposure — ポートフォリオ集計
# ---------------------------------------------------------------------------


def test_summary_separates_unknown_from_known_totals():
    exposures = [
        fx.resolve_economic_exposure(position=_pos('AAPL'), market_value_jpy=1_000_000, listing_currency='USD'),
        fx.resolve_economic_exposure(position=_pos('9432.T'), market_value_jpy=1_000_000, listing_currency='JPY'),
        fx.resolve_economic_exposure(position=_pos('SLIM_ORCAN'), market_value_jpy=500_000, listing_currency='JPY', is_fund=True),
    ]
    summary = fx.summarize_fx_exposure(exposures)
    assert summary.unknown_value_jpy == pytest.approx(500_000)
    assert 'SLIM_ORCAN' in summary.unknown_tickers
    assert summary.total_market_value_jpy == pytest.approx(2_500_000)
    # unknown 分は capital_allocation の USD/JPY どちらの分子にも含まれない
    assert summary.capital_allocation_usd_pct == pytest.approx(1_000_000 / 2_500_000)


def test_summary_separates_other_currency_from_jpy():
    """本題: IEV (EUR) を JPY 側へ黙って合算しない。"""
    exposures = [
        fx.resolve_economic_exposure(position=_pos('AAPL'), market_value_jpy=1_000_000, listing_currency='USD'),
        fx.resolve_economic_exposure(position=_pos('IEV'), market_value_jpy=500_000, listing_currency='USD'),
    ]
    summary = fx.summarize_fx_exposure(exposures)
    assert summary.other_currency_value_jpy == pytest.approx(500_000)
    assert 'IEV' in summary.other_currency_tickers
    # IEV 分が jpy_pct にも usd_pct にも入っていないこと
    assert summary.capital_allocation_usd_pct == pytest.approx(1_000_000 / 1_500_000, abs=1e-4)
    assert summary.capital_allocation_jpy_pct == pytest.approx(0.0, abs=1e-4)


def test_summary_gross_vs_net_diverge_when_hedged_positions_present():
    """本題: ヘッジ済みポジションがあると gross と net が乖離する
    (3分類では表現できず、4量分離が必要な理由そのもの)。"""
    exposures = [
        fx.resolve_economic_exposure(position=_pos('2634.T'), market_value_jpy=1_000_000, listing_currency='JPY'),
    ]
    summary = fx.summarize_fx_exposure(exposures)
    assert summary.gross_fx_exposure_usd_jpy == pytest.approx(1_000_000)
    assert summary.net_fx_exposure_usd_jpy == pytest.approx(0)
    assert summary.gross_fx_exposure_usd_jpy != summary.net_fx_exposure_usd_jpy


def test_summary_capital_allocation_sums_to_full_portfolio_when_all_known():
    exposures = [
        fx.resolve_economic_exposure(position=_pos('AAPL'), market_value_jpy=600_000, listing_currency='USD'),
        fx.resolve_economic_exposure(position=_pos('9432.T'), market_value_jpy=400_000, listing_currency='JPY'),
    ]
    summary = fx.summarize_fx_exposure(exposures)
    assert summary.capital_allocation_usd_pct + summary.capital_allocation_jpy_pct == pytest.approx(1.0)


def test_empty_portfolio_does_not_divide_by_zero():
    summary = fx.summarize_fx_exposure([])
    assert summary.capital_allocation_usd_pct == 0.0
    assert summary.total_market_value_jpy == 0.0
