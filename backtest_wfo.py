import yfinance as yf
import pandas as pd
import numpy as np
import json
import os
import time
from datetime import datetime, timedelta
from itertools import product

from almanac.runtime_config import get_env

TICKERS_FILE = os.path.expanduser('~/portfolio-bot/tickers.json')
BACKTEST_STATUS = "retired"
RESEARCH_OPT_IN_ENV = "ALMANAC_ENABLE_LEGACY_WFO"

# ============================================================
# データ取得
# ============================================================
def load_tickers():
    with open(TICKERS_FILE) as f:
        data = json.load(f)['all']
    return [t for t in data if not t.endswith('.T')], [t for t in data if t.endswith('.T')]

def load_data(tickers, period_years=4):
    """4年分取得（ウォークフォワードに十分な期間）"""
    print(f"  データ取得中: {len(tickers)}銘柄 ({period_years}年分)...")
    data = {}
    for i, ticker in enumerate(tickers):
        try:
            end = datetime.now()
            start = end - timedelta(days=365 * period_years)
            hist = yf.Ticker(ticker).history(start=start, end=end)
            if hist.empty or len(hist) < 200:
                continue
            hist = hist.reset_index()
            hist['Date'] = pd.to_datetime(hist['Date']).dt.tz_localize(None)

            # 指標計算
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
            if (i + 1) % 20 == 0:
                print(f"    {i+1}/{len(tickers)}...", flush=True)
        except:
            pass
    print(f"  ✅ 取得完了: {len(data)}銘柄")
    return data

# ============================================================
# レジーム判定
# ============================================================
def build_regime_map(start_year=2021):
    """日次のVIX・SPY50日線からレジームを構築"""
    print("  レジームマップ構築中...")
    try:
        vix = yf.Ticker("^VIX").history(start=f"{start_year}-01-01")['Close']
        spy = yf.Ticker("SPY").history(start=f"{start_year}-01-01")['Close']
        spy_ma50 = spy.rolling(50).mean()

        vix.index = vix.index.tz_localize(None)
        spy.index = spy.index.tz_localize(None)
        spy_ma50.index = spy_ma50.index.tz_localize(None)

        regime = pd.DataFrame({'vix': vix, 'spy': spy, 'spy_ma50': spy_ma50})
        regime = regime.dropna()

        def classify(row):
            if row['vix'] > 30:
                return 'C_弱気'
            elif row['vix'] > 20 or row['spy'] < row['spy_ma50']:
                return 'B_中立'
            else:
                return 'A_強気'

        regime['regime'] = regime.apply(classify, axis=1)
        print(f"  ✅ レジームマップ完成: {len(regime)}日分")
        dist = regime['regime'].value_counts()
        for k, v in dist.items():
            print(f"    {k}: {v}日 ({v/len(regime)*100:.1f}%)")
        return regime
    except Exception as e:
        print(f"  レジームマップ失敗: {e}")
        return None

def get_regime(date, regime_map):
    if regime_map is None:
        return 'A_強気'
    try:
        date = pd.Timestamp(date)
        idx = regime_map.index.get_indexer([date], method='ffill')[0]
        if idx < 0:
            return 'A_強気'
        return regime_map.iloc[idx]['regime']
    except:
        return 'A_強気'

# ============================================================
# シミュレーション
# ============================================================
def simulate_trade(hist, entry_idx, hold_days, stop_atr_mult=2.0, trail_days=5):
    entry_price = float(hist.iloc[entry_idx]['Close'])
    atr = float(hist.iloc[entry_idx]['ATR'])
    if np.isnan(atr) or atr <= 0:
        return 0, 'エラー', 0
    stop_price = entry_price - stop_atr_mult * atr
    exit_price = entry_price
    exit_reason = 'タイムストップ'
    actual_hold = 0
    for j in range(1, hold_days + 1):
        if entry_idx + j >= len(hist): break
        price = float(hist.iloc[entry_idx + j]['Close'])
        if price <= stop_price:
            exit_price = stop_price; exit_reason = 'ストップロス'; actual_hold = j; break
        if j >= trail_days:
            trail_low = float(hist.iloc[entry_idx + j - trail_days:entry_idx + j]['Low'].min())
            if price < trail_low:
                exit_price = price; exit_reason = 'トレーリング'; actual_hold = j; break
        exit_price = price; actual_hold = j
    return (exit_price - entry_price) / entry_price * 100, exit_reason, actual_hold

