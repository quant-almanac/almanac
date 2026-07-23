"""
ALMANAC v5.0 - SEC EDGAR 財務データフェッチャー
認証不要・完全無料の SEC EDGAR XBRL API から公式財務データを取得する。
long_term_screener.py の yfinance フォールバックとして使用。

エンドポイント:
  https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json
  https://www.sec.gov/files/company_tickers.json  (CIKルックアップ)

レート制限: 10 req/秒（実質無制限）
キャッシュ: data/edgar_cache/{ticker}.json（TTL: 24時間）
"""

import json
import time
import requests
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional

BASE_DIR   = Path(__file__).parent
CACHE_DIR  = BASE_DIR / "data" / "edgar_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL  = 3600 * 24   # 24時間
_HEADERS   = {"User-Agent": "ALMANAC research@almanac.local"}

# ============================================================
# CIK ルックアップ
# ============================================================

_cik_map_cache: dict = {}

def _load_cik_map() -> dict:
    """EDGAR の全社 CIK マッピングを取得してキャッシュ。"""
    global _cik_map_cache
    if _cik_map_cache:
        return _cik_map_cache

    cik_cache_file = CACHE_DIR / "_company_tickers.json"
    # ローカルキャッシュ（1週間有効）
    if cik_cache_file.exists():
        age = (datetime.now() - datetime.fromtimestamp(cik_cache_file.stat().st_mtime)).total_seconds()
        if age < 3600 * 24 * 7:
            try:
                raw = json.loads(cik_cache_file.read_text(encoding="utf-8"))
                _cik_map_cache = {v["ticker"].upper(): v["cik_str"] for v in raw.values()}
                return _cik_map_cache
            except Exception:
                pass

    try:
        r = requests.get(
            "https://www.sec.gov/files/company_tickers.json",
            headers=_HEADERS, timeout=15
        )
        r.raise_for_status()
        raw = r.json()
        cik_cache_file.write_text(json.dumps(raw, ensure_ascii=False))
        _cik_map_cache = {v["ticker"].upper(): v["cik_str"] for v in raw.values()}
        return _cik_map_cache
    except Exception as e:
        print(f"[edgar] CIKマップ取得失敗: {e}")
        return {}


def get_cik(ticker: str) -> Optional[str]:
    """ティッカーから CIK（10桁ゼロ埋め文字列）を返す。"""
    cmap = _load_cik_map()
    raw = cmap.get(ticker.upper().replace(".T", ""))
    if raw is None:
        return None
    return str(raw).zfill(10)


# ============================================================
# XBRL facts 取得
# ============================================================

def _load_facts_cached(ticker: str) -> Optional[dict]:
    """companyfacts をキャッシュから読み込む（TTL: 24h）。"""
    path = CACHE_DIR / f"{ticker.upper()}.json"
    if path.exists():
        try:
            age = (datetime.now() - datetime.fromtimestamp(path.stat().st_mtime)).total_seconds()
            if age < CACHE_TTL:
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _fetch_facts(cik: str, ticker: str) -> Optional[dict]:
    """EDGAR から companyfacts JSON を取得してキャッシュ保存。"""
    url = f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        path = CACHE_DIR / f"{ticker.upper()}.json"
        path.write_text(json.dumps(data, ensure_ascii=False))
        return data
    except Exception as e:
        print(f"[edgar] {ticker} facts取得失敗: {e}")
        return None


def _extract_annual_series(facts: dict, *tag_candidates: str) -> list[dict]:
    """
    us-gaap から年次（10-K）データを抽出。複数タグ名の候補から最初に見つかったものを使う。
    Returns: [{'year': int, 'value': float}, ...] 降順
    """
    us_gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tag_candidates:
        concept = us_gaap.get(tag)
        if not concept:
            continue
        units = concept.get("units", {})
        # USD 単位を優先
        values_list = units.get("USD") or units.get("USD/shares") or next(iter(units.values()), [])
        annual = [
            {"year": int(v["end"][:4]), "value": float(v["val"]), "form": v.get("form", "")}
            for v in values_list
            if v.get("form", "") in ("10-K", "10-K/A") and v.get("end")
        ]
        if annual:
            # 同年の最新値のみ保持
            by_year: dict[int, float] = {}
            for item in sorted(annual, key=lambda x: x["year"]):
                by_year[item["year"]] = item["value"]
            return [{"year": y, "value": v} for y, v in sorted(by_year.items(), reverse=True)]
    return []


def _yoy_growth(series: list[dict]) -> Optional[float]:
    """直近2年のデータから YoY 成長率を計算。"""
    if len(series) < 2:
        return None
    latest = series[0]["value"]
    prev   = series[1]["value"]
    if prev == 0:
        return None
    return (latest - prev) / abs(prev)


