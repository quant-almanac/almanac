"""
ALMANAC v4.0 - ポートフォリオ統合管理
全口座の通貨・セクター配分集計、テック集中解消進捗、リバランス判定
"""

import json
import os
from pathlib import Path
from datetime import datetime
from typing import Optional
import yfinance as yf
from utils import atomic_write_json, init_yfinance_timeout

init_yfinance_timeout()

BASE_DIR = Path(__file__).parent

# ============================================================
# 目標配分
# ============================================================

CURRENCY_TARGETS = {
    'USD': (0.60, 0.70),   # 目標レンジ
    'JPY': (0.30, 0.40),
}

SECTOR_TARGETS = {
    'Technology':          0.30,
    'Financial Services':  0.15,
    'Healthcare':          0.15,
    'Industrials':         0.10,   # 持株会銘柄含む
    'Basic Materials':     0.15,   # コモディティ（GLD含む）
    'Energy':              0.05,
    'Consumer Defensive':  0.05,
    'Other':               0.05,
}

SECTOR_REBALANCE_THRESHOLD = 0.35   # 35%超でリバランス検討（fallback）

def _tp_pm(key: str, fallback):
    """tunable_params から値を取得（fallback 必須）。循環回避のため関数内 import。"""
    try:
        from tunable_params import get as _tp_get
        v = _tp_get(key)
        return v if v is not None else fallback
    except Exception:
        return fallback


def _get_sector_rebalance_threshold() -> float:
    """tunable_params: sector_rebalance_threshold_pct を 0-1 化して返す（warn 閾値）。"""
    return float(_tp_pm("sector_rebalance_threshold_pct", SECTOR_REBALANCE_THRESHOLD * 100)) / 100.0


def _get_sector_max_pct() -> float:
    """tunable_params: sector_max_pct を 0-1 化して返す（hard alert 閾値）。"""
    return float(_tp_pm("sector_max_pct", 40)) / 100.0


def _get_cash_critical_ratio() -> float:
    """tunable_params: cash_critical_ratio_pct を 0-1 化して返す。"""
    return float(_tp_pm("cash_critical_ratio_pct", 15)) / 100.0

# yfinanceのセクター名 → 統一セクター名マッピング
SECTOR_MAP = {
    'Technology':            'Technology',
    'Information Technology':'Technology',
    'Communication Services':'Technology',
    'Financial Services':    'Financial Services',
    'Financials':            'Financial Services',
    'Healthcare':            'Healthcare',
    'Health Care':           'Healthcare',
    'Industrials':           'Industrials',
    'Basic Materials':       'Basic Materials',
    'Materials':             'Basic Materials',
    'Energy':                'Energy',
    'Consumer Defensive':    'Consumer Defensive',
    'Consumer Staples':      'Consumer Defensive',
    'Consumer Cyclical':     'Consumer Cyclical',
    'Real Estate':           'Real Estate',
    'Utilities':             'Utilities',
}

# yfinanceで取得できない銘柄の手動セクター定義
MANUAL_SECTORS = {
    # 1489 is a diversified 50-stock high-dividend index ETF, not a pure
    # financial-sector fund.  Keep it in Other until look-through weights are
    # available; assigning the whole ETF to Financial Services falsely lets a
    # sector-specific execution plan authorize it.
    '1489.T':         'Other',                    # NF日経高配当50（業種分散ETF）
    'XLF':            'Financial Services',       # Financial Select Sector SPDR ETF
    '6762.T':         'Technology',               # TDK（電子部品）
    'GLD':            'Basic Materials',          # ゴールドETF
    'EPOL':           'Other',                    # ポーランドETF（地域分散）
    'EWG':            'Other',                    # ドイツETF（地域分散）
    'IEV':            'Other',                    # ヨーロッパETF（地域分散）
    'SLIM_SP500':     'Other',                    # 全世界インデックス
    'SLIM_ORCAN':     'Other',                    # 全世界インデックス
    'MNXACT':         'Other',                    # アクティビスト
    'IFREE_FANGPLUS': 'Technology',               # FANG+ レバレッジ
    'NOMURA_SEMI':    'Technology',               # 半導体セクター
    '9999.T':         'Industrials',              # 持株会銘柄
    'CASH_JPY':       'Cash',                     # 楽天証券 預り金
    'CASH_USD':       'Cash',                     # 楽天証券 USドル
    'CASH_JPY_SBI':   'Cash',                     # SBI証券 円預かり金
    'CASH_JPY_SBI_WIFE': 'Cash',                  # SBI証券 円預かり金（サブ）
    'GS_MMF_USD':     'Cash',                     # GS米ドルMMF
    # --- 以下: yf.info ハング防止のため手動登録 ---
    'META':           'Communication Services',   # Meta Platforms
    'AVGO':           'Technology',               # Broadcom
    'AVGO_toku':      'Technology',               # Broadcom（特定口座）
    'AVGO_ippan':     'Technology',               # Broadcom（一般口座）
    'NVDA':           'Technology',               # NVIDIA
    'CRWV':           'Technology',               # CoreWeave
    'RCL':            'Consumer Cyclical',        # Royal Caribbean
}

