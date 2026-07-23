"""
ALMANAC v4.0 - 税務最適化エンジン
損出し候補検出・NISA枠残額・外国税額控除シミュレーション・クレカ積立売却税金計算
"""

import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).parent

# ============================================================
# 税率定数
# ============================================================

TAX_TOKUTEI  = 0.20315   # 特定口座（所得税+住民税+復興税）
TAX_IPPAN    = 0.20315   # 一般口座（同率・確定申告必要）
TAX_NISA     = 0.0       # NISA非課税

# 米国株配当源泉徴収（W-8BEN提出済み）
US_DIVIDEND_WITHHOLDING = 0.10   # 米国側10%
JP_DIVIDEND_TAX         = 0.20315

# NISA年間上限
NISA_TSUMITATE_ANNUAL = 1_200_000    # つみたて投資枠
NISA_GROWTH_ANNUAL    = 2_400_000    # 成長投資枠
NISA_LIFETIME         = 18_000_000  # 生涯非課税枠

# 損出し年間スケジュール
SONDASHI_DEADLINE_MONTH = 12
SONDASHI_DEADLINE_DAY   = 26   # 12/26営業日ベース（目安）


# ============================================================
# データロード
# ============================================================

def _load_holdings() -> dict:
    path = BASE_DIR / 'holdings.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _load_nisa() -> dict:
    path = BASE_DIR / 'nisa_portfolio.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _load_espp() -> dict:
    path = BASE_DIR / 'espp_plan.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


def _load_cc_plans() -> dict:
    path = BASE_DIR / 'credit_card_plans.json'
    if path.exists():
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    return {}


# ============================================================
# NISA枠分析
# ============================================================

def analyze_nisa_usage() -> dict:
    """
    NISA枠の使用状況と残余枠を計算する。

    Returns:
        {
          'husband': {tsumitate_used, tsumitate_remaining, growth_used, growth_remaining, ...},
          'wife':    {...},
          'recommendations': [推奨事項リスト],
        }
    """
    nisa    = _load_nisa()
    holdings = _load_holdings()
    today   = date.today()
    year    = today.year

    recommendations = []
    result = {}

    for person in ['husband', 'wife']:
        pdata = nisa.get(person, {})

        # つみたて投資枠
        ts_used = pdata.get('tsumitate_used_this_year', 0)
        ts_planned = pdata.get('tsumitate_planned_this_year', 0)
        ts_rem_before_planned = max(0, NISA_TSUMITATE_ANNUAL - ts_used)
        ts_rem  = max(0, NISA_TSUMITATE_ANNUAL - ts_used - ts_planned)

        # 成長投資枠（holdings.jsonのNISA成長投資枠ポジションを集計）
        growth_used = pdata.get('growth_used_this_year', 0)
        if person == 'husband' and not growth_used:
            # holdings.jsonから計算
            for key, info in holdings.items():
                if info.get('account') == 'NISA成長投資枠':
                    shares = info.get('shares', 0)
                    entry  = info.get('entry_price', 0)
                    is_fund = bool(info.get('unit'))   # 投資信託（口単位）
                    if is_fund:
                        # 口数 × NAV(円) ÷ 10000 = 円
                        cost = shares * entry / 10000
                    elif info.get('currency', 'JPY') == 'USD':
                        from utils import get_fx_rate_cached
                        _fx, _ = get_fx_rate_cached()
                        cost = shares * entry * _fx
                    else:
                        cost = shares * entry
                    growth_used += cost

        growth_planned = pdata.get('growth_planned_this_year', 0)
        growth_rem_before_planned = max(0, NISA_GROWTH_ANNUAL - growth_used)
        growth_rem = max(0, NISA_GROWTH_ANNUAL - growth_used - growth_planned)

        # 生涯枠（P2-10: 過年度売却分の簿価を復活）
        lifetime_used = pdata.get('lifetime_used', pdata.get('lifetime_used_estimate', 0))
        if not lifetime_used:
            lifetime_used = ts_used + growth_used
        restored = compute_nisa_quota_restoration(person, as_of_year=year)
        lifetime_used_net = max(0, lifetime_used - restored)
        lifetime_rem = max(0, NISA_LIFETIME - lifetime_used_net)

        # 来年度復活見込み（当年売却分）
        restore_next_year = 0.0
        for sale in _load_nisa_sales():
            if sale.get('person') == person and int(sale.get('sale_date', '0000-00-00')[:4]) == year:
                restore_next_year += float(sale.get('cost_basis_jpy', 0))

        result[person] = {
            'broker':             pdata.get('broker', '未設定'),
            'tsumitate_used':     round(ts_used, 0),
            'tsumitate_planned':  round(ts_planned, 0),
            'tsumitate_remaining_before_planned': round(ts_rem_before_planned, 0),
            'tsumitate_remaining': round(ts_rem, 0),
            'tsumitate_annual':   NISA_TSUMITATE_ANNUAL,
            'growth_used':        round(growth_used, 0),
            'growth_planned':     round(growth_planned, 0),
            'growth_remaining_before_planned': round(growth_rem_before_planned, 0),
            'growth_remaining':   round(growth_rem, 0),
            'growth_annual':      NISA_GROWTH_ANNUAL,
            'lifetime_used_gross': round(lifetime_used, 0),
            'lifetime_restored':  round(restored, 0),
            'lifetime_used':      round(lifetime_used_net, 0),
            'lifetime_remaining': round(lifetime_rem, 0),
            'lifetime_restore_next_year': round(restore_next_year, 0),
            'daily_tsumitate':    pdata.get('daily_amount', 0),
            'year':               year,
        }

        # 推奨事項
        if ts_rem > 0:
            months_remaining = max(1, 12 - today.month + 1)
            monthly_pace     = ts_used / max(today.month - 1, 1) if today.month > 1 else 0
            projected_total  = ts_used + monthly_pace * months_remaining
            if projected_total < NISA_TSUMITATE_ANNUAL * 0.90:
                recommendations.append({
                    'person':  person,
                    'type':    'tsumitate_increase',
                    'message': f'{person}のNISAつみたて枠: 残{ts_rem/10000:.0f}万円。'
                               f'現ペースでは年間上限¥{NISA_TSUMITATE_ANNUAL/10000:.0f}万の'
                               f'{projected_total/NISA_TSUMITATE_ANNUAL*100:.0f}%しか使えません。'
                               f'積立額を増やすか一時買付を検討。',
                    'priority': 1,
                })

        if growth_rem > 500_000:
            recommendations.append({
                'person':  person,
                'type':    'growth_underused',
                'message': f'{person}のNISA成長投資枠: 残{growth_rem/10000:.0f}万円。'
                           f'高成長US個別株（NVDA・AVGO等）のNISA移管を検討。'
                           f'注意: 高配当US株はNISAに入れないこと（米国源泉10%が永久に戻らない）。',
                'priority': 2,
            })

    # NISAに入れるべきでない銘柄チェック
    high_dividend_in_nisa = []
    for key, info in holdings.items():
        if info.get('account', '').startswith('NISA') and info.get('currency') == 'USD':
            # 高配当チェック（GLD等は配当なし、EPOL/EWGは配当あり）
            if key in ('EPOL', 'EWG', 'IEV', '1489'):
                high_dividend_in_nisa.append({
                    'key':    key,
                    'name':   info.get('name', key),
                    'account': info.get('account'),
                    'issue':  '米国籍ETFの配当にかかる源泉徴収10%はNISAで控除不可',
                })

    if high_dividend_in_nisa:
        recommendations.append({
            'person':  'general',
            'type':    'nisa_suboptimal',
            'message': f'NISA口座に配当源泉徴収が不利な銘柄: {[x["name"] for x in high_dividend_in_nisa]}。'
                       f'課税口座への移動を検討（外国税額控除で回収可能）。',
            'priority': 2,
        })

    return {
        'husband':         result.get('husband', {}),
        'wife':            result.get('wife', {}),
        'recommendations': sorted(recommendations, key=lambda x: x['priority']),
        'as_of':           today.isoformat(),
    }


