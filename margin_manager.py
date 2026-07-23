"""
margin_manager.py — 信用建玉管理・証拠金維持率監視

信用建玉（margin_positions.json に永続化）の管理。
- 信用買い / 空売り ポジション記録
- 証拠金維持率計算（委託保証金 / 建玉評価額）
- 追証警戒ライン通知（維持率 < 130% で警告 / < 110% で緊急アラート）
- ロールオーバー期日管理（6ヶ月 / 無期限）

証拠金維持率の計算式（SBI 証券準拠）:
  委託保証金 = 現金担保 + 有価証券担保（掛目80%）
  建玉評価額 = 信用買い建玉の評価額 + 空売り建玉の評価額
  維持率(%) = 委託保証金 / 建玉評価額 × 100

追証発生ライン: 20%（最低維持率）
警戒ライン   : 130%（保守的な管理基準）
緊急ライン   : 110%（即座な対応が必要）
"""

import json
import os
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests
import yfinance as yf
from utils import init_yfinance_timeout

init_yfinance_timeout()

BASE_DIR = Path(__file__).parent

# ---- Telegram ----
TELEGRAM_TOKEN   = os.environ.get('TELEGRAM_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# ---- 証拠金維持率基準 ----
MARGIN_EMERGENCY_PCT  = 110.0   # 緊急アラート
MARGIN_WARNING_PCT    = 130.0   # 警告
MARGIN_COMFORTABLE_PCT = 200.0  # 安全水準

# ---- 建玉期日 ----
MARGIN_EXPIRY_MONTHS_6M  = 6    # 一般信用（6ヶ月）
DAYS_BEFORE_EXPIRY_WARN  = 14   # 期日 14 日前に警告

# ---- データファイル ----
MARGIN_POS_FILE = BASE_DIR / 'margin_positions.json'


# ============================================================
# 内部ユーティリティ
# ============================================================

def _send_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        url = f'https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage'
        requests.post(url, data={'chat_id': TELEGRAM_CHAT_ID, 'text': msg,
                                 'parse_mode': 'HTML'}, timeout=10)
    except Exception:
        pass


def _load_positions() -> dict:
    """margin_positions.json をロード（存在しない場合は空データを返す）"""
    if not MARGIN_POS_FILE.exists():
        return {
            'cash_collateral':       0,       # 現金担保（円）
            'securities_collateral': 0,       # 有価証券担保（時価、円）
            'sec_haircut':           0.80,    # 有価証券担保の掛目
            'positions':             [],      # 建玉リスト
            'updated':               '',
        }
    try:
        with open(MARGIN_POS_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {'cash_collateral': 0, 'securities_collateral': 0,
                'sec_haircut': 0.80, 'positions': [], 'updated': ''}


def _save_positions(data: dict) -> None:
    data['updated'] = datetime.now().strftime('%Y-%m-%d %H:%M')
    with open(MARGIN_POS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _get_current_price(ticker: str) -> float | None:
    """yfinance で現在価格を取得"""
    try:
        hist = yf.Ticker(ticker).history(period='5d')
        if hist.empty:
            return None
        return float(hist['Close'].iloc[-1])
    except Exception:
        return None


def _usdjpy() -> float:
    """USD/JPY レートを取得（P0-1: utils.get_fx_rate_cached() へ委譲、TTL キャッシュ + stale fallback）"""
    try:
        from utils import get_fx_rate_cached
        rate, _source = get_fx_rate_cached()
        return float(rate)
    except Exception:
        return 150.0


# ============================================================
# 担保・維持率計算
# ============================================================

def _calc_collateral(data: dict) -> float:
    """委託保証金を計算"""
    cash = float(data.get('cash_collateral', 0))
    sec  = float(data.get('securities_collateral', 0))
    haircut = float(data.get('sec_haircut', 0.80))
    return cash + sec * haircut


def _calc_position_value(pos: dict, fx: float) -> float:
    """建玉の評価額（円）を計算"""
    price    = float(pos.get('current_price', pos.get('entry_price', 0)))
    shares   = float(pos.get('shares', 0))
    currency = pos.get('currency', 'JPY')
    value    = price * shares
    if currency == 'USD':
        value *= fx
    return value


def calc_maintenance_ratio(data: dict, fx: float) -> float:
    """
    証拠金維持率(%) を計算。
    委託保証金 / Σ建玉評価額 × 100
    建玉がない場合は inf を返す。
    """
    collateral = _calc_collateral(data)
    positions  = data.get('positions', [])
    if not positions:
        return float('inf')

    total_pos_value = sum(_calc_position_value(p, fx) for p in positions)
    if total_pos_value <= 0:
        return float('inf')

    return collateral / total_pos_value * 100.0


# ============================================================
# 建玉の CRUD
# ============================================================

def add_position(
    ticker:     str,
    side:       str,          # 'long'（信用買い）or 'short'（空売り）
    shares:     float,
    entry_price: float,
    currency:   str = 'JPY',
    account:    str = '信用口座',
    position_type: str = '一般信用',   # '制度信用' or '一般信用'
    memo:       str = '',
) -> dict:
    """
    建玉を追加して保存する。
    返り値: 追加したポジション dict
    """
    if side not in ('long', 'short'):
        raise ValueError(f'side は "long" または "short" を指定してください: {side}')

    data = _load_positions()

    # 期日計算
    opened = date.today().isoformat()
    if position_type == '一般信用':
        expiry = (date.today() + timedelta(days=MARGIN_EXPIRY_MONTHS_6M * 30)).isoformat()
    else:
        expiry = None   # 制度信用は個別設定

    # 現在価格を取得（なければエントリー価格を使用）
    current_price = _get_current_price(ticker) or entry_price

    pos = {
        'id':            len(data['positions']) + 1,
        'ticker':        ticker,
        'side':          side,
        'shares':        shares,
        'entry_price':   entry_price,
        'current_price': current_price,
        'currency':      currency,
        'account':       account,
        'position_type': position_type,
        'opened':        opened,
        'expiry':        expiry,
        'memo':          memo,
        'closed':        False,
    }

    data['positions'].append(pos)
    _save_positions(data)
    print(f'建玉追加: {side} {ticker} × {shares} @ {entry_price:.2f} ({currency})')
    return pos


def close_position(pos_id: int, close_price: float | None = None) -> dict | None:
    """建玉を決済する（closed=True にする）"""
    data = _load_positions()
    for pos in data['positions']:
        if pos['id'] == pos_id and not pos.get('closed'):
            if close_price is None:
                close_price = _get_current_price(pos['ticker']) or pos['current_price']
            pos['closed']       = True
            pos['close_price']  = close_price
            pos['closed_at']    = datetime.now().strftime('%Y-%m-%d %H:%M')

            # 損益計算
            entry = float(pos['entry_price'])
            cl    = float(close_price)
            sh    = float(pos['shares'])
            if pos['side'] == 'long':
                pnl = (cl - entry) * sh
            else:  # short
                pnl = (entry - cl) * sh
            if pos['currency'] == 'USD':
                fx = _usdjpy()
                pnl *= fx
            pos['realized_pnl_jpy'] = round(pnl, 0)

            _save_positions(data)
            print(f'建玉決済: ID={pos_id} / 損益={pnl:+,.0f}円')
            return pos
    print(f'建玉 ID={pos_id} が見つかりません。')
    return None


def update_prices() -> dict:
    """全オープン建玉の現在価格を更新して維持率を計算"""
    data = _load_positions()
    fx   = _usdjpy()

    open_positions = [p for p in data['positions'] if not p.get('closed')]
    for pos in open_positions:
        price = _get_current_price(pos['ticker'])
        if price is not None:
            pos['current_price'] = price

    _save_positions(data)

    ratio = calc_maintenance_ratio(data, fx)
    return {
        'positions':         open_positions,
        'collateral':        _calc_collateral(data),
        'maintenance_ratio': ratio,
        'fx_usdjpy':         fx,
        'as_of':             datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


def set_collateral(cash: float = 0, securities: float = 0) -> None:
    """担保金額を設定"""
    data = _load_positions()
    data['cash_collateral']       = cash
    data['securities_collateral'] = securities
    _save_positions(data)
    print(f'担保設定: 現金={cash:,.0f}円 / 有価証券時価={securities:,.0f}円')


# ============================================================
# ポジション分析
# ============================================================

def get_summary() -> dict:
    """全建玉サマリーを返す"""
    data = _load_positions()
    fx   = _usdjpy()

    open_positions   = [p for p in data['positions'] if not p.get('closed')]
    closed_positions = [p for p in data['positions'] if p.get('closed')]

    # 含み損益計算
    for pos in open_positions:
        entry = float(pos['entry_price'])
        cur   = float(pos.get('current_price', entry))
        sh    = float(pos['shares'])
        if pos['side'] == 'long':
            pnl = (cur - entry) * sh
        else:
            pnl = (entry - cur) * sh
        if pos['currency'] == 'USD':
            pnl *= fx
        pos['unrealized_pnl_jpy'] = round(pnl, 0)
        pos['pnl_pct']            = (cur / entry - 1) * 100 * (1 if pos['side'] == 'long' else -1)

    total_unrealized = sum(p['unrealized_pnl_jpy'] for p in open_positions)
    total_realized   = sum(p.get('realized_pnl_jpy', 0) for p in closed_positions)
    collateral = _calc_collateral(data)
    ratio      = calc_maintenance_ratio(data, fx)

    # 期日チェック
    today         = date.today()
    expiry_alerts = []
    for pos in open_positions:
        if pos.get('expiry'):
            exp = date.fromisoformat(pos['expiry'])
            days_left = (exp - today).days
            if days_left <= DAYS_BEFORE_EXPIRY_WARN:
                expiry_alerts.append({
                    'ticker':    pos['ticker'],
                    'side':      pos['side'],
                    'days_left': days_left,
                    'expiry':    pos['expiry'],
                })

    # 維持率ステータス
    if ratio == float('inf'):
        margin_status = 'safe'
    elif ratio < MARGIN_EMERGENCY_PCT:
        margin_status = 'emergency'
    elif ratio < MARGIN_WARNING_PCT:
        margin_status = 'warning'
    elif ratio < MARGIN_COMFORTABLE_PCT:
        margin_status = 'caution'
    else:
        margin_status = 'safe'

    return {
        'open_positions':    open_positions,
        'closed_positions':  closed_positions,
        'collateral':        collateral,
        'maintenance_ratio': ratio,
        'margin_status':     margin_status,
        'total_unrealized':  total_unrealized,
        'total_realized':    total_realized,
        'expiry_alerts':     expiry_alerts,
        'fx_usdjpy':         fx,
        'as_of':             datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


def get_current_leverage(portfolio_total_jpy: float | None = None) -> dict:
    """
    現在の portfolio leverage (信用込み総ポジ ÷ 純資産) を返す。

    leverage = (現金ポジ評価額 + 信用買い評価額 - 信用空売り評価額) / 純資産
    純資産 = 現金ポジ評価額 + (信用買い評価額 - 信用買いの借入額) + (空売り利益)

    簡易計算:
      leverage ≒ (portfolio_total + margin_long_value - margin_short_value) / portfolio_total

    Args:
        portfolio_total_jpy: 現物ポートフォリオ総額（None なら portfolio_manager から取得）

    Returns:
        {
          "leverage": 1.0,             # 1.0 = フル現物・無レバ、1.2 = 20% レバ
          "margin_long_value":  XXX,   # 信用買い建玉評価額（円）
          "margin_short_value": XXX,   # 信用空売り建玉評価額（円）
          "portfolio_total":    XXX,   # 現物ポートフォリオ総額
          "maintenance_ratio":  XXX,   # 信用建玉維持率
          "as_of":              "..."
        }
    """
    data = _load_positions()
    fx   = _usdjpy()
    open_positions = [p for p in data['positions'] if not p.get('closed')]

    margin_long_value  = 0.0
    margin_short_value = 0.0
    for pos in open_positions:
        val = _calc_position_value(pos, fx)
        if pos['side'] == 'long':
            margin_long_value += val
        else:
            margin_short_value += val

    # 現物ポートフォリオ総額を取得
    if portfolio_total_jpy is None:
        try:
            from portfolio_manager import build_portfolio_snapshot
            snap = build_portfolio_snapshot() or {}
            portfolio_total_jpy = float(snap.get('total_jpy') or 0)
        except Exception:
            portfolio_total_jpy = 0.0

    portfolio_total_jpy = float(portfolio_total_jpy or 0)
    # leverage = (現物 + 信用買い - 信用空売り) / 現物（信用空売りは現物に対するヘッジ）
    if portfolio_total_jpy > 0:
        leverage = (portfolio_total_jpy + margin_long_value - margin_short_value) / portfolio_total_jpy
    else:
        leverage = 1.0

    return {
        "leverage":           round(leverage, 4),
        "margin_long_value":  round(margin_long_value, 0),
        "margin_short_value": round(margin_short_value, 0),
        "portfolio_total":    round(portfolio_total_jpy, 0),
        "maintenance_ratio":  calc_maintenance_ratio(data, fx),
        "as_of":              datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


def check_and_alert() -> dict:
    """維持率チェックして必要なら Telegram 通知"""
    summary = get_summary()
    ratio   = summary['maintenance_ratio']
    status  = summary['margin_status']

    if status == 'emergency':
        msg = (
            f'🚨 <b>追証警戒！</b>\n'
            f'証拠金維持率: <b>{ratio:.1f}%</b>（緊急ライン{MARGIN_EMERGENCY_PCT}%以下）\n'
            f'即座の対応が必要です。建玉を減らしてください。'
        )
        # ALMANAC: telegram disabled — ai_analysis only
        # _send_telegram(msg)

    elif status == 'warning':
        msg = (
            f'⚠️ <b>証拠金警告</b>\n'
            f'証拠金維持率: {ratio:.1f}%（警戒ライン{MARGIN_WARNING_PCT}%以下）\n'
            f'担保追加または建玉縮小を検討してください。'
        )
        # ALMANAC: telegram disabled — ai_analysis only
        # _send_telegram(msg)

    # 期日アラート
    for ea in summary['expiry_alerts']:
        msg = (
            f'📅 <b>建玉期日警告</b>\n'
            f'{ea["ticker"]} ({ea["side"]}) — 期日まで残り {ea["days_left"]} 日\n'
            f'ロールオーバーまたは決済を検討してください。'
        )
        # ALMANAC: telegram disabled — ai_analysis only
        # _send_telegram(msg)

    return summary


# ============================================================
# CLI
# ============================================================

def _print_summary(summary: dict) -> None:
    ratio  = summary['maintenance_ratio']
    status = summary['margin_status']
    status_label = {
        'safe':      '✅ 安全',
        'caution':   '🟡 注意',
        'warning':   '⚠️  警戒',
        'emergency': '🚨 緊急',
    }.get(status, status)

    print(f'\n=== 信用建玉サマリー ===')
    print(f'実行時刻  : {summary["as_of"]}')
    print(f'USD/JPY   : {summary["fx_usdjpy"]:.2f}')
    print(f'委託保証金: ¥{summary["collateral"]:,.0f}')
    if ratio == float('inf'):
        print(f'証拠金維持率: --- （建玉なし）')
    else:
        print(f'証拠金維持率: {ratio:.1f}%  {status_label}')

    print(f'\n含み損益: ¥{summary["total_unrealized"]:+,.0f}')
    print(f'確定損益: ¥{summary["total_realized"]:+,.0f}')

    open_pos = summary['open_positions']
    if open_pos:
        print(f'\nオープン建玉 ({len(open_pos)} 件):')
        for pos in open_pos:
            side_label = '信用買' if pos['side'] == 'long' else '空売り'
            pnl        = pos.get('unrealized_pnl_jpy', 0)
            pnl_pct    = pos.get('pnl_pct', 0)
            expiry     = pos.get('expiry', '---')
            print(f"  [{pos['id']}] {side_label} {pos['ticker']} × {pos['shares']}"
                  f"  @ {pos['entry_price']:.2f} → {pos.get('current_price', '?'):.2f}"
                  f"  | 含み: ¥{pnl:+,.0f} ({pnl_pct:+.1f}%)"
                  f"  | 期日: {expiry}")
    else:
        print('\nオープン建玉なし')

    if summary['expiry_alerts']:
        print('\n【期日警告】')
        for ea in summary['expiry_alerts']:
            print(f"  {ea['ticker']} ({ea['side']}) — 残り {ea['days_left']} 日 (期日: {ea['expiry']})")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='信用建玉管理')
    sub = parser.add_subparsers(dest='cmd')

    # status
    sub.add_parser('status', help='建玉サマリーを表示')

    # update
    sub.add_parser('update', help='現在価格を更新')

    # check
    sub.add_parser('check', help='維持率チェック + Telegram アラート')

    # add
    add_p = sub.add_parser('add', help='建玉を追加')
    add_p.add_argument('ticker')
    add_p.add_argument('side', choices=['long', 'short'])
    add_p.add_argument('shares', type=float)
    add_p.add_argument('entry_price', type=float)
    add_p.add_argument('--currency', default='JPY', choices=['JPY', 'USD'])
    add_p.add_argument('--type', dest='pos_type', default='一般信用',
                       choices=['一般信用', '制度信用'])
    add_p.add_argument('--memo', default='')

    # close
    close_p = sub.add_parser('close', help='建玉を決済')
    close_p.add_argument('pos_id', type=int)
    close_p.add_argument('--price', type=float, default=None)

    # collateral
    coll_p = sub.add_parser('collateral', help='担保を設定')
    coll_p.add_argument('--cash', type=float, default=0)
    coll_p.add_argument('--sec', type=float, default=0,
                        help='有価証券担保（時価）')

    args = parser.parse_args()

    if args.cmd == 'status':
        summary = get_summary()
        _print_summary(summary)

    elif args.cmd == 'update':
        result = update_prices()
        print(f'価格更新完了 | 維持率: {result["maintenance_ratio"]:.1f}%')
        summary = get_summary()
        _print_summary(summary)

    elif args.cmd == 'check':
        # P2-9: ヘルスチェック用ハートビート
        try:
            from utils import heartbeat as _hb
        except Exception:
            _hb = None
        try:
            summary = check_and_alert()
            _print_summary(summary)
            if _hb:
                _hb('margin_manager', 'ok')
        except Exception as _e:
            if _hb:
                _hb('margin_manager', 'error', str(_e)[:500])
            raise

    elif args.cmd == 'add':
        add_position(
            ticker=args.ticker,
            side=args.side,
            shares=args.shares,
            entry_price=args.entry_price,
            currency=args.currency,
            position_type=args.pos_type,
            memo=args.memo,
        )
        summary = get_summary()
        _print_summary(summary)

    elif args.cmd == 'close':
        result = close_position(args.pos_id, args.price)
        if result:
            summary = get_summary()
            _print_summary(summary)

    elif args.cmd == 'collateral':
        set_collateral(cash=args.cash, securities=args.sec)

    else:
        parser.print_help()
