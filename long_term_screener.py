"""
ALMANAC v4.0 - 長期投資スクリーナー（拡張版）

対象: 約90銘柄（US 全セクター + 日本株 非テック）
スコアリング: 10指標 / 最大160点
  EPS成長 25pt / ROE 20pt / 売上成長 15pt / 粗利率 15pt
  FCF利回り 15pt / PEG比率 15pt / アナリスト 15pt
  テクニカル 10pt / 優先セクターボーナス 10pt
  インサイダー・自社株買いボーナス 5pt
実行: 毎週日曜 7:00（crontab）
"""

import json
import os
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np
import requests
import yfinance as yf
from almanac.runtime_config import get_env
from utils import init_yfinance_timeout

init_yfinance_timeout()

warnings.filterwarnings('ignore')

BASE_DIR = Path(__file__).parent

# ============================================================
# スクリーニング基準
# ============================================================

CRITERIA = {
    'eps_growth_min':    0.12,   # EPS成長率 ≥ 12%
    'roe_min':           0.12,   # ROE ≥ 12%
    'revenue_growth_min':0.08,   # 売上成長率 ≥ 8%
    'gross_margin_min':  0.30,   # 粗利率 ≥ 30%（モート指標）
    'fcf_yield_min':     0.02,   # FCF利回り ≥ 2%
    'peg_max':           3.0,    # PEG ≤ 3.0
    'pe_max':            60.0,   # PER ≤ 60
    'debt_equity_max':   1.5,    # D/E ≤ 1.5
    'market_cap_min':    5e9,    # 時価総額 ≥ 50億ドル
    'analyst_buy_min':   0.60,   # アナリストBuy ≥ 60%
}

# テック集中解消 優先セクター
PRIORITY_SECTORS = {
    'Healthcare', 'Financial Services', 'Consumer Defensive',
    'Industrials', 'Basic Materials', 'Energy', 'Utilities',
    'Real Estate', 'Consumer Cyclical', 'Communication Services',
}

# ============================================================
# ウォッチリスト（tickers.json + long_term_meta.json から動的ロード）
# ============================================================
# 旧: ハードコード 90 銘柄を本ファイル内で管理
# 新（2026-04-26）: tickers.json["long_term_universe"] が銘柄リスト、
#                   long_term_meta.json が name/sector/note メタデータ。
#                   expand_tickers.py でメンテ・拡張可能。

def _load_watchlist() -> dict[str, dict]:
    """tickers.json + long_term_meta.json から WATCHLIST を再構築。
    tickers.json["long_term_universe"] にあって meta に無い銘柄は最小エントリで補完。
    """
    tickers_path = BASE_DIR / "tickers.json"
    meta_path    = BASE_DIR / "long_term_meta.json"
    try:
        with open(tickers_path) as f:
            universe = json.load(f).get("long_term_universe", [])
    except Exception as e:
        print(f"⚠️  tickers.json 読込失敗: {e} — 空 WATCHLIST")
        return {}
    meta: dict = {}
    if meta_path.exists():
        try:
            with open(meta_path) as f:
                meta = json.load(f)
        except Exception as e:
            print(f"⚠️  long_term_meta.json 読込失敗: {e}")
    out: dict[str, dict] = {}
    for ticker in universe:
        out[ticker] = meta.get(ticker, {"name": ticker, "sector": "Unknown", "note": ""})
    return out


WATCHLIST = _load_watchlist()


# ============================================================
# データ取得
# ============================================================

def _calc_rsi(close: 'pd.Series', period: int = 14) -> float:
    delta    = close.diff()
    gain     = delta.clip(lower=0)
    loss     = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs  = avg_gain / avg_loss.replace(0, float('nan'))
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1]) if not rsi.empty else 50.0


def _get_fundamentals(ticker: str) -> Optional[dict]:
    """ファンダメンタル + テクニカルを統合取得"""
    try:
        t    = yf.Ticker(ticker)
        info = t.info

        if not info or (info.get('regularMarketPrice') is None and
                        info.get('currentPrice') is None):
            return None

        price = info.get('regularMarketPrice') or info.get('currentPrice') or 0

        # ---- EPS成長率 ----
        eps_trailing = info.get('trailingEps')
        eps_forward  = info.get('forwardEps')
        eps_growth   = None
        if eps_trailing and eps_forward and eps_trailing > 0:
            eps_growth = (eps_forward - eps_trailing) / abs(eps_trailing)
        # 5年EPS成長率（あれば）
        eps_growth_5y = info.get('earningsGrowth') or eps_growth

        # ---- 売上成長率 ----
        rev_growth = info.get('revenueGrowth')   # YoY
        rev_growth_3y = info.get('revenueGrowth')   # 同上（yfinanceには3年平均なし）

        # ---- 利益率 ----
        gross_margin = info.get('grossMargins')
        operating_margin = info.get('operatingMargins')
        net_margin   = info.get('profitMargins')

        # ---- ROE / ROIC ----
        roe  = info.get('returnOnEquity')
        roa  = info.get('returnOnAssets')

        # ---- D/E 比率 ----
        debt_equity = info.get('debtToEquity')
        try:
            debt_equity = float(debt_equity) / 100 if debt_equity is not None else None
        except (TypeError, ValueError):
            debt_equity = None

        # ---- バリュエーション ----
        pe_trailing  = info.get('trailingPE')
        pe_forward   = info.get('forwardPE')
        pe_used      = pe_forward or pe_trailing

        # PEG = PER / EPS成長率(%)
        peg = None
        if pe_used and eps_growth and eps_growth > 0:
            peg = pe_used / (eps_growth * 100)
        peg_ratio = info.get('pegRatio') or peg

        # EV/EBITDA
        ev_ebitda = info.get('enterpriseToEbitda')

        # ---- FCF利回り ----
        fcf           = info.get('freeCashflow')
        market_cap    = info.get('marketCap')
        fcf_yield     = (fcf / market_cap) if (fcf and market_cap and market_cap > 0) else None

        # ── SEC EDGAR フォールバック（yfinance が None を返したキーを補完）──
        _edgar_missing = (
            eps_growth is None or rev_growth is None or
            gross_margin is None or roe is None or fcf is None
        )
        if _edgar_missing and not ticker.endswith('.T'):
            try:
                from edgar_fetcher import get_edgar_financials
                ed = get_edgar_financials(ticker)
                if ed.get('source') in ('edgar', 'cache'):
                    if eps_growth is None and ed.get('eps_growth') is not None:
                        eps_growth   = ed['eps_growth']
                        eps_growth_5y = eps_growth_5y or eps_growth
                    if rev_growth is None and ed.get('rev_growth') is not None:
                        rev_growth   = ed['rev_growth']
                        rev_growth_3y = rev_growth
                    if gross_margin is None and ed.get('gross_margin') is not None:
                        gross_margin = ed['gross_margin']
                    if roe is None and ed.get('roe') is not None:
                        roe          = ed['roe']
                    if fcf is None and ed.get('fcf') is not None:
                        fcf = ed['fcf']
                        if market_cap and market_cap > 0:
                            fcf_yield = fcf / market_cap
            except Exception:
                pass   # EDGAR 失敗時は yfinance 値のみで続行

        # ---- アナリスト評価 ----
        reco_mean = info.get('recommendationMean')   # 1=Strong Buy, 5=Sell
        buy_pct   = None
        if reco_mean is not None:
            buy_pct = max(0.0, min(1.0, (3.5 - reco_mean) / 2.0))
        analyst_count = info.get('numberOfAnalystOpinions', 0) or 0

        # ---- 配当 ----
        div_yield    = info.get('dividendYield')
        div_rate     = info.get('dividendRate')
        payout_ratio = info.get('payoutRatio')

        # ---- テクニカル ----
        hist = t.history(period='1y')
        rsi  = None
        ma200_pct = None   # 現値 vs MA200（%）
        mom_3m    = None   # 3ヶ月モメンタム
        mom_6m    = None   # 6ヶ月モメンタム
        vol_ann   = None   # 年率ボラティリティ

        if not hist.empty and len(hist) >= 50:
            close    = hist['Close'].dropna()
            rsi      = _calc_rsi(close)
            if len(close) >= 200:
                _ma200_val = close.rolling(200).mean().iloc[-1]
                if not np.isnan(float(_ma200_val)) and float(_ma200_val) > 0:
                    ma200     = float(_ma200_val)
                    ma200_pct = (float(close.iloc[-1]) / ma200 - 1) * 100
            if len(close) >= 63:
                mom_3m = (float(close.iloc[-1]) / float(close.iloc[-63]) - 1) * 100
            if len(close) >= 126:
                mom_6m = (float(close.iloc[-1]) / float(close.iloc[-126]) - 1) * 100
            rets  = close.pct_change().dropna()
            vol_ann = float(rets.std() * np.sqrt(252) * 100)

        # ---- インサイダー・自社株買い（プロキシ） ----
        # yfinanceにbuyback情報はないので代替としてbeta・shares変化を使用
        beta            = info.get('beta')
        shares_outstanding = info.get('sharesOutstanding')
        float_shares    = info.get('floatShares')
        insider_pct     = info.get('heldPercentInsiders')

        return {
            'ticker':          ticker,
            'name':            info.get('longName', ticker),
            'sector':          info.get('sector', 'Other'),
            'industry':        info.get('industry', ''),
            'currency':        info.get('currency', 'USD'),
            'price':           price,
            'market_cap':      market_cap,
            # 成長性
            'eps_growth':      eps_growth,
            'eps_growth_5y':   eps_growth_5y,
            'rev_growth':      rev_growth,
            # 収益性
            'roe':             roe,
            'roa':             roa,
            'gross_margin':    gross_margin,
            'operating_margin':operating_margin,
            'net_margin':      net_margin,
            # バリュエーション
            'pe_trailing':     pe_trailing,
            'pe_forward':      pe_forward,
            'peg_ratio':       peg_ratio,
            'ev_ebitda':       ev_ebitda,
            'fcf_yield':       fcf_yield,
            # 財務健全性
            'debt_equity':     debt_equity,
            # アナリスト
            'buy_pct':         buy_pct,
            'reco_mean':       reco_mean,
            'analyst_count':   analyst_count,
            # 配当
            'div_yield':       div_yield,
            'div_rate':        div_rate,
            'payout_ratio':    payout_ratio,
            # テクニカル
            'rsi':             rsi,
            'ma200_pct':       ma200_pct,
            'mom_3m':          mom_3m,
            'mom_6m':          mom_6m,
            'vol_ann':         vol_ann,
            'beta':            beta,
            # その他
            'insider_pct':     insider_pct,
            '52w_high':        info.get('fiftyTwoWeekHigh'),
            '52w_low':         info.get('fiftyTwoWeekLow'),
            'note':            WATCHLIST.get(ticker, {}).get('note', ''),
        }
    except Exception as e:
        print(f'    データ取得エラー ({ticker}): {e}')
        return None


