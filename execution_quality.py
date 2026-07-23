"""
A-8: 執行品質トラッキング
---------------------------
約定時の bid/ask、注文タイプ、約定時刻を action_executions.json から読み取り、
slippage (bps)、limit 成立率、平均執行コストを月次集計する。

拡張フィールド（api/routes/actions.py ExecutionRequest に追加推奨）:
  order_type:      Literal['market','limit','stop']
  bid_at_order:    float | None
  ask_at_order:    float | None
  executed_at_time: ISO str | None

既存レコードに欠損があっても落ちない設計。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

BASE_DIR  = Path(__file__).parent
EXEC_LOG  = BASE_DIR / 'action_executions.json'
REPORT    = BASE_DIR / 'execution_quality_report.json'

ALERT_SLIPPAGE_BPS = 100     # 100bps 超で alert 対象
ALERT_COUNT_THRESH = 3       # 月に 3 件以上で Telegram


# ============================================================
# slippage 計算
# ============================================================

def _compute_slippage_bps(ex: dict) -> Optional[float]:
    """
    1 レコードの slippage を bps で返す。計算不能なら None。

    Formula:
      buy:  (executed_price - mid) / mid * 10000
      sell: (mid - executed_price) / mid * 10000
    正値 = 不利な方向、負値 = 有利な方向
    """
    price = ex.get('price')
    bid = ex.get('bid_at_order')
    ask = ex.get('ask_at_order')
    if price is None or bid is None or ask is None:
        return None
    try:
        mid = (float(bid) + float(ask)) / 2
        if mid <= 0:
            return None
        direction = (ex.get('direction') or '').lower()
        if direction == 'buy':
            slip = (float(price) - mid) / mid * 10000
        elif direction == 'sell':
            slip = (mid - float(price)) / mid * 10000
        else:
            return None
        return float(slip)
    except Exception:
        return None


def _spread_bps(ex: dict) -> Optional[float]:
    bid, ask = ex.get('bid_at_order'), ex.get('ask_at_order')
    if bid is None or ask is None or bid <= 0:
        return None
    try:
        return (float(ask) - float(bid)) / float(bid) * 10000
    except Exception:
        return None


# ============================================================
# v5.1: Implementation Shortfall（AI 決定価格との比較）
# ============================================================

def _compute_shortfall_bps(
    executed_price: Optional[float],
    decision_price: Optional[float],
    direction: Optional[str],
) -> Optional[float]:
    """
    Implementation Shortfall (bps) を返す。

    Formula:
      buy / margin_buy / cover:
             (executed_price - decision_price) / decision_price * 10000
      sell / short:
             (decision_price - executed_price) / decision_price * 10000

    正値 = 不利（AI 想定より悪い約定）、負値 = 有利。
    """
    if executed_price is None or decision_price is None or not direction:
        return None
    try:
        ep = float(executed_price)
        dp = float(decision_price)
        if dp <= 0:
            return None
        d = direction.lower()
        if d in {'buy', 'margin_buy', 'cover'}:
            sf = (ep - dp) / dp * 10000
        elif d in {'sell', 'short'}:
            sf = (dp - ep) / dp * 10000
        else:
            return None
        return float(sf)
    except Exception:
        return None


def shortfall_summary(
    execs: Optional[list] = None,
    week_start: Optional[str] = None,
    week_end: Optional[str] = None,
    min_n: int = 5,
) -> dict:
    """
    指定期間の Implementation Shortfall を集計。
    week_start/end は ISO 'YYYY-MM-DD'。None ならフィルタなし（全期間）。

    出力:
      n / median_shortfall_bps / iqr_bps / worst / ai_compliance_rate
      （ai_compliance_rate: AI が limit を提案 → 実際 limit で発注した率）
    """
    if execs is None:
        data = _load_execs()
        execs = data if isinstance(data, list) else data.get('executions', [])

    rows: list[float] = []
    worst = None
    ai_proposed_limit = 0
    ai_followed_limit = 0
    ai_proposed_market = 0

    for ex in execs:
        dt = (ex.get('saved_at') or ex.get('decision_ts') or '')[:10]
        if week_start and dt < week_start:
            continue
        if week_end and dt > week_end:
            continue

        sf = _compute_shortfall_bps(
            ex.get('price'), ex.get('decision_price'), ex.get('direction')
        )
        if sf is not None:
            rows.append(sf)
            if worst is None or sf > worst.get('sf', float('-inf')):
                worst = {
                    'ticker':     ex.get('ticker'),
                    'id':         ex.get('id'),
                    'sf':         round(sf, 1),
                    'direction':  ex.get('direction'),
                }

        ai_ot = (ex.get('ai_recommended_order_type') or '').lower()
        actual_ot = (ex.get('order_type') or '').lower()
        if ai_ot == 'limit':
            ai_proposed_limit += 1
            if actual_ot == 'limit':
                ai_followed_limit += 1
        elif ai_ot == 'market':
            ai_proposed_market += 1

    rows_sorted = sorted(rows)

    def _percentile(xs, p):
        if not xs:
            return None
        idx = max(0, min(len(xs) - 1, int(p * (len(xs) - 1))))
        return round(xs[idx], 1)

    if len(rows) >= min_n:
        median = _percentile(rows_sorted, 0.5)
        q25 = _percentile(rows_sorted, 0.25)
        q75 = _percentile(rows_sorted, 0.75)
    else:
        median = q25 = q75 = None

    return {
        'window':                  {'start': week_start, 'end': week_end},
        'n':                       len(rows),
        'median_shortfall_bps':    median,
        'q25_shortfall_bps':       q25,
        'q75_shortfall_bps':       q75,
        'iqr_bps':                 round((q75 - q25), 1) if q75 is not None and q25 is not None else None,
        'worst':                   worst,
        'ai_compliance_rate':      round(ai_followed_limit / ai_proposed_limit, 3)
                                    if ai_proposed_limit else None,
        'ai_proposed_limit_n':     ai_proposed_limit,
        'ai_proposed_market_n':    ai_proposed_market,
        'sample_too_small':        len(rows) < min_n,
    }


# ============================================================
# 集計
# ============================================================

def monthly_summary(
    execs: Optional[list] = None,
    ym: Optional[str] = None,
) -> dict:
    """
    指定月の執行品質サマリ。ym=None は今月。
    """
    if execs is None:
        data = _load_execs()
        execs = data if isinstance(data, list) else data.get('executions', [])

    ym = ym or datetime.now().strftime('%Y-%m')

    filtered = []
    for ex in execs:
        dt = ex.get('saved_at') or ex.get('executed_at_time') or ''
        if not dt.startswith(ym):
            continue
        filtered.append(ex)

    slippages = []
    spreads = []
    by_type: dict = {'market': 0, 'limit': 0, 'stop': 0, 'unknown': 0}
    limit_filled = 0
    limit_total = 0
    worst = None
    best = None
    high_slippage_entries = []

    for ex in filtered:
        ot = (ex.get('order_type') or 'unknown').lower()
        by_type[ot] = by_type.get(ot, 0) + 1

        if ot == 'limit':
            limit_total += 1
            if ex.get('status') in ('filled', 'executed'):
                limit_filled += 1

        slip = _compute_slippage_bps(ex)
        if slip is not None:
            slippages.append(slip)
            if worst is None or slip > worst['slip']:
                worst = {'ticker': ex.get('ticker'), 'slip': slip, 'id': ex.get('id')}
            if best is None or slip < best['slip']:
                best = {'ticker': ex.get('ticker'), 'slip': slip, 'id': ex.get('id')}
            if slip > ALERT_SLIPPAGE_BPS:
                high_slippage_entries.append({
                    'ticker':   ex.get('ticker'),
                    'id':       ex.get('id'),
                    'slip_bps': round(slip, 1),
                    'direction': ex.get('direction'),
                    'order_type': ot,
                })

        s = _spread_bps(ex)
        if s is not None:
            spreads.append(s)

    def _avg(xs):
        return round(sum(xs) / len(xs), 2) if xs else None

    result = {
        'month':               ym,
        'n_executions':        len(filtered),
        'n_with_slippage':     len(slippages),
        'avg_slippage_bps':    _avg(slippages),
        'median_slippage_bps': _median(slippages),
        'avg_spread_bps':      _avg(spreads),
        'worst':               worst,
        'best':                best,
        'by_order_type':       by_type,
        'limit_fill_rate':     round(limit_filled / limit_total, 3) if limit_total > 0 else None,
        'high_slippage_count': len(high_slippage_entries),
        'high_slippage_entries': high_slippage_entries,
        'alert_triggered':     len(high_slippage_entries) >= ALERT_COUNT_THRESH,
        'as_of':               datetime.now().isoformat(),
    }
    return result


def _median(xs: list) -> Optional[float]:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    if n % 2 == 1:
        return round(s[n // 2], 2)
    return round((s[n//2 - 1] + s[n//2]) / 2, 2)


def _load_execs():
    if EXEC_LOG.exists():
        try:
            return json.loads(EXEC_LOG.read_text(encoding='utf-8'))
        except Exception:
            return {'executions': []}
    return {'executions': []}


# ============================================================
# last-N executions ウィジェット用
# ============================================================

def recent_executions(n: int = 10) -> list:
    data = _load_execs()
    execs = data if isinstance(data, list) else data.get('executions', [])
    execs_sorted = sorted(execs, key=lambda x: x.get('saved_at', ''), reverse=True)
    out = []
    for ex in execs_sorted[:n]:
        slip = _compute_slippage_bps(ex)
        out.append({
            'id':        ex.get('id'),
            'ticker':    ex.get('ticker'),
            'direction': ex.get('direction'),
            'price':     ex.get('price'),
            'quantity':  ex.get('quantity'),
            'saved_at':  ex.get('saved_at'),
            'order_type': ex.get('order_type', 'unknown'),
            'slippage_bps': round(slip, 1) if slip is not None else None,
            'spread_bps':   round(_spread_bps(ex), 1) if _spread_bps(ex) is not None else None,
        })
    return out


# ============================================================
# 月次レポート保存 + Telegram
# ============================================================

def write_report(summary: dict) -> None:
    tmp = REPORT.with_suffix('.tmp')
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(REPORT)


def send_alert(summary: dict) -> bool:
    if not summary.get('alert_triggered'):
        return False
    try:
        import requests
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        chat  = os.environ.get('TELEGRAM_CHAT_ID', '')
        if not token or not chat:
            return False
        lines = [
            f'⚠️ *ALMANAC 執行品質アラート* ({summary["month"]})',
            f'  slippage > {ALERT_SLIPPAGE_BPS}bps: {summary["high_slippage_count"]} 件',
        ]
        for e in summary['high_slippage_entries'][:5]:
            lines.append(f'    🔴 `{e["ticker"]}` {e["direction"]} ({e["order_type"]}): {e["slip_bps"]:.0f}bps')
        lines.append('\n成行注文を減らして指値注文に切替推奨。')
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': '\n'.join(lines), 'parse_mode': 'Markdown'},
            timeout=10,
        )
        return True
    except Exception:
        return False


# ============================================================
# CLI
# ============================================================

if __name__ == '__main__':
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'month'

    if cmd == 'month':
        ym = sys.argv[2] if len(sys.argv) > 2 else None
        s = monthly_summary(ym=ym)
        print(json.dumps(s, ensure_ascii=False, indent=2))
        write_report(s)
        if s.get('alert_triggered'):
            ok = send_alert(s)
            print(f'Alert sent: {ok}')

    elif cmd == 'recent':
        n = int(sys.argv[2]) if len(sys.argv) > 2 else 10
        for r in recent_executions(n):
            slip = f'{r["slippage_bps"]:+.0f}bps' if r['slippage_bps'] is not None else 'n/a'
            print(f'  {r["saved_at"]:.19s} {r["ticker"]:6} {r["direction"]:4} '
                  f'qty={r["quantity"]} @ {r["price"]} [{r["order_type"]:6}] slip={slip}')

    elif cmd == 'selftest':
        synthetic = [
            {'id': 'a1', 'ticker': 'NVDA', 'direction': 'buy',
             'saved_at': datetime.now().strftime('%Y-%m-%d'),
             'price': 150.50, 'bid_at_order': 150.00, 'ask_at_order': 150.20,
             'order_type': 'market', 'quantity': 5},
            {'id': 'a2', 'ticker': 'CRWV', 'direction': 'sell',
             'saved_at': datetime.now().strftime('%Y-%m-%d'),
             'price': 95.00, 'bid_at_order': 95.10, 'ask_at_order': 95.20,
             'order_type': 'market', 'quantity': 10},
            {'id': 'a3', 'ticker': 'META', 'direction': 'buy',
             'saved_at': datetime.now().strftime('%Y-%m-%d'),
             'price': 500.00, 'bid_at_order': 499.80, 'ask_at_order': 500.00,
             'order_type': 'limit', 'quantity': 2, 'status': 'filled'},
            # 3rd high-slippage case → triggers alert (>100bps)
            {'id': 'a4', 'ticker': 'SMH', 'direction': 'buy',
             'saved_at': datetime.now().strftime('%Y-%m-%d'),
             'price': 262.00, 'bid_at_order': 258.00, 'ask_at_order': 258.50,
             'order_type': 'market', 'quantity': 3},
            {'id': 'a5', 'ticker': 'SOXX', 'direction': 'buy',
             'saved_at': datetime.now().strftime('%Y-%m-%d'),
             'price': 250.00, 'bid_at_order': 247.00, 'ask_at_order': 247.50,
             'order_type': 'market', 'quantity': 2},
            {'id': 'a6', 'ticker': 'AVGO', 'direction': 'sell',
             'saved_at': datetime.now().strftime('%Y-%m-%d'),
             'price': 195.00, 'bid_at_order': 197.50, 'ask_at_order': 198.00,
             'order_type': 'market', 'quantity': 1},
        ]
        s = monthly_summary(execs=synthetic)
        print(f'selftest: n={s["n_executions"]} avg_slip={s["avg_slippage_bps"]}bps '
              f'high={s["high_slippage_count"]} alert={s["alert_triggered"]}')
        print(f'  worst: {s["worst"]}')
        print(f'  best:  {s["best"]}')
        assert s['high_slippage_count'] >= 2
        assert s['alert_triggered'] is True or s['high_slippage_count'] >= ALERT_COUNT_THRESH
        print('✅ A-8 selftest pass')

    else:
        print('Usage: execution_quality.py [month [YYYY-MM] | recent [N] | selftest]')
