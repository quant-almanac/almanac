"""
A-5: AI 推奨 Follow-Rate Tracker
---------------------------------
AI の priority_actions と action_executions.json を時系列マッチし:
  - follow-rate = 従ったアクション数 / AI 推奨総数
  - 仮想 P&L（全推奨に従った場合）vs 実績 P&L
  - 3 ヶ月連続で実績 < 仮想 なら Telegram 警告
を算出する。

マッチング基準: as_of ±3 営業日 + 同 ticker + 同 direction
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

BASE_DIR        = Path(__file__).parent
REC_LOG         = BASE_DIR / 'ai_recommendation_log.json'
EXEC_LOG        = BASE_DIR / 'action_executions.json'
SHADOW_PATH     = BASE_DIR / 'shadow_portfolio.json'
SELL_DECISION_LOG = BASE_DIR / 'sell_decision_log.jsonl'

# AI 推奨の action type → direction 変換
BUY_TYPES  = {'buy', 'add', 'dca'}
SELL_TYPES = {'sell', 'trim', 'reduce', 'stop_loss', 'take_profit'}
REBALANCE_TYPES = {'rebalance'}
MARGIN_BUY_TYPES = {'margin_buy'}
SHORT_TYPES = {'short'}
COVER_TYPES = {'cover'}

# マッチング窓（営業日 ≒ カレンダー日で緩く）
MATCH_WINDOW_DAYS = 3


# ============================================================
# 内部
# ============================================================

def _load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return default
    return default


def _atomic_write_json(path: Path, data) -> None:
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def _load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text(encoding='utf-8').splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load_recommendations(
    rec_path: Path = REC_LOG,
    sell_decision_path: Path = SELL_DECISION_LOG,
) -> list[dict]:
    """Reconstruct proposals from the general log plus append-only sell decisions."""
    recs = _load_json(rec_path, [])
    if not isinstance(recs, list):
        recs = []
    result = [row for row in recs if isinstance(row, dict)]
    seen = {
        (
            str(row.get('ticker') or '').upper(),
            _type_to_direction(row.get('type', row.get('action_type', ''))),
            str(row.get('as_of') or '')[:10],
        )
        for row in result
    }
    latest_sell: dict[str, dict] = {}
    for row in _load_jsonl(sell_decision_path):
        key = str(row.get('sell_decision_id') or row.get('row_id') or '')
        if key:
            latest_sell[key] = row
    for row in latest_sell.values():
        rec = {
            'as_of': row.get('recommended_at'),
            'ticker': row.get('ticker'),
            'type': row.get('action_type'),
            'price_at_rec': row.get('price_at_recommend'),
            'confidence_pct': row.get('conviction_at_sell'),
            'source': 'sell_decision_log',
        }
        key = (
            str(rec.get('ticker') or '').upper(),
            _type_to_direction(str(rec.get('type') or '')),
            str(rec.get('as_of') or '')[:10],
        )
        if key not in seen:
            result.append(rec)
            seen.add(key)
    return result


def _parse_dt(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace('Z', '+00:00'))
        # 正規化: tz-aware/naive 混在で subtraction が TypeError になるのを防ぐ。
        # aware は UTC へ変換してから tzinfo を剥がし、全て naive-UTC に揃える。
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    except Exception:
        return None


def _type_to_direction(t: str) -> Optional[str]:
    t = (t or '').lower()
    if t in BUY_TYPES:
        return 'buy'
    if t in SELL_TYPES:
        return 'sell'
    if t in MARGIN_BUY_TYPES:
        return 'margin_buy'
    if t in SHORT_TYPES:
        return 'short'
    if t in COVER_TYPES:
        return 'cover'
    if t in REBALANCE_TYPES:
        return 'rebalance'
    return None


def _price_at(ticker: str, dt: datetime) -> Optional[float]:
    """as_of 以降で最初に取得できる終値（近似）"""
    try:
        import yfinance as yf
        # ±5 日で範囲取得
        start = (dt - timedelta(days=2)).strftime('%Y-%m-%d')
        end   = (dt + timedelta(days=5)).strftime('%Y-%m-%d')
        hist = yf.Ticker(ticker).history(start=start, end=end, auto_adjust=False)
        if hist is None or hist.empty:
            return None
        # dt 以降の最初の Close
        for ts, row in hist.iterrows():
            if ts.to_pydatetime().date() >= dt.date():
                return float(row['Close'])
        return float(hist['Close'].iloc[-1])
    except Exception:
        return None


def _price_now(ticker: str) -> Optional[float]:
    try:
        import yfinance as yf
        return float(yf.Ticker(ticker).fast_info['lastPrice'])
    except Exception:
        return None


# ============================================================
# マッチング
# ============================================================

def match_recommendations(
    recs: Optional[list] = None,
    execs: Optional[list] = None,
    window_days: int = MATCH_WINDOW_DAYS,
) -> dict:
    """
    推奨と実行のマッチングを行い、follow-rate 基礎データを返す。

    Returns:
        {
          'total_recs':    推奨総数,
          'total_matched': マッチ成立数,
          'follow_rate':   ratio,
          'matched':       [{rec, exec, lag_days}],
          'unmatched':     [{rec, reason}],
          'by_direction':  {'buy': {recs, matched}, 'sell': {...}, ...}
        }
    """
    if recs is None:
        recs = load_recommendations()
    if execs is None:
        execs_data = _load_json(EXEC_LOG, {'executions': []})
        execs = execs_data.get('executions', []) if isinstance(execs_data, dict) else execs_data

    matched = []
    unmatched = []
    by_dir: dict = {}

    used_exec_ids: set = set()

    for rec in recs:
        direction = _type_to_direction(rec.get('type', rec.get('action_type', '')))
        if direction is None or direction == 'rebalance':
            # rebalance は対象外（個別約定に直結しない）
            continue

        ticker = (rec.get('ticker') or '').upper()
        rec_dt = _parse_dt(rec.get('as_of', ''))
        if not ticker or not rec_dt:
            continue

        by_dir.setdefault(direction, {'recs': 0, 'matched': 0})
        by_dir[direction]['recs'] += 1

        # マッチング: 同 ticker + 同 direction + 日付差 <= window
        found = None
        for ex in execs:
            if ex.get('id') in used_exec_ids:
                continue
            if (ex.get('ticker') or '').upper() != ticker:
                continue
            if ex.get('direction') != direction:
                continue
            ex_dt = _parse_dt(ex.get('saved_at', ''))
            if not ex_dt:
                continue
            lag = abs((ex_dt - rec_dt).total_seconds() / 86400)
            if lag <= window_days:
                found = (ex, lag)
                break

        if found is not None:
            ex, lag = found
            used_exec_ids.add(ex.get('id', f'{ticker}_{ex.get("saved_at")}'))
            matched.append({
                'rec':       {'ticker': ticker, 'type': rec.get('type'),
                              'direction': direction, 'as_of': rec.get('as_of'),
                              'urgency': rec.get('urgency'), 'price_at_rec': rec.get('price_at_rec')},
                'exec':      {'id': ex.get('id'), 'price': ex.get('price'),
                              'quantity': ex.get('quantity'), 'saved_at': ex.get('saved_at')},
                'lag_days':  round(lag, 2),
            })
            by_dir[direction]['matched'] += 1
        else:
            unmatched.append({
                'ticker':  ticker,
                'type':    rec.get('type'),
                'as_of':   rec.get('as_of'),
                'urgency': rec.get('urgency'),
                'reason':  'no execution within ±%d days' % window_days,
            })

    total_recs    = sum(v['recs'] for v in by_dir.values())
    total_matched = sum(v['matched'] for v in by_dir.values())
    follow_rate   = (total_matched / total_recs) if total_recs > 0 else 0.0

    return {
        'total_recs':    total_recs,
        'total_matched': total_matched,
        'follow_rate':   round(follow_rate, 4),
        'matched':       matched,
        'unmatched':     unmatched,
        'by_direction':  by_dir,
        'window_days':   window_days,
    }


def _compact_match_summary(summary: dict) -> dict:
    return {
        'total_recs': summary.get('total_recs', 0),
        'total_matched': summary.get('total_matched', 0),
        'follow_rate': summary.get('follow_rate', 0.0),
        'by_direction': summary.get('by_direction', {}),
        'window_days': summary.get('window_days', MATCH_WINDOW_DAYS),
    }


def build_status_snapshot(
    *,
    recs: Optional[list] = None,
    execs: Optional[list] = None,
    shadow_path: Path = SHADOW_PATH,
) -> dict:
    """Return follow-rate status even when monthly shadow history is absent."""
    state = _load_json(shadow_path, {})
    if not isinstance(state, dict):
        state = {}
    match_summary = match_recommendations(recs=recs, execs=execs)
    return {
        'shadow_state_available': bool(state),
        'last_updated': state.get('last_updated'),
        'underperform_3m': bool(state.get('underperform_3m', False)),
        'history': state.get('history', []) if isinstance(state.get('history'), list) else [],
        'follow_rate': _compact_match_summary(match_summary),
    }


# ============================================================
# 仮想 P&L 計算（全推奨に従った場合）
# ============================================================

def compute_shadow_pnl(
    recs: Optional[list] = None,
    lookback_days: int = 120,
    use_live_prices: bool = True,
) -> dict:
    """
    全 AI 推奨（buy/sell）に従って即日約定したと仮定した時の仮想 P&L。
    - buy: as_of 価格で買い、現在まで保有（または sell 推奨で手仕舞い）
    - sell: 保有があれば売却
    単純化のため数量は「規定単位 1」で計算し、比率で評価。

    P&L は % ベース（リターン）で記録、累積もリターン率。
    """
    if recs is None:
        recs = load_recommendations()

    cutoff = datetime.now() - timedelta(days=lookback_days)
    per_ticker_position = {}  # ticker -> list of (qty, entry_price)
    trades = []

    # 時系列順
    recs_sorted = sorted(recs, key=lambda r: r.get('as_of', ''))
    for rec in recs_sorted:
        dt = _parse_dt(rec.get('as_of', ''))
        if not dt or dt < cutoff:
            continue
        direction = _type_to_direction(rec.get('type', ''))
        if direction not in ('buy', 'sell'):
            continue
        ticker = (rec.get('ticker') or '').upper()
        if not ticker:
            continue

        price = rec.get('price_at_rec')
        if price is None:
            price = _price_at(ticker, dt) if use_live_prices else None
        if price is None or price <= 0:
            continue

        pos = per_ticker_position.setdefault(ticker, [])
        if direction == 'buy':
            pos.append({'qty': 1.0, 'entry': price, 'dt': dt})
            trades.append({'ticker': ticker, 'dir': 'buy', 'price': price, 'dt': dt.isoformat()})
        else:  # sell
            if pos:
                lot = pos.pop(0)
                pnl_pct = (price - lot['entry']) / lot['entry']
                trades.append({
                    'ticker': ticker, 'dir': 'sell', 'price': price,
                    'entry_price': lot['entry'], 'pnl_pct': pnl_pct,
                    'dt': dt.isoformat(), 'held_days': (dt - lot['dt']).days,
                })

    # 現在保有のマーク・トゥ・マーケット
    mtm_entries = []
    total_realized = sum(t.get('pnl_pct', 0) for t in trades if t['dir'] == 'sell')
    total_unrealized = 0.0
    for ticker, lots in per_ticker_position.items():
        if not lots:
            continue
        now_price = _price_now(ticker) if use_live_prices else None
        if now_price is None:
            continue
        for lot in lots:
            pnl_pct = (now_price - lot['entry']) / lot['entry']
            total_unrealized += pnl_pct
            mtm_entries.append({'ticker': ticker, 'entry': lot['entry'], 'mark': now_price,
                                 'pnl_pct': pnl_pct, 'held_days': (datetime.now() - lot['dt']).days})

    return {
        'shadow_realized_pnl_pct':   round(total_realized, 4),
        'shadow_unrealized_pnl_pct': round(total_unrealized, 4),
        'shadow_total_pnl_pct':      round(total_realized + total_unrealized, 4),
        'lookback_days':             lookback_days,
        'n_trades':                  len(trades),
        'open_positions':            mtm_entries,
        'trades':                    trades[-20:],   # 直近 20 件のみ
    }


# ============================================================
# 実績 P&L（executions から）
# ============================================================

def compute_actual_pnl_pct(
    execs: Optional[list] = None,
    lookback_days: int = 120,
    use_live_prices: bool = True,
) -> dict:
    """
    action_executions から実績 P&L%（ポジション単位）を FIFO で計算。
    注記: 既存 holdings には反映済みだが、ここでは "follow-rate 比較用" の
    単純化リターンを算出する。
    """
    if execs is None:
        ed = _load_json(EXEC_LOG, {'executions': []})
        execs = ed.get('executions', []) if isinstance(ed, dict) else ed

    cutoff = datetime.now() - timedelta(days=lookback_days)
    positions: dict = {}
    realized = 0.0
    unrealized = 0.0
    trades = []

    for ex in sorted(execs, key=lambda x: x.get('saved_at', '')):
        dt = _parse_dt(ex.get('saved_at', ''))
        if not dt or dt < cutoff:
            continue
        direction = (ex.get('direction') or '').lower()
        ticker = (ex.get('ticker') or '').upper()
        price = float(ex.get('price') or 0)
        qty   = float(ex.get('quantity') or 0)
        if price <= 0 or qty <= 0:
            continue

        lots = positions.setdefault(ticker, [])
        if direction == 'buy':
            lots.append({'qty': qty, 'entry': price, 'dt': dt})
        elif direction == 'sell':
            remaining = qty
            while remaining > 0 and lots:
                lot = lots[0]
                take = min(remaining, lot['qty'])
                pnl_pct = (price - lot['entry']) / lot['entry']
                realized += pnl_pct * (take / qty)
                trades.append({'ticker': ticker, 'price': price, 'entry_price': lot['entry'],
                                'pnl_pct': pnl_pct, 'dt': dt.isoformat()})
                lot['qty'] -= take
                remaining -= take
                if lot['qty'] <= 0:
                    lots.pop(0)

    # 現在ポジションの mark
    if use_live_prices:
        for ticker, lots in positions.items():
            if not lots:
                continue
            now_price = _price_now(ticker)
            if not now_price:
                continue
            for lot in lots:
                pnl_pct = (now_price - lot['entry']) / lot['entry']
                unrealized += pnl_pct

    return {
        'actual_realized_pnl_pct':   round(realized, 4),
        'actual_unrealized_pnl_pct': round(unrealized, 4),
        'actual_total_pnl_pct':      round(realized + unrealized, 4),
        'lookback_days':             lookback_days,
        'n_trades':                  len(trades),
    }


# ============================================================
# 月次比較 + アラート
# ============================================================

def update_monthly_comparison(
    shadow: Optional[dict] = None,
    actual: Optional[dict] = None,
) -> dict:
    """
    shadow_portfolio.json に月次レコードを追加し、3 ヶ月連続で
    実績 < 仮想 なら underperform フラグを立てる。
    """
    state = _load_json(SHADOW_PATH, {'history': []})

    if shadow is None:
        shadow = compute_shadow_pnl(use_live_prices=False)
    if actual is None:
        actual = compute_actual_pnl_pct(use_live_prices=False)

    this_month = datetime.now().strftime('%Y-%m')
    entry = {
        'month':                    this_month,
        'as_of':                    datetime.now().isoformat(),
        'shadow_total_pnl_pct':     shadow.get('shadow_total_pnl_pct', 0),
        'actual_total_pnl_pct':     actual.get('actual_total_pnl_pct', 0),
        'gap_pct':                  round(shadow.get('shadow_total_pnl_pct', 0) -
                                          actual.get('actual_total_pnl_pct', 0), 4),
    }

    # 同月は上書き
    hist = [h for h in state.get('history', []) if h.get('month') != this_month]
    hist.append(entry)
    hist = sorted(hist, key=lambda h: h['month'])[-24:]   # 最大 2 年
    state['history'] = hist

    # 3 ヶ月連続で shadow > actual ?
    last3 = hist[-3:]
    underperform = len(last3) >= 3 and all(h['gap_pct'] > 0.01 for h in last3)
    state['underperform_3m'] = underperform
    state['last_updated']    = datetime.now().isoformat()

    _atomic_write_json(SHADOW_PATH, state)

    return {'entry': entry, 'underperform_3m': underperform, 'history_len': len(hist)}


def send_underperform_alert(comparison: dict) -> bool:
    """3 ヶ月連続の underperform を Telegram 通知"""
    if not comparison.get('underperform_3m'):
        return False
    try:
        import requests
        token = os.environ.get('TELEGRAM_BOT_TOKEN', '')
        chat  = os.environ.get('TELEGRAM_CHAT_ID', '')
        if not token or not chat:
            return False
        e = comparison['entry']
        msg = (
            '📊 *ALMANAC Follow-Rate 警告*\n'
            f'3 ヶ月連続で実績 < AI 推奨（仮想）\n'
            f'今月 gap: {e["gap_pct"]*100:+.1f}% '
            f'(実績 {e["actual_total_pnl_pct"]*100:+.1f}% vs 仮想 {e["shadow_total_pnl_pct"]*100:+.1f}%)\n\n'
            'AI に素直に従う方が成績が良い傾向です。recommendations を見直してください。'
        )
        requests.post(
            f'https://api.telegram.org/bot{token}/sendMessage',
            json={'chat_id': chat, 'text': msg, 'parse_mode': 'Markdown'},
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else 'status'

    if cmd == 'match':
        result = match_recommendations()
        print(f'推奨総数: {result["total_recs"]}, マッチ: {result["total_matched"]}, '
              f'follow-rate: {result["follow_rate"]*100:.1f}%')
        for d, v in result['by_direction'].items():
            rate = (v['matched'] / v['recs']) if v['recs'] > 0 else 0
            print(f'  {d}: {v["matched"]}/{v["recs"]} = {rate*100:.1f}%')

    elif cmd == 'shadow':
        lookback = int(sys.argv[2]) if len(sys.argv) > 2 else 120
        use_live = '--live' in sys.argv
        result = compute_shadow_pnl(lookback_days=lookback, use_live_prices=use_live)
        print(json.dumps({k: v for k, v in result.items() if k != 'trades'},
                          ensure_ascii=False, indent=2))

    elif cmd == 'update':
        use_live = '--live' in sys.argv
        shadow = compute_shadow_pnl(use_live_prices=use_live)
        actual = compute_actual_pnl_pct(use_live_prices=use_live)
        comp = update_monthly_comparison(shadow, actual)
        print(json.dumps(comp, ensure_ascii=False, indent=2))
        if comp['underperform_3m']:
            ok = send_underperform_alert(comp)
            print(f'Telegram 通知: {"✅" if ok else "❌"}')

    elif cmd == 'status':
        print(json.dumps(build_status_snapshot(), ensure_ascii=False, indent=2))

    else:
        print('Usage: follow_rate_analyzer.py [match|shadow|update|status]')