# ============================================================
# スコアリング（最大 160 点）
# ============================================================

def _score_ticker(data: dict, priority_sector: bool = False) -> dict:
    """
    10指標でスコアリング。内訳を dict で返す。
    """
    breakdown = {}

    # 1. EPS成長率（25点）
    eps = data.get('eps_growth') or data.get('eps_growth_5y') or 0
    s_eps = min(25, max(0, 25 * eps / 0.25)) if eps > 0 else 0
    breakdown['EPS成長'] = round(s_eps, 1)

    # 2. ROE（20点）
    roe = data.get('roe') or 0
    s_roe = min(20, max(0, 20 * roe / 0.30)) if roe > 0 else 0
    breakdown['ROE'] = round(s_roe, 1)

    # 3. 売上成長率（15点）
    rev = data.get('rev_growth') or 0
    s_rev = min(15, max(0, 15 * rev / 0.20)) if rev > 0 else 0
    breakdown['売上成長'] = round(s_rev, 1)

    # 4. 粗利率 ≥ 30%（15点）— モート（競争優位）指標
    gm = data.get('gross_margin') or 0
    if gm >= 0.70:
        s_gm = 15
    elif gm >= 0.50:
        s_gm = 12
    elif gm >= 0.40:
        s_gm = 9
    elif gm >= 0.30:
        s_gm = 6
    else:
        s_gm = 0
    breakdown['粗利率'] = round(s_gm, 1)

    # 5. FCF利回り（15点）
    fcf_y = data.get('fcf_yield') or 0
    s_fcf = min(15, max(0, 15 * fcf_y / 0.06)) if fcf_y > 0 else 0
    breakdown['FCF利回り'] = round(s_fcf, 1)

    # 6. PEG比率（15点）— 低いほど良い
    peg = data.get('peg_ratio')
    if peg is not None and peg > 0:
        if peg <= 1.0:
            s_peg = 15
        elif peg <= 1.5:
            s_peg = 12
        elif peg <= 2.0:
            s_peg = 9
        elif peg <= 2.5:
            s_peg = 6
        elif peg <= 3.0:
            s_peg = 3
        else:
            s_peg = 0
    else:
        # PEGなしの場合はPERで代替
        pe = data.get('pe_forward') or data.get('pe_trailing') or 999
        s_peg = 10 if pe <= 20 else (7 if pe <= 30 else (4 if pe <= 40 else 0))
    breakdown['PEG'] = round(s_peg, 1)

    # 7. アナリスト評価（15点）
    buy = data.get('buy_pct') or 0
    s_ana = min(15, max(0, 15 * buy)) if buy >= CRITERIA['analyst_buy_min'] else min(8, 15 * buy)
    breakdown['アナリスト'] = round(s_ana, 1)

    # 8. テクニカル（10点）
    # RSI 40-65 が理想（過熱でも売られすぎでもない）
    # MA200比 > 0% で上昇トレンド
    # 3ヶ月モメンタム > 0%
    rsi     = data.get('rsi') or 50
    ma200p  = data.get('ma200_pct') or 0
    mom3m   = data.get('mom_3m') or 0

    s_tech = 0
    if 35 <= rsi <= 70:
        s_tech += 4     # 適切なRSI
    elif rsi < 35:
        s_tech += 3     # 売られすぎ（逆張り機会）
    if ma200p > 0:
        s_tech += 3     # 200日線上
    if mom3m > 0:
        s_tech += 3     # 3ヶ月上昇トレンド
    s_tech = min(10, s_tech)
    breakdown['テクニカル'] = round(s_tech, 1)

    # 9. 優先セクターボーナス（10点）
    s_sector = 10 if priority_sector else 0
    breakdown['セクターボーナス'] = s_sector

    # 10. インサイダー・FCF活用ボーナス（最大10点）
    s_bonus = 0
    insider = data.get('insider_pct') or 0
    if insider >= 0.10:
        s_bonus += 5    # インサイダー保有 ≥ 10%
    elif insider >= 0.05:
        s_bonus += 3
    # 自社株買い proxy: FCF余剰があり配当性向が低い
    payout  = data.get('payout_ratio') or 1.0
    fcf_y2  = data.get('fcf_yield') or 0
    if fcf_y2 > 0.04 and payout < 0.40:
        s_bonus += 5    # FCF豊富かつ低配当性向 → 積極的な還元余力
    s_bonus = min(10, s_bonus)
    breakdown['ボーナス'] = s_bonus

    total = sum(breakdown.values())
    breakdown['合計'] = round(total, 1)
    return breakdown


