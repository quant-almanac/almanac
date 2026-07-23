"""
ALMANAC v4.0 - クレカ積立管理モジュール
メイン・サブの2口座管理・ポイント効果計算・売却タイミング推奨・税金計算
"""

import json
from datetime import datetime, date
from pathlib import Path
from typing import Optional

# ============================================================
# 設定
# ============================================================

CREDIT_CARD_PLANS = {
    'husband': {
        'monthly_amount':      0,
        'fund':                'CONFIGURE_LOCALLY',
        'account_type':        'tokutei',    # 特定口座
        'card':                '',           # 使用クレカ（設定時に入力）
        'point_rate':          0.0,
    },
    'wife': {
        'monthly_amount':      0,
        'fund':                'CONFIGURE_LOCALLY',
        'account_type':        'tokutei',
        'card':                '',
        'point_rate':          0.0,
    },
}

SELL_STRATEGY = {
    'method':          'quarterly',   # 四半期売却
    'trigger_amount':  10_000_000,    # Public placeholder; configure locally
    'sell_target':     'all',         # 全額 or 一部
    'purpose':         'user_defined',
}

TAX_RATE = {
    'tokutei': 0.20315,   # 特定口座: 20.315%
    'nisa':    0.0,       # NISA: 非課税
}

DATA_PATH = Path(__file__).parent / 'credit_card_plans.json'

# ============================================================
# データ管理
# ============================================================

def _default_account(plan_key: str) -> dict:
    config = CREDIT_CARD_PLANS[plan_key]
    return {
        'plan_key':           plan_key,
        'monthly_amount':     config['monthly_amount'],
        'fund':               config['fund'],
        'account_type':       config['account_type'],
        'card':               config['card'],
        'point_rate':         config['point_rate'],
        'current_units':      0.0,         # 保有口数
        'avg_nav':            0.0,         # 平均取得NAV（円）
        'total_invested':     0.0,         # 累計積立額
        'total_points':       0.0,         # 累計獲得ポイント（円換算）
        'purchase_history':   [],          # [{date, amount, nav, units}]
        'sell_history':       [],          # [{date, units, nav, gain, tax, net}]
        'current_nav':        0.0,         # 最新NAV（手動or自動更新）
        'last_purchase_date': None,
        'notes':              '',
    }


def _default_data() -> dict:
    return {
        'husband': _default_account('husband'),
        'wife':    _default_account('wife'),
        'sell_strategy': SELL_STRATEGY.copy(),
    }


def load_cc_data() -> dict:
    """クレカ積立データをJSONから読み込む。"""
    if DATA_PATH.exists():
        with open(DATA_PATH, 'r', encoding='utf-8') as f:
            data = json.load(f)
        defaults = _default_data()
        for key in ['husband', 'wife']:
            if key not in data:
                data[key] = defaults[key]
            else:
                for k, v in defaults[key].items():
                    if k not in data[key]:
                        data[key][k] = v
        return data
    return _default_data()