# ============================================================
# 公開 API
# ============================================================

def get_edgar_financials(ticker: str) -> dict:
    """
    SEC EDGAR から公式財務データを取得する。

    Returns:
        {
          'source':        'edgar' | 'cache' | 'unavailable'
          'eps_growth':    float | None   # YoY EPS成長率（小数）
          'rev_growth':    float | None   # YoY 売上成長率（小数）
          'gross_margin':  float | None   # 粗利率（小数）
          'op_cashflow':   float | None   # 営業CF（USD）
          'capex':         float | None   # 設備投資（USD, 正数）
          'fcf':           float | None   # FCF = OpCF - CapEx（USD）
          'net_income':    float | None   # 当期純利益（USD）
          'equity':        float | None   # 純資産（USD）
          'roe':           float | None   # ROE = NetIncome / Equity
          'cik':           str | None
          'fetched_at':    str            # ISO 日時
        }
    """
    result_base = {
        "source": "unavailable", "eps_growth": None, "rev_growth": None,
        "gross_margin": None, "op_cashflow": None, "capex": None,
        "fcf": None, "net_income": None, "equity": None, "roe": None,
        "cik": None, "fetched_at": datetime.now().isoformat(),
    }

    # 日本株は EDGAR 対象外
    if ticker.endswith(".T"):
        return result_base

    cik = get_cik(ticker)
    if not cik:
        return result_base
    result_base["cik"] = cik

    # キャッシュ確認
    cached_facts = _load_facts_cached(ticker)
    source = "cache"
    if cached_facts is None:
        cached_facts = _fetch_facts(cik, ticker)
        source = "edgar"
        time.sleep(0.15)   # 10 req/s 制限に対して余裕を持たせる

    if not cached_facts:
        return result_base

    facts = cached_facts

    # EPS 成長率
    eps_series = _extract_annual_series(
        facts,
        "EarningsPerShareDiluted",
        "EarningsPerShareBasic",
    )
    eps_growth = _yoy_growth(eps_series)

    # 売上成長率
    rev_series = _extract_annual_series(
        facts,
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
    )
    rev_growth = _yoy_growth(rev_series)

    # 粗利益 / 粗利率
    gross_profit_series = _extract_annual_series(facts, "GrossProfit")
    gross_margin = None
    if gross_profit_series and rev_series:
        gp = gross_profit_series[0]["value"]
        rev = rev_series[0]["value"]
        if rev > 0:
            gross_margin = gp / rev

    # 営業CF
    op_cf_series = _extract_annual_series(
        facts,
        "NetCashProvidedByUsedInOperatingActivities",
        "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
    )
    op_cashflow = op_cf_series[0]["value"] if op_cf_series else None

    # CapEx（設定投資の支出は負値で記録されることが多いため abs）
    capex_series = _extract_annual_series(
        facts,
        "PaymentsToAcquirePropertyPlantAndEquipment",
        "CapitalExpendituresIncurredButNotYetPaid",
    )
    capex = abs(capex_series[0]["value"]) if capex_series else None

    # FCF
    fcf = None
    if op_cashflow is not None and capex is not None:
        fcf = op_cashflow - capex

    # 純利益
    ni_series = _extract_annual_series(
        facts,
        "NetIncomeLoss",
        "NetIncome",
        "ProfitLoss",
    )
    net_income = ni_series[0]["value"] if ni_series else None

    # 純資産
    eq_series = _extract_annual_series(
        facts,
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
        "LiabilitiesAndStockholdersEquity",
    )
    equity = eq_series[0]["value"] if eq_series else None

    # ROE
    roe = None
    if net_income is not None and equity and equity > 0:
        roe = net_income / equity

    return {
        "source":       source,
        "eps_growth":   eps_growth,
        "rev_growth":   rev_growth,
        "gross_margin": gross_margin,
        "op_cashflow":  op_cashflow,
        "capex":        capex,
        "fcf":          fcf,
        "net_income":   net_income,
        "equity":       equity,
        "roe":          roe,
        "cik":          cik,
        "fetched_at":   datetime.now().isoformat(),
    }


# ============================================================
# 開示イベント（8-K / 10-Q / 10-K 等）— disclosure feature pipeline 用
# ============================================================

# SEC form → disclosure_type 分類（almanac.observability.disclosure_features の語彙）
_FORM_TO_TYPE = {
    "10-K": "earnings", "10-Q": "earnings", "20-F": "earnings", "6-K": "earnings",
    "8-K": "other",          # 8-K は多目的。本文が無いと細分類できないので other。
    "S-1": "shelf", "S-3": "shelf", "424B5": "shelf", "424B3": "shelf",
    "4": "insider", "3": "insider", "5": "insider",
    "SC 13D": "stake", "SC 13D/A": "stake",
    "SC 13G": "stake", "SC 13G/A": "stake",
}
# Phase 0 で取り込む対象フォーム（ノイズの多い細かいフォームは除外）。
_DEFAULT_FORMS = (
    "8-K", "10-Q", "10-K", "20-F", "6-K", "S-1", "S-3",
    "SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A",
)


