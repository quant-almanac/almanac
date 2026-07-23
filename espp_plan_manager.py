"""
ALMANAC v4.0 - 持株会管理モジュール
集中リスク分析・奨励金込みリターン計算・売却戦略・Claude判断支援
"""

import json
import os
from datetime import datetime, date
from pathlib import Path
from typing import Optional
import yfinance as yf

# ============================================================
# 設定
# ============================================================

ESPP_PLAN_CONFIG = {
    # Public-safe defaults. Configure the real plan only in local private state/env.
    'ticker':             os.getenv('ALMANAC_ESPP_TICKER', '9999.T'),
    'monthly_amount':     int(os.getenv('ALMANAC_ESPP_MONTHLY_AMOUNT_JPY', '0')),
    'monthly_employee':   int(os.getenv('ALMANAC_ESPP_EMPLOYEE_AMOUNT_JPY', '0')),
    'bonus_amount':       int(os.getenv('ALMANAC_ESPP_BONUS_AMOUNT_JPY', '0')),
    'incentive_rate':     float(os.getenv('ALMANAC_ESPP_INCENTIVE_RATE', '0')),
    'hold_limit_pct':     0.10,      # 総ポートフォリオの10%を超えたら売却検討
    'sell_strategy':     'quarterly',# 四半期ごとに上限超過分を売却
}

DATA_PATH = Path(__file__).parent / 'espp_plan.json'

# ============================================================
# データ管理
# ============================================================

def _default_data() -> dict:
    return {
        'ticker':              ESPP_PLAN_CONFIG['ticker'],
        'monthly_amount':      ESPP_PLAN_CONFIG['monthly_amount'],
        'incentive_rate':      ESPP_PLAN_CONFIG['incentive_rate'],
        'hold_limit_pct':      0.10,
        'current_shares':      0.0,
        'avg_cost':            0.0,       # 奨励金調整前の平均取得単価
        'total_invested':      0.0,       # 累計積立額
        'total_incentive':     0.0,       # 累計奨励金受領額
        'purchase_history':    [],        # [{date, shares, price, incentive}]
        'last_purchase_date':  None,
        'sell_history':        [],        # [{date, shares, price, reason}]
        'notes':               '',
    }


def load_espp_data() -> dict:
    """持株会データをJSONから読み込む。なければデフォルト値を返す。"""
    if DATA_PATH.exists():
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        # 旧データとのマージ（キー追加）
        defaults = _default_data()
        for key, val in defaults.items():
            if key not in data:
                data[key] = val
        return data
    return _default_data()


def save_espp_data(data: dict) -> None:
    """持株会データをJSONに保存する。"""
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ============================================================
# 価格取得
# ============================================================

def get_espp_price() -> Optional[float]:
    """yfinanceからローカル設定済みの持株会銘柄の現在株価を取得する。"""
    try:
        symbol = str(load_espp_data().get('ticker') or ESPP_PLAN_CONFIG['ticker'])
        ticker = yf.Ticker(symbol)
        hist = ticker.history(period='2d')
        if not hist.empty:
            return float(hist['Close'].iloc[-1])
        info = ticker.info
        return float(info.get('currentPrice') or info.get('regularMarketPrice', 0))
    except Exception:
        return None


def get_espp_financials() -> dict:
    """持株会銘柄の財務情報（yfinance）を取得する。"""
    try:
        symbol = str(load_espp_data().get('ticker') or ESPP_PLAN_CONFIG['ticker'])
        ticker = yf.Ticker(symbol)
        info = ticker.info
        return {
            'company_name':  info.get('longName', symbol),
            'sector':        info.get('sector', '資本財'),
            'pe_ratio':      info.get('trailingPE'),
            'pb_ratio':      info.get('priceToBook'),
            'roe':           info.get('returnOnEquity'),
            'revenue_growth': info.get('revenueGrowth'),
            'dividend_yield': info.get('dividendYield'),
            'market_cap':    info.get('marketCap'),
            'analyst_recommendation': info.get('recommendationKey', 'N/A'),
        }
    except Exception:
        return {'error': '財務データ取得失敗'}


# ============================================================
# 積立記録
# ============================================================

