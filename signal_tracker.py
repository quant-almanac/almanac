"""
signal_tracker.py — シグナル勝率トラッキング
crontab: 毎日 18:45 に実行し、5営業日/10営業日後のリターンを計測して signal_history.json を更新する。
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

SIGNAL_HISTORY_FILE = os.path.expanduser('~/portfolio-bot/signal_history.json')

# 評価は 5営業日後 / 10営業日後（単純カレンダー日数ではなく取引日ベース）
# 取引日 N 営業日後 ≒ カレンダー日で N*7/5 日後 + 余裕 4 日
_BUSDAY_CALENDAR_SLACK = {5: 11, 10: 18}  # 5営業日→11日後以降, 10営業日→18日後以降


def load_history() -> list:
    if not os.path.exists(SIGNAL_HISTORY_FILE):
        return []
    with open(SIGNAL_HISTORY_FILE) as f:
        return json.load(f)


def save_history(history: list) -> None:
    try:
        from utils import atomic_write_json
        atomic_write_json(SIGNAL_HISTORY_FILE, history)
    except ImportError:
        with open(SIGNAL_HISTORY_FILE, 'w') as f:
            json.dump(history, f, indent=2, ensure_ascii=False)


def _business_days_after(date_str: str, n: int) -> str:
    """date_str（YYYY-MM-DD）から n 営業日後の日付文字列を返す（numpy.busday_offset 使用）"""
    import numpy as np
    result = np.busday_offset(date_str, n, roll='forward')
    return str(result)


def bulk_fetch_prices(tickers: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    """yfinance で一括ダウンロードして {ticker: DataFrame} を返す"""
    if not tickers:
        return {}
    try:
        raw = yf.download(tickers, start=start, end=end, progress=False, threads=True, auto_adjust=True,
                          group_by="ticker")
        result: dict[str, pd.DataFrame] = {}
        for t in tickers:
            try:
                df = raw[t].dropna(how='all') if len(tickers) > 1 else raw.dropna(how='all')
                if not df.empty:
                    result[t] = df
            except Exception:
                pass
        return result
    except Exception:
        return {}


def update_outcomes(history: list) -> tuple[list, int]:
    """outcome_5biz / outcome_10biz を埋める。更新件数を返す。"""
    today = datetime.now().date()
    updated = 0

    # 未評価レコードを収集
    pending_5 = [r for r in history if r.get('outcome_5d') is None and r.get('price_at_signal')]
    pending_10 = [r for r in history if r.get('outcome_10d') is None and r.get('price_at_signal')]

    def need_5d(r):
        slack = timedelta(days=_BUSDAY_CALENDAR_SLACK[5])
        return today >= datetime.strptime(r['date'], '%Y-%m-%d').date() + slack

    def need_10d(r):
        slack = timedelta(days=_BUSDAY_CALENDAR_SLACK[10])
        return today >= datetime.strptime(r['date'], '%Y-%m-%d').date() + slack

    ready_5 = [r for r in pending_5 if need_5d(r)]
    ready_10 = [r for r in pending_10 if need_10d(r)]

    # 一括フェッチ対象の日付範囲を計算
    all_ready = {r['ticker'] for r in ready_5 + ready_10}
    if not all_ready:
        return history, 0

    # 最古のシグナル日から今日まで一括取得
    all_dates = [r['date'] for r in ready_5 + ready_10]
    fetch_start = min(all_dates)
    fetch_end = (today + timedelta(days=1)).strftime('%Y-%m-%d')

    print(f"  yfinance 一括取得: {len(all_ready)}銘柄 ({fetch_start} 〜 {fetch_end})")
    price_data = bulk_fetch_prices(list(all_ready), fetch_start, fetch_end)

    def get_price_after(ticker: str, after_date: str) -> float | None:
        """after_date 以降の最初の取引日終値を返す"""
        df = price_data.get(ticker)
        if df is None or df.empty:
            return None
        try:
            # タイムゾーンを除去して日付比較
            idx = df.index
            if hasattr(idx, 'tz') and idx.tz is not None:
                idx = idx.tz_localize(None)
            after = pd.Timestamp(after_date)
            subset = df[idx >= after]
            if subset.empty:
                return None
            return float(subset['Close'].iloc[0])
        except Exception:
            return None

    # レコードマップを作成（date + ticker でキー）
    record_map = {(r['date'], r['ticker']): r for r in history}

    for r in ready_5:
        try:
            due_str = _business_days_after(r['date'], 5)
            price = get_price_after(r['ticker'], due_str)
            if price:
                r['outcome_5d'] = round((price - r['price_at_signal']) / r['price_at_signal'] * 100, 2)
                updated += 1
        except Exception as e:
            print(f"  [5d] {r['ticker']}: {e}")

    for r in ready_10:
        try:
            due_str = _business_days_after(r['date'], 10)
            price = get_price_after(r['ticker'], due_str)
            if price:
                r['outcome_10d'] = round((price - r['price_at_signal']) / r['price_at_signal'] * 100, 2)
                updated += 1
        except Exception as e:
            print(f"  [10d] {r['ticker']}: {e}")

    return history, updated


def compute_stats(history: list) -> dict:
    """シグナル別の勝率・平均リターンを計算して返す"""
    stats: dict = {}
    for signal in ('BUY', 'WATCH', 'SKIP'):
        records = [r for r in history if r.get('signal') == signal and r.get('outcome_5d') is not None]
        if not records:
            stats[signal] = {'count': 0, 'win_rate_5d': None, 'avg_return_5d': None}
            continue
        returns = [r['outcome_5d'] for r in records]
        wins = [r for r in returns if r > 0]
        stats[signal] = {
            'count': len(records),
            'win_rate_5d': round(len(wins) / len(records) * 100, 1),
            'avg_return_5d': round(sum(returns) / len(returns), 2),
        }
    return stats


def _sharpe(returns: list[float]) -> float | None:
    """簡易Sharpe比（年率換算: 252営業日 / 5日保有）"""
    if len(returns) < 3:
        return None
    import statistics
    mean = statistics.mean(returns)
    std = statistics.stdev(returns)
    if std == 0:
        return None
    # 5日保有 → 年52回転
    return round(mean / std * (52 ** 0.5), 2)


def compute_weekly_trend(history: list, weeks: int = 12) -> list[dict]:
    """
    直近 weeks 週の週次シグナル性能推移を計算して返す。
    フロントエンドの Sharpe 推移チャート用。
    """
    from collections import defaultdict
    today = datetime.now().date()
    trend = []
    for w in range(weeks - 1, -1, -1):
        week_end = today - timedelta(days=today.weekday() + 7 * w)
        week_start = week_end - timedelta(days=6)
        week_label = week_start.strftime('%m/%d')
        records = [
            r for r in history
            if r.get('outcome_5d') is not None
            and week_start <= datetime.strptime(r['date'], '%Y-%m-%d').date() <= week_end
        ]
        if not records:
            trend.append({'week': week_label, 'buy_win_rate': None, 'buy_sharpe': None, 'count': 0})
            continue
        buy_records = [r for r in records if r.get('signal') == 'BUY']
        buy_returns = [r['outcome_5d'] for r in buy_records]
        buy_wins = [x for x in buy_returns if x > 0]
        trend.append({
            'week': week_label,
            'buy_win_rate': round(len(buy_wins) / len(buy_returns) * 100, 1) if buy_returns else None,
            'buy_sharpe': _sharpe(buy_returns),
            'count': len(records),
            'buy_count': len(buy_records),
        })
    return trend


def compute_strategy_stats(history: list) -> dict:
    """戦略別（逆張り/モメンタム等）の勝率・平均リターンを計算"""
    from collections import defaultdict
    by_strategy: dict = defaultdict(list)
    for r in history:
        if r.get('outcome_5d') is not None and r.get('strategy'):
            by_strategy[r['strategy']].append(r['outcome_5d'])

    result = {}
    for strat, returns in by_strategy.items():
        wins = [x for x in returns if x > 0]
        result[strat] = {
            'count': len(returns),
            'win_rate_5d': round(len(wins) / len(returns) * 100, 1),
            'avg_return_5d': round(sum(returns) / len(returns), 2),
            'sharpe': _sharpe(returns),
        }
    return result


def compute_watch_transition(history: list, follow_up_window_days: int = 7) -> dict:
    """
    WATCH→BUY / WATCH→SKIP の遷移率を計算し、ティッカー別 false-positive を返す。

    アルゴリズム:
      - 各 WATCH レコードについて、同じ ticker で発行日から follow_up_window_days
        以内に発行された BUY/SKIP を「フォローアップシグナル」として紐付け
      - レコードに follow_up_signal / follow_up_at を埋め込む（次回保存時に永続化）
      - 集計: 全 WATCH に対する watch_to_buy_rate / watch_to_skip_rate
      - ティッカー別 false-positive 上位ランキング（WATCH→SKIP 多発銘柄）

    Returns: {
        watch_to_buy_rate, watch_to_skip_rate, watch_total_eligible,
        false_positive_tickers: [{ticker, watch_n, skip_n, fp_rate}],
    }
    """
    by_ticker_chrono: dict[str, list] = defaultdict(list)
    for r in history:
        if not r.get('ticker') or not r.get('date'):
            continue
        by_ticker_chrono[r['ticker']].append(r)

    today = datetime.now().date()
    eligible_total = 0
    moved_to_buy   = 0
    moved_to_skip  = 0
    per_ticker: dict[str, dict] = {}

    for ticker, recs in by_ticker_chrono.items():
        # 日付昇順
        recs.sort(key=lambda r: r.get('date', ''))
        watch_n = 0
        skip_n  = 0
        buy_n   = 0
        for i, r in enumerate(recs):
            if r.get('signal') != 'WATCH':
                continue
            try:
                start = datetime.strptime(r['date'], '%Y-%m-%d').date()
            except Exception:
                continue
            # フォローアップ可能になっているか判定（信号日 + window <= today）
            window_end = start + timedelta(days=follow_up_window_days)
            if today < window_end:
                continue
            eligible_total += 1
            watch_n += 1
            # 同 ticker の後続レコードを探す
            follow_signal = None
            follow_at     = None
            for j in range(i + 1, len(recs)):
                later = recs[j]
                try:
                    ldate = datetime.strptime(later['date'], '%Y-%m-%d').date()
                except Exception:
                    continue
                if ldate <= start:
                    continue
                if ldate > window_end:
                    break
                if later.get('signal') in ('BUY', 'SKIP'):
                    follow_signal = later['signal']
                    follow_at     = later['date']
                    break
            # レコードに永続化（次回 save_history で保存）
            if follow_signal:
                r['follow_up_signal'] = follow_signal
                r['follow_up_at']     = follow_at
                if follow_signal == 'BUY':
                    moved_to_buy += 1
                    buy_n += 1
                elif follow_signal == 'SKIP':
                    moved_to_skip += 1
                    skip_n += 1
        if watch_n >= 3:
            per_ticker[ticker] = {
                'watch_n': watch_n, 'buy_n': buy_n, 'skip_n': skip_n,
                'fp_rate': round(skip_n / watch_n * 100, 1),
            }

    fp_top = sorted(
        [{'ticker': t, **v} for t, v in per_ticker.items()],
        key=lambda x: x['fp_rate'], reverse=True,
    )[:10]

    return {
        'watch_total_eligible': eligible_total,
        'watch_to_buy_rate':  round(moved_to_buy  / max(1, eligible_total) * 100, 1),
        'watch_to_skip_rate': round(moved_to_skip / max(1, eligible_total) * 100, 1),
        'follow_up_window_days': follow_up_window_days,
        'false_positive_tickers': fp_top,
    }


def detect_degradation(weekly_trend: list) -> dict | None:
    """
    直近4週のBUY勝率が前4週より10pt以上低下していれば劣化警告を返す。
    データ不足時は None。
    """
    valid = [w for w in weekly_trend if w.get('buy_win_rate') is not None]
    if len(valid) < 8:
        return None
    recent = [w['buy_win_rate'] for w in valid[-4:]]
    prior  = [w['buy_win_rate'] for w in valid[-8:-4]]
    recent_avg = sum(recent) / len(recent)
    prior_avg  = sum(prior)  / len(prior)
    diff = recent_avg - prior_avg
    if diff <= -10:
        return {
            'degraded': True,
            'recent_avg': round(recent_avg, 1),
            'prior_avg':  round(prior_avg, 1),
            'drop_pt':    round(-diff, 1),
            'message': f"直近4週の BUY 勝率 {recent_avg:.1f}% → 前4週比 {diff:.1f}pt 低下。閾値見直しを推奨。",
        }
    return {'degraded': False, 'recent_avg': round(recent_avg, 1), 'prior_avg': round(prior_avg, 1)}


def audit_outcomes(history: list) -> dict:
    """
    P2-13: outcome 評価済み/未評価の棚卸し。

    - 総シグナル数
    - outcome_5d NULL 件数と割合
    - outcome_10d NULL 件数と割合
    - 5d 評価期限超過（6営業日以上前）で未評価 = stale
    - 署名別の評価状況
    """
    now_str = datetime.now().strftime('%Y-%m-%d')
    today = datetime.strptime(now_str, '%Y-%m-%d').date()

    total = len(history)
    null_5d = 0
    null_10d = 0
    stale_5d = 0   # 評価期限を過ぎても outcome が null
    stale_10d = 0
    by_signal: dict = {}

    for r in history:
        sig = r.get('signal', 'unknown')
        entry = by_signal.setdefault(sig, {'total': 0, 'null_5d': 0, 'null_10d': 0})
        entry['total'] += 1

        date_str = r.get('date') or r.get('signal_date')
        if not date_str:
            continue
        try:
            signal_date = datetime.strptime(date_str[:10], '%Y-%m-%d').date()
        except Exception:
            continue
        business_days_elapsed = (today - signal_date).days

        if r.get('outcome_5d') is None:
            null_5d += 1
            entry['null_5d'] += 1
            if business_days_elapsed > 8:
                stale_5d += 1
        if r.get('outcome_10d') is None:
            null_10d += 1
            entry['null_10d'] += 1
            if business_days_elapsed > 15:
                stale_10d += 1

    for sig, e in by_signal.items():
        e['null_5d_pct'] = round(e['null_5d'] / max(1, e['total']) * 100, 1)
        e['null_10d_pct'] = round(e['null_10d'] / max(1, e['total']) * 100, 1)

    report = {
        'audited_at':         datetime.now().isoformat(),
        'total_signals':      total,
        'null_5d_count':      null_5d,
        'null_5d_pct':        round(null_5d / max(1, total) * 100, 1),
        'null_10d_count':     null_10d,
        'null_10d_pct':       round(null_10d / max(1, total) * 100, 1),
        'stale_5d_count':     stale_5d,   # 評価期限を超えたのに未評価（update_outcomes を呼んでも埋まらない=休場/銘柄廃止など）
        'stale_10d_count':    stale_10d,
        'by_signal':          by_signal,
        'health':             'ok' if stale_5d < 5 else 'degraded',
    }
    return report


def _send_audit_alert(report: dict) -> None:
    """シグナル audit の Telegram 通知は廃止。signal_stats.json / Web UI を参照。"""
    if report['null_5d_pct'] <= 20 and report['stale_5d_count'] < 5:
        return
    print(
        f"[signal_tracker] audit 警告: 5d 未評価 {report['null_5d_count']} 件 "
        f"({report['null_5d_pct']}%), stale {report['stale_5d_count']} 件"
    )


def _run_tracking() -> None:
    """デフォルト: outcome 更新 + stats 計算 + JSON 保存"""
    history = load_history()
    print(f"  総レコード数: {len(history)}")

    history, updated = update_outcomes(history)
    print(f"  アウトカム更新: {updated}件")

    if updated > 0:
        save_history(history)

    stats = compute_stats(history)
    print("\n--- シグナル別統計 ---")
    for sig, s in stats.items():
        if s['count'] > 0:
            print(f"  {sig}: {s['count']}件 | 勝率(5営業日) {s['win_rate_5d']}% | 平均{s['avg_return_5d']}%")
        else:
            print(f"  {sig}: データなし")

    weekly_trend = compute_weekly_trend(history)
    strategy_stats = compute_strategy_stats(history)
    degradation = detect_degradation(weekly_trend)

    # WATCH→BUY/SKIP 遷移率（follow_up_signal をレコードに埋め込む副作用あり）
    watch_transition = compute_watch_transition(history, follow_up_window_days=7)
    if watch_transition.get('watch_total_eligible', 0) > 0:
        save_history(history)  # follow_up_signal の永続化
        print(f"  WATCH→BUY 遷移率: {watch_transition['watch_to_buy_rate']}%  "
              f"WATCH→SKIP 遷移率: {watch_transition['watch_to_skip_rate']}%  "
              f"(対象 {watch_transition['watch_total_eligible']} 件)")

    if degradation and degradation.get('degraded'):
        print(f"\n⚠️  劣化検知: {degradation['message']}")

    # stats を JSON 保存（API から参照）
    stats_file = os.path.expanduser('~/portfolio-bot/signal_stats.json')
    stats_data = {
        'updated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'total_records': len(history),
        'by_signal': stats,
        'by_strategy': strategy_stats,
        'weekly_trend': weekly_trend,
        'degradation': degradation,
        'watch_transition': watch_transition,
    }
    try:
        from utils import atomic_write_json
        atomic_write_json(stats_file, stats_data)
    except ImportError:
        with open(stats_file, 'w') as f:
            json.dump(stats_data, f, indent=2, ensure_ascii=False)

    print("完了。")


if __name__ == '__main__':
    import sys
    args = sys.argv[1:]
    print(f"[{datetime.now().strftime('%H:%M:%S')}] シグナル勝率トラッキング開始...")

    if args and args[0] == 'audit':
        # P2-13: outcome 欠損の棚卸し
        history = load_history()
        report = audit_outcomes(history)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        try:
            from utils import atomic_write_json
            atomic_write_json('signal_audit_report.json', report)
        except ImportError:
            with open('signal_audit_report.json', 'w') as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        # 警告レベルなら Telegram
        if '--notify' in args:
            _send_audit_alert(report)
    else:
        _run_tracking()