def normalize_edgar_submissions(
    submissions: dict,
    ticker: str,
    *,
    forms: "tuple[str, ...] | None" = None,
    limit: int = 40,
) -> list[dict]:
    """Convert a SEC ``submissions/CIK*.json`` payload to disclosure items (pure).

    Output items match the contract consumed by
    ``disclosure_feature_extractor.extract_features``: ``source`` / ``ticker`` /
    ``native_doc_id`` (accession) / ``source_url`` / ``publish_time`` / ``title`` /
    ``body`` / ``market`` / ``language`` / ``disclosure_type`` / ticker resolution.

    Body is the form description only — full filing text is a separate (heavier)
    enrichment fetch and is intentionally out of Phase-0 scope; observe_only
    validation will simply see weaker features for body-less events.
    """
    want = set(forms or _DEFAULT_FORMS)
    recent = (submissions or {}).get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber") or []
    form_list = recent.get("form") or []
    dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []
    descriptions = recent.get("primaryDocDescription") or []
    # Prefer the CIK embedded in the payload so the normalizer stays pure (no
    # network); fall back to the CIK map only if the payload omits it.
    cik = str(submissions.get("cik") or "").lstrip("0")
    if not cik:
        cik = (get_cik(ticker) or "").lstrip("0")

    items: list[dict] = []
    for i, accession in enumerate(accessions):
        form = form_list[i] if i < len(form_list) else ""
        if form not in want:
            continue
        filing_date = dates[i] if i < len(dates) else ""
        if not filing_date:
            continue
        primary = primary_docs[i] if i < len(primary_docs) else ""
        desc = descriptions[i] if i < len(descriptions) else ""
        acc_nodash = accession.replace("-", "")
        source_url = (
            f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{primary}"
            if cik and primary else
            f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={cik}"
        )
        items.append({
            "source": "edgar",
            "ticker": ticker,
            "native_doc_id": accession,
            "source_url": source_url,
            "publish_time": filing_date,           # ISO date; midnight-UTC on parse
            "market": "US",
            "language": "en",
            "disclosure_type": _FORM_TO_TYPE.get(form, "other"),
            "title": f"{form}: {desc or form}",
            "body": desc or form,                  # metadata only; see docstring
            "ticker_resolution_method": "cik",
            "ticker_resolution_confidence": 1.0,
        })
        if len(items) >= limit:
            break
    return items


def fetch_edgar_filings(
    ticker: str,
    *,
    live: bool = False,
    forms: "tuple[str, ...] | None" = None,
    limit: int = 40,
) -> list[dict]:
    """Fetch recent SEC filing events for ``ticker`` as disclosure items.

    Network is **gated**: returns ``[]`` unless ``live=True`` so importing or
    dry-running the ingest pipeline never hits SEC. Live mode uses the public
    ``data.sec.gov/submissions`` endpoint (no auth, 10 req/s).
    """
    if not live:
        return []
    cik = get_cik(ticker)
    if not cik:
        return []
    url = f"https://data.sec.gov/submissions/CIK{cik}.json"
    try:
        r = requests.get(url, headers=_HEADERS, timeout=30)
        r.raise_for_status()
        return normalize_edgar_submissions(r.json(), ticker, forms=forms, limit=limit)
    except Exception as e:
        print(f"[edgar] {ticker} filings取得失敗: {e}")
        return []


# ============================================================
# CLI
# ============================================================

if __name__ == "__main__":
    import sys

    tickers = sys.argv[1:] or ["NVDA", "AVGO", "META"]
    for t in tickers:
        print(f"\n── {t} ──")
        d = get_edgar_financials(t)
        print(f"  source:      {d['source']}")
        print(f"  CIK:         {d['cik']}")
        if d.get('eps_growth') is not None:
            print(f"  EPS成長率:   {d['eps_growth']*100:.1f}%")
        if d.get('rev_growth') is not None:
            print(f"  売上成長率:  {d['rev_growth']*100:.1f}%")
        if d.get('gross_margin') is not None:
            print(f"  粗利率:      {d['gross_margin']*100:.1f}%")
        if d.get('roe') is not None:
            print(f"  ROE:         {d['roe']*100:.1f}%")
        if d.get('fcf') is not None:
            print(f"  FCF:         ${d['fcf']/1e9:.2f}B")
