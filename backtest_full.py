import yfinance as yf
import pandas as pd
import json
import os
from datetime import datetime, timedelta
from itertools import product

TICKERS_FILE = os.path.expanduser('~/portfolio-bot/tickers.json')

def load_tickers():
    with open(TICKERS_FILE) as f:
        data = json.load(f)['all']
    return [t for t in data if not t.endswith('.T')], [t for t in data if t.endswith('.T')]

def load_data(tickers, period_years=3):
    print(f"  データ取得中: {len(tickers)}銘柄...")
    data = {}
    for i, ticker in enumerate(tickers):
        try:
            end = datetime.now()
            start = end - timedelta(days=365 * period_years)
            hist = yf.Ticker(ticker).history(start=start, end=end)
            if hist.empty or len(hist) < 100:
                continue
            hist = hist.reset_index()
            hist['Date'] = pd.to_datetime(hist['Date']).dt.tz_localize(None)

            delta = hist['Close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = -delta.where(delta < 0, 0).rolling(14).mean()
            hist['RSI'] = 100 - (100 / (1 + gain / loss))
            hist['MA50'] = hist['Close'].rolling(50).mean()
            hist['MA50_dev'] = (hist['Close'] - hist['MA50']) / hist['MA50'] * 100
            hist['VolRatio'] = hist['Volume'] / hist['Volume'].rolling(20).mean()
            hist['Mom5d'] = hist['Close'].pct_change(5) * 100
            hist['Change'] = hist['Close'].pct_change() * 100
            hist['Gap'] = (hist['Open'] - hist['Close'].shift()) / hist['Close'].shift() * 100
            tr = pd.concat([
                hist['High'] - hist['Low'],
                abs(hist['High'] - hist['Close'].shift()),
                abs(hist['Low'] - hist['Close'].shift())
            ], axis=1).max(axis=1)
            hist['ATR'] = tr.rolling(14).mean()
            hist['ATR_pct'] = hist['ATR'] / hist['Close'] * 100
            hist['High52w'] = hist['High'].rolling(252).max()
            hist['New52wHigh'] = hist['High'].rolling(5).max() >= hist['High52w'] * 0.99
            hist['AvgTurnover'] = hist['Volume'].rolling(20).mean() * hist['Close'].rolling(20).mean()
            data[ticker] = hist
            if (i + 1) % 30 == 0:
                print(f"    {i+1}/{len(tickers)}...")
        except:
            pass
    print(f"  完了: {len(data)}銘柄")
    return data

def simulate_trade(hist, entry_idx, hold_days, stop_atr_mult=2.0, trail_days=5):
    entry_price = float(hist.iloc[entry_idx]['Close'])
    atr = float(hist.iloc[entry_idx]['ATR'])
    stop_price = entry_price - stop_atr_mult * atr
    exit_price = entry_price
    exit_reason = 'タイムストップ'
    actual_hold = 0
    for j in range(1, hold_days + 1):
        if entry_idx + j >= len(hist): break
        price = float(hist.iloc[entry_idx + j]['Close'])
        if price <= stop_price:
            exit_price = stop_price
            exit_reason = 'ストップロス'
            actual_hold = j
            break
        if j >= trail_days:
            trail_low = float(hist.iloc[entry_idx + j - trail_days:entry_idx + j]['Low'].min())
            if price < trail_low:
                exit_price = price
                exit_reason = 'トレーリング'
                actual_hold = j
                break
        exit_price = price
        actual_hold = j
    return (exit_price - entry_price) / entry_price * 100, exit_reason, actual_hold

def collect_and_score(data, strategy, params, is_japan, min_count=30):
    turnover_min = 1e9 if is_japan else 1e7
    trades = []
    for ticker, hist in data.items():
        for i in range(60, len(hist) - params['hold']):
            row = hist.iloc[i]
            if pd.isna(row.get('RSI')) or pd.isna(row.get('ATR_pct')): continue
            if row['ATR_pct'] < 2.0: continue
            if row['AvgTurnover'] < turnover_min: continue
            match = False
            if strategy == '逆張り':
                match = (row['RSI'] < params['rsi'] and row['VolRatio'] >= params['vol'] and row['Mom5d'] <= params['mom5d'])
            elif strategy == 'モメンタム':
                match = (row['RSI'] >= params['rsi_min'] and
                         params['ma50_min'] <= row['MA50_dev'] <= params['ma50_max'] and
                         row['New52wHigh'] and row['Close'] > row['MA50'] and row['VolRatio'] >= params['vol'])
            elif strategy == 'ギャップダウン':
                match = (row['Gap'] <= params['gap'] and row['VolRatio'] >= params['vol'])
            elif strategy == 'イベントドリブン後':
                match = (row['Change'] <= params['change'] and row['VolRatio'] >= params['vol'])
            if match:
                pnl, ex, hold = simulate_trade(hist, i, params['hold'], params['stop_mult'])
                trades.append(pnl)

    if len(trades) < min_count: return None
    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    if not wins or not losses: return None
    pf = abs(sum(wins) / sum(losses))
    wr = len(wins) / len(trades)
    avg = sum(trades) / len(trades)
    return {'trades': len(trades), 'win_rate': round(wr*100,1),
            'avg_pnl': round(avg,2), 'profit_factor': round(pf,2),
            'score': round(avg * wr * pf, 3)}

def optimize(data, strategy, grid, is_japan, label):
    print(f"  【{label}】", end='', flush=True)
    keys = list(grid.keys())
    combos = list(product(*grid.values()))
    print(f" {len(combos)}パターン検証中...", flush=True)
    best_score = -999
    best_params = None
    best_stats = None
    results = []
    for combo in combos:
        params = dict(zip(keys, combo))
        stats = collect_and_score(data, strategy, params, is_japan)
        if stats:
            results.append((stats['score'], params, stats))
            if stats['score'] > best_score:
                best_score = stats['score']
                best_params = params
                best_stats = stats

    results.sort(key=lambda x: x[0], reverse=True)
    print(f"    有効パターン: {len(results)}")
    for sc, p, st in results[:3]:
        print(f"    {p} → 勝率{st['win_rate']}% | 平均{st['avg_pnl']:+.2f}% | PF{st['profit_factor']} | score{sc}")
    return best_params, best_stats

if __name__ == "__main__":
    print("="*65)
    print("ALMANAC 全銘柄バックテスト＋最適化")
    print("="*65)

    us_tickers, jp_tickers = load_tickers()
    print(f"\n米国株: {len(us_tickers)}銘柄 / 日本株: {len(jp_tickers)}銘柄")

    print("\n【米国株データ取得】")
    us_data = load_data(us_tickers)
    print("\n【日本株データ取得】")
    jp_data = load_data(jp_tickers)

    optimal = {}

    # 逆張り：4パラメータ×3値 = 81パターン
    grid_mr = {'rsi': [25,30,35], 'vol': [1.2,1.5,2.0],
               'mom5d': [-3,-5,-8], 'hold': [10,15,20], 'stop_mult': [1.5,2.0,2.5]}
    print("\n逆張り最適化:")
    p,s = optimize(us_data, '逆張り', grid_mr, False, "逆張り(US)")
    optimal['逆張り_US'] = {'params': p, 'stats': s}
    p,s = optimize(jp_data, '逆張り', grid_mr, True, "逆張り(JP)")
    optimal['逆張り_JP'] = {'params': p, 'stats': s}

    # モメンタム：72パターン
    grid_mo = {'rsi_min': [50,55,60], 'ma50_min': [3,5,8], 'ma50_max': [12,15,20],
               'vol': [1.2,1.5], 'hold': [7,10,14], 'stop_mult': [1.5,2.0]}
    print("\nモメンタム最適化:")
    p,s = optimize(us_data, 'モメンタム', grid_mo, False, "モメンタム(US)")
    optimal['モメンタム_US'] = {'params': p, 'stats': s}
    p,s = optimize(jp_data, 'モメンタム', grid_mo, True, "モメンタム(JP)")
    optimal['モメンタム_JP'] = {'params': p, 'stats': s}

    # ギャップダウン：36パターン
    grid_gd = {'gap': [-2,-3,-5], 'vol': [1.2,1.5,2.0],
               'hold': [5,7,10], 'stop_mult': [1.5,2.0,2.5]}
    print("\nギャップダウン最適化:")
    p,s = optimize(us_data, 'ギャップダウン', grid_gd, False, "ギャップダウン(US)")
    optimal['ギャップダウン_US'] = {'params': p, 'stats': s}
    p,s = optimize(jp_data, 'ギャップダウン', grid_gd, True, "ギャップダウン(JP)")
    optimal['ギャップダウン_JP'] = {'params': p, 'stats': s}

    # イベントドリブン後：36パターン
    grid_ed = {'change': [-5,-7,-10], 'vol': [2.0,3.0],
               'hold': [7,10,14], 'stop_mult': [1.5,2.0,2.5]}
    print("\nイベントドリブン後最適化:")
    p,s = optimize(us_data, 'イベントドリブン後', grid_ed, False, "イベントドリブン後(US)")
    optimal['イベントドリブン後_US'] = {'params': p, 'stats': s}
    p,s = optimize(jp_data, 'イベントドリブン後', grid_ed, True, "イベントドリブン後(JP)")
    optimal['イベントドリブン後_JP'] = {'params': p, 'stats': s}

    print("\n" + "="*65)
    print("最適パラメータ まとめ")
    print("="*65)
    for label, r in optimal.items():
        if r['stats']:
            s = r['stats']
            print(f"\n【{label}】")
            print(f"  パラメータ: {r['params']}")
            print(f"  勝率: {s['win_rate']}% | 平均損益: {s['avg_pnl']:+.2f}% | PF: {s['profit_factor']} | トレード数: {s['trades']}")
        else:
            print(f"\n【{label}】最適解なし")

    out = os.path.expanduser('~/portfolio-bot/backtest_full_results.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(optimal, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n結果保存: {out}")