def record_monthly_purchase(
    shares: float,
    purchase_price: float,
    incentive_rate: Optional[float] = None,
    purchase_date: Optional[str] = None,
) -> dict:
    """
    月次積立の記録を追加する。

    Args:
        shares: 取得株数
        purchase_price: 1株あたりの取得価格（奨励金適用前）
        incentive_rate: 奨励金率（Noneの場合は設定値を使用）
        purchase_date: 購入日（YYYY-MM-DD。Noneの場合は今日）

    Returns:
        更新後の持株会データ
    """
    data = load_espp_data()
    rate = incentive_rate if incentive_rate is not None else data['incentive_rate']
    pdate = purchase_date or date.today().isoformat()

    # 奨励金額
    incentive_amount = purchase_price * shares * rate
    net_cost = purchase_price * shares   # 積立額（奨励金は別途受領）

    # 加重平均取得単価を更新（奨励金調整前）
    total_shares_new = data['current_shares'] + shares
    if total_shares_new > 0:
        data['avg_cost'] = (
            data['avg_cost'] * data['current_shares'] + purchase_price * shares
        ) / total_shares_new

    data['current_shares']   = total_shares_new
    data['total_invested']   += net_cost
    data['total_incentive']  += incentive_amount
    data['last_purchase_date'] = pdate

    data['purchase_history'].append({
        'date':             pdate,
        'shares':           shares,
        'price':            purchase_price,
        'cost':             net_cost,
        'incentive':        incentive_amount,
        'incentive_rate':   rate,
    })

    save_espp_data(data)
    return data


def record_sell(
    shares: float,
    sell_price: float,
    reason: str = 'quarterly',
    sell_date: Optional[str] = None,
) -> dict:
    """
    売却記録を追加する。

    Args:
        shares: 売却株数
        sell_price: 売却単価
        reason: 売却理由（'quarterly', 'concentration', 'tax_harvest' 等）
        sell_date: 売却日

    Returns:
        {'net_proceeds', 'tax', 'gain', '更新データ'}
    """
    data = load_espp_data()
    sdate = sell_date or date.today().isoformat()

    if shares > data['current_shares']:
        return {'error': f'売却株数 {shares} が保有株数 {data["current_shares"]} を超えています'}

    # 税金計算（特定口座: 20.315%）
    sell_amount = shares * sell_price
    cost_basis  = shares * data['avg_cost']
    gain        = sell_amount - cost_basis
    tax         = max(0, gain * 0.20315)
    net_proceeds = sell_amount - tax

    data['sell_history'].append({
        'date':          sdate,
        'shares':        shares,
        'price':         sell_price,
        'gain':          round(gain, 0),
        'tax':           round(tax, 0),
        'net_proceeds':  round(net_proceeds, 0),
        'reason':        reason,
    })

    data['current_shares'] -= shares
    # 株数がゼロになったら平均取得単価もリセット
    if data['current_shares'] <= 0:
        data['current_shares'] = 0
        data['avg_cost'] = 0

    save_espp_data(data)
    return {
        'sell_amount':   round(sell_amount, 0),
        'gain':          round(gain, 0),
        'tax':           round(tax, 0),
        'net_proceeds':  round(net_proceeds, 0),
        'data':          data,
    }


# ============================================================
# 分析関数
# ============================================================

def calculate_effective_return(
    purchase_price: float,
    incentive_rate: float,
    current_price: float,
) -> dict:
    """
    奨励金を含めた実質リターンを計算する。
    実質取得コスト = 購入価格 × (1 - 奨励金率)

    Args:
        purchase_price: 取得価格
        incentive_rate: 奨励金率
        current_price: 現在株価

    Returns:
        {
          'effective_cost': 実質取得コスト（奨励金調整後）,
          'price_return': 価格上昇率のみ,
          'effective_return': 実質リターン（奨励金込み）,
          'incentive_bonus': 奨励金ボーナス（%）,
        }
    """
    effective_cost   = purchase_price * (1 - incentive_rate)
    price_return     = (current_price - purchase_price) / purchase_price
    effective_return = (current_price - effective_cost) / effective_cost

    return {
        'effective_cost':    round(effective_cost, 0),
        'price_return':      round(price_return, 4),
        'effective_return':  round(effective_return, 4),
        'incentive_bonus':   round(incentive_rate, 4),
        'breakeven_price':   round(effective_cost, 0),
    }