# ============================================================
# フィルタリング
# ============================================================

def _passes_filter(data: dict) -> tuple[bool, list[str]]:
    """スクリーニング基準チェック。通過しなかった理由リストを返す。"""
    reasons = []

    mc = data.get('market_cap')
    if mc and mc < CRITERIA['market_cap_min']:
        reasons.append(f'時価総額 ${mc/1e9:.1f}B < ${CRITERIA["market_cap_min"]/1e9:.0f}B')

    pe = data.get('pe_forward') or data.get('pe_trailing')
    if pe and pe > CRITERIA['pe_max']:
        reasons.append(f'PER {pe:.0f} > {CRITERIA["pe_max"]:.0f}')

    de = data.get('debt_equity')
    if de and de > CRITERIA['debt_equity_max']:
        reasons.append(f'D/E {de:.1f} > {CRITERIA["debt_equity_max"]:.1f}')

    eps_ok = (data.get('eps_growth') or 0) >= CRITERIA['eps_growth_min']
    rev_ok = (data.get('rev_growth') or 0) >= CRITERIA['revenue_growth_min']
    roe_ok = (data.get('roe') or 0) >= CRITERIA['roe_min']
    fcf_ok = (data.get('fcf_yield') or 0) >= CRITERIA['fcf_yield_min']

    if not (eps_ok or rev_ok or roe_ok or fcf_ok):
        reasons.append('EPS・売上・ROE・FCFいずれも基準未達')

    return len(reasons) == 0, reasons


# ============================================================
# セクター内ランキング
# ============================================================

def _add_sector_ranks(results: list[dict]) -> list[dict]:
    """セクター内でのスコア順位を付与する。"""
    from collections import defaultdict
    sectors: dict = defaultdict(list)
    for r in results:
        sectors[r['sector']].append(r)

    for items in sectors.values():
        items.sort(key=lambda x: -x['score'])
        for rank, item in enumerate(items, 1):
            item['sector_rank']  = rank
            item['sector_total'] = len(items)

    return results


# ============================================================
# メインスクリーニング
# ============================================================

def run_screening(
    tickers: Optional[list] = None,
    top_n:   int   = 20,
    delay:   float = 1.0,
) -> dict:
    """
    長期投資候補をスクリーニングする。

    Returns
    -------
    dict:
        passed              : 通過銘柄リスト（スコア順）
        rejected            : 不通過銘柄リスト
        watchlist_by_sector : セクター別分類
        as_of               : 実行日時
    """
    from insider_restrictions import filter_allowed_tickers
    target = filter_allowed_tickers(tickers or list(WATCHLIST.keys()))
    print(f'スクリーニング開始: {len(target)}銘柄')
    print(f'基準: 時価総額≥${CRITERIA["market_cap_min"]/1e9:.0f}B / PER≤{CRITERIA["pe_max"]:.0f} / D/E≤{CRITERIA["debt_equity_max"]:.1f}')

    passed   = []
    rejected = []

    for i, ticker in enumerate(target, 1):
        print(f'  [{i:02d}/{len(target)}] {ticker}...', end=' ', flush=True)
        data = _get_fundamentals(ticker)
        if data is None:
            print('データなし')
            time.sleep(delay)
            continue

        watch_info      = WATCHLIST.get(ticker, {})
        sector          = data.get('sector') or watch_info.get('sector', 'Other')
        priority_sector = sector in PRIORITY_SECTORS

        ok, reasons  = _passes_filter(data)
        breakdown    = _score_ticker(data, priority_sector)
        score        = breakdown['合計']

        result = {
            **data,
            'score':           score,
            'score_breakdown': breakdown,
            'priority_sector': priority_sector,
            'passes_filter':   ok,
            'filter_reasons':  reasons,
            'note':            watch_info.get('note', data.get('note', '')),
        }

        if ok:
            passed.append(result)
            print(f'✅ {score:.0f}pt  ROE:{(data.get("roe") or 0)*100:.0f}%  PEG:{data.get("peg_ratio") or "N/A"}')
        else:
            rejected.append(result)
            print(f'✗  {"; ".join(reasons[:2])}')

        time.sleep(delay)

    # スコア順ソート
    passed.sort(key=lambda x: -x['score'])
    rejected.sort(key=lambda x: -x['score'])

    # セクター内ランキング付与
    all_results = _add_sector_ranks(passed + rejected)
    passed   = [r for r in all_results if r['passes_filter']]
    rejected = [r for r in all_results if not r['passes_filter']]

    # セクター別サマリー
    by_sector: dict = {}
    for item in passed + rejected:
        s = item.get('sector', 'Other')
        by_sector.setdefault(s, [])
        by_sector[s].append({
            'ticker':       item['ticker'],
            'name':         item['name'],
            'score':        item['score'],
            'passes':       item['passes_filter'],
            'price':        item.get('price'),
            'sector_rank':  item.get('sector_rank'),
            'sector_total': item.get('sector_total'),
            'note':         item.get('note', ''),
        })
    for s in by_sector:
        by_sector[s].sort(key=lambda x: -x['score'])

    result = {
        'passed':              passed[:top_n],
        'rejected':            rejected[:10],   # 惜しくも落ちた上位10件も保存
        'rejected_count':      len(rejected),
        'total_screened':      len(target),
        'watchlist_by_sector': by_sector,
        'criteria':            CRITERIA,
        'as_of':               datetime.now().strftime('%Y-%m-%d %H:%M'),
    }

    return result


# ============================================================
# 短期→長期 転換候補
# ============================================================

def find_upgrade_candidates(holdings_path: Optional[Path] = None) -> list:
    path = holdings_path or (BASE_DIR / 'holdings.json')
    if not path.exists():
        return []

    with open(path, encoding='utf-8') as f:
        holdings = json.load(f)

    candidates = []
    skip = {'SLIM_SP500', 'SLIM_ORCAN', 'MNXACT', 'IFREE_FANGPLUS', 'NOMURA_SEMI'}
    for key, info in holdings.items():
        if key in skip:
            continue
        if info.get('investment_type') not in ('swing', None):
            continue
        ticker = info.get('ticker', key)
        from insider_restrictions import is_restricted_ticker
        if is_restricted_ticker(ticker):
            continue
        data   = _get_fundamentals(ticker)
        if not data:
            continue
        ok, _ = _passes_filter(data)
        if ok:
            breakdown = _score_ticker(data, data.get('sector', '') in PRIORITY_SECTORS)
            candidates.append({
                'key':          key,
                'ticker':       ticker,
                'name':         info.get('name', ticker),
                'score':        breakdown['合計'],
                'current_type': info.get('investment_type', 'swing'),
                'reason':       '長期投資基準を満たしています。medium/longへの転換を検討。',
            })

    candidates.sort(key=lambda x: -x['score'])
    return candidates


# ============================================================
# 保存・ロード
# ============================================================