def collect_signals(data, strategy, params, is_japan, regime_map=None,
                    date_from=None, date_to=None, target_regime=None):
    """シグナル収集（期間・レジームフィルタ付き）"""
    turnover_min = 1e9 if is_japan else 1e7
    trades = []
    for ticker, hist in data.items():
        for i in range(60, len(hist) - params['hold']):
            row = hist.iloc[i]
            row_date = row['Date'] if hasattr(row['Date'], 'date') else pd.Timestamp(row['Date'])

            # 期間フィルタ
            if date_from and row_date < date_from: continue
            if date_to and row_date > date_to: continue

            # レジームフィルタ
            if target_regime and regime_map is not None:
                regime = get_regime(row_date, regime_map)
                if regime != target_regime: continue

            if pd.isna(row.get('RSI')) or pd.isna(row.get('ATR_pct')): continue
            if row['ATR_pct'] < 2.0: continue
            if row['AvgTurnover'] < turnover_min: continue

            match = False
            if strategy == '逆張り':
                match = (row['RSI'] < params['rsi'] and
                         row['VolRatio'] >= params['vol'] and
                         row['Mom5d'] <= params['mom5d'])
            elif strategy == 'モメンタム':
                match = (row['RSI'] >= params['rsi_min'] and
                         params['ma50_min'] <= row['MA50_dev'] <= params['ma50_max'] and
                         row['New52wHigh'] and row['Close'] > row['MA50'] and
                         row['VolRatio'] >= params['vol'])
            elif strategy == 'ギャップダウン':
                match = (row['Gap'] <= params['gap'] and row['VolRatio'] >= params['vol'])
            elif strategy == 'イベントドリブン後':
                match = (row['Change'] <= params['change'] and row['VolRatio'] >= params['vol'])

            if match:
                pnl, ex, hold = simulate_trade(hist, i, params['hold'], params['stop_mult'])
                trades.append({'pnl': pnl, 'exit': ex, 'date': str(row_date)[:10]})
    return trades

def score_trades(trades, min_count=20):
    if len(trades) < min_count: return None
    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    if not wins or not losses: return None
    pf = abs(sum(wins) / sum(losses))
    wr = len(wins) / len(trades)
    avg = sum(pnls) / len(pnls)
    return {'trades': len(trades), 'win_rate': round(wr*100,1),
            'avg_pnl': round(avg,2), 'profit_factor': round(pf,2),
            'score': round(avg * wr * pf, 3)}

# ============================================================
# ウォークフォワード最適化
# ============================================================
WFO_WINDOWS = [
    # (訓練開始, 訓練終了, テスト開始, テスト終了)
    (datetime(2021,  6, 1), datetime(2022, 11, 30), datetime(2022, 12, 1), datetime(2023,  5, 31)),
    (datetime(2021, 12, 1), datetime(2023,  5, 31), datetime(2023,  6, 1), datetime(2023, 11, 30)),
    (datetime(2022,  6, 1), datetime(2023, 11, 30), datetime(2023, 12, 1), datetime(2024,  5, 31)),
    (datetime(2022, 12, 1), datetime(2024,  5, 31), datetime(2024,  6, 1), datetime(2024, 11, 30)),
    (datetime(2023,  6, 1), datetime(2024, 11, 30), datetime(2024, 12, 1), datetime(2025,  2, 28)),
]

def progress_bar(current, total, start_time, label='', width=25):
    pct = current / total if total > 0 else 0
    filled = int(width * pct)
    bar = '█' * filled + '░' * (width - filled)
    elapsed = time.time() - start_time
    eta = (elapsed / current * (total - current)) if current > 0 else 0
    eta_str = f"{int(eta//60)}m{int(eta%60)}s" if eta > 60 else f"{int(eta)}s"
    print(f"\r  [{bar}] {current}/{total} {label} ETA:{eta_str}   ", end='', flush=True)

def optimize_window(data, strategy, grid, is_japan, regime_map,
                    train_from, train_to, min_count=20):
    """訓練期間で最適パラメータを探す"""
    keys = list(grid.keys())
    combos = list(product(*grid.values()))
    best_score = -999
    best_params = None
    start_t = time.time()

    for idx, combo in enumerate(combos):
        progress_bar(idx+1, len(combos), start_t, f"訓練中")
        params = dict(zip(keys, combo))
        trades = collect_signals(data, strategy, params, is_japan, regime_map,
                                 date_from=train_from, date_to=train_to)
        stats = score_trades(trades, min_count)
        if stats and stats['score'] > best_score:
            best_score = stats['score']
            best_params = params
    print()
    return best_params