# ============================================================
# NISA 売却履歴 & 枠復活ロジック（P2-10）
# ============================================================
# 新NISA（2024〜）: 売却すると売却した簿価分だけ翌年に「生涯枠」が復活する。
# 年間枠（つみたて120万 / 成長240万）は復活しない（年内に使い切り）。
# NISA 内の損失は特定口座と通算不可 & 繰越不可（所得税法 22 条）。

NISA_SALE_HISTORY_PATH = BASE_DIR / 'nisa_sale_history.json'


def _load_nisa_sales() -> list:
    if NISA_SALE_HISTORY_PATH.exists():
        try:
            with open(NISA_SALE_HISTORY_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return []
    return []


def record_nisa_sale(
    person: str,
    key: str,
    cost_basis_jpy: float,
    proceeds_jpy: float,
    sale_date: Optional[str] = None,
    quota_type: str = 'growth',   # 'growth' | 'tsumitate'
) -> dict:
    """
    NISA 口座での売却を記録する。

    売却した簿価（取得原価）が翌年に生涯枠として復活する。
    譲渡益は非課税、譲渡損は特定口座と通算不可。

    Args:
        person:         'husband' | 'wife'
        key:            holdings のキー
        cost_basis_jpy: 取得原価（円）
        proceeds_jpy:   売却額（円）
        sale_date:      ISO 日付（None なら today）
        quota_type:     枠種類（復活は生涯枠のみだが記録用）

    Returns:
        {'recorded': True, 'cost_basis': ..., 'gain_loss_jpy': ...,
         'restored_next_year': ..., 'loss_offsetable': False}
    """
    from utils import atomic_write_json

    sale = {
        'person':           person,
        'key':              key,
        'cost_basis_jpy':   round(cost_basis_jpy, 0),
        'proceeds_jpy':     round(proceeds_jpy, 0),
        'gain_loss_jpy':    round(proceeds_jpy - cost_basis_jpy, 0),
        'sale_date':        sale_date or date.today().isoformat(),
        'quota_type':       quota_type,
        'loss_offsetable':  False,   # NISA 損失は特定口座と通算不可
    }

    history = _load_nisa_sales()
    history.append(sale)
    atomic_write_json(NISA_SALE_HISTORY_PATH, history)

    sale_year = int(sale['sale_date'][:4])
    return {
        'recorded':            True,
        'cost_basis_jpy':      sale['cost_basis_jpy'],
        'gain_loss_jpy':       sale['gain_loss_jpy'],
        'restored_next_year':  sale['cost_basis_jpy'],
        'restore_year':        sale_year + 1,
        'loss_offsetable':     False,
        'note':                '新NISA: 翌年の生涯枠が簿価分だけ復活。損失は特定口座と通算不可。',
    }


def compute_nisa_quota_restoration(person: str, as_of_year: Optional[int] = None) -> float:
    """
    指定年時点で復活済みの生涯枠（円）を返す。

    sale_year < as_of_year のすべての売却簿価を合計。
    """
    as_of_year = as_of_year or date.today().year
    total = 0.0
    for sale in _load_nisa_sales():
        if sale.get('person') != person:
            continue
        sale_year = int(sale.get('sale_date', '9999-01-01')[:4])
        if sale_year < as_of_year:
            total += float(sale.get('cost_basis_jpy', 0))
    return total


# ============================================================
# NISA 外国税額漏れガード（A-1）
# ============================================================

def detect_nisa_foreign_tax_leak(
    dividend_yield_threshold: float = 0.015,
    holdings: Optional[dict] = None,
) -> dict:
    """
    NISA 口座内の US 高配当銘柄を検出し、年間の米国源泉税流出額を試算する。

    日米租税条約: US 10% 源泉は通常なら外国税額控除で取り戻せるが、
    NISA 内は日本側非課税のため「控除できる国内税が存在せず」取り戻せない（永久損失）。

    Args:
        dividend_yield_threshold: この利回り（小数、例 0.015 = 1.5%）超なら警告
        holdings: None なら自動ロード

    Returns:
        {
          'leaks':           [{'key','ticker','name','value_jpy','yield','annual_leak_jpy',...}],
          'total_leak_jpy':  年間総流出額（円）,
          'recommendation':  移管提案文字列,
        }
    """
    if holdings is None:
        holdings = _load_holdings()

    # 簡易な配当利回りマップ（yfinance で取得が望ましいが、保存値で代替）
    # 利回り未登録でも info.get('dividend_yield') が無ければ 0 扱い
    KNOWN_YIELDS = {
        'VYM':    0.028,
        'SCHD':   0.035,
        'HDV':    0.032,
        'SPYD':   0.041,
        'DVY':    0.035,
        'IEV':    0.023,
        'EPOL':   0.031,
        'EWG':    0.032,
        'JEPI':   0.072,
        'JEPQ':   0.098,
        'QYLD':   0.115,
        'XYLD':   0.108,
        'DIV':    0.062,
    }

    leaks = []
    total = 0.0

    for key, info in holdings.items():
        account = info.get('account', '')
        if 'NISA' not in account and '成長投資枠' not in account and 'つみたて' not in account:
            continue
        if info.get('currency') != 'USD':
            continue

        dy = float(info.get('dividend_yield') or KNOWN_YIELDS.get(info.get('ticker', key), 0))
        if dy < dividend_yield_threshold:
            continue

        shares = float(info.get('shares', 0))
        price  = float(info.get('current_price', 0) or info.get('entry_price', 0))
        if shares <= 0 or price <= 0:
            continue

        from utils import get_fx_rate_cached
        fx, _ = get_fx_rate_cached()
        value_jpy = shares * price * fx
        annual_div_jpy  = value_jpy * dy
        annual_leak_jpy = annual_div_jpy * US_DIVIDEND_WITHHOLDING  # 10%

        leaks.append({
            'key':              key,
            'ticker':           info.get('ticker', key),
            'name':             info.get('name', key),
            'account':          account,
            'value_jpy':        round(value_jpy, 0),
            'dividend_yield':   round(dy, 4),
            'annual_div_jpy':   round(annual_div_jpy, 0),
            'annual_leak_jpy':  round(annual_leak_jpy, 0),
            'priority':         'high' if annual_leak_jpy > 10_000 else 'medium',
        })
        total += annual_leak_jpy

    leaks.sort(key=lambda x: x['annual_leak_jpy'], reverse=True)
    recommendation = None
    if leaks:
        names = ', '.join(x['name'] for x in leaks[:3])
        recommendation = (
            f'NISA 内 US 高配当 {len(leaks)} 件で年間 ¥{total:,.0f} が米国源泉税で失われています。'
            f'特定口座への移管を検討（外国税額控除で取り戻し可能）: {names}'
        )

    return {
        'leaks':           leaks,
        'total_leak_jpy':  round(total, 0),
        'recommendation':  recommendation,
    }


# ============================================================
# 損出し分析
# ============================================================

def analyze_loss_harvest(
    snapshot: Optional[dict] = None,
    min_loss_jpy: float = 50_000,
) -> dict:
    """
    損出し候補銘柄と節税効果を計算する。

    Args:
        snapshot:     portfolio_manager スナップショット（Noneなら自動取得）
        min_loss_jpy: 最低含み損額（円）

    Returns:
        {
          'candidates':     [{ticker, name, unrealized_jpy, tax_saving, ...}],
          'total_loss_jpy': 損出し可能な総損失額,
          'total_tax_saving': 最大節税効果（円）,
          'deadline':       損出し期限,
          'notes':          注意事項,
        }
    """
    if snapshot is None:
        try:
            import portfolio_manager
            snapshot = portfolio_manager.build_portfolio_snapshot()
        except Exception:
            snapshot = {'positions': []}

    today    = date.today()
    year_end = date(today.year, SONDASHI_DEADLINE_MONTH, SONDASHI_DEADLINE_DAY)

    candidates = []
    total_loss = 0

    for p in snapshot.get('positions', []):
        loss = p.get('unrealized_jpy', 0)
        if loss >= -min_loss_jpy:
            continue

        account = p.get('account', '')
        # NISA口座は損出し対象外（損失が通算できない）
        if 'NISA' in account:
            continue

        itype    = p.get('investment_type', 'long')
        tax_rate = TAX_TOKUTEI if '特定' in account else TAX_IPPAN

        # 節税効果: 損失 × 税率（他の利益と相殺できた場合）
        tax_saving = abs(loss) * tax_rate

        # 同日再購入の注意（日本にウォッシュセールルールなし）
        same_day_repurchase = True

        candidates.append({
            'key':                p['key'],
            'ticker':             p['ticker'],
            'name':               p['name'],
            'account':            account,
            'investment_type':    itype,
            'unrealized_jpy':     round(loss, 0),
            'unrealized_pct':     p.get('unrealized_pct', 0),
            'tax_saving_jpy':     round(tax_saving, 0),
            'tax_rate':           tax_rate,
            'same_day_repurchase': same_day_repurchase,
            'priority':           'high' if abs(loss) > 200_000 else 'medium',
        })
        total_loss += loss

    candidates.sort(key=lambda x: x['unrealized_jpy'])   # 損失大きい順
    total_tax_saving = abs(total_loss) * TAX_TOKUTEI

    days_to_deadline = (year_end - today).days if today <= year_end else -1

    return {
        'candidates':        candidates,
        'total_loss_jpy':    round(total_loss, 0),
        'total_tax_saving':  round(total_tax_saving, 0),
        'deadline':          year_end.isoformat(),
        'days_to_deadline':  days_to_deadline,
        'notes': [
            '日本に米国型の法定ウォッシュセール期間はありません。',
            'ただし同日中の同一銘柄買い戻しは、総平均法に準ずる取得価額再計算で損出し額が目減りする場合があります。翌営業日以降の買い戻しを推奨します。',
            '特定口座内で損益通算されるため、確定申告不要（特定口座の場合）。',
            '持株会の損出しは12/26までに証券口座へ移す必要があります。',
        ],
    }


# ============================================================
# A-2: 損出しハーベスト + wash-sale 回避（30日代替銘柄経由）
# ============================================================

SUBSTITUTES_PATH = BASE_DIR / 'loss_harvest_substitutes.json'
WASH_SALE_WINDOW_DAYS = 30

# 取引コスト（片道、bps）— backtest.py の COST_BPS と整合
COST_BPS_US_ROUNDTRIP = 49.5 + 5     # 手数料 + スプレッド（往復）
COST_BPS_JP_ROUNDTRIP = 5 + 2
# 代替 ETF 乗換は 2 銘柄分（旧売却 + 新規購入 + 将来戻し）→ 1.5 倍の往復コストを計上
SWITCH_COST_MULTIPLIER = 1.5


# Codex re-re-review: loss_harvest_substitutes.json は gitignored で fresh checkout に存在しない。
# ファイル欠落時に substitutes 空で静かに無効化されないよう、broad-market の built-in fallback を持つ
# (ファイルがあれば curated 版が優先)。wash-sale 回避は「実質同一でない近い指数」が目的なので、
# 個別銘柄ではなく広域 ETF を既定にする。
_DEFAULT_SUBSTITUTES: dict = {
    "_fallback": {
        "us_equity_long": {"substitutes": ["VTI", "VOO", "SPY"]},
        "jp_equity_long": {"substitutes": ["1306.T", "1321.T"]},
    },
}


def _load_substitutes() -> dict:
    if SUBSTITUTES_PATH.exists():
        try:
            data = json.loads(SUBSTITUTES_PATH.read_text(encoding='utf-8'))
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return dict(_DEFAULT_SUBSTITUTES)


def _lookup_substitutes(ticker: str, is_japan: bool, subs_map: dict) -> list[str]:
    # Codex re-review P2: 対象ティッカー自身を代替候補から必ず除外する
    # (built-in fallback が VTI などを対象にすると自身を含み、wash-sale 回避にならない)。
    tk = (ticker or "").upper()

    def _filt(lst):
        return [s for s in lst if str(s).upper() != tk]

    # Codex P3: entry キーを大文字正規化して case 差を吸収 ("VTI" vs "vti")。
    norm = {(str(k).upper() if k != "_fallback" else k): v for k, v in subs_map.items()}
    entry = norm.get(tk)
    if entry and entry.get('substitutes'):
        return _filt(list(entry['substitutes']))
    fb = subs_map.get('_fallback', {})
    key = 'jp_equity_long' if is_japan else 'us_equity_long'
    return _filt(list(fb.get(key, {}).get('substitutes', [])))


def _estimate_switch_cost_bps(is_japan: bool) -> float:
    """乗換コスト（bps、旧売却 + 新規購入 + 将来の戻し考慮 ≒ 1.5 × 往復）"""
    bps = COST_BPS_JP_ROUNDTRIP if is_japan else COST_BPS_US_ROUNDTRIP
    return bps * SWITCH_COST_MULTIPLIER


def suggest_loss_harvest_pairs(
    snapshot: Optional[dict] = None,
    min_unrealized_pct: float = -0.10,
    min_loss_jpy: float = 30_000,
    ytd_gain_jpy: Optional[float] = None,
) -> dict:
    """
    特定口座の含み損 ≤ -10% の銘柄について、30日 wash-sale 回避用の
    代替 ETF 提案を生成する。節税額 > 乗換コスト のペアのみを返す。

    Args:
        snapshot:           portfolio_manager.build_portfolio_snapshot()
        min_unrealized_pct: 含み損率の下限（例 -0.10 = -10% 以下）
        min_loss_jpy:       最低含み損額
        ytd_gain_jpy:       年初来の実現益。None なら全損失が節税対象と仮定

    Returns:
        {
          'pairs':        [{ticker, substitutes, loss_jpy, tax_saving, switch_cost,
                             net_benefit, restart_eligible_date, ...}],
          'total_net_benefit': ...,
          'wash_sale_window_days': 30,
          'ytd_gain_jpy': ytd_gain_jpy,
        }
    """
    if snapshot is None:
        try:
            import portfolio_manager
            snapshot = portfolio_manager.build_portfolio_snapshot()
        except Exception:
            snapshot = {'positions': []}

    subs_map = _load_substitutes()
    today = date.today()
    restart_date = today + timedelta(days=WASH_SALE_WINDOW_DAYS)

    pairs = []
    remaining_gain = ytd_gain_jpy if ytd_gain_jpy is not None else None

    # 損失大きい順に優先（節税額を YTD 益の範囲で配分）
    sorted_positions = sorted(
        snapshot.get('positions', []),
        key=lambda p: p.get('unrealized_jpy', 0),
    )

    for p in sorted_positions:
        account = p.get('account', '')
        if 'NISA' in account:  # NISA は通算不可
            continue
        if '特定' not in account and '一般' not in account:
            continue

        loss = p.get('unrealized_jpy', 0)
        pct  = p.get('unrealized_pct', 0)
        if pct > min_unrealized_pct or loss > -min_loss_jpy:
            continue

        ticker  = p.get('ticker', p.get('key', ''))
        is_jp   = ticker.endswith('.T') or ticker.endswith('.JP')
        subs    = _lookup_substitutes(ticker, is_jp, subs_map)
        if not subs:
            continue

        # 節税対象額: YTD 益の範囲内でキャップ（超過分は繰越3年だが即効性低下）
        effective_loss = abs(loss)
        if remaining_gain is not None:
            if remaining_gain <= 0:
                break
            effective_loss = min(effective_loss, remaining_gain)
            remaining_gain -= effective_loss

        tax_rate     = TAX_TOKUTEI if '特定' in account else TAX_IPPAN
        tax_saving   = effective_loss * tax_rate

        # 乗換コスト試算（保有評価額ベース）
        value_jpy    = float(p.get('value_jpy', 0) or p.get('market_value_jpy', 0))
        if value_jpy <= 0:
            # fallback: shares × current_price × fx
            shares = float(p.get('shares', 0))
            price  = float(p.get('current_price', p.get('entry_price', 0)) or 0)
            fx     = 1.0
            if p.get('currency') == 'USD':
                from utils import get_fx_rate_cached
                fx, _ = get_fx_rate_cached()
            value_jpy = shares * price * fx

        switch_bps  = _estimate_switch_cost_bps(is_jp)
        switch_cost = value_jpy * switch_bps / 10000
        net_benefit = tax_saving - switch_cost

        if net_benefit <= 0:
            continue

        sub_entry = subs_map.get(ticker, {})
        pairs.append({
            'ticker':                ticker,
            'name':                  p.get('name', ticker),
            'account':               account,
            'unrealized_jpy':        round(loss, 0),
            'unrealized_pct':        round(pct, 4),
            'effective_loss_jpy':    round(effective_loss, 0),
            'tax_saving_jpy':        round(tax_saving, 0),
            'switch_cost_jpy':       round(switch_cost, 0),
            'net_benefit_jpy':       round(net_benefit, 0),
            'substitutes':           subs[:3],
            'theme':                 sub_entry.get('theme', 'unknown'),
            'correlation_hint':      sub_entry.get('correlation_hint'),
            'wash_sale_window_days': WASH_SALE_WINDOW_DAYS,
            'sold_on':               today.isoformat(),
            'restart_eligible_date': restart_date.isoformat(),
            'priority':              'high' if net_benefit > 50_000 else 'medium',
        })

    pairs.sort(key=lambda x: -x['net_benefit_jpy'])
    total_net = sum(x['net_benefit_jpy'] for x in pairs)

    return {
        'pairs':                 pairs,
        'total_net_benefit_jpy': round(total_net, 0),
        'wash_sale_window_days': WASH_SALE_WINDOW_DAYS,
        'ytd_gain_jpy':          ytd_gain_jpy,
        'notes': [
            f'日本に強制ウォッシュセールルールは無いが、国税庁「取引の形式否認」回避のため {WASH_SALE_WINDOW_DAYS} 日空ける運用。',
            '代替 ETF は相関 0.7〜0.9 を目安にテーマ維持。',
            '節税額 > 乗換コスト のペアのみ返却。YTD 利益範囲で効果を上限化。',
        ],
    }


def register_wash_sale_timers(pairs: list[dict]) -> int:
    """
    提案ペアを action_state_tracker に登録して 30 日後の復帰リマインダーを作る。
    Returns: 登録件数
    """
    try:
        import action_state_tracker as ast
    except ImportError:
        return 0

    actions = []
    for p in pairs:
        substitutes = ', '.join(p.get('substitutes', [])[:3]) or '—'
        actions.append({
            'ticker':   p['ticker'],
            'type':     'loss_harvest_reentry',
            'urgency':  'low',
            'action':   (
                f'{p["ticker"]} 損出し後の再購入候補（wash-sale {WASH_SALE_WINDOW_DAYS} 日後 '
                f'= {p["restart_eligible_date"]}）。代替 ETF: {substitutes}'
            ),
            'reason':   (
                f'節税¥{p["tax_saving_jpy"]/10000:.1f}万 − 乗換コスト¥{p["switch_cost_jpy"]/10000:.1f}万 '
                f'= 純利益¥{p["net_benefit_jpy"]/10000:.1f}万'
            ),
        })
    return ast.record_recommendations(actions, source='loss_harvest')


# ============================================================
# 外国税額控除シミュレーション
# ============================================================

def simulate_foreign_tax_credit(
    annual_us_dividends_usd: float,
    fx_rate: Optional[float] = None,
) -> dict:
    """
    米国株配当に対する外国税額控除の効果をシミュレーションする。

    Args:
        annual_us_dividends_usd: 年間米国株配当（ドル）
        fx_rate:                 USD/JPY

    Returns:
        {
          'dividends_usd': ...,
          'dividends_jpy': ...,
          'us_withholding_jpy': 米国源泉税（円）,
          'jp_tax_jpy':         日本での課税額（円）,
          'total_tax_jpy':      合計税負担（円）,
          'effective_rate':     実効税率,
          'credit_jpy':         外国税額控除で取り戻せる額（円）,
          'net_after_credit':   控除後手取り（円）,
          'recommendation':     推奨事項,
        }
    """
    if fx_rate is None:
        try:
            from utils import get_fx_rate_cached
            fx_rate, _ = get_fx_rate_cached()
        except Exception:
            fx_rate = 150.0
    div_jpy          = annual_us_dividends_usd * float(fx_rate)
    us_withholding   = div_jpy * US_DIVIDEND_WITHHOLDING
    # 日本では米国源泉後の金額に課税（二重課税）
    jp_tax           = div_jpy * JP_DIVIDEND_TAX
    total_tax        = us_withholding + jp_tax
    effective_rate   = total_tax / div_jpy if div_jpy > 0 else 0

    # 外国税額控除（確定申告）で米国分を取り戻せる上限
    # 控除上限: (jp_tax / 0.2) * 0.1 = jp_tax × 0.5
    credit_limit     = jp_tax * (US_DIVIDEND_WITHHOLDING / JP_DIVIDEND_TAX)
    credit           = min(us_withholding, credit_limit)
    total_after_credit = total_tax - credit
    effective_after  = total_after_credit / div_jpy if div_jpy > 0 else 0

    recommendation = (
        '確定申告で外国税額控除を申請することで米国源泉税の一部を取り戻せます。'
        if annual_us_dividends_usd > 100 else
        '配当が少ない場合は確定申告のコストと効果を比較してください。'
    )

    return {
        'dividends_usd':      round(annual_us_dividends_usd, 2),
        'dividends_jpy':      round(div_jpy, 0),
        'us_withholding_jpy': round(us_withholding, 0),
        'jp_tax_jpy':         round(jp_tax, 0),
        'total_tax_no_credit': round(total_tax, 0),
        'effective_rate_no_credit': round(effective_rate, 4),
        'credit_jpy':         round(credit, 0),
        'total_after_credit': round(total_after_credit, 0),
        'effective_after_credit': round(effective_after, 4),
        'net_dividend_jpy':   round(div_jpy - total_after_credit, 0),
        'recommendation':     recommendation,
    }


# ============================================================
# 売却税金シミュレーション
# ============================================================

def calculate_sell_tax(
    ticker:       str,
    shares:       float,
    entry_price:  float,
    current_price: float,
    account_type: str = 'tokutei',   # 'tokutei' / 'ippan' / 'nisa'
    currency:     str = 'USD',
    fx_rate:      Optional[float] = None,
) -> dict:
    """
    売却時の税金と手取りを計算する。P0-1: fx_rate=None なら get_fx_rate_cached() 経由で取得。

    Returns:
        {
          'gross_proceeds_jpy': 売却総額（円）,
          'cost_jpy':           取得原価（円）,
          'gain_jpy':           利益（円）,
          'tax_jpy':            税額（円）,
          'net_jpy':            手取り（円）,
          'effective_rate':     実効税率,
        }
    """
    if currency == 'USD':
        if fx_rate is None:
            from utils import get_fx_rate_cached
            fx_rate, _ = get_fx_rate_cached()
        proceeds_jpy = shares * current_price * fx_rate
        cost_jpy     = shares * entry_price    * fx_rate
    else:
        proceeds_jpy = shares * current_price
        cost_jpy     = shares * entry_price

    gain_jpy = proceeds_jpy - cost_jpy
    tax_rate = {'tokutei': TAX_TOKUTEI, 'ippan': TAX_IPPAN, 'nisa': TAX_NISA}.get(account_type, TAX_TOKUTEI)
    tax_jpy  = max(0, gain_jpy * tax_rate)
    net_jpy  = proceeds_jpy - tax_jpy

    # P2-10: NISA 損失は特定口座と通算不可・繰越不可。明示的に警告。
    warnings = []
    if account_type == 'nisa' and gain_jpy < 0:
        warnings.append(
            f'⚠️ NISA 内の譲渡損 ¥{gain_jpy:,.0f} は特定口座と通算不可、繰越控除も不可。'
            f'この損失は税務上消失します（新NISA の制度仕様）。'
        )
    if account_type == 'nisa' and gain_jpy > 0:
        warnings.append(
            f'✅ NISA 内譲渡益 ¥{gain_jpy:,.0f} は完全非課税。'
            f'売却後、簿価 ¥{cost_jpy:,.0f} は翌年に生涯枠が復活。'
        )

    return {
        'ticker':           ticker,
        'shares':           shares,
        'entry_price':      entry_price,
        'current_price':    current_price,
        'currency':         currency,
        'account_type':     account_type,
        'gross_proceeds_jpy': round(proceeds_jpy, 0),
        'cost_jpy':           round(cost_jpy, 0),
        'gain_jpy':           round(gain_jpy, 0),
        'tax_jpy':            round(tax_jpy, 0),
        'net_jpy':            round(net_jpy, 0),
        'effective_rate':     round(tax_rate if gain_jpy > 0 else 0, 4),
        'warnings':           warnings,
        'nisa_quota_restored_next_year': round(cost_jpy, 0) if account_type == 'nisa' else 0,
    }


# ============================================================
# 総合分析レポート
# ============================================================

def get_full_tax_report(snapshot: Optional[dict] = None) -> dict:
    """
    NISA・損出し・外国税額控除の総合レポートを生成する。

    Returns:
        {
          'nisa':         NISA枠分析,
          'loss_harvest': 損出し候補,
          'summary':      総合サマリー,
          'as_of':        日時,
        }
    """
    nisa   = analyze_nisa_usage()
    losses = analyze_loss_harvest(snapshot)
    nisa_leak = detect_nisa_foreign_tax_leak()   # A-1
    harvest_pairs = suggest_loss_harvest_pairs(snapshot)  # A-2

    # 概算配当収入（holdings.jsonのUS株から推計）
    holdings     = _load_holdings()
    us_value_usd = 0
    for key, info in holdings.items():
        if info.get('currency') == 'USD' and info.get('investment_type') in ('medium', 'long'):
            us_value_usd += info.get('shares', 0) * info.get('entry_price', 0)
    # 配当利回り概算1%で推計
    est_dividends_usd = us_value_usd * 0.01
    foreign_tax = simulate_foreign_tax_credit(est_dividends_usd)

    today = date.today()
    urgent_actions = []

    # 12月26日まで1ヶ月以内なら損出しを急ぐ
    deadline = date(today.year, SONDASHI_DEADLINE_MONTH, SONDASHI_DEADLINE_DAY)
    if 0 <= losses['days_to_deadline'] <= 30 and losses['candidates']:
        urgent_actions.append({
            'priority': 1,
            'message':  f'⚠️ 損出し期限まで{losses["days_to_deadline"]}日！'
                        f'節税効果¥{losses["total_tax_saving"]/10000:.0f}万の損出しを急いでください。',
        })

    # NISA枠の推奨
    for rec in nisa['recommendations']:
        if rec.get('priority', 3) <= 2:
            urgent_actions.append({
                'priority': rec['priority'] + 1,
                'message':  rec['message'],
            })

    # A-1: NISA 内 US 高配当源泉税漏れ
    if nisa_leak.get('total_leak_jpy', 0) > 5000:
        urgent_actions.append({
            'priority': 2,
            'message':  nisa_leak['recommendation'],
        })

    # A-2: 損出しハーベスト（純利益 > ¥3 万）
    if harvest_pairs.get('total_net_benefit_jpy', 0) > 30_000:
        top = harvest_pairs['pairs'][0]
        urgent_actions.append({
            'priority': 2,
            'message':  (
                f'損出しハーベスト: {len(harvest_pairs["pairs"])} 件で純利益 '
                f'¥{harvest_pairs["total_net_benefit_jpy"]/10000:.1f}万（最大 {top["ticker"]} '
                f'→ {", ".join(top["substitutes"][:2])} 乗換、{WASH_SALE_WINDOW_DAYS} 日後再購入可）'
            ),
        })

    return {
        'nisa':         nisa,
        'loss_harvest': losses,
        'nisa_foreign_tax_leak': nisa_leak,   # A-1
        'loss_harvest_pairs': harvest_pairs,  # A-2
        'foreign_tax':  foreign_tax,
        'urgent_actions': sorted(urgent_actions, key=lambda x: x['priority']),
        'as_of':        today.isoformat(),
    }


# ============================================================
# CLI
# ============================================================

def _print_full_report(report: dict):
    nisa   = report['nisa']
    losses = report['loss_harvest']
    ft     = report['foreign_tax']

    print(f'\n=== 税務最適化レポート {report["as_of"]} ===')

    # 緊急アクション
    if report['urgent_actions']:
        print('\n【⚠️ 緊急アクション】')
        for a in report['urgent_actions']:
            print(f'  {a["message"]}')

    # NISA枠
    print('\n【NISA枠使用状況】')
    for person in ['husband', 'wife']:
        p = nisa.get(person, {})
        if not p:
            continue
        label = 'メイン' if person == 'husband' else 'サブ'
        print(f'\n  {label}（{p.get("broker","不明")}）')
        print(f'    つみたて投資枠: ¥{p["tsumitate_used"]/10000:.1f}万 / ¥{p["tsumitate_annual"]/10000:.0f}万'
              f'（残¥{p["tsumitate_remaining"]/10000:.1f}万）')
        print(f'    成長投資枠:     ¥{p["growth_used"]/10000:.1f}万 / ¥{p["growth_annual"]/10000:.0f}万'
              f'（残¥{p["growth_remaining"]/10000:.1f}万）')
        print(f'    生涯枠残余:     ¥{p["lifetime_remaining"]/10000:.0f}万')

    if nisa['recommendations']:
        print('\n  NISA推奨事項:')
        for r in nisa['recommendations'][:3]:
            print(f'    → {r["message"]}')

    # 損出し
    print(f'\n【損出し候補】（期限: {losses["deadline"]}）')
    if losses['candidates']:
        print(f'  損出し可能合計: ¥{losses["total_loss_jpy"]/10000:.1f}万  '
              f'節税効果: ¥{losses["total_tax_saving"]/10000:.1f}万')
        for c in losses['candidates'][:5]:
            print(f'  ⬇ {c["name"]}（{c["ticker"]}）: '
                  f'¥{c["unrealized_jpy"]/10000:.1f}万 ({c["unrealized_pct"]*100:.1f}%) '
                  f'→ 節税¥{c["tax_saving_jpy"]/10000:.1f}万')
    else:
        print('  損出し候補なし（含み損¥5万以上の銘柄がありません）')

    # 外国税額控除
    print(f'\n【外国税額控除シミュレーション】')
    print(f'  推定年間米国配当: ${ft["dividends_usd"]:,.0f} (¥{ft["dividends_jpy"]/10000:.1f}万)')
    print(f'  米国源泉税（10%）: ¥{ft["us_withholding_jpy"]/10000:.1f}万')
    print(f'  控除で取り戻せる額: ¥{ft["credit_jpy"]/10000:.1f}万')
    print(f'  実効税率: {ft["effective_rate_no_credit"]*100:.1f}% → 控除後{ft["effective_after_credit"]*100:.1f}%')
    print(f'  {ft["recommendation"]}')


if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'full'

    if cmd == 'nisa':
        result = analyze_nisa_usage()
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif cmd == 'loss':
        result = analyze_loss_harvest()
        if result['candidates']:
            print(f'損出し候補 {len(result["candidates"])}件: 節税効果¥{result["total_tax_saving"]/10000:.1f}万')
            for c in result['candidates']:
                print(f'  {c["name"]}: ¥{c["unrealized_jpy"]/10000:.1f}万 → 節税¥{c["tax_saving_jpy"]/10000:.1f}万')
        else:
            print('損出し候補なし')

    elif cmd == 'harvest':
        register = '--register' in sys.argv
        result = suggest_loss_harvest_pairs()
        pairs = result['pairs']
        if not pairs:
            print('損出しハーベスト提案なし（含み損 ≤ -10% かつ乗換純利益 > 0 の銘柄が無い）')
        else:
            print(f'損出しハーベスト {len(pairs)} 件 '
                  f'純利益合計 ¥{result["total_net_benefit_jpy"]/10000:.1f}万')
            for p in pairs:
                print(f'  [{p["priority"]}] {p["ticker"]}: '
                      f'含み損¥{p["unrealized_jpy"]/10000:.1f}万 '
                      f'→ 節税¥{p["tax_saving_jpy"]/10000:.1f}万 '
                      f'− 乗換¥{p["switch_cost_jpy"]/10000:.1f}万 '
                      f'= ¥{p["net_benefit_jpy"]/10000:.1f}万 '
                      f'代替: {",".join(p["substitutes"][:3])} '
                      f'復帰: {p["restart_eligible_date"]}')
            if register:
                n = register_wash_sale_timers(pairs)
                print(f'\n✅ {n} 件を action_state_tracker に登録（wash-sale 復帰リマインダー）')

    elif cmd == 'sell':
        # 使い方: python tax_optimizer.py sell <ticker> <shares> <entry> <current> [account] [currency]
        if len(sys.argv) < 6:
            print('使い方: python tax_optimizer.py sell <ticker> <株数> <取得単価> <現在値> [特定/nisa] [USD/JPY]')
            sys.exit(1)
        result = calculate_sell_tax(
            ticker        = sys.argv[2],
            shares        = float(sys.argv[3]),
            entry_price   = float(sys.argv[4]),
            current_price = float(sys.argv[5]),
            account_type  = sys.argv[6] if len(sys.argv) > 6 else 'tokutei',
            currency      = sys.argv[7] if len(sys.argv) > 7 else 'USD',
        )
        print(f'\n売却試算: {result["ticker"]} {result["shares"]}株')
        print(f'  売却総額: ¥{result["gross_proceeds_jpy"]/10000:.1f}万')
        print(f'  取得原価: ¥{result["cost_jpy"]/10000:.1f}万')
        print(f'  利益:     ¥{result["gain_jpy"]/10000:.1f}万')
        print(f'  税額:     ¥{result["tax_jpy"]/10000:.1f}万（{result["effective_rate"]*100:.2f}%）')
        print(f'  手取り:   ¥{result["net_jpy"]/10000:.1f}万')

    else:
        try:
            import portfolio_manager
            snapshot = portfolio_manager.build_portfolio_snapshot()
        except Exception:
            snapshot = None
        report = get_full_tax_report(snapshot)
        _print_full_report(report)