def analyze_espp_concentration(
    portfolio_total: float,
    espp_value: float,
    annual_salary: float = 0,
    years_to_retirement: int = 30,
) -> dict:
    """
    持株会の集中リスク分析。
    ポートフォリオ比率の計算・10%超過アラート・人的資本との連動リスク。

    Args:
        portfolio_total: 総ポートフォリオ価値（円）
        espp_value: 持株会の現在評価額（円）
        annual_salary: 年収（0の場合は人的資本計算スキップ）
        years_to_retirement: 退職までの年数

    Returns:
        集中リスク分析結果のdict
    """
    config = ESPP_PLAN_CONFIG
    # tunable_params: espp_hold_limit_pct があれば動的上書き（％ → 0-1）
    try:
        from tunable_params import get as _tp_get
        _v = _tp_get("espp_hold_limit_pct")
        _hold_limit = float(_v) / 100.0 if _v is not None else config['hold_limit_pct']
    except Exception:
        _hold_limit = config['hold_limit_pct']
    ratio = espp_value / portfolio_total if portfolio_total > 0 else 0
    excess_pct = ratio - _hold_limit
    excess_value = max(0, excess_pct * portfolio_total)

    # 売却推奨額（上限超過分）
    sell_recommendation = max(0, espp_value - portfolio_total * _hold_limit)

    alert_level = 'normal'
    message = ''
    if ratio > _hold_limit:
        alert_level = 'warning'
        message = (
            f'持株会保有が総資産の{ratio*100:.1f}%に達しています（上限{_hold_limit*100:.0f}%）。'
            f'約¥{sell_recommendation:,.0f}の売却を検討してください。'
        )
    elif ratio > _hold_limit * 0.8:
        alert_level = 'caution'
        message = f'持株会保有が{ratio*100:.1f}%です。上限（{_hold_limit*100:.0f}%）に近づいています。'

    # 人的資本リスク
    human_capital_risk = None
    if annual_salary > 0:
        human_capital_pv = annual_salary * years_to_retirement * 0.7
        total_wealth = portfolio_total + human_capital_pv
        espp_total_exposure = (espp_value + human_capital_pv) / total_wealth
        human_capital_risk = {
            'human_capital_pv':        round(human_capital_pv, 0),
            'total_wealth_incl_hc':    round(total_wealth, 0),
            'espp_exposure_incl_hc': round(espp_total_exposure, 4),
            'description':             '持株会株式＋将来収入の合計がトータル資産に占める割合',
            'risk_level': (
                'critical' if espp_total_exposure > 0.5 else
                'warning'  if espp_total_exposure > 0.3 else
                'normal'
            ),
        }

    return {
        'espp_value':          round(espp_value, 0),
        'portfolio_total':       round(portfolio_total, 0),
        'ratio':                 round(ratio, 4),
        'hold_limit_pct':        config['hold_limit_pct'],
        'alert_level':           alert_level,
        'message':               message,
        'sell_recommendation':   round(sell_recommendation, 0),
        'excess_value':          round(max(0, excess_pct) * portfolio_total, 0),
        'human_capital_risk':    human_capital_risk,
    }