# account.total_cash に含める楽天口座の現金ミラー。
# holdings 側にも表示用に置いているが、snapshot 集計では二重計上しない。
ACCOUNT_CASH_MIRROR_KEYS = {'CASH_JPY', 'CASH_USD'}


# ============================================================
# データロード
# ============================================================

def load_holdings() -> dict:
    path = BASE_DIR / 'holdings.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_account() -> dict:
    path = BASE_DIR / 'account.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def load_espp_data() -> dict:
    path = BASE_DIR / 'espp_plan.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


# ============================================================
# 現在値取得
# ============================================================

_price_cache: dict = {}
_price_cache_time: dict = {}
_PRICE_CACHE_TTL = 900  # 15分

def get_current_price(ticker: str, currency: str, current_nav: Optional[float] = None) -> Optional[float]:
    """yfinanceから現在値を取得。投資信託はcurrent_navを使用。"""
    if current_nav:
        return current_nav

    now = datetime.now().timestamp()
    try:
        from tunable_params import get as _tp_get_pc
        _ttl = int(_tp_get_pc("price_cache_minutes", 15)) * 60
    except Exception:
        _ttl = _PRICE_CACHE_TTL
    if ticker in _price_cache and now - _price_cache_time.get(ticker, 0) < _ttl:
        return _price_cache[ticker]

    try:
        price = yf.Ticker(ticker).fast_info['lastPrice']
        _price_cache[ticker] = float(price)
        _price_cache_time[ticker] = now
        return float(price)
    except Exception:
        return None


def get_fx_rate(pair: str = 'USDJPY=X') -> float:
    """USD/JPYレートを取得（P0-1 後: utils.get_fx_rate_cached() に委譲、TTL キャッシュ + stale fallback）。"""
    from utils import get_fx_rate_cached
    rate, _source = get_fx_rate_cached(pair)
    return float(rate)


# ============================================================
# ポートフォリオ集計
# ============================================================

