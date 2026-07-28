"""fx_exposure.py — Stage 7A: economic exposure resolver + 4量分離

現状の課題: currency_breakdown (portfolio_manager.py) は取引通貨 (listing
currency) の単純合計で USD/JPY 比率を出している
(`sum(value_jpy for p in positions if p['currency']=='USD')`)。これは
以下のケースで経済的な為替エクスポージャーと一致しない:
  - 2634.T は JPY 建てだが円ヘッジ済み S&P500 → 経済的には米国株
    (ネットではヘッジ比率分だけ USD 感応が減る)
  - IEV は USD 建てだが欧州株 ETF → 経済通貨は欧州の複数通貨であって
    USD 単一でも EUR 単一でもない
  - レバレッジ/インバース商品は look-through すると gross が100%を超えうる

3分類 (取得資本配分/ヘッジ後純エクスポージャー/オーバーレイ) では
レバレッジ商品を表現できないため、4つの量に分ける:
  capital_allocation_by_economic_currency — 投入資本の配分。合計100%
  gross_fx_exposure_notional             — ヘッジ前の為替感応 notional。100%超過可
  net_fx_exposure_notional               — ヘッジ後。負値や100%超過も可
  hedge_overlay_notional                 — 先物・FX 等の overlay

instrument_master は JPX・発行体資料で個別に確認した銘柄のみを載せる
(Stage 3 の fx_hedge_manager.py 是正で確認済みの銘柄を流用)。未登録の
fund/ETF は "経済通貨=上場通貨" と憶測せず exposure_source="unknown" で
fail-closed する — 上場通貨と経済通貨が一致するのは「単一企業の普通株」
だけで、fund/ETF は個別に確認しない限り判定できない。

「これは fund か単一株か」は本モジュールが ticker 文字列から推測しない
(誤判定のリスクがある) — 呼び出し側 (実際のポートフォリオ構造を知って
いる側) が is_fund で明示する。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from position_identity import PositionIdentity

BASE_DIR = Path(__file__).parent


@dataclass(frozen=True)
class InstrumentClassification:
    ticker: str
    listing_currency: str
    economic_currency: str          # look-through 後の経済的な通貨
    hedge_ratio: float               # 0.0 (無ヘッジ) 〜 1.0 (完全ヘッジ)
    is_leveraged_or_inverse: bool
    underlying_description: str
    source: str                      # 確認方法 (例: "verified_2026_07_jpx_issuer")
    confirmed_as_of: str
    instrument_kind: str = "fund"
    # 複数通貨ファンド用。None は economic_currency=="USD" なら1、それ以外0。
    # 国別比率は変動するため runtime master では valid_until も必須にする。
    usd_exposure_ratio: Optional[float] = None
    valid_until: Optional[str] = None


# Stage 3 (fx_hedge_manager.py 是正) で JPX・発行体資料により確認済みの銘柄。
# 2026-07 時点の一次資料に基づく — 商品性は変わりうるため定期的な再確認が必要。
INSTRUMENT_MASTER: dict[str, InstrumentClassification] = {
    "2634.T": InstrumentClassification(
        ticker="2634.T", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=1.0, is_leveraged_or_inverse=False,
        underlying_description="NEXT FUNDS S&P500指数(為替ヘッジあり)",
        source="verified_2026_07_jpx_issuer", confirmed_as_of="2026-07-27",
    ),
    "2632.T": InstrumentClassification(
        ticker="2632.T", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=1.0, is_leveraged_or_inverse=False,
        underlying_description="MAXISナスダック100上場投信(為替ヘッジあり)",
        source="verified_2026_07_jpx_issuer", confirmed_as_of="2026-07-27",
    ),
    "1655.T": InstrumentClassification(
        ticker="1655.T", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="iシェアーズ S&P 500 米国株ETF(為替ヘッジなし、円建てのみ)",
        source="verified_2026_07_jpx_issuer", confirmed_as_of="2026-07-27",
    ),
    "2631.T": InstrumentClassification(
        ticker="2631.T", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="MAXISナスダック100上場投信(為替ヘッジなし)",
        source="verified_2026_07_jpx_issuer", confirmed_as_of="2026-07-27",
    ),
    "1545.T": InstrumentClassification(
        ticker="1545.T", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="NEXT FUNDS NASDAQ-100(為替ヘッジなし)",
        source="verified_2026_07_jpx_issuer", confirmed_as_of="2026-07-27",
    ),
    "2040.T": InstrumentClassification(
        ticker="2040.T", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=1.0, is_leveraged_or_inverse=True,  # 2倍レバレッジ ETN
        underlying_description="NEXT NOTES NYダウ・ダブル・ブル・ドルヘッジETN "
                                "(2倍レバレッジ商品 — ヘッジ目的の素朴な資産クラスではない)",
        source="verified_2026_07_jpx_issuer", confirmed_as_of="2026-07-27",
    ),
    # IEV: 公式資料では S&P Europe 350 連動。構成国には英国・スイス等も
    # 含むため「EUR」と単一通貨化するのも誤り。欧州通貨バスケットとして
    # USD/JPY の2区分から分離する。
    "IEV": InstrumentClassification(
        ticker="IEV", listing_currency="USD", economic_currency="MULTI_EUROPE",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="iShares Europe ETF (S&P Europe 350 連動、無ヘッジ)",
        source="https://www.ishares.com/us/products/239736/",
        confirmed_as_of="2026-07-28",
    ),
    "1489.T": InstrumentClassification(
        ticker="1489.T", listing_currency="JPY", economic_currency="JPY",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="NEXT FUNDS 日経平均高配当株50指数連動型上場投信",
        source="https://nextfunds.jp/en/lineup/1489/?from=code",
        confirmed_as_of="2026-07-28",
    ),
    "1306.T": InstrumentClassification(
        ticker="1306.T", listing_currency="JPY", economic_currency="JPY",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="NEXT FUNDS TOPIX連動型上場投信",
        source="https://global.nomura-am.co.jp/nextfunds/products/1306_TPros.pdf",
        confirmed_as_of="2026-07-28",
    ),
    "XLF": InstrumentClassification(
        ticker="XLF", listing_currency="USD", economic_currency="USD",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="Financial Select Sector SPDR ETF",
        source="https://www.ssga.com/us/en/individual/etfs/"
               "state-street-financial-select-sector-spdr-etf-xlf",
        confirmed_as_of="2026-07-28",
    ),
    "GLD": InstrumentClassification(
        ticker="GLD", listing_currency="USD", economic_currency="USD_COMMODITY",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="SPDR Gold Shares (gold bullion, USD base currency)",
        source="https://www.ssga.com/us/en/individual/etfs/spdr-gold-shares-gld",
        confirmed_as_of="2026-07-28",
        usd_exposure_ratio=1.0,
    ),
    "SLIM_SP500": InstrumentClassification(
        ticker="SLIM_SP500", listing_currency="JPY", economic_currency="USD",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="eMAXIS Slim 米国株式(S&P500)、原則為替ヘッジなし",
        source="https://emaxis.am.mufg.jp/fund/topics/__icsFiles/"
               "afieldfile/2024/07/30/253266_240730.pdf",
        confirmed_as_of="2026-07-28",
    ),
    "GS_MMF_USD": InstrumentClassification(
        ticker="GS_MMF_USD", listing_currency="USD", economic_currency="USD",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="USD-denominated money market fund",
        source="broker_security_type:外貨建MMF",
        confirmed_as_of="2026-07-28",
        instrument_kind="money_market_fund",
    ),
    "MNXACT": InstrumentClassification(
        ticker="MNXACT", listing_currency="JPY", economic_currency="JPY",
        hedge_ratio=0.0, is_leveraged_or_inverse=False,
        underlying_description="マネックス・アクティビスト・ファンド（国内株式）",
        source="https://media.monex-am.co.jp/files/assets/"
               "cdc5146b10764826abd49e40dd945381/"
               "deff448a15e9457da97dae2fad414455/121001_UHK_202403.pdf",
        confirmed_as_of="2026-07-28",
    ),
}


def _runtime_master_path() -> Path:
    explicit = os.environ.get("ALMANAC_FX_INSTRUMENT_MASTER")
    if explicit:
        return Path(explicit)
    state_dir = os.environ.get("ALMANAC_STATE_DIR")
    return (
        Path(state_dir) / "fx_instrument_master.json"
        if state_dir
        else BASE_DIR / "fx_instrument_master.json"
    )


def _runtime_classifications(now: datetime) -> dict[str, InstrumentClassification]:
    """Load optional private/dynamic look-through rows.

    The public repository intentionally contains no portfolio-specific
    instrument list. Runtime rows must cite their source and, when they use a
    changing USD country weight, provide valid_until so stale weights fail
    closed instead of silently becoming permanent policy inputs.
    """
    try:
        raw = json.loads(_runtime_master_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = raw.get("instruments") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return {}
    resolved: dict[str, InstrumentClassification] = {}
    for ticker, row in rows.items():
        if not isinstance(row, dict) or not row.get("source") or not row.get("confirmed_as_of"):
            continue
        ratio = row.get("usd_exposure_ratio")
        if ratio is not None:
            try:
                ratio = float(ratio)
            except (TypeError, ValueError):
                continue
            if not 0.0 <= ratio <= 1.0 or not row.get("valid_until"):
                continue
            try:
                if now.date() > datetime.fromisoformat(str(row["valid_until"])).date():
                    continue
            except ValueError:
                continue
        try:
            resolved[str(ticker).upper()] = InstrumentClassification(
                ticker=str(ticker).upper(),
                listing_currency=str(row["listing_currency"]).upper(),
                economic_currency=str(row["economic_currency"]).upper(),
                hedge_ratio=float(row.get("hedge_ratio", 0.0)),
                is_leveraged_or_inverse=bool(row.get("is_leveraged_or_inverse", False)),
                underlying_description=str(row["underlying_description"]),
                source=str(row["source"]),
                confirmed_as_of=str(row["confirmed_as_of"]),
                instrument_kind=str(row.get("instrument_kind") or "fund"),
                usd_exposure_ratio=ratio,
                valid_until=str(row.get("valid_until") or "") or None,
            )
        except (KeyError, TypeError, ValueError):
            continue
    return resolved


def _runtime_instrument_kinds() -> dict[str, str]:
    try:
        raw = json.loads(_runtime_master_path().read_text(encoding="utf-8"))
    except Exception:
        return {}
    rows = raw.get("instrument_kinds") if isinstance(raw, dict) else None
    if not isinstance(rows, dict):
        return {}
    allowed = {
        "stock", "cash", "fund", "etf", "mutual_fund",
        "investment_trust", "etn", "money_market_fund",
    }
    return {
        str(ticker).upper(): str(kind).lower()
        for ticker, kind in rows.items()
        if str(kind).lower() in allowed
    }


def instrument_classification(
    ticker: str,
    *,
    now: Optional[datetime] = None,
) -> Optional[InstrumentClassification]:
    now = now or datetime.now()
    canonical = str(ticker or "").upper()
    return _runtime_classifications(now).get(canonical) or INSTRUMENT_MASTER.get(canonical)


def instrument_kind_for(ticker: str, *, now: Optional[datetime] = None) -> Optional[str]:
    classification = instrument_classification(ticker, now=now)
    if classification:
        return classification.instrument_kind
    return _runtime_instrument_kinds().get(str(ticker or "").upper())


@dataclass(frozen=True)
class EconomicCurrencyExposure:
    position_identity: PositionIdentity
    market_value_jpy: float
    gross_usd_exposure_jpy: Optional[float]   # None = unknown (fail-closed)
    embedded_hedge_notional_jpy: Optional[float]
    net_usd_exposure_jpy: Optional[float]
    exposure_source: str  # "lookthrough" | "instrument_master" | "broker" | "unknown" | "single_stock"
    as_of: str
    economic_currency: Optional[str] = None
    usd_exposure_ratio: Optional[float] = None


def resolve_economic_exposure(
    *,
    position: PositionIdentity,
    market_value_jpy: float,
    listing_currency: str,
    is_fund: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> EconomicCurrencyExposure:
    """1ポジション分の経済的為替エクスポージャーを解決する。

    - instrument_master に載っている銘柄: そこから hedge_ratio 等を使う
    - is_fund=False (単一株と明示): 経済通貨=上場通貨のtrivialケース
      (単一企業の普通株は look-through の余地が無い)
    - is_fund=True かつ master 未登録: fail-closed で unknown
      (fund/ETF は個別確認なしに経済通貨を推測しない)
    """
    now = now or datetime.now()
    as_of = now.isoformat()
    ticker = position.canonical_instrument_id

    master = instrument_classification(ticker, now=now)
    if master is not None:
        usd_ratio = (
            master.usd_exposure_ratio
            if master.usd_exposure_ratio is not None
            else (1.0 if master.economic_currency == "USD" else 0.0)
        )
        gross = market_value_jpy * usd_ratio
        hedge_notional = gross * master.hedge_ratio
        net = gross - hedge_notional
        return EconomicCurrencyExposure(
            position_identity=position, market_value_jpy=market_value_jpy,
            gross_usd_exposure_jpy=round(gross, 0),
            embedded_hedge_notional_jpy=round(hedge_notional, 0),
            net_usd_exposure_jpy=round(net, 0),
            exposure_source="instrument_master", as_of=as_of,
            economic_currency=master.economic_currency,
            usd_exposure_ratio=usd_ratio,
        )

    if is_fund is False:
        # 単一株: 上場通貨=経済通貨 (look-through の余地が無い)
        is_usd = listing_currency.upper() == "USD"
        gross = market_value_jpy if is_usd else 0.0
        return EconomicCurrencyExposure(
            position_identity=position, market_value_jpy=market_value_jpy,
            gross_usd_exposure_jpy=round(gross, 0),
            embedded_hedge_notional_jpy=0.0,
            net_usd_exposure_jpy=round(gross, 0),
            exposure_source="single_stock", as_of=as_of,
            economic_currency=listing_currency.upper(),
            usd_exposure_ratio=1.0 if is_usd else 0.0,
        )

    # fund または商品種別未確認で instrument_master 未登録 → unknown。
    # default を単一株扱いにすると、未分類の投信/ETFを上場通貨へ誤帰属する。
    return EconomicCurrencyExposure(
        position_identity=position, market_value_jpy=market_value_jpy,
        gross_usd_exposure_jpy=None, embedded_hedge_notional_jpy=None, net_usd_exposure_jpy=None,
        exposure_source="unknown", as_of=as_of,
        economic_currency=None, usd_exposure_ratio=None,
    )


@dataclass(frozen=True)
class FxExposureSummary:
    """4量に分けた為替エクスポージャーのポートフォリオ集計。

    USD/JPY の2通貨に単純化した集計 (稼働中の CURRENCY_TARGETS が
    USD/JPY の2区分だけを持つため、それに対応する形にしている)。
    USD でも JPY でもない経済通貨 (例: IEV の EUR) は other_currency 側へ
    明示的に分離し、"不明を JPY とみなして薄める" ことをしない。
    """
    capital_allocation_usd_pct: float       # 投入資本の USD 配分 (合計100%の一部)
    capital_allocation_jpy_pct: float
    gross_fx_exposure_usd_jpy: float        # ヘッジ前の為替感応 notional (100%超過可)
    net_fx_exposure_usd_jpy: float          # ヘッジ後 (負値・100%超過も可)
    hedge_overlay_notional_jpy: float       # 先物・FX 等の overlay (現状常に0 — 7Bで導入)
    total_market_value_jpy: float
    unknown_value_jpy: float                # exposure_source="unknown" だった分の時価総額
    unknown_tickers: tuple[str, ...]
    other_currency_value_jpy: float         # USD/JPY 以外または複数通貨バスケット
    other_currency_tickers: tuple[str, ...]


def summarize_fx_exposure(exposures: list[EconomicCurrencyExposure]) -> FxExposureSummary:
    """複数ポジションの EconomicCurrencyExposure を集計する。

    unknown (fail-closed) だった分・USD/JPY 以外の経済通貨だった分は
    capital_allocation の JPY 側へ黙って合算しない — unknown_value_jpy /
    other_currency_value_jpy として別途明示する。
    """
    total_mv = sum(e.market_value_jpy for e in exposures)
    gross_usd = sum(e.gross_usd_exposure_jpy for e in exposures if e.gross_usd_exposure_jpy is not None)
    net_usd = sum(e.net_usd_exposure_jpy for e in exposures if e.net_usd_exposure_jpy is not None)
    unknown_value = sum(e.market_value_jpy for e in exposures if e.exposure_source == "unknown")
    unknown_tickers = tuple(
        e.position_identity.canonical_instrument_id for e in exposures if e.exposure_source == "unknown"
    )

    usd_capital = sum(
        e.market_value_jpy for e in exposures
        if e.exposure_source == "single_stock" and e.gross_usd_exposure_jpy
    ) + sum(
        e.market_value_jpy * float(e.usd_exposure_ratio or 0.0)
        for e in exposures if e.exposure_source == "instrument_master"
    )
    other_currency_rows = [
        e for e in exposures
        if e.exposure_source == "instrument_master"
        and e.economic_currency not in ("USD", "JPY")
    ]
    other_currency_value = sum(
        e.market_value_jpy * (1.0 - float(e.usd_exposure_ratio or 0.0))
        for e in other_currency_rows
    )
    other_currency_tickers = tuple(e.position_identity.canonical_instrument_id for e in other_currency_rows)

    jpy_capital = total_mv - usd_capital - unknown_value - other_currency_value

    return FxExposureSummary(
        capital_allocation_usd_pct=round(usd_capital / total_mv, 4) if total_mv > 0 else 0.0,
        capital_allocation_jpy_pct=round(jpy_capital / total_mv, 4) if total_mv > 0 else 0.0,
        gross_fx_exposure_usd_jpy=round(gross_usd, 0),
        net_fx_exposure_usd_jpy=round(net_usd, 0),
        hedge_overlay_notional_jpy=0.0,  # Stage 7B (先物・FX overlay) で導入
        total_market_value_jpy=round(total_mv, 0),
        unknown_value_jpy=round(unknown_value, 0),
        unknown_tickers=unknown_tickers,
        other_currency_value_jpy=round(other_currency_value, 0),
        other_currency_tickers=other_currency_tickers,
    )
