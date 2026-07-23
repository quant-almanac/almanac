import yfinance as yf
import pandas as pd
import json
import os
from datetime import datetime, timedelta
from itertools import product

TEST_TICKERS = [
    'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA',
    'JPM', 'JNJ', 'XOM', 'WMT', 'PG', 'KO', 'MRK', 'ADI',
    'TMO', 'INTU', 'BKNG', 'PANW', 'ADBE'
]

def load_data(tickers, period_years=3):
    """全銘柄のデータを一括取得"""
    print("データ取得中...")
    data = {}
    for ticker in tickers:
        try:
            end = datetime.now()
            start = end - timedelta(days=365 * period_years)
            hist = yf.Ticker(ticker).history(start=start, end=end)
            if hist.empty or len(hist) < 60:
                continue
            hist = hist.reset_index()
            hist['Date'] = pd.to_datetime(hist['Date']).dt.tz_localize(None)

            delta = hist['Close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = -delta.where(delta < 0, 0).rolling(14).mean()
            hist['RSI'] = 100 - (100 / (1 + gain / loss))
            hist['VolRatio'] = hist['Volume'] / hist['Volume'].rolling(20).mean()
            hist['Mom1M'] = hist['Close'].pct_change(22) * 100
            hist['Mom3M'] = hist['Close'].pct_change(66) * 100
            hist['Change'] = hist['Close'].pct_change() * 100
            data[ticker] = hist
            print(f"  {ticker}: {len(hist)}日分")
        except Exception as e:
            print(f"  {ticker} エラー: {e}")
    return data

def simulate(data, params):
    """パラメータセットでシミュレーション"""
    trades = []
    for ticker, hist in data.items():
        for i in range(66, len(hist) - params['hold']):
            row = hist.iloc[i]
            rsi = row['RSI']
            vol_ratio = row['VolRatio']
            mom_1m = row['Mom1M']
            mom_3m = row['Mom3M']
            change = row['Change']

            if pd.isna(rsi) or pd.isna(vol_ratio) or pd.isna(mom_1m) or pd.isna(mom_3m):
                continue

            # 戦略別条件チェック
            strategy = params['strategy']
            match = False

            if strategy == '逆張り':
                match = rsi < params['rsi_max']
            elif strategy == '順張り':
                match = (rsi > params['rsi_min'] and
                        mom_1m > params['mom1m_min'] and
                        mom_3m > params['mom3m_min'] and
                        vol_ratio > params['vol_min'])
            elif strategy == 'ギャップダウン':
                match = (change <= params['gap_max'] and
                        vol_ratio > params['vol_min'] and
                        params['rsi_min'] < rsi < params['rsi_max'])

            if not match:
                continue

            entry_price = row['Close']
            exit_price = entry_price
            hit_stop = False

            for j in range(1, params['hold'] + 1):
                future_price = hist.iloc[i + j]['Close']
                ret = (future_price - entry_price) / entry_price * 100
                if ret <= params['stop']:
                    exit_price = entry_price * (1 + params['stop'] / 100)
                    hit_stop = True
                    break
                exit_price = future_price

            pnl = (exit_price - entry_price) / entry_price * 100
            trades.append(pnl)

    if len(trades) < 30:  # 30件未満は統計的に無意味
        return None

    wins = [p for p in trades if p > 0]
    losses = [p for p in trades if p <= 0]
    win_rate = len(wins) / len(trades) * 100
    avg_pnl = sum(trades) / len(trades)
    pf = abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999

    return {
        'trades': len(trades),
        'win_rate': round(win_rate, 1),
        'avg_pnl': round(avg_pnl, 2),
        'profit_factor': round(pf, 2),
        'score': round(avg_pnl * win_rate / 100 * pf, 3)  # 総合スコア
    }

def optimize_mean_reversion(data):
    print("\n【逆張り戦略 最適化】")
    best = None
    best_params = None
    results = []

    for rsi_max, hold, stop in product(
        [15, 20, 25, 30],      # RSI閾値
        [5, 10, 15, 20],       # 保有日数
        [-5, -7, -10]          # 損切り
    ):
        params = {'strategy': '逆張り', 'rsi_max': rsi_max, 'hold': hold, 'stop': stop}
        result = simulate(data, params)
        if result:
            result.update(params)
            results.append(result)
            if best is None or result['score'] > best['score']:
                best = result
                best_params = params

    results.sort(key=lambda x: x['score'], reverse=True)
    print("上位5パラメータ:")
    for r in results[:5]:
        print(f"  RSI<{r['rsi_max']} | 保有{r['hold']}日 | 損切{r['stop']}% → "
              f"勝率{r['win_rate']}% | 平均{r['avg_pnl']:+.2f}% | PF{r['profit_factor']} | スコア{r['score']}")
    return best_params, best

def optimize_momentum(data):
    print("\n【順張り戦略 最適化】")
    best = None
    best_params = None
    results = []

    for rsi_min, mom1m_min, mom3m_min, vol_min, hold, stop in product(
        [60, 65, 70],          # RSI下限
        [5, 8, 12],            # 1Mモメンタム
        [10, 15, 20],          # 3Mモメンタム
        [1.0, 1.2, 1.5],       # 出来高
        [5, 7, 10],            # 保有日数
        [-3, -5]               # 損切り
    ):
        params = {
            'strategy': '順張り',
            'rsi_min': rsi_min, 'mom1m_min': mom1m_min,
            'mom3m_min': mom3m_min, 'vol_min': vol_min,
            'hold': hold, 'stop': stop
        }
        result = simulate(data, params)
        if result:
            result.update(params)
            results.append(result)
            if best is None or result['score'] > best['score']:
                best = result
                best_params = params

    results.sort(key=lambda x: x['score'], reverse=True)
    print("上位5パラメータ:")
    for r in results[:5]:
        print(f"  RSI>{r['rsi_min']} | Mom1M>{r['mom1m_min']}% | Mom3M>{r['mom3m_min']}% | "
              f"Vol>{r['vol_min']}x | 保有{r['hold']}日 | 損切{r['stop']}% → "
              f"勝率{r['win_rate']}% | 平均{r['avg_pnl']:+.2f}% | PF{r['profit_factor']} | スコア{r['score']}")
    return best_params, best

def optimize_gap_down(data):
    print("\n【ギャップダウン戦略 最適化】")
    best = None
    best_params = None
    results = []

    for gap_max, vol_min, rsi_min, rsi_max, hold, stop in product(
        [-2, -3, -5],          # ギャップ下限
        [1.2, 1.5, 2.0],       # 出来高
        [25, 30, 35],          # RSI下限
        [50, 55, 60],          # RSI上限
        [3, 5, 7],             # 保有日数
        [-3, -5]               # 損切り
    ):
        params = {
            'strategy': 'ギャップダウン',
            'gap_max': gap_max, 'vol_min': vol_min,
            'rsi_min': rsi_min, 'rsi_max': rsi_max,
            'hold': hold, 'stop': stop
        }
        result = simulate(data, params)
        if result:
            result.update(params)
            results.append(result)
            if best is None or result['score'] > best['score']:
                best = result
                best_params = params

    results.sort(key=lambda x: x['score'], reverse=True)
    print("上位5パラメータ:")
    for r in results[:5]:
        print(f"  Gap<{r['gap_max']}% | Vol>{r['vol_min']}x | RSI {r['rsi_min']}-{r['rsi_max']} | "
              f"保有{r['hold']}日 | 損切{r['stop']}% → "
              f"勝率{r['win_rate']}% | 平均{r['avg_pnl']:+.2f}% | PF{r['profit_factor']} | スコア{r['score']}")
    return best_params, best

if __name__ == "__main__":
    print("パラメータ最適化開始")
    print("="*60)

    # データ一括取得
    data = load_data(TEST_TICKERS)
    print(f"\n{len(data)}銘柄のデータ取得完了")

    # 各戦略を最適化
    mr_params, mr_best = optimize_mean_reversion(data)
    mo_params, mo_best = optimize_momentum(data)
    gd_params, gd_best = optimize_gap_down(data)

    print("\n" + "="*60)
    print("最適パラメータまとめ")
    print("="*60)

    results = {
        '逆張り': {'params': mr_params, 'stats': mr_best},
        '順張り': {'params': mo_params, 'stats': mo_best},
        'ギャップダウン': {'params': gd_params, 'stats': gd_best},
    }

    for strategy, r in results.items():
        if r['params']:
            print(f"\n【{strategy}】")
            print(f"  パラメータ: {r['params']}")
            print(f"  勝率: {r['stats']['win_rate']}% | 平均損益: {r['stats']['avg_pnl']:+.2f}% | "
                  f"PF: {r['stats']['profit_factor']} | トレード数: {r['stats']['trades']}")

    # 結果保存
    output_path = os.path.expanduser('~/portfolio-bot/optimize_results.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n結果を保存: {output_path}")