def build_portfolio_snapshot(
    include_espp: bool = True,
    fetch_missing_sectors: bool = True,
) -> dict:
    """
    全口座の保有銘柄を集計してスナップショットを返す。

    Returns:
        {
          'positions': [{ticker, name, value_jpy, currency, sector, investment_type, ...}],
          'total_jpy': 総資産（円）,
          'fx_rate': USD/JPY,
          'currency_breakdown': {USD: ..., JPY: ...},
          'sector_breakdown': {セクター名: 金額},
          'as_of': 取得日時,
        }
    """
    holdings  = load_holdings()
    account   = load_account()
    fx_rate   = get_fx_rate()
    positions = []
    try:
        from execution_safety import canonical_broker, canonical_owner, load_nisa_profiles

        _nisa_raw, nisa_profiles = load_nisa_profiles(BASE_DIR)
    except Exception:
        canonical_broker = lambda value: str(value or "").strip().lower()  # type: ignore
        canonical_owner = lambda value: str(value or "").strip().lower()  # type: ignore
        nisa_profiles = {}

    for key, info in holdings.items():
        if key in ACCOUNT_CASH_MIRROR_KEYS:
            continue
        ticker       = info.get('ticker', key)
        currency     = info.get('currency', 'USD')
        name         = info.get('name', ticker)
        itype        = info.get('investment_type', 'long')
        entry_price  = info.get('entry_price', 0)
        shares       = info.get('shares', 0)
        current_nav  = info.get('current_nav')
        is_cash      = itype == 'cash' or str(key).startswith('CASH_')
        broker       = canonical_broker(info.get('broker'))
        owner        = canonical_owner(info.get('owner'))
        if broker and not owner:
            owner_matches = [
                profile_owner
                for profile_owner, profile in nisa_profiles.items()
                if profile.get('execution_broker') == broker
            ]
            if len(owner_matches) == 1:
                owner = owner_matches[0]

        # 現在値取得（投資信託はNAV固定、株式はyfinance）
        is_fund = bool(info.get('unit'))
        if is_cash:
            current_price = current_nav or entry_price or 1.0
        elif is_fund:
            current_price = current_nav or entry_price
        else:
            current_price = get_current_price(ticker, currency, current_nav) or entry_price

        # 評価額（JPY換算）
        if currency == 'USD':
            value_jpy = shares * current_price * fx_rate
        else:
            # 投資信託は口数 × NAV / 10000
            if is_fund:
                value_jpy = shares * current_price / 10000
            else:
                value_jpy = shares * current_price

        # 含み損益（通貨チェックを先に行い、USD建てファンドが /10000 される誤りを防ぐ）
        if currency == 'USD':
            cost_jpy = shares * entry_price * fx_rate
        elif is_fund:
            cost_jpy = shares * entry_price / 10000
        else:
            cost_jpy = shares * entry_price

        unrealized_jpy = value_jpy - cost_jpy

        # セクター
        sector = MANUAL_SECTORS.get(key) or MANUAL_SECTORS.get(ticker)
        if not sector and fetch_missing_sectors:
            # yf.info はネットワーク遅延で無制限にハングするためスレッドタイムアウトで保護
            try:
                from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FTO
                with ThreadPoolExecutor(max_workers=1) as _ex:
                    _f = _ex.submit(lambda: yf.Ticker(ticker).info)
                    info_yf = _f.result(timeout=8)
                    raw_sector = info_yf.get('sector', 'Other')
                    sector = SECTOR_MAP.get(raw_sector, 'Other')
            except Exception:
                sector = 'Other'
        elif not sector:
            sector = 'Other'

        # 保有日数を計算
        entry_date_str = info.get('entry_date', '')
        holding_days = None
        if entry_date_str:
            try:
                from datetime import date
                ed = date.fromisoformat(entry_date_str)
                holding_days = (date.today() - ed).days
            except Exception:
                pass

        positions.append({
            'key':             key,
            'ticker':          ticker,
            'name':            name,
            'currency':        currency,
            'shares':          shares,
            'current_price':   round(current_price, 4),
            'value_jpy':       round(value_jpy, 0),
            'cost_jpy':        round(cost_jpy, 0),
            'unrealized_jpy':  round(unrealized_jpy, 0),
            'unrealized_pct':  round(unrealized_jpy / cost_jpy, 4) if cost_jpy > 0 else 0,
            'sector':          sector,
            'investment_type': itype,
            'account':         info.get('account', ''),
            'broker':          broker,
            'owner':           owner,
            'entry_date':      entry_date_str,
            'entry_price':     entry_price,
            'holding_days':    holding_days,
        })

    # 持株会を追加
    if include_espp:
        espp = load_espp_data()
        if espp.get('current_shares', 0) > 0:
            espp_price = get_current_price('9999.T', 'JPY') or (espp.get('avg_cost', 0) * 1.7)
            espp_value = espp['current_shares'] * espp_price
            espp_cost  = espp['current_shares'] * espp.get('avg_cost', 0)
            positions.append({
                'key':             '9999.T_持株会',
                'ticker':          '9999.T',
                'name':            '持株会（9999.T）',
                'currency':        'JPY',
                'shares':          espp['current_shares'],
                'current_price':   espp_price,
                'value_jpy':       round(espp_value, 0),
                'cost_jpy':        round(espp_cost, 0),
                'unrealized_jpy':  round(espp_value - espp_cost, 0),
                'unrealized_pct':  round((espp_value - espp_cost) / espp_cost, 4) if espp_cost > 0 else 0,
                'sector':          'Industrials',
                'investment_type': 'long',
                'account':         '持株会',
            })

    # 現金
    #
    # 現金の保存済み派生値 (account.total_cash / jpy_equivalent_usd) は
    # FX更新だけで古くなるため、読み取り時は balance / usd_balance / fx_rate から再計算する。
    # 通貨配分では JPY 現金と USD 現金を分けないと、USD 現金を JPY と誤認する。
    try:
        cash_jpy_native = float(account.get('balance', 0) or 0)
    except (TypeError, ValueError):
        cash_jpy_native = 0.0
    try:
        cash_usd_native = float(account.get('usd_balance', 0) or 0)
    except (TypeError, ValueError):
        cash_usd_native = 0.0
    cash_usd_jpy = cash_usd_native * fx_rate
    cash_total_jpy = cash_jpy_native + cash_usd_jpy

    total_jpy = sum(p['value_jpy'] for p in positions) + cash_total_jpy

    # 通貨配分
    usd_jpy = sum(p['value_jpy'] for p in positions if p['currency'] == 'USD') + cash_usd_jpy
    jpy_val = sum(p['value_jpy'] for p in positions if p['currency'] == 'JPY') + cash_jpy_native
    currency_breakdown = {
        'USD': {'value_jpy': round(usd_jpy, 0), 'ratio': round(usd_jpy / total_jpy, 4) if total_jpy > 0 else 0},
        'JPY': {'value_jpy': round(jpy_val, 0), 'ratio': round(jpy_val / total_jpy, 4) if total_jpy > 0 else 0},
    }

    # セクター配分
    sector_breakdown: dict = {}
    for p in positions:
        s = p['sector']
        sector_breakdown[s] = sector_breakdown.get(s, 0) + p['value_jpy']
    # account cash は positions には入れないため、再計算した現金合計を Cash セクターへ足して
    # セクター比率の分子を総資産定義に揃える。
    sector_breakdown['Cash'] = sector_breakdown.get('Cash', 0) + cash_total_jpy
    sector_breakdown = {k: {'value_jpy': round(v, 0), 'ratio': round(v / total_jpy, 4) if total_jpy > 0 else 0}
                        for k, v in sorted(sector_breakdown.items(), key=lambda x: -x[1])}

    return {
        'positions':          positions,
        'total_jpy':          round(total_jpy, 0),
        # cash_jpy is kept for one compatibility cycle.  New consumers must
        # use cash_total_jpy or the explicit native-currency fields.
        'cash_jpy':           round(cash_total_jpy, 0),
        'cash_total_jpy':     round(cash_total_jpy, 0),
        'cash_jpy_native':    round(cash_jpy_native, 0),
        'cash_usd':           round(cash_usd_native, 2),
        'cash_usd_native':    round(cash_usd_native, 2),
        'cash_usd_jpy':       round(cash_usd_jpy, 0),
        'fx_rate':            round(fx_rate, 2),
        'currency_breakdown': currency_breakdown,
        'sector_breakdown':   sector_breakdown,
        'as_of':              datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


# ============================================================
# 通貨配分チェック
# ============================================================

def check_currency_balance(snapshot: dict) -> dict:
    """
    通貨配分が目標レンジ内かチェック。

    Returns:
        {
          'USD': {'ratio': ..., 'target': (0.6, 0.7), 'status': 'ok'/'over'/'under'},
          'JPY': {...},
          'alerts': [...],
        }
    """
    breakdown = snapshot['currency_breakdown']
    alerts    = []
    result    = {}

    for ccy, (low, high) in CURRENCY_TARGETS.items():
        ratio = breakdown.get(ccy, {}).get('ratio', 0)
        if ratio > high + 0.05:
            status = 'over'
            alerts.append(f'{ccy}比率 {ratio*100:.1f}%（目標上限{high*100:.0f}%を超過）')
        elif ratio < low - 0.05:
            status = 'under'
            alerts.append(f'{ccy}比率 {ratio*100:.1f}%（目標下限{low*100:.0f}%を下回る）')
        else:
            status = 'ok'

        result[ccy] = {
            'ratio':  ratio,
            'target': (low, high),
            'status': status,
        }

    return {'currencies': result, 'alerts': alerts}


# ============================================================
# A-3: キャッシュドラッグ検知 + MMF / 短期債 ETF 誘導
# ============================================================

# 閾値設定（総資産比）
CASH_WARN_RATIO     = 0.03   # 3% 超 7 日滞留 → warn
CASH_CRITICAL_RATIO = 0.15   # 15% 超       → critical

# 滞留判定に使う日数
CASH_STALE_DAYS = 7

# 想定利回り（年）— JPY と USD で別
YIELD_JPY_ETF = 0.0020   # 2552.T（国内 1-3 年債）/ 住信 SBI 米ドル普通預金レンジの JPY 側
YIELD_USD_MMF = 0.0450   # SGOV / BIL など米 T-bill ETF

# ルーティング候補（通貨別）
CASH_ROUTING = {
    'JPY': {
        'recommendations': ['2552.T', '2561.T'],
        'description':     '国内 1-3 年債 ETF（円建て超低リスク）',
        'assumed_yield':   YIELD_JPY_ETF,
    },
    'USD': {
        'recommendations': ['SGOV', 'BIL', 'USFR'],
        'description':     '米 T-bill ETF / FRN ETF（~4.5% 年利）',
        'assumed_yield':   YIELD_USD_MMF,
    },
}

CASH_STATE_PATH = BASE_DIR / 'cash_state.json'


def _load_cash_state() -> dict:
    """前回チェック時刻と残高のスナップショット（滞留日数判定用）"""
    import json as _json
    if CASH_STATE_PATH.exists():
        try:
            return _json.loads(CASH_STATE_PATH.read_text(encoding='utf-8'))
        except Exception:
            pass
    return {}


def _save_cash_state(state: dict) -> None:
    import json as _json
    tmp = CASH_STATE_PATH.with_suffix('.tmp')
    tmp.write_text(_json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(CASH_STATE_PATH)


def detect_cash_drag(
    snapshot: Optional[dict] = None,
    persist: bool = True,
) -> dict:
    """
    総資産比で現金が遊んでいるか（キャッシュドラッグ）を検知する。

    ロジック:
      1. JPY 現金（account.balance） / USD 現金（account.usd_balance × FX）を個別取得
      2. 合計比 <= CASH_WARN_RATIO: ok
         CASH_WARN_RATIO 超: 初回は注意（滞留日数カウント開始）、
                             CASH_STALE_DAYS 日連続で超過なら warn
         CASH_CRITICAL_RATIO 超: 即 critical
      3. ルーティング候補と想定年リターン（¥ベース）を提案
    """
    import time as _time

    if snapshot is None:
        snapshot = build_portfolio_snapshot()

    account  = load_account()
    fx, _fx_src = 1.0, 'n/a'
    try:
        from utils import get_fx_rate_cached
        fx, _fx_src = get_fx_rate_cached()
    except Exception:
        fx = float(account.get('fx_rate_usdjpy', 150.0))

    cash_jpy    = float(account.get('balance', 0))
    cash_usd    = float(account.get('usd_balance', 0))
    cash_usd_jpy = cash_usd * fx
    total_cash_jpy = cash_jpy + cash_usd_jpy
    total_jpy   = float(snapshot.get('total_jpy', 0))
    cash_ratio  = (total_cash_jpy / total_jpy) if total_jpy > 0 else 0.0

    now  = _time.time()
    state = _load_cash_state()
    prev_over_since = state.get('over_warn_since')  # 初回超過時刻
    prev_ratio      = state.get('last_ratio', 0)

    # 滞留カウント
    if cash_ratio > CASH_WARN_RATIO:
        if prev_over_since is None:
            over_since = now
        else:
            over_since = prev_over_since
        stale_days = int((now - over_since) / 86400)
    else:
        over_since = None
        stale_days = 0

    # レベル判定
    _cash_crit = _get_cash_critical_ratio()
    if cash_ratio >= _cash_crit:
        level = 'critical'
    elif cash_ratio >= CASH_WARN_RATIO and stale_days >= CASH_STALE_DAYS:
        level = 'warn'
    elif cash_ratio >= CASH_WARN_RATIO:
        level = 'monitor'   # 超過はしているが滞留日数未達
    else:
        level = 'ok'

    # 提案
    suggestions = []
    if level in ('warn', 'critical'):
        if cash_jpy > total_jpy * 0.01:
            rec = CASH_ROUTING['JPY']
            annual_yield_jpy = cash_jpy * rec['assumed_yield']
            suggestions.append({
                'currency':       'JPY',
                'amount_jpy':     round(cash_jpy, 0),
                'candidates':     rec['recommendations'],
                'description':    rec['description'],
                'annual_yield_jpy': round(annual_yield_jpy, 0),
            })
        if cash_usd > 100:  # 100 ドル以上なら意味のある退避
            rec = CASH_ROUTING['USD']
            annual_yield_jpy = cash_usd_jpy * rec['assumed_yield']
            suggestions.append({
                'currency':       'USD',
                'amount_usd':     round(cash_usd, 2),
                'amount_jpy':     round(cash_usd_jpy, 0),
                'candidates':     rec['recommendations'],
                'description':    rec['description'],
                'annual_yield_jpy': round(annual_yield_jpy, 0),
            })

    total_annual_yield = sum(s['annual_yield_jpy'] for s in suggestions)

    result = {
        'level':             level,
        'cash_ratio':        round(cash_ratio, 4),
        'cash_jpy':          round(cash_jpy, 0),
        'cash_usd':          round(cash_usd, 2),
        'cash_usd_jpy':      round(cash_usd_jpy, 0),
        'total_cash_jpy':    round(total_cash_jpy, 0),
        'total_portfolio_jpy': round(total_jpy, 0),
        'stale_days':        stale_days,
        'threshold_warn':    CASH_WARN_RATIO,
        'threshold_critical': _get_cash_critical_ratio(),
        'suggestions':       suggestions,
        'total_annual_yield_jpy': round(total_annual_yield, 0),
        'fx_rate':           round(fx, 2),
        'fx_source':         _fx_src,
    }

    # 持続監視状態を永続化
    if persist:
        new_state = {
            'last_check_ts':   now,
            'last_check_iso':  datetime.now().isoformat(),
            'last_ratio':      round(cash_ratio, 4),
            'over_warn_since': over_since,
            'last_level':      level,
        }
        try:
            _save_cash_state(new_state)
        except Exception:
            pass

    return result


def format_cash_drag_message(result: dict) -> str:
    """priority_actions / Telegram 用の人間可読メッセージ"""
    if result['level'] == 'ok':
        return ''

    lines = [
        f'💰 キャッシュドラッグ {result["level"].upper()}: '
        f'現金比率 {result["cash_ratio"]*100:.1f}% '
        f'(¥{result["total_cash_jpy"]/10000:.0f}万 / ¥{result["total_portfolio_jpy"]/10000:.0f}万)'
    ]
    if result['stale_days'] >= CASH_STALE_DAYS:
        lines.append(f'  滞留 {result["stale_days"]} 日')
    for s in result['suggestions']:
        if s['currency'] == 'JPY':
            lines.append(
                f'  → JPY ¥{s["amount_jpy"]/10000:.0f}万 を {"/".join(s["candidates"])} へ '
                f'（年¥{s["annual_yield_jpy"]/10000:.1f}万想定）'
            )
        else:
            lines.append(
                f'  → USD ${s["amount_usd"]:,.0f} (¥{s["amount_jpy"]/10000:.0f}万) を '
                f'{"/".join(s["candidates"])} へ（年¥{s["annual_yield_jpy"]/10000:.1f}万想定）'
            )
    if result['total_annual_yield_jpy'] > 0:
        lines.append(f'  合計機会コスト: 年¥{result["total_annual_yield_jpy"]/10000:.1f}万')
    return '\n'.join(lines)


# ============================================================
# セクター配分チェック
# ============================================================

def check_sector_balance(snapshot: dict) -> dict:
    """
    セクター配分が閾値を超えていないかチェック。

    Returns:
        {
          セクター名: {'ratio': ..., 'target': ..., 'status': ...},
          'alerts': [...],
          'tech_ratio': テック比率（集中解消進捗用）,
        }
    """
    breakdown = snapshot['sector_breakdown']
    total_jpy = snapshot['total_jpy']
    alerts    = []
    result    = {}

    for sector, data in breakdown.items():
        ratio  = data['ratio']
        target = SECTOR_TARGETS.get(sector, 0.10)

        _sec_warn = _get_sector_rebalance_threshold()   # warn 閾値（35%）
        _sec_max  = _get_sector_max_pct()               # hard alert 閾値（40%）
        if ratio > _sec_max:
            status = 'critical'
            alerts.append(
                f'⛔ {sector} {ratio*100:.1f}%（{_sec_max*100:.0f}%上限超過 — '
                f'即時リバランス必須・新規 buy 全停止）'
            )
        elif ratio > _sec_warn:
            status = 'warning'
            alerts.append(
                f'⚠️ {sector} {ratio*100:.1f}%（{_sec_warn*100:.0f}%超でリバランス推奨）'
            )
        elif ratio > target * 1.5:
            status = 'over'
        else:
            status = 'ok'

        result[sector] = {
            'ratio':  ratio,
            'target': target,
            'status': status,
        }

    tech_ratio = breakdown.get('Technology', {}).get('ratio', 0)

    return {
        'sectors': result,
        'alerts':  alerts,
        'tech_ratio': tech_ratio,
        'tech_progress': {
            'current':  round(tech_ratio, 4),
            'target':   0.30,
            'start':    0.82,
            'progress': round(max(0, (0.82 - tech_ratio) / (0.82 - 0.30)), 4),
        },
    }


# ============================================================
# investment_type 別サマリー
# ============================================================

def get_type_summary(snapshot: dict) -> dict:
    """long / medium / short 別の評価額・損益を集計する。"""
    result = {'long': {}, 'medium': {}, 'swing': {}}

    for itype in result:
        pos = [p for p in snapshot['positions'] if p['investment_type'] == itype]
        total_value = sum(p['value_jpy'] for p in pos)
        total_cost  = sum(p['cost_jpy']  for p in pos)
        result[itype] = {
            'count':          len(pos),
            'value_jpy':      round(total_value, 0),
            'cost_jpy':       round(total_cost, 0),
            'unrealized_jpy': round(total_value - total_cost, 0),
            'unrealized_pct': round((total_value - total_cost) / total_cost, 4) if total_cost > 0 else 0,
            'positions':      pos,
        }

    return result


# ============================================================
# リバランス判定
# ============================================================

def get_rebalance_triggers(snapshot: dict) -> list:
    """
    リバランストリガーのリストを返す。

    Returns:
        [{type, message, severity}]
    """
    triggers  = []
    currency  = check_currency_balance(snapshot)
    sector    = check_sector_balance(snapshot)

    for alert in currency['alerts']:
        triggers.append({'type': 'currency', 'message': alert, 'severity': 'warning'})

    for alert in sector['alerts']:
        severity = 'critical' if 'リバランス推奨' in alert else 'warning'
        triggers.append({'type': 'sector', 'message': alert, 'severity': severity})

    # 四半期定期リバランス
    month = datetime.now().month
    if month in [3, 6, 9, 12]:
        triggers.append({
            'type':     'quarterly',
            'message':  '四半期定期リバランスの時期です',
            'severity': 'info',
        })

    return triggers


# ============================================================
# スナップショット保存
# ============================================================

def save_snapshot(snapshot: dict):
    """スナップショットをJSONとして保存する（履歴追跡用）。"""
    path = BASE_DIR / 'portfolio_snapshot.json'
    atomic_write_json(path, snapshot, default=str)


def get_full_analysis(include_espp: bool = True) -> dict:
    """ダッシュボード用の全分析結果をまとめて返す。"""
    snapshot  = build_portfolio_snapshot(include_espp)
    currency  = check_currency_balance(snapshot)
    sector    = check_sector_balance(snapshot)
    type_sum  = get_type_summary(snapshot)
    triggers  = get_rebalance_triggers(snapshot)

    return {
        'snapshot':      snapshot,
        'currency':      currency,
        'sector':        sector,
        'type_summary':  type_sum,
        'triggers':      triggers,
    }


if __name__ == '__main__':
    print('ポートフォリオ分析中...')
    analysis = get_full_analysis()
    snap     = analysis['snapshot']

    print(f'\n総資産: ¥{snap["total_jpy"]:,.0f}  (USD/JPY: {snap["fx_rate"]})')
    print(f'更新: {snap["as_of"]}')

    print('\n【通貨配分】')
    for ccy, data in analysis['currency']['currencies'].items():
        icon = '✅' if data['status'] == 'ok' else '⚠️'
        print(f'  {icon} {ccy}: {data["ratio"]*100:.1f}%  目標{data["target"][0]*100:.0f}〜{data["target"][1]*100:.0f}%')

    print('\n【セクター配分】')
    for sector, data in analysis['sector']['sectors'].items():
        icon = '🔴' if data['status'] == 'critical' else ('🟡' if data['status'] == 'over' else '🟢')
        print(f'  {icon} {sector}: {data["ratio"]*100:.1f}%')

    tech = analysis['sector']['tech_progress']
    print(f'\n【テック集中解消】{tech["current"]*100:.1f}% → 目標30%  進捗{tech["progress"]*100:.0f}%')

    if analysis['triggers']:
        print('\n【リバランストリガー】')
        for t in analysis['triggers']:
            print(f'  ・{t["message"]}')