def save_cc_data(data: dict) -> None:
    """クレカ積立データをJSONに保存する。"""
    with open(DATA_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


# ============================================================
# 積立記録
# ============================================================

def record_monthly_purchase(
    person: str,
    amount: int,
    nav: float,
    purchase_date: Optional[str] = None,
) -> dict:
    """
    月次積立を記録する。

    Args:
        person: 'husband' または 'wife'
        amount: 積立金額（円）
        nav: 買付時のNAV（円/口）
        purchase_date: 購入日（YYYY-MM-DD）

    Returns:
        更新後の口座データ
    """
    if person not in ('husband', 'wife'):
        return {'error': f'personは husband または wife を指定してください'}

    data  = load_cc_data()
    acc   = data[person]
    pdate = purchase_date or date.today().isoformat()

    # 口数計算
    units = amount / nav if nav > 0 else 0

    # 加重平均NAVを更新
    total_units_new = acc['current_units'] + units
    if total_units_new > 0:
        acc['avg_nav'] = (
            acc['avg_nav'] * acc['current_units'] + nav * units
        ) / total_units_new

    # ポイント計算
    points_earned = amount * acc['point_rate']

    acc['current_units']      = total_units_new
    acc['total_invested']     += amount
    acc['total_points']       += points_earned
    acc['last_purchase_date'] = pdate

    acc['purchase_history'].append({
        'date':           pdate,
        'amount':         amount,
        'nav':            nav,
        'units':          round(units, 4),
        'points_earned':  round(points_earned, 0),
    })

    data[person] = acc
    save_cc_data(data)
    return acc


def update_nav(person: str, current_nav: float) -> dict:
    """現在のNAVを更新する（評価額計算用）。"""
    if person not in ('husband', 'wife'):
        return {'error': '不正なperson指定'}
    data = load_cc_data()
    data[person]['current_nav'] = current_nav
    save_cc_data(data)
    return data[person]


def _recompute_from_history(acc: dict) -> dict:
    """purchase_history から current_units/avg_nav/total_invested/total_points/
    last_purchase_date を再計算する（record_monthly_purchase の累積ロジックを
    履歴全体に対して再生する。増分での巻き戻しによる浮動小数点誤差を避けるため）。
    """
    current_units = 0.0
    avg_nav = 0.0
    total_invested = 0.0
    total_points = 0.0
    last_purchase_date = None
    for p in acc.get('purchase_history', []):
        units = float(p.get('units', 0) or 0)
        nav = float(p.get('nav', 0) or 0)
        amount = float(p.get('amount', 0) or 0)
        new_total_units = current_units + units
        if new_total_units > 0:
            avg_nav = (avg_nav * current_units + nav * units) / new_total_units
        current_units = new_total_units
        total_invested += amount
        total_points += float(p.get('points_earned', amount * acc.get('point_rate', 0)) or 0)
        last_purchase_date = p.get('date')
    acc['current_units'] = current_units
    acc['avg_nav'] = avg_nav
    acc['total_invested'] = total_invested
    acc['total_points'] = total_points
    acc['last_purchase_date'] = last_purchase_date
    return acc


def remove_purchase(person: str, purchase_date: str) -> dict:
    """指定日の積立記録を取り消す（誤POST・二重送信からの復旧用）。

    purchase_history から該当日の最新（末尾）1件のみを削除し、
    current_units/avg_nav/total_invested/total_points を履歴全体から再計算する。
    """
    if person not in ('husband', 'wife'):
        return {'error': '不正なperson指定'}
    data = load_cc_data()
    acc = data[person]
    history = acc.get('purchase_history', [])
    idx = None
    for i in range(len(history) - 1, -1, -1):
        if history[i].get('date') == purchase_date:
            idx = i
            break
    if idx is None:
        return {'error': f'{purchase_date} の積立記録が見つかりません'}
    removed = history.pop(idx)
    acc = _recompute_from_history(acc)
    data[person] = acc
    save_cc_data(data)
    return {'removed': removed, 'account': acc}


# ============================================================
# 計算関数
# ============================================================

def calculate_point_benefit(
    monthly_amount: int,
    point_rate: float,
    months: int = 12,
) -> dict:
    """
    ポイント獲得効果の計算（年間・累計）。

    Args:
        monthly_amount: 月次積立額
        point_rate: ポイント還元率
        months: 計算期間（月数）

    Returns:
        {
          'monthly_points': 月間獲得ポイント（円換算）,
          'annual_points': 年間ポイント,
          'total_points': 累計ポイント,
          'effective_cost_reduction': 実質コスト削減率,
        }
    """
    monthly_points = monthly_amount * point_rate
    annual_points  = monthly_amount * 12 * point_rate
    total_points   = monthly_amount * months * point_rate

    return {
        'monthly_points':          round(monthly_points, 0),
        'annual_points':           round(annual_points, 0),
        'total_points':            round(total_points, 0),
        'effective_cost_reduction': round(point_rate, 4),
        'months':                  months,
    }


def calculate_current_value(person: str) -> dict:
    """
    現在の評価額・含み損益を計算する。

    Args:
        person: 'husband' または 'wife'

    Returns:
        {
          'current_value': 現在評価額（円）,
          'cost_basis': 取得コスト,
          'unrealized_pnl': 含み損益,
          'unrealized_pnl_pct': 含み損益率,
        }
    """
    data = load_cc_data()
    acc  = data[person]

    current_nav   = acc['current_nav'] if acc['current_nav'] > 0 else acc['avg_nav']
    current_value = acc['current_units'] * current_nav
    cost_basis    = acc['current_units'] * acc['avg_nav']
    unrealized    = current_value - cost_basis

    return {
        'person':           person,
        'current_units':    acc['current_units'],
        'avg_nav':          acc['avg_nav'],
        'current_nav':      current_nav,
        'current_value':    round(current_value, 0),
        'cost_basis':       round(cost_basis, 0),
        'unrealized_pnl':   round(unrealized, 0),
        'unrealized_pnl_pct': round(unrealized / cost_basis, 4) if cost_basis > 0 else 0,
        'total_invested':   acc['total_invested'],
        'total_points':     acc['total_points'],
    }


def calculate_sell_tax(
    person: str,
    sell_units: Optional[float] = None,
    sell_amount: Optional[float] = None,
) -> dict:
    """
    売却時の税金・手取り額を計算する。

    Args:
        person: 'husband' または 'wife'
        sell_units: 売却口数（Noneの場合は全口数）
        sell_amount: 売却金額（円）。sell_unitsより優先

    Returns:
        {
          'sell_amount': 売却金額,
          'gain': 利益,
          'tax': 税金,
          'net_proceeds': 手取り額,
          'tax_rate': 適用税率,
        }
    """
    data = load_cc_data()
    acc  = data[person]

    current_nav = acc['current_nav'] if acc['current_nav'] > 0 else acc['avg_nav']

    if sell_amount is not None:
        # 金額指定 → 口数を逆算
        units_to_sell = sell_amount / current_nav if current_nav > 0 else 0
    elif sell_units is not None:
        units_to_sell = sell_units
    else:
        units_to_sell = acc['current_units']    # 全口数

    units_to_sell = min(units_to_sell, acc['current_units'])
    actual_sell_amount = units_to_sell * current_nav
    cost_basis_sell    = units_to_sell * acc['avg_nav']
    gain               = actual_sell_amount - cost_basis_sell

    account_type = acc['account_type']
    tax_rate     = TAX_RATE.get(account_type, 0.20315)
    tax          = max(0, gain * tax_rate)
    net_proceeds = actual_sell_amount - tax

    return {
        'person':        person,
        'units_to_sell': round(units_to_sell, 4),
        'sell_amount':   round(actual_sell_amount, 0),
        'cost_basis':    round(cost_basis_sell, 0),
        'gain':          round(gain, 0),
        'tax_rate':      tax_rate,
        'tax':           round(tax, 0),
        'net_proceeds':  round(net_proceeds, 0),
        'account_type':  account_type,
        'note':          '含み損の場合は課税なし（損出し活用可能）' if gain < 0 else '',
    }


def recommend_sell_timing(person: str) -> dict:
    """
    売却タイミングの推奨判断。

    条件:
      1. 残高が売却閾値（¥300,000）を超えている
      2. 四半期末が近い
      3. 含み損の場合は損出し提案

    Args:
        person: 'husband' または 'wife'

    Returns:
        {
          'should_sell': 売却推奨か,
          'reason': 理由,
          'sell_amount': 推奨売却額,
          'next_sell_date': 次回売却推奨日,
          'tax_calc': 税金計算結果,
        }
    """
    data     = load_cc_data()
    acc      = data[person]
    strategy = data['sell_strategy']

    val_result  = calculate_current_value(person)
    current_val = val_result['current_value']
    unrealized  = val_result['unrealized_pnl']

    # 売却閾値チェック
    trigger_amount = strategy['trigger_amount']
    should_sell    = current_val >= trigger_amount
    reasons        = []

    if current_val >= trigger_amount:
        reasons.append(f'残高¥{current_val:,.0f}が閾値¥{trigger_amount:,.0f}を超えました')

    # 四半期売却チェック
    today = date.today()
    is_quarter_end = today.month in [3, 6, 9, 12] and today.day >= 20
    if is_quarter_end:
        should_sell = True
        reasons.append('四半期末（定期売却タイミング）')

    # 損出し機会
    if unrealized < -10_000:
        should_sell = True
        reasons.append(f'含み損¥{abs(unrealized):,.0f}あり → 損出しに活用可能')

    # 次回推奨日（次の四半期末）
    quarter = (today.month - 1) // 3
    next_q_month = [(3, 20), (6, 20), (9, 20), (12, 20)]
    q_idx = quarter
    if today.month == next_q_month[q_idx][0] and today.day >= 20:
        q_idx = (q_idx + 1) % 4
    next_q = next_q_month[q_idx]
    next_sell_year = today.year + (1 if next_q[0] < today.month else 0)
    next_sell_date = date(next_sell_year, next_q[0], next_q[1]).isoformat()

    tax_calc = calculate_sell_tax(person) if should_sell else {}

    return {
        'person':         person,
        'should_sell':    should_sell,
        'reason':         ' / '.join(reasons) if reasons else '売却タイミングではありません',
        'current_value':  current_val,
        'sell_amount':    tax_calc.get('sell_amount', 0),
        'net_proceeds':   tax_calc.get('net_proceeds', 0),
        'tax':            tax_calc.get('tax', 0),
        'next_sell_date': next_sell_date,
        'strategy':       strategy['method'],
    }


# ============================================================
# 売却記録
# ============================================================

def record_sell(
    person: str,
    units: float,
    nav: float,
    reason: str = 'quarterly',
    sell_date: Optional[str] = None,
) -> dict:
    """
    売却を実行・記録する。

    Args:
        person: 'husband' または 'wife'
        units: 売却口数
        nav: 売却時のNAV（円）
        reason: 売却理由
        sell_date: 売却日

    Returns:
        {
          'net_proceeds', 'tax', 'gain', ...
        }
    """
    if person not in ('husband', 'wife'):
        return {'error': '不正なperson指定'}

    data  = load_cc_data()
    acc   = data[person]
    sdate = sell_date or date.today().isoformat()

    if units > acc['current_units']:
        return {'error': f'売却口数 {units} が保有口数 {acc["current_units"]} を超えています'}

    sell_amount = units * nav
    cost_basis  = units * acc['avg_nav']
    gain        = sell_amount - cost_basis
    tax_rate    = TAX_RATE.get(acc['account_type'], 0.20315)
    tax         = max(0, gain * tax_rate)
    net_proceeds = sell_amount - tax

    acc['sell_history'].append({
        'date':          sdate,
        'units':         units,
        'nav':           nav,
        'sell_amount':   round(sell_amount, 0),
        'cost_basis':    round(cost_basis, 0),
        'gain':          round(gain, 0),
        'tax':           round(tax, 0),
        'net_proceeds':  round(net_proceeds, 0),
        'reason':        reason,
    })

    acc['current_units'] -= units
    if acc['current_units'] <= 0:
        acc['current_units'] = 0
        acc['avg_nav']       = 0

    data[person] = acc
    save_cc_data(data)

    return {
        'person':        person,
        'units_sold':    units,
        'sell_amount':   round(sell_amount, 0),
        'gain':          round(gain, 0),
        'tax':           round(tax, 0),
        'net_proceeds':  round(net_proceeds, 0),
        'remaining_units': acc['current_units'],
    }


# ============================================================
# ダッシュボード用集計
# ============================================================

def get_combined_summary() -> dict:
    """
    メイン・サブの2口座合計サマリーを返す。

    Returns:
        2口座合計の評価額・ポイント・売却フロー等
    """
    data = load_cc_data()

    husband_val = calculate_current_value('husband')
    wife_val    = calculate_current_value('wife')
    husband_rec = recommend_sell_timing('husband')
    wife_rec    = recommend_sell_timing('wife')
    husband_pts = calculate_point_benefit(
        data['husband']['monthly_amount'],
        data['husband']['point_rate'],
    )
    wife_pts = calculate_point_benefit(
        data['wife']['monthly_amount'],
        data['wife']['point_rate'],
    )

    total_value         = husband_val['current_value'] + wife_val['current_value']
    total_invested      = data['husband']['total_invested'] + data['wife']['total_invested']
    total_points        = data['husband']['total_points'] + data['wife']['total_points']
    annual_points_total = husband_pts['annual_points'] + wife_pts['annual_points']
    total_monthly       = data['husband']['monthly_amount'] + data['wife']['monthly_amount']

    return {
        'husband': {
            'valuation':          husband_val,
            'sell_recommendation': husband_rec,
            'point_benefit':      husband_pts,
            'monthly_amount':     data['husband']['monthly_amount'],
            'fund':               data['husband']['fund'],
            'last_purchase':      data['husband']['last_purchase_date'],
        },
        'wife': {
            'valuation':          wife_val,
            'sell_recommendation': wife_rec,
            'point_benefit':      wife_pts,
            'monthly_amount':     data['wife']['monthly_amount'],
            'fund':               data['wife']['fund'],
            'last_purchase':      data['wife']['last_purchase_date'],
        },
        'combined': {
            'total_value':         round(total_value, 0),
            'total_invested':      round(total_invested, 0),
            'total_monthly':       total_monthly,
            'total_points':        round(total_points, 0),
            'annual_points_effect': round(annual_points_total, 0),
            'sell_cash_flow': {
                'husband_next_sell': husband_rec['next_sell_date'],
                'husband_net':       husband_rec['net_proceeds'],
                'wife_next_sell':    wife_rec['next_sell_date'],
                'wife_net':          wife_rec['net_proceeds'],
                'total_available':   round(husband_rec['net_proceeds'] + wife_rec['net_proceeds'], 0),
            },
        },
    }


def get_dashboard_data() -> dict:
    """
    ダッシュボード表示に必要な全データをまとめて返す。
    """
    summary = get_combined_summary()

    # 今月の積立状況
    today      = date.today()
    this_month = today.strftime('%Y-%m')
    data       = load_cc_data()

    def get_this_month_purchase(acc: dict) -> Optional[dict]:
        for p in reversed(acc['purchase_history']):
            if p['date'].startswith(this_month):
                return p
        return None

    husband_purchase = get_this_month_purchase(data['husband'])
    wife_purchase    = get_this_month_purchase(data['wife'])

    return {
        'summary':        summary,
        'this_month': {
            'husband': husband_purchase,
            'wife':    wife_purchase,
            'both_completed': (husband_purchase is not None and wife_purchase is not None),
        },
        'annual_point_benefit_total': (
            data['husband']['monthly_amount'] * 12 * data['husband']['point_rate']
            + data['wife']['monthly_amount']  * 12 * data['wife']['point_rate']
        ),
    }


# ============================================================
# 月次リマインダー（crontabから呼び出す）
# ============================================================

def send_monthly_reminder():
    """
    毎月1日にTelegramでクレカ積立の記録リマインダーを送信する。
    crontabに登録して自動化する。
    """
    import os
    import requests

    token   = os.environ.get('TELEGRAM_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print('TELEGRAM_TOKEN / TELEGRAM_CHAT_ID が未設定です')
        return

    data    = load_cc_data()
    summary = get_combined_summary()
    today   = date.today()
    month   = today.strftime('%Y年%m月')

    husband = data['husband']
    wife    = data['wife']

    msg = (
        f'📅 <b>クレカ積立 月次記録リマインダー</b>\n'
        f'{'━' * 16}\n'
        f'{month}の積立を記録してください。\n\n'
        f'<b>メイン</b>: ¥{husband["monthly_amount"]:,}/月 ({husband["fund"]})\n'
        f'<b>サブ</b>: ¥{wife["monthly_amount"]:,}/月 ({wife["fund"]})\n\n'
        f'📊 現在残高\n'
        f'  メイン: ¥{summary["husband"]["valuation"]["current_value"]:,.0f}\n'
        f'  サブ:   ¥{summary["wife"]["valuation"]["current_value"]:,.0f}\n\n'
        f'💡 Streamlitダッシュボード → タブ5「クレカ積立」で記録'
    )

    url = f'https://api.telegram.org/bot{token}/sendMessage'
    resp = requests.post(url, data={
        'chat_id':    chat_id,
        'text':       msg,
        'parse_mode': 'HTML',
    })
    if resp.ok:
        print(f'月次リマインダーを送信しました（{today.isoformat()}）')
    else:
        print(f'送信エラー: {resp.text}')


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]

    if not args or args[0] == 'status':
        summary = get_combined_summary()
        combined = summary['combined']
        print(f'\n=== クレカ積立状況 {date.today().isoformat()} ===')
        print(f'合計残高: ¥{combined["total_value"]:,.0f}')
        print(f'年間ポイント効果: ¥{combined["annual_points_effect"]:,.0f}')
        for person in ['husband', 'wife']:
            p = summary[person]
            label = 'メイン' if person == 'husband' else 'サブ'
            print(f'\n{label}:')
            print(f'  残高: ¥{p["valuation"]["current_value"]:,.0f}')
            print(f'  含み損益: ¥{p["valuation"]["unrealized_pnl"]:+,.0f}')
            rec = p['sell_recommendation']
            if rec['should_sell']:
                print(f'  ⚠️ 売却推奨: {rec["reason"]}')

    elif args[0] == 'remind':
        send_monthly_reminder()

    else:
        print('使い方:')
        print('  python credit_card_investment.py status   # 現在状況')
        print('  python credit_card_investment.py remind   # Telegramリマインダー送信')