RESULTS_FILE = BASE_DIR / 'long_term_screen_results.json'

def save_results(results: dict):
    from insider_restrictions import filter_signal_records
    results = dict(results)
    results['passed'] = filter_signal_records(results.get('passed', []))
    results['rejected'] = filter_signal_records(results.get('rejected', []))
    results['watchlist_by_sector'] = {
        sector: filter_signal_records(rows)
        for sector, rows in (results.get('watchlist_by_sector') or {}).items()
    }
    with open(RESULTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f'結果保存: {RESULTS_FILE.name}')


def load_results() -> Optional[dict]:
    if RESULTS_FILE.exists():
        with open(RESULTS_FILE, encoding='utf-8') as f:
            return json.load(f)
    return None


# ============================================================
# Telegram 通知
# ============================================================

def send_screening_alert(results: dict):
    token   = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        return

    from insider_restrictions import filter_signal_records
    passed = filter_signal_records(results.get('passed', []))
    total  = results.get('total_screened', 0)
    if not passed:
        return

    sector_icons = {
        'Healthcare': '💊', 'Financial Services': '🏦',
        'Consumer Defensive': '🛒', 'Industrials': '🏭',
        'Energy': '⛽', 'Basic Materials': '⚗️',
        'Consumer Cyclical': '🛍️', 'Communication Services': '📡',
        'Utilities': '⚡', 'Real Estate': '🏢',
    }

    lines = [
        f'📊 <b>週次 長期投資スクリーニング</b>',
        f'実行日時: {results["as_of"]}',
        f'通過: <b>{len(passed)}件</b> / {total}件スクリーニング',
        '━' * 16,
        '<b>TOP5候補</b>',
    ]
    for p in passed[:5]:
        icon = sector_icons.get(p.get('sector', ''), '📈')
        roe  = (p.get('roe') or 0) * 100
        peg  = p.get('peg_ratio')
        peg_str = f'{peg:.1f}' if peg else 'N/A'
        gm   = (p.get('gross_margin') or 0) * 100
        fcfy = (p.get('fcf_yield') or 0) * 100
        lines.append(
            f'\n{icon} <b>{p["ticker"]}</b>（{p.get("name", "")}）\n'
            f'   スコア:{p["score"]:.0f}pt  ROE:{roe:.0f}%  PEG:{peg_str}\n'
            f'   粗利率:{gm:.0f}%  FCF利回:{fcfy:.1f}%\n'
            f'   {p.get("note", "")}'
        )

    # セクター別TOP1
    lines.append('\n━' * 16)
    lines.append('<b>セクター別No.1</b>')
    for sector, items in sorted(results['watchlist_by_sector'].items()):
        if items and items[0]['passes']:
            icon = sector_icons.get(sector, '📈')
            t    = items[0]
            lines.append(f'{icon} {sector[:12]}: <b>{t["ticker"]}</b>（{t["score"]:.0f}pt）')

    msg = '\n'.join(lines)
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    try:
        requests.post(url, data={'chat_id': chat_id, 'text': msg,
                                 'parse_mode': 'HTML'}, timeout=10)
    except Exception:
        pass


# ============================================================
# Batch API — AI 投資テーゼ評価（週次・非同期・50%割引）
# ============================================================

_BATCH_STATE_FILE = BASE_DIR / 'long_term_batch_state.json'
SONNET_MODEL_ID = "claude-sonnet-5"
HAIKU_MODEL_ID = "claude-haiku-4-5-20251001"


def _append_llm_call_log(row: dict) -> None:
    try:
        from analyst.llm_client import _append_llm_call_log as _append
        _append(row)
    except Exception:
        pass


def _log_anthropic_usage(
    *,
    role: str,
    model: str,
    max_tokens: int,
    started: float,
    prompt_chars: int,
    response=None,
    status: str = "ok",
    use_tool: bool = True,
    error: Exception | None = None,
    **extra,
) -> None:
    usage = getattr(response, "usage", None)
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": model,
        "use_tool": use_tool,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": status,
        **extra,
    }
    if response is not None:
        row.update({
            "stop_reason": getattr(response, "stop_reason", None),
            "content_types": [getattr(block, "type", None) for block in getattr(response, "content", [])],
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        })
    if error is not None:
        row.update({
            "error_type": type(error).__name__,
            "error": str(error)[:500],
        })
    _append_llm_call_log(row)


def _log_anthropic_batch_usage(
    *,
    role: str,
    batch_id: str | None,
    status: str,
    started: float,
    **extra,
) -> None:
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": HAIKU_MODEL_ID,
        "use_tool": False,
        "batch": True,
        "max_tokens": 300,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "batch_id": batch_id,
        "status": status,
        **extra,
    }
    _append_llm_call_log(row)


def _log_adapter_usage(
    *,
    role: str,
    result: dict,
    started: float,
    prompt_chars: int,
    max_tokens: int,
    status: str | None = None,
    **extra,
) -> None:
    usage = result.get("usage") or {}
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "role": role,
        "model": result.get("model"),
        "adapter": result.get("adapter"),
        "use_tool": False,
        "max_tokens": max_tokens,
        "elapsed_sec": round(time.monotonic() - started, 2),
        "prompt_chars": prompt_chars,
        "status": status or ("error" if result.get("error") else "ok"),
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        **extra,
    }
    if result.get("error"):
        row["error"] = str(result.get("error"))[:500]
        if not usage:
            row["cost_usd"] = 0.0
    _append_llm_call_log(row)


def _extract_message_usage(message) -> tuple[int | None, int | None]:
    usage = getattr(message, "usage", None)
    return (
        getattr(usage, "input_tokens", None),
        getattr(usage, "output_tokens", None),
    )


def _fmt_candidates_for_thesis(candidates: list) -> str:
    """通過銘柄一覧テキスト（Sonnet×3共通入力）"""
    lines = []
    for i, c in enumerate(candidates, 1):
        roe  = f"{(c.get('roe') or 0)*100:.1f}%"
        eps  = f"{(c.get('eps_growth') or 0)*100:.1f}%"
        rev  = f"{(c.get('rev_growth') or 0)*100:.1f}%"
        gm   = f"{(c.get('gross_margin') or 0)*100:.1f}%"
        fcfy = f"{(c.get('fcf_yield') or 0)*100:.1f}%"
        peg  = c.get('peg_ratio')
        lines.append(
            f"{i}. {c['ticker']} ({c.get('name','')}) [{c.get('sector','不明')}]"
            f" スコア:{c.get('score',0):.0f}pt"
            f" ROE:{roe} EPS成長:{eps} 売上成長:{rev}"
            f" 粗利率:{gm} FCF利回:{fcfy} PEG:{f'{peg:.2f}' if peg else 'N/A'}"
        )
    return "\n".join(lines)


# Tool Use スキーマ: Sonnet の視点出力（長期）
_THESIS_VIEW_TOOL = {
    "name": "submit_thesis_views",
    "description": "各銘柄への分析視点をリストで提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "views": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "point": {"type": "string", "description": "60字以内の重要ポイント"}
                    },
                    "required": ["ticker", "point"]
                }
            }
        },
        "required": ["views"]
    }
}

# Tool Use スキーマ: Sonnet の最終テーゼ出力
_THESIS_FINAL_TOOL = {
    "name": "submit_thesis",
    "description": "各銘柄の投資テーゼをリストで提出する",
    "input_schema": {
        "type": "object",
        "properties": {
            "theses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ticker": {"type": "string"},
                        "thesis": {"type": "string", "description": "投資テーゼ2文（成長ドライバー＋リスク）"},
                        "bull_point": {"type": "string", "description": "最大の強気ポイント30字"},
                        "bear_point": {"type": "string", "description": "最大のリスク30字"}
                    },
                    "required": ["ticker", "thesis"]
                }
            }
        },
        "required": ["theses"]
    }
}