def get_quarterly_sell_plan(portfolio_total: float) -> dict:
    """
    四半期売却計画を生成する。
    現在のポジションと上限に基づいて売却推奨額・株数を計算する。

    Args:
        portfolio_total: 総ポートフォリオ価値（円）

    Returns:
        {
          'should_sell': 売却すべきか,
          'sell_shares': 売却推奨株数,
          'sell_value': 売却推奨額（円）,
          'after_ratio': 売却後の比率,
          'next_review_date': 次回見直し日,
        }
    """
    data = load_espp_data()
    current_price = get_espp_price() or data['avg_cost']

    current_value = data['current_shares'] * current_price
    limit_value   = portfolio_total * ESPP_PLAN_CONFIG['hold_limit_pct']
    excess_value  = max(0, current_value - limit_value)
    sell_shares   = excess_value / current_price if current_price > 0 else 0

    # 次の四半期末を計算
    today = date.today()
    quarter = (today.month - 1) // 3
    next_quarter_month = (quarter + 1) * 3 + 1
    next_quarter_year  = today.year + (1 if next_quarter_month > 12 else 0)
    next_quarter_month = next_quarter_month if next_quarter_month <= 12 else next_quarter_month - 12
    next_review = date(next_quarter_year, next_quarter_month, 1).isoformat()

    after_shares = data['current_shares'] - sell_shares
    after_value  = after_shares * current_price
    after_ratio  = after_value / portfolio_total if portfolio_total > 0 else 0

    return {
        'should_sell':       excess_value > 0,
        'current_shares':    data['current_shares'],
        'current_value':     round(current_value, 0),
        'current_ratio':     round(current_value / portfolio_total, 4) if portfolio_total > 0 else 0,
        'sell_shares':       round(sell_shares, 2),
        'sell_value':        round(excess_value, 0),
        'after_ratio':       round(after_ratio, 4),
        'current_price':     current_price,
        'next_review_date':  next_review,
    }


def check_tax_harvest_opportunity() -> dict:
    """
    損出し機会チェック。含み損の場合は損出し活用を提案する。

    Returns:
        {
          'has_unrealized_loss': 含み損があるか,
          'unrealized_pnl': 含み損益（円）,
          'tax_saving_estimate': 損出しで節税できる見込み額,
          'recommendation': 推奨アクション,
        }
    """
    data = load_espp_data()
    current_price = get_espp_price()

    if current_price is None or data['current_shares'] == 0 or data['avg_cost'] == 0:
        return {'error': 'データ不足（株数・取得単価・現在価格が必要）'}

    market_value    = data['current_shares'] * current_price
    cost_basis      = data['current_shares'] * data['avg_cost']
    unrealized_pnl  = market_value - cost_basis
    has_loss        = unrealized_pnl < 0

    tax_saving = 0
    recommendation = ''

    if has_loss:
        # 損出しで節税できる見込み（他に利益があった場合に相殺）
        tax_saving = abs(unrealized_pnl) * 0.20315
        recommendation = (
            f'含み損¥{abs(unrealized_pnl):,.0f}を損出しに活用可能。'
            f'節税効果は約¥{tax_saving:,.0f}。'
            f'翌日に同額を買い戻すことで持株会ポジションを継続できます。'
            f'（日本にはウォッシュセールルールなし）'
        )
    else:
        recommendation = f'現在含み益¥{unrealized_pnl:,.0f}。損出しの対象ではありません。'

    return {
        'has_unrealized_loss':  has_loss,
        'current_price':        current_price,
        'avg_cost':             data['avg_cost'],
        'current_shares':       data['current_shares'],
        'market_value':         round(market_value, 0),
        'cost_basis':           round(cost_basis, 0),
        'unrealized_pnl':       round(unrealized_pnl, 0),
        'unrealized_pnl_pct':   round(unrealized_pnl / cost_basis, 4) if cost_basis > 0 else 0,
        'tax_saving_estimate':  round(tax_saving, 0),
        'recommendation':       recommendation,
        'year_end_deadline':    '12月26日（損出し実行期限）',
    }