def run_wfo(data, strategy, grid, is_japan, regime_map, label):
    """ウォークフォワード実行"""
    print(f"\n  ── {label} ──")
    all_test_trades = []
    window_results = []

    for w_idx, (tr_from, tr_to, te_from, te_to) in enumerate(WFO_WINDOWS):
        print(f"  Window{w_idx+1}: 訓練({tr_from.strftime('%Y/%m')}-{tr_to.strftime('%Y/%m')}) "
              f"→ テスト({te_from.strftime('%Y/%m')}-{te_to.strftime('%Y/%m')})")

        # 訓練：最適パラメータ探索
        best_params = optimize_window(data, strategy, grid, is_japan, regime_map,
                                      tr_from, tr_to)
        if not best_params:
            print(f"    ⚠️  Window{w_idx+1}: 有効パラメータなし")
            continue

        # テスト：最適パラメータで未知期間を検証
        test_trades = collect_signals(data, strategy, best_params, is_japan, regime_map,
                                      date_from=te_from, date_to=te_to)
        stats = score_trades(test_trades, min_count=10)

        result = {
            'window': w_idx + 1,
            'train': f"{tr_from.strftime('%Y/%m')}-{tr_to.strftime('%Y/%m')}",
            'test': f"{te_from.strftime('%Y/%m')}-{te_to.strftime('%Y/%m')}",
            'best_params': best_params,
            'test_stats': stats,
            'test_trades': len(test_trades)
        }
        window_results.append(result)
        all_test_trades.extend(test_trades)

        if stats:
            print(f"    ✅ テスト結果: 勝率{stats['win_rate']}% | 平均{stats['avg_pnl']:+.2f}% | "
                  f"PF{stats['profit_factor']} | {stats['trades']}件")
            print(f"    最適パラメータ: {best_params}")
        else:
            print(f"    ⚠️  テスト件数不足 ({len(test_trades)}件)")

    # 全テスト期間の統合スコア
    overall = score_trades(all_test_trades, min_count=20)
    if overall:
        print(f"\n  📊 {label} 全期間テスト統合:")
        print(f"     勝率{overall['win_rate']}% | 平均{overall['avg_pnl']:+.2f}% | "
              f"PF{overall['profit_factor']} | 総トレード{overall['trades']}件")
    else:
        print(f"\n  ⚠️  {label} 統合結果: データ不足")

    return window_results, overall

# ============================================================
# レジーム別最適化
# ============================================================
def run_regime_optimization(data, strategy, grid, is_japan, regime_map, label):
    """レジーム別に最適パラメータを探す"""
    print(f"\n  ── {label} レジーム別 ──")
    regimes = ['A_強気', 'B_中立', 'C_弱気']
    regime_results = {}

    for regime in regimes:
        keys = list(grid.keys())
        combos = list(product(*grid.values()))
        best_score = -999
        best_params = None
        best_stats = None
        start_t = time.time()

        for idx, combo in enumerate(combos):
            progress_bar(idx+1, len(combos), start_t, f"{regime}")
            params = dict(zip(keys, combo))
            trades = collect_signals(data, strategy, params, is_japan, regime_map,
                                     target_regime=regime)
            stats = score_trades(trades, min_count=15)
            if stats and stats['score'] > best_score:
                best_score = stats['score']
                best_params = params
                best_stats = stats
        print()

        regime_results[regime] = {'params': best_params, 'stats': best_stats}
        if best_stats:
            print(f"    {regime}: 勝率{best_stats['win_rate']}% | 平均{best_stats['avg_pnl']:+.2f}% | "
                  f"PF{best_stats['profit_factor']} | {best_stats['trades']}件")
            print(f"    → {best_params}")
        else:
            print(f"    {regime}: データ不足")

    return regime_results