_PREDEBATE_SYSTEM = (
    "あなたは ALMANAC の統合長期投資アナリストです。"
    "各銘柄について 3 つの視点（成長性・リスク・マクロ/セクター）を順番に評価し、"
    "最後に投資テーゼに繋がるサマリを提出してください。"
    "必ず純粋な JSON のみを出力（解説文・markdown禁止）。"
)

_PREDEBATE_USER_TEMPLATE = """
【通過銘柄】
{ctx}

各銘柄について以下の3視点をそれぞれ60字以内で生成してください:
  growth_view: 最大の成長ドライバー・競争優位性
  risk_view:   最大のリスク要因（競合・バリュエーション・事業リスク）
  macro_view:  セクタートレンド・金利・地政学との適合性

出力 JSON 形式:
{{
  "perspectives": [
    {{
      "ticker": "AAPL",
      "growth_view": "...",
      "risk_view":   "...",
      "macro_view":  "..."
    }}
  ]
}}
"""


def _call_deepseek_predebate(candidates: list) -> list:
    """DeepSeek V4 単一コールで成長/リスク/マクロ 3 視点を一括生成。"""
    try:
        from llm_adapters import call_deepseek
    except Exception as e:
        print(f"  [DeepSeek predebate] 未利用 (import failure): {e}")
        return []

    ctx  = _fmt_candidates_for_thesis(candidates)
    user = _PREDEBATE_USER_TEMPLATE.format(ctx=ctx)
    try:
        started = time.monotonic()
        res = call_deepseek(
            _PREDEBATE_SYSTEM, user,
            max_tokens=4000, temperature=0.3, json_mode=True,
        )
        _log_adapter_usage(
            role="long_term_predebate_deepseek",
            result=res,
            started=started,
            prompt_chars=len(_PREDEBATE_SYSTEM) + len(user),
            max_tokens=4000,
            candidate_count=len(candidates),
        )
        if res.get("error"):
            print(f"  [DeepSeek predebate] error: {res.get('error')}")
            return []
        content = res.get("content", "")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # truncate fallback
            from utils import _extract_json  # type: ignore
            data = _extract_json(content) or {}
        return data.get("perspectives", []) if isinstance(data, dict) else []
    except Exception as e:
        _append_llm_call_log({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "role": "long_term_predebate_deepseek",
            "model": "deepseek",
            "adapter": "deepseek",
            "use_tool": False,
            "max_tokens": 4000,
            "elapsed_sec": round(time.monotonic() - started, 2) if "started" in locals() else 0.0,
            "prompt_chars": len(_PREDEBATE_SYSTEM) + len(user),
            "status": "error",
            "candidate_count": len(candidates),
            "error_type": type(e).__name__,
            "error": str(e)[:500],
            "cost_usd": 0.0,
        })
        print(f"  [DeepSeek predebate] 失敗: {e}")
        return []


def _generate_debate_thesis(candidates: list) -> dict:
    """
    新ハーネス: DeepSeek V4 単一コール（成長/リスク/マクロ 3 視点予選）→ Sonnet 統合。
    全通過銘柄をまとめて処理（最大 2 API呼び出し）。
    Returns: {ticker: {"thesis": str, "bull_point": str, "bear_point": str}}
    """
    import anthropic

    ctx = _fmt_candidates_for_thesis(candidates)

    print(f"  🤖 DeepSeek V4 予選: {len(candidates)}件の3視点（成長性・リスク・マクロ）一括分析中...")
    perspectives = _call_deepseek_predebate(candidates)
    if not perspectives:
        print("  ⚠️  DeepSeek 予選失敗 → Sonnet ×3 レガシーへフォールバック")
        return _legacy_sonnet_debate_thesis(candidates)

    # DeepSeek の出力を 3 視点リストに整形
    views_growth: list = []
    views_risk:   list = []
    views_macro:  list = []
    for p in perspectives:
        if not isinstance(p, dict) or not p.get("ticker"):
            continue
        t = p["ticker"]
        if p.get("growth_view"):
            views_growth.append({"ticker": t, "point": p.get("growth_view", "")})
        if p.get("risk_view"):
            views_risk.append({"ticker": t,   "point": p.get("risk_view",   "")})
        if p.get("macro_view"):
            views_macro.append({"ticker": t,  "point": p.get("macro_view",  "")})

    def _fmt_views(views: list, label: str) -> str:
        if not views:
            return f"{label}: データなし"
        return f"{label}:\n" + "\n".join(
            f"  {v.get('ticker','?')}: {v.get('point','')}" for v in views
        )

    synthesis_prompt = (
        f"{ctx}\n\n"
        f"{_fmt_views(views_growth, '【成長性視点】')}\n\n"
        f"{_fmt_views(views_risk,   '【リスク視点】')}\n\n"
        f"{_fmt_views(views_macro,  '【マクロ視点】')}\n\n"
        "上記3視点を統合し、各銘柄の投資テーゼを生成してください。"
        "1文目は主な成長ドライバー、2文目は主なリスクを記述してください（各文60字以内）。"
    )

    print("  🎯 Sonnet 最終テーゼ統合中...")
    result: dict = {}
    try:
        client = anthropic.Anthropic()
        started = time.monotonic()
        resp = client.messages.create(
            model=SONNET_MODEL_ID,
            max_tokens=1500,
            system=[{"type": "text", "text": "あなたは長期投資の専門アナリストです。複数の視点を統合し、簡潔で具体的な投資テーゼを作成します。", "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": synthesis_prompt}],
            tools=[_THESIS_FINAL_TOOL],
            tool_choice={"type": "tool", "name": "submit_thesis"},
        )
        _log_anthropic_usage(
            role="long_term_thesis_synthesis",
            model=SONNET_MODEL_ID,
            max_tokens=1500,
            started=started,
            prompt_chars=len(synthesis_prompt),
            response=resp,
            candidate_count=len(candidates),
            harness="deepseek_predebate",
        )
        for block in resp.content:
            if block.type == "tool_use":
                for t in block.input.get("theses", []):
                    result[t["ticker"]] = {
                        "thesis":      t.get("thesis", ""),
                        "bull_point":  t.get("bull_point", ""),
                        "bear_point":  t.get("bear_point", ""),
                        "_source":     "deepseek_v4_predebate + sonnet_synthesis",
                    }
    except Exception as e:
        _log_anthropic_usage(
            role="long_term_thesis_synthesis",
            model=SONNET_MODEL_ID,
            max_tokens=1500,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(synthesis_prompt),
            status="error",
            error=e,
            candidate_count=len(candidates),
            harness="deepseek_predebate",
        )
        print(f"  [Sonnet synthesis] エラー: {e}")

    return result