def espp_hold_or_sell_analysis(portfolio_total: float) -> dict:
    """
    保有継続 vs 売却の総合分析（Claude API呼び出し用のデータ収集）。

    Args:
        portfolio_total: 総ポートフォリオ価値（円）

    Returns:
        Claude API に渡すための分析データ（dict）
    """
    data = load_espp_data()
    current_price = get_espp_price()
    financials    = get_espp_financials()

    if current_price is None:
        current_price = data['avg_cost']

    current_value   = data['current_shares'] * current_price
    effective_ret   = calculate_effective_return(
        data['avg_cost'], data['incentive_rate'], current_price
    ) if data['avg_cost'] > 0 else {}
    concentration   = analyze_espp_concentration(portfolio_total, current_value)
    tax_harvest     = check_tax_harvest_opportunity()
    quarterly_plan  = get_quarterly_sell_plan(portfolio_total)

    return {
        'summary': {
            'current_shares':    data['current_shares'],
            'avg_cost':          data['avg_cost'],
            'current_price':     current_price,
            'current_value':     round(current_value, 0),
            'total_invested':    data['total_invested'],
            'total_incentive':   data['total_incentive'],
            'monthly_amount':    data['monthly_amount'],
            'incentive_rate':    data['incentive_rate'],
        },
        'returns':           effective_ret,
        'concentration':     concentration,
        'tax_harvest':       tax_harvest,
        'quarterly_plan':    quarterly_plan,
        'financials':        financials,
        'analysis_prompt': (
            f"持株会銘柄（{data.get('ticker') or 'EMPLOYER_STOCK'}）について保有継続vs売却を判断してください。\n"
            f"現在株価: ¥{current_price:,.0f} / 保有株数: {data['current_shares']}株 / "
            f"平均取得単価: ¥{data['avg_cost']:,.0f}\n"
            f"含み損益: ¥{current_value - data['current_shares'] * data['avg_cost']:,.0f}\n"
            f"ポートフォリオ比率: {concentration['ratio']*100:.1f}%（上限10%）\n"
            f"奨励金込み実質リターン: {effective_ret.get('effective_return', 'N/A')}\n"
            f"PER: {financials.get('pe_ratio', 'N/A')} / ROE: {financials.get('roe', 'N/A')}\n"
            f"アナリスト評価: {financials.get('analyst_recommendation', 'N/A')}\n"
            f"集中リスク: {concentration['alert_level']}\n"
            f"損出し機会: {tax_harvest.get('has_unrealized_loss', False)}\n"
            f"四半期売却推奨: {quarterly_plan['sell_value']:,.0f}円\n\n"
            f"人的資本（勤務先非公開）との集中リスクも考慮して判断してください。"
        ),
    }


def get_dashboard_data(portfolio_total: float) -> dict:
    """
    ダッシュボード表示用データをまとめて返す。

    Args:
        portfolio_total: 総ポートフォリオ価値（円）

    Returns:
        ダッシュボード表示用dict
    """
    data = load_espp_data()
    current_price = get_espp_price()
    if current_price is None:
        current_price = data['avg_cost'] if data['avg_cost'] > 0 else 0

    current_value   = data['current_shares'] * current_price
    unrealized_pnl  = current_value - data['current_shares'] * data['avg_cost']
    ratio           = current_value / portfolio_total if portfolio_total > 0 else 0

    effective_ret   = calculate_effective_return(
        data['avg_cost'], data['incentive_rate'], current_price
    ) if data['avg_cost'] > 0 else {}

    # 今月の積立予定日（翌月1日）
    today = date.today()
    next_month = today.replace(day=1, month=today.month % 12 + 1) if today.month < 12 else today.replace(year=today.year + 1, month=1, day=1)

    concentration = analyze_espp_concentration(portfolio_total, current_value)

    return {
        'current_shares':       data['current_shares'],
        'current_price':        current_price,
        'current_value':        round(current_value, 0),
        'avg_cost':             data['avg_cost'],
        'unrealized_pnl':       round(unrealized_pnl, 0),
        'unrealized_pnl_pct':   round(unrealized_pnl / (data['current_shares'] * data['avg_cost']), 4) if data['current_shares'] > 0 and data['avg_cost'] > 0 else 0,
        'portfolio_ratio':      round(ratio, 4),
        'hold_limit_pct':       ESPP_PLAN_CONFIG['hold_limit_pct'],
        'total_invested':       data['total_invested'],
        'total_incentive':      data['total_incentive'],
        'monthly_amount':       data['monthly_amount'],
        'incentive_rate':       data['incentive_rate'],
        'effective_return':     effective_ret.get('effective_return'),
        'next_purchase_date':   next_month.isoformat(),
        'concentration_alert':  concentration['alert_level'],
        'concentration_message': concentration['message'],
        'sell_recommendation':  concentration['sell_recommendation'],
        'last_purchase_date':   data['last_purchase_date'],
    }