# ============================================================
# メイン
# ============================================================
GRIDS = {
    '逆張り': {
        'rsi':       [20, 25, 30, 35],
        'vol':       [1.2, 1.5, 2.0],
        'mom5d':     [-3, -5, -7, -10],
        'hold':      [7, 10, 15, 20],
        'stop_mult': [1.5, 2.0, 2.5],
    },
    'モメンタム': {
        'rsi_min':   [50, 55, 60],
        'ma50_min':  [3, 5, 8],
        'ma50_max':  [12, 15, 20],
        'vol':       [1.2, 1.5, 2.0],
        'hold':      [5, 7, 10, 14],
        'stop_mult': [1.5, 2.0, 2.5],
    },
    'ギャップダウン': {
        'gap':       [-2, -3, -5, -7],
        'vol':       [1.2, 1.5, 2.0, 2.5],
        'hold':      [3, 5, 7, 10],
        'stop_mult': [1.5, 2.0, 2.5],
    },
    'イベントドリブン後': {
        'change':    [-5, -7, -10, -15],
        'vol':       [2.0, 3.0, 4.0],
        'hold':      [5, 7, 10, 14],
        'stop_mult': [1.5, 2.0, 2.5],
    },
}

if __name__ == "__main__":
    if get_env(RESEARCH_OPT_IN_ENV) != "1":
        print(json.dumps({
            "status": BACKTEST_STATUS,
            "reason": "legacy WFO lacks DSR and realized transaction costs",
            "replacement": "feature_validation.py certify",
            "research_opt_in": f"set {RESEARCH_OPT_IN_ENV}=1 explicitly",
        }, ensure_ascii=False, indent=2))
        raise SystemExit(0)
    total_start = time.time()
    print("=" * 65)
    print("ALMANAC ウォークフォワード＋レジーム別最適化")
    print(f"開始: {datetime.now().strftime('%Y/%m/%d %H:%M:%S')}")
    print("=" * 65)

    us_tickers, jp_tickers = load_tickers()
    print(f"\n米国株: {len(us_tickers)}銘柄 / 日本株: {len(jp_tickers)}銘柄")

    print("\n【データ取得】")
    us_data = load_data(us_tickers, period_years=4)
    jp_data = load_data(jp_tickers, period_years=4)

    print("\n【レジームマップ構築】")
    regime_map = build_regime_map(start_year=2021)

    results = {'wfo': {}, 'regime': {}}

    for strategy in ['逆張り', 'モメンタム', 'ギャップダウン', 'イベントドリブン後']:
        grid = GRIDS[strategy]
        combos = len(list(product(*grid.values())))
        windows = len(WFO_WINDOWS)
        regimes = 3
        total_combos = combos * (windows + regimes) * 2  # US+JP
        print(f"\n{'='*65}")
        print(f"【{strategy}戦略】 {combos}パターン × {windows}ウィンドウ + {regimes}レジーム × US/JP")
        print(f"  推定処理量: {total_combos:,}パターン")
        print(f"{'='*65}")

        for market, data, is_jp in [('US', us_data, False), ('JP', jp_data, True)]:
            label = f"{strategy}({market})"

            # ウォークフォワード
            print(f"\n  ◆ {label} ウォークフォワード最適化")
            wfo_result, wfo_overall = run_wfo(data, strategy, grid, is_jp, regime_map, label)
            results['wfo'][label] = {'windows': wfo_result, 'overall': wfo_overall}

            # レジーム別
            print(f"\n  ◆ {label} レジーム別最適化")
            regime_result = run_regime_optimization(data, strategy, grid, is_jp, regime_map, label)
            results['regime'][label] = regime_result

    # ============================================================
    # 最終サマリー
    # ============================================================
    elapsed = (time.time() - total_start) / 60
    print(f"\n{'='*65}")
    print(f"最終サマリー（総所要時間: {elapsed:.1f}分）")
    print(f"{'='*65}")

    print("\n【ウォークフォワード 全期間テスト結果（信頼性順）】")
    wfo_ranking = []
    for label, r in results['wfo'].items():
        if r['overall']:
            wfo_ranking.append((r['overall']['profit_factor'], label, r['overall']))
    wfo_ranking.sort(reverse=True)
    for pf, label, stats in wfo_ranking:
        print(f"  {label}: PF{stats['profit_factor']} | 勝率{stats['win_rate']}% | "
              f"平均{stats['avg_pnl']:+.2f}% | {stats['trades']}件")

    print("\n【レジーム別 最適パラメータ】")
    for label, regimes in results['regime'].items():
        print(f"\n  {label}:")
        for regime, r in regimes.items():
            if r['stats']:
                print(f"    {regime}: PF{r['stats']['profit_factor']} | {r['params']}")
            else:
                print(f"    {regime}: データ不足")

    # 保存
    out = os.path.expanduser('~/portfolio-bot/backtest_wfo_results.json')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n結果保存: {out}")
    print(f"終了: {datetime.now().strftime('%H:%M:%S')}")