def _legacy_sonnet_debate_thesis(candidates: list) -> dict:
    """旧ハーネス: Sonnet×3 並列予選 → Sonnet 統合（DeepSeek 失敗時のフォールバック）。"""
    from concurrent.futures import ThreadPoolExecutor
    import anthropic

    client = anthropic.Anthropic()
    ctx = _fmt_candidates_for_thesis(candidates)

    def _sonnet_view(system_text: str, instruction: str) -> list:
        try:
            started = time.monotonic()
            resp = client.messages.create(
                model=SONNET_MODEL_ID,
                max_tokens=1000,
                system=[{"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": [
                    {"type": "text", "text": ctx, "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": instruction},
                ]}],
                tools=[_THESIS_VIEW_TOOL],
                tool_choice={"type": "tool", "name": "submit_thesis_views"},
            )
            _log_anthropic_usage(
                role="long_term_thesis_view",
                model=SONNET_MODEL_ID,
                max_tokens=1000,
                started=started,
                prompt_chars=len(ctx) + len(instruction),
                response=resp,
                candidate_count=len(candidates),
            )
            for block in resp.content:
                if block.type == "tool_use":
                    return block.input.get("views", [])
        except Exception as e:
            _log_anthropic_usage(
                role="long_term_thesis_view",
                model=SONNET_MODEL_ID,
                max_tokens=1000,
                started=started if "started" in locals() else time.monotonic(),
                prompt_chars=len(ctx) + len(instruction),
                status="error",
                error=e,
                candidate_count=len(candidates),
            )
            print(f"  [Sonnet thesis legacy] エラー: {e}")
        return []

    with ThreadPoolExecutor(max_workers=3) as ex:
        fa = ex.submit(_sonnet_view,
            "あなたは成長株投資の専門アナリストです。各銘柄の成長ドライバーと競争優位性を評価します。",
            "各銘柄の最大の成長ドライバー・競争優位性を評価し、最も重要なポイントを1点ずつ挙げてください。")
        fb = ex.submit(_sonnet_view,
            "あなたはリスク管理の専門アナリストです。各銘柄のダウンサイドリスクと投資失敗シナリオを評価します。",
            "各銘柄の最大のリスク要因を評価し、最も重要なリスクを1点ずつ挙げてください。")
        fc = ex.submit(_sonnet_view,
            "あなたはマクロ経済・セクター分析の専門アナリストです。各銘柄とマクロ環境・セクタートレンドの適合性を評価します。",
            "各銘柄のセクタートレンド・金利環境・地政学リスクとの適合性を評価し、最も重要なポイントを1点ずつ挙げてください。")
        views_growth = fa.result()
        views_risk   = fb.result()
        views_macro  = fc.result()

    def _fmt_views(views: list, label: str) -> str:
        if not views:
            return f"{label}: データなし"
        return f"{label}:\n" + "\n".join(
            f"  {v.get('ticker','?')}: {v.get('point','')}" for v in views
        )

    synthesis_prompt = (
        f"{ctx}\n\n"
        f"{_fmt_views(views_growth, '【成長性アナリスト】')}\n\n"
        f"{_fmt_views(views_risk,   '【リスクアナリスト】')}\n\n"
        f"{_fmt_views(views_macro,  '【マクロアナリスト】')}\n\n"
        "3人のアナリストの視点を統合し、各銘柄の投資テーゼを生成してください。"
        "1文目は主な成長ドライバー、2文目は主なリスクを記述してください（各文60字以内）。"
    )

    result: dict = {}
    try:
        started = time.monotonic()
        resp = client.messages.create(
            model=SONNET_MODEL_ID,
            max_tokens=1500,
            system=[{"type": "text", "text": "あなたは長期投資の専門アナリストです。複数の視点を統合し、簡潔で具体的な投資テーゼを作成します。", "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": synthesis_prompt}],
            tools=[_THESIS_FINAL_TOOL],
            tool_choice={"type": "tool", "name": "submit_thesis"},
        )
        _log_anthropic_usage(
            role="long_term_thesis_legacy_synthesis",
            model=SONNET_MODEL_ID,
            max_tokens=1500,
            started=started,
            prompt_chars=len(synthesis_prompt),
            response=resp,
            candidate_count=len(candidates),
        )
        for block in resp.content:
            if block.type == "tool_use":
                for t in block.input.get("theses", []):
                    result[t["ticker"]] = {
                        "thesis":      t.get("thesis", ""),
                        "bull_point":  t.get("bull_point", ""),
                        "bear_point":  t.get("bear_point", ""),
                        "_source":     "legacy_sonnet_debate",
                    }
    except Exception as e:
        _log_anthropic_usage(
            role="long_term_thesis_legacy_synthesis",
            model=SONNET_MODEL_ID,
            max_tokens=1500,
            started=started if "started" in locals() else time.monotonic(),
            prompt_chars=len(synthesis_prompt),
            status="error",
            error=e,
            candidate_count=len(candidates),
        )
        print(f"  [Sonnet synthesis legacy] エラー: {e}")

    return result


def _generate_thesis_sync(results_path: Path) -> None:
    """
    AI テーゼを同期生成し long_term_screen_results.json の ai_thesis フィールドを更新する。
    Anthropic API 利用可能時: Sonnet×3並列ディベート → Sonnet統合（全銘柄を4呼び出しで処理）
    フォールバック: Haiku 単体（銘柄ごと個別呼び出し）
    """
    existing = json.loads(results_path.read_text(encoding="utf-8"))
    passed = existing.get("passed", [])
    if not passed:
        print("[Thesis] 通過銘柄が0件のため生成をスキップ")
        return

    # テーゼ未生成の候補のみ対象
    targets = [c for c in passed if not c.get("ai_thesis")]
    if not targets:
        print("[Thesis] 更新なし（既存テーゼ完備）")
        return

    # ── DeepSeek V4 予選 → Sonnet 統合（環境変数で legacy にも切替可） ──
    harness = (get_env("ALMANAC_LONGTERM_HARNESS", "deepseek") or "deepseek").lower()
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            if harness == "legacy":
                print(f"[Thesis] レガシー Sonnet×3 → Sonnet統合（{len(targets)}件）")
                debate_results = _legacy_sonnet_debate_thesis(targets)
            else:
                print(f"[Thesis] DeepSeek V4 予選 → Sonnet統合（{len(targets)}件）")
                debate_results = _generate_debate_thesis(targets)
            updated = 0
            for c in passed:
                r = debate_results.get(c["ticker"])
                if r and r.get("thesis"):
                    c["ai_thesis"]      = r["thesis"]
                    c["ai_bull_point"]  = r.get("bull_point", "")
                    c["ai_bear_point"]  = r.get("bear_point", "")
                    updated += 1
            if updated > 0:
                existing["passed"] = passed
                results_path.write_text(
                    json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"[Thesis] {updated}件のディベートテーゼを保存しました")
                return
        except Exception as e:
            print(f"[Thesis] ディベート失敗、Haikuフォールバックへ: {e}")

    # ── Haiku フォールバック ───────────────────────────────────
    print(f"[Thesis] Haiku 単体モード（{len(targets)}件）")
    system_prompt = "あなたは長期投資アナリストです。簡潔・具体的に答えてください。"

    def _call_one(c: dict) -> str:
        roe  = f"{(c.get('roe') or 0)*100:.1f}%"
        eps  = f"{(c.get('eps_growth') or 0)*100:.1f}%"
        rev  = f"{(c.get('rev_growth') or 0)*100:.1f}%"
        gm   = f"{(c.get('gross_margin') or 0)*100:.1f}%"
        fcfy = f"{(c.get('fcf_yield') or 0)*100:.1f}%"
        peg  = c.get('peg_ratio')
        user_msg = (
            f"銘柄: {c['ticker']} ({c.get('name','')})\n"
            f"セクター: {c.get('sector','不明')}\n"
            f"スコア: {c.get('score',0):.0f}/160pt\n"
            f"ROE:{roe}  EPS成長:{eps}  売上成長:{rev}\n"
            f"粗利率:{gm}  FCF利回:{fcfy}  PEG:{f'{peg:.2f}' if peg else 'N/A'}\n"
            "3〜5年の長期投資家視点で、投資テーゼを日本語2文で述べてください。"
            "1文目は主な成長ドライバー、2文目は主なリスクを記述してください。"
        )
        try:
            import anthropic as _anthropic
            client = _anthropic.Anthropic()
            started = time.monotonic()
            resp = client.messages.create(
                model=HAIKU_MODEL_ID,
                max_tokens=300,
                system=system_prompt,
                messages=[{"role": "user", "content": user_msg}],
            )
            _log_anthropic_usage(
                role="long_term_thesis_haiku",
                model=HAIKU_MODEL_ID,
                max_tokens=300,
                started=started,
                prompt_chars=len(user_msg),
                response=resp,
                use_tool=False,
                ticker=c.get("ticker"),
            )
            return resp.content[0].text.strip()
        except Exception as e:
            _log_anthropic_usage(
                role="long_term_thesis_haiku",
                model=HAIKU_MODEL_ID,
                max_tokens=300,
                started=started if "started" in locals() else time.monotonic(),
                prompt_chars=len(user_msg),
                status="error",
                use_tool=False,
                error=e,
                ticker=c.get("ticker"),
            )
            print(f"  [Haiku] {c['ticker']} エラー: {e}")
        return None

    updated = 0
    for c in passed:
        if c.get("ai_thesis"):
            continue
        print(f"  生成中: {c['ticker']} ({c.get('name', '')})")
        thesis = _call_one(c)
        if thesis:
            c["ai_thesis"] = thesis
            updated += 1
        time.sleep(0.5)

    if updated > 0:
        existing["passed"] = passed
        results_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[Thesis] {updated}件の AI テーゼを生成・保存しました")
    else:
        print("[Thesis] 更新なし（既存テーゼ完備 or 全件エラー）")


def submit_ai_batch(candidates: list) -> Optional[str]:
    """
    通過銘柄に対して Anthropic Batch API で AI 投資テーゼを一括生成する。
    バッチ ID を long_term_batch_state.json に保存して返す。
    通常の同期 API より 50% 安く、最大 24h で完了。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or not candidates:
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        print("[Batch] anthropic パッケージ未インストール → スキップ")
        return None

    requests_list = []
    for c in candidates:
        roe  = f"{(c.get('roe') or 0)*100:.1f}%"
        eps  = f"{(c.get('eps_growth') or 0)*100:.1f}%"
        rev  = f"{(c.get('rev_growth') or 0)*100:.1f}%"
        gm   = f"{(c.get('gross_margin') or 0)*100:.1f}%"
        fcfy = f"{(c.get('fcf_yield') or 0)*100:.1f}%"
        peg  = c.get('peg_ratio')
        peg_str = f"{peg:.2f}" if peg else "N/A"

        user_msg = (
            f"銘柄: {c['ticker']} ({c.get('name','')})\n"
            f"セクター: {c.get('sector','不明')}\n"
            f"スコア: {c.get('score',0):.0f}/160pt\n"
            f"ROE:{roe}  EPS成長:{eps}  売上成長:{rev}\n"
            f"粗利率:{gm}  FCF利回:{fcfy}  PEG:{peg_str}\n"
            f"備考: {c.get('note','')}\n\n"
            "3〜5年の長期投資家視点で、この銘柄の投資テーゼを日本語2文で述べてください。"
            "1文目は主な成長ドライバー、2文目は主なリスクを記述してください。"
        )
        requests_list.append(
            anthropic.types.message_create_params.MessageCreateParamsNonStreaming(
                # Anthropic Batch API: ASCII alnum + _- のみ。日本株 "1489.T" や指数 "^N225" を変換。
                custom_id=c['ticker'].replace('.', '_').replace('^', '_'),
                params={
                    "model":      HAIKU_MODEL_ID,
                    "max_tokens": 300,
                    "system":     "あなたは長期投資アナリストです。簡潔・具体的に答えてください。",
                    "messages":   [{"role": "user", "content": user_msg}],
                }
            )
        )

    try:
        started = time.monotonic()
        batch = client.messages.batches.create(requests=requests_list)
        state = {
            "batch_id":   batch.id,
            "submitted":  datetime.now().isoformat(),
            "tickers":    [c['ticker'] for c in candidates],
            "status":     batch.processing_status,
        }
        _BATCH_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        _log_anthropic_batch_usage(
            role="long_term_thesis_batch_submit",
            batch_id=batch.id,
            status="submitted",
            started=started,
            batch_status=batch.processing_status,
            request_count=len(requests_list),
            tickers=[c["ticker"] for c in candidates],
            cost_usd=0.0,
        )
        print(f"[Batch] バッチ送信完了: {batch.id}  ({len(requests_list)}件)")
        return batch.id
    except Exception as e:
        _log_anthropic_batch_usage(
            role="long_term_thesis_batch_submit",
            batch_id=None,
            status="error",
            started=started if "started" in locals() else time.monotonic(),
            request_count=len(requests_list),
            tickers=[c["ticker"] for c in candidates],
            cost_usd=0.0,
            error_type=type(e).__name__,
            error=str(e)[:500],
        )
        print(f"[Batch] 送信エラー: {e}")
        return None


def poll_ai_batch(results_path: Optional[Path] = None) -> bool:
    """
    long_term_batch_state.json のバッチ ID を確認し、完了していれば
    AI テーゼを long_term_screen_results.json に追記する。

    Returns: True = 完了処理済み / False = まだ処理中 or エラー
    """
    if not _BATCH_STATE_FILE.exists():
        print("[Batch] バッチ状態ファイルなし。先に submit_ai_batch を実行してください。")
        return False

    state = json.loads(_BATCH_STATE_FILE.read_text(encoding="utf-8"))
    batch_id = state.get("batch_id")
    if not batch_id:
        return False

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return False

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        batch = client.messages.batches.retrieve(batch_id)
        print(f"[Batch] {batch_id}  ステータス: {batch.processing_status}")

        if batch.processing_status != "ended":
            print("[Batch] まだ処理中です。後でもう一度実行してください。")
            return False

        # 結果を sanitized_id → テーゼ の dict にまとめる
        # （submit 時に "1489.T" → "1489_T" などサニタイズしているため、ここでも同じキーで持つ）
        def _sanitize_id(t: str) -> str:
            return t.replace('.', '_').replace('^', '_') if t else t

        tickers_by_sid = {_sanitize_id(t): t for t in state.get("tickers", [])}
        ai_thesis: dict = {}
        for result in client.messages.batches.results(batch_id):
            sid = result.custom_id  # 既にサニタイズ済の id
            started = time.monotonic()
            if result.result.type == "succeeded":
                message = result.result.message
                thesis = message.content[0].text.strip()
                ai_thesis[sid] = thesis
                input_tokens, output_tokens = _extract_message_usage(message)
                _log_anthropic_batch_usage(
                    role="long_term_thesis_batch_result",
                    batch_id=batch_id,
                    status="ok",
                    started=started,
                    custom_id=sid,
                    ticker=tickers_by_sid.get(sid, sid),
                    stop_reason=getattr(message, "stop_reason", None),
                    content_types=[getattr(block, "type", None) for block in getattr(message, "content", [])],
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                )
            else:
                ai_thesis[sid] = None
                _log_anthropic_batch_usage(
                    role="long_term_thesis_batch_result",
                    batch_id=batch_id,
                    status=getattr(result.result, "type", "error"),
                    started=started,
                    custom_id=sid,
                    ticker=tickers_by_sid.get(sid, sid),
                    cost_usd=0.0,
                )

        # 既存の screen_results に ai_thesis フィールドを追加（lookup 時にサニタイズ）
        target_path = results_path or RESULTS_FILE
        if target_path.exists():
            existing = json.loads(target_path.read_text(encoding="utf-8"))
            for item in existing.get("passed", []):
                t = item.get("ticker")
                sid = _sanitize_id(t)
                if sid and sid in ai_thesis:
                    item["ai_thesis"] = ai_thesis[sid]
            for item in existing.get("rejected", []):
                t = item.get("ticker")
                sid = _sanitize_id(t)
                if sid and sid in ai_thesis:
                    item["ai_thesis"] = ai_thesis[sid]
            target_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2, default=str))
            print(f"[Batch] AI テーゼを {target_path.name} に追記しました ({len(ai_thesis)}件)")

        # バッチ状態をクリーンアップ
        state["status"] = "ended"
        _BATCH_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return True

    except Exception as e:
        print(f"[Batch] ポーリングエラー: {e}")
        return False


# ============================================================
# CLI
# ============================================================

def _print_results(results: dict):
    passed = results.get('passed', [])
    print(f'\n{"="*60}')
    print(f'長期投資スクリーニング結果  {results["as_of"]}')
    print(f'通過: {len(passed)}件 / スクリーニング: {results["total_screened"]}件 '
          f'/ 不通過: {results["rejected_count"]}件')
    print(f'{"="*60}')

    if not passed:
        print('\n候補銘柄なし')
        return

    print('\n【TOP候補】')
    for i, p in enumerate(passed, 1):
        ps  = '⭐' if p['priority_sector'] else '  '
        roe = (p.get('roe') or 0) * 100
        eps = (p.get('eps_growth') or 0) * 100
        rev = (p.get('rev_growth') or 0) * 100
        gm  = (p.get('gross_margin') or 0) * 100
        fcf = (p.get('fcf_yield') or 0) * 100
        peg = p.get('peg_ratio')
        rsi = p.get('rsi')
        bd  = p.get('score_breakdown', {})

        print(f'\n{i:2d}. {ps}{p["ticker"]:8s} {p["name"][:25]}')
        print(f'    スコア:{p["score"]:.0f}pt  セクター:{p.get("sector","不明")}  '
              f'セクター内{p.get("sector_rank","?")}位/{p.get("sector_total","?")}件')
        print(f'    価格:${p.get("price","N/A")}  時価総額:${(p.get("market_cap") or 0)/1e9:.1f}B')
        print(f'    ROE:{roe:.1f}%  EPS成長:{eps:.1f}%  売上成長:{rev:.1f}%')
        peg_str = f"{peg:.2f}" if peg else "N/A"
        print(f'    粗利率:{gm:.1f}%  FCF利回:{fcf:.1f}%  PEG:{peg_str}')
        pe = p.get('pe_forward') or p.get('pe_trailing')
        pe_str  = f"{pe:.1f}"  if pe  else "N/A"
        rsi_str = f"{rsi:.0f}" if rsi else "N/A"
        print(f'    PER:{pe_str}  RSI:{rsi_str}  '
              f'MA200比:{p.get("ma200_pct") or 0:.1f}%  3Mモメ:{p.get("mom_3m") or 0:.1f}%')
        print(f'    スコア内訳: {bd}')
        if p.get('note'):
            print(f'    備考: {p["note"]}')

    print(f'\n{"="*60}')
    print('【セクター別TOP（通過のみ）】')
    for sector in sorted(results['watchlist_by_sector']):
        items = [x for x in results['watchlist_by_sector'][sector] if x['passes']]
        if items:
            top = items[0]
            print(f'  {sector[:25]:25s}: {top["ticker"]:6s} ({top["score"]:.0f}pt)'
                  f' + {len(items)-1}件')

    # 惜しくも落ちた上位5件
    rejected = results.get('rejected', [])
    if rejected:
        print(f'\n{"─"*40}')
        print('【惜しくも基準未達（上位5件）】')
        for r in rejected[:5]:
            print(f'  {r["ticker"]:8s} {r["score"]:.0f}pt  理由: {"; ".join(r["filter_reasons"][:2])}')


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]

    if args and args[0] == 'upgrade':
        candidates = find_upgrade_candidates()
        if candidates:
            print(f'転換候補: {len(candidates)}件')
            for c in candidates:
                print(f'  {c["ticker"]}: スコア{c["score"]:.0f} - {c["reason"]}')
        else:
            print('転換候補なし')

    elif args and args[0] == 'quick':
        # Healthcare + Financialsのみ
        quick_list = [t for t, v in WATCHLIST.items()
                      if v['sector'] in ('Healthcare', 'Financial Services')]
        results = run_screening(quick_list, top_n=10)
        _print_results(results)
        save_results(results)

    elif args and args[0] == 'jp':
        # 日本株のみ
        jp_list = [t for t in WATCHLIST if t.endswith('.T')]
        results = run_screening(jp_list, top_n=10)
        _print_results(results)
        save_results(results)

    elif args and args[0] == 'batch-poll':
        # Batch API 結果をポーリングして results に AI テーゼを追記
        done = poll_ai_batch()
        if done:
            print("バッチ完了: AI テーゼを long_term_screen_results.json に追記しました")

    elif args and args[0] == 'submit-batch':
        # 既存の long_term_screen_results.json から通過銘柄を読み込んでバッチ送信
        results_path = BASE_DIR / "long_term_screen_results.json"
        if not results_path.exists():
            print("[Batch] long_term_screen_results.json が存在しません。先にスクリーニングを実行してください。")
        else:
            existing = json.loads(results_path.read_text(encoding="utf-8"))
            passed = existing.get("passed", [])
            if not passed:
                print("[Batch] 通過銘柄が0件のため送信をスキップ")
            else:
                print(f"[Batch] 既存結果から {len(passed)} 銘柄のバッチを送信中...")
                batch_id = submit_ai_batch(passed)
                if batch_id:
                    print(f"[Batch] 送信完了: {batch_id}")
                else:
                    print("[Batch] 送信失敗 → DeepSeek V3 で同期生成に切り替え中...")
                    _generate_thesis_sync(results_path)

    elif args and args[0] == 'generate-thesis':
        # DeepSeek V3 → Haiku フォールバックで AI テーゼを即時同期生成
        results_path = BASE_DIR / "long_term_screen_results.json"
        if not results_path.exists():
            print("[Thesis] long_term_screen_results.json が存在しません。")
        else:
            _generate_thesis_sync(results_path)

    else:
        # フルスクリーニング（全銘柄）
        # P2-9: ヘルスチェック用ハートビート
        try:
            from utils import heartbeat as _hb
        except Exception:
            _hb = None
        try:
            results = run_screening()
            _print_results(results)
            save_results(results)
            send_screening_alert(results)
            # AI 投資テーゼをバッチで非同期生成（翌日 batch-poll で結果回収）
            submit_ai_batch(results.get('passed', []))
            if _hb:
                _hb('long_term_screener', 'ok')
        except Exception as _e:
            if _hb:
                _hb('long_term_screener', 'error', str(_e)[:500])
            raise
