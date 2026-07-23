import yfinance as yf
import pandas as pd
import json
import os
from datetime import datetime, timedelta

# S&P500代表銘柄（各セクターから）
SP500_TICKERS = [
    # テック
    'AAPL', 'MSFT', 'GOOGL', 'NVDA', 'META', 'ADBE', 'CRM', 'INTU',
    # ヘルスケア
    'JNJ', 'UNH', 'TMO', 'ABT', 'ISRG',
    # 金融
    'JPM', 'BAC', 'GS', 'V', 'MA',
    # 消費財
    'AMZN', 'TSLA', 'HD', 'MCD', 'BKNG',
    # 資本財
    'CAT', 'DE', 'RTX', 'HON',
    # エネルギー
    'XOM', 'CVX',
    # 生活必需品
    'PG', 'KO', 'WMT', 'COST',
]

# 日経225代表銘柄
NIKKEI_TICKERS = [
    '7203.T', '6758.T', '9984.T', '8306.T', '7267.T',
    '6861.T', '9433.T', '4063.T', '6098.T', '8316.T',
    '7974.T', '6594.T', '4519.T', '9022.T', '8766.T',
]

def load_data(tickers, period_years=3):
    print(f"  データ取得中: {len(tickers)}銘柄...")
    data = {}
    for ticker in tickers:
        try:
            end = datetime.now()
            start = end - timedelta(days=365 * period_years)
            hist = yf.Ticker(ticker).history(start=start, end=end)
            if hist.empty or len(hist) < 100:
                continue
            hist = hist.reset_index()
            hist['Date'] = pd.to_datetime(hist['Date']).dt.tz_localize(None)

            # 各種指標計算
            delta = hist['Close'].diff()
            gain = delta.where(delta > 0, 0).rolling(14).mean()
            loss = -delta.where(delta < 0, 0).rolling(14).mean()
            hist['RSI'] = 100 - (100 / (1 + gain / loss))
            hist['MA50'] = hist['Close'].rolling(50).mean()
            hist['MA50_dev'] = (hist['Close'] - hist['MA50']) / hist['MA50'] * 100
            hist['VolRatio'] = hist['Volume'] / hist['Volume'].rolling(20).mean()
            hist['Mom5d'] = hist['Close'].pct_change(5) * 100
            hist['Mom1m'] = hist['Close'].pct_change(22) * 100
            hist['Change'] = hist['Close'].pct_change() * 100
            hist['Gap'] = (hist['Open'] - hist['Close'].shift()) / hist['Close'].shift() * 100

            # ATR
            tr = pd.concat([
                hist['High'] - hist['Low'],
                abs(hist['High'] - hist['Close'].shift()),
                abs(hist['Low'] - hist['Close'].shift())
            ], axis=1).max(axis=1)
            hist['ATR'] = tr.rolling(14).mean()
            hist['ATR_pct'] = hist['ATR'] / hist['Close'] * 100

            # 52週高値（直近5日で更新したか）
            hist['High52w'] = hist['High'].rolling(252).max()
            hist['New52wHigh'] = hist['High'].rolling(5).max() >= hist['High52w'] * 0.99

            # 平均売買代金
            hist['AvgTurnover'] = hist['Volume'].rolling(20).mean() * hist['Close'].rolling(20).mean()

            data[ticker] = hist
        except Exception as e:
            pass
    print(f"  取得完了: {len(data)}銘柄")
    return data

# ============================================================
# P1-6: 取引コスト・スプレッド定数（bps 単位、往復で 2 倍される）
# ============================================================
# US: SBI 米国株 約定 0.495% = 49.5 bps（片道）+ 平均スプレッド 5 bps
# JP: SBI 国内株 約定 0.05% = 5 bps（アクティブ）+ 板スプレッド 2 bps
COST_BPS_US   = 49.5
COST_BPS_JP   = 5.0
SPREAD_BPS_US = 5.0
SPREAD_BPS_JP = 2.0


def _round_trip_cost_pct(is_japan: bool) -> float:
    """往復（buy+sell）の取引コスト+スプレッドを % で返す。"""
    cost   = COST_BPS_JP   if is_japan else COST_BPS_US
    spread = SPREAD_BPS_JP if is_japan else SPREAD_BPS_US
    # bps × 2（往復）÷ 100 = %
    return (cost + spread) * 2 / 100


def simulate_trade(hist, entry_idx, hold_days, stop_atr_multiplier=2.0, trail_days=5,
                   is_japan: bool = False):
    """
    エグジット戦略：
    1. ストップロス: エントリー価格 - stop_atr_multiplier × ATR
    2. トレーリング: 直近5日安値を更新
    3. タイムストップ: hold_days経過

    P1-5: ルックアヘッドバイアス除去。シグナル判定は entry_idx 日の Close、
          エントリーは entry_idx + 1 日の Open で約定（現実に約定可能な価格）。
    P1-6: 往復の取引コスト（JP 14bps / US 109bps）を pnl から差し引く。
    """
    # P1-5: entry_idx+1 が存在しないと約定不能
    if entry_idx + 1 >= len(hist):
        return 0.0, 'シグナル無効', 0

    entry_price = hist.iloc[entry_idx + 1]['Open']
    # ATR はシグナル日の値（判定時点で既知）をストップ幅算出に使用
    atr = hist.iloc[entry_idx]['ATR']
    stop_price = entry_price - stop_atr_multiplier * atr

    exit_price = entry_price
    exit_reason = 'タイムストップ'
    actual_hold = 0

    # 約定日（entry_idx + 1）から hold_days 日間監視
    # j=1 は約定翌日（= entry_idx + 2）
    for j in range(1, hold_days + 1):
        bar_idx = entry_idx + 1 + j
        if bar_idx >= len(hist):
            break
        row = hist.iloc[bar_idx]
        low   = row['Low']
        high  = row['High']
        close = row['Close']

        # ストップロス: その日の Low が stop_price を割ったら stop で約定
        if low <= stop_price:
            exit_price = stop_price
            exit_reason = 'ストップロス'
            actual_hold = j
            break

        # トレーリングストップ（直近 trail_days 日安値）
        if j >= trail_days:
            trail_low = hist.iloc[bar_idx - trail_days:bar_idx]['Low'].min()
            if close < trail_low:
                exit_price = close
                exit_reason = 'トレーリング'
                actual_hold = j
                break

        exit_price = close
        actual_hold = j

    pnl = (exit_price - entry_price) / entry_price * 100
    # P1-6: 往復コスト差し引き
    pnl -= _round_trip_cost_pct(is_japan)
    return pnl, exit_reason, actual_hold

def backtest_mean_reversion(data, is_japan=False):
    """逆張り戦略バックテスト。P1-5: i+1 Open 約定のため上限を -16 に延長。"""
    trades = []
    rsi_threshold = 27 if is_japan else 30

    for ticker, hist in data.items():
        for i in range(60, len(hist) - 16):   # P1-5: hold_days=15 + 1 (entry 翌日)
            row = hist.iloc[i]
            if pd.isna(row['RSI']) or pd.isna(row['ATR_pct']): continue
            if row['ATR_pct'] < 2.0: continue
            if row['AvgTurnover'] < (1e9 if is_japan else 1e7): continue

            if (row['RSI'] < rsi_threshold and
                row['VolRatio'] >= 1.5 and
                row['Mom5d'] <= -5.0):

                pnl, exit_reason, hold = simulate_trade(hist, i, hold_days=15, is_japan=is_japan)
                trades.append({'ticker': ticker, 'pnl': pnl, 'exit': exit_reason, 'hold': hold,
                               'date': hist.iloc[i]['Date'].strftime('%Y-%m-%d')})
    return trades

def backtest_momentum(data, is_japan=False):
    """モメンタム戦略バックテスト。P1-5: hold_days=10 + 1。"""
    trades = []

    for ticker, hist in data.items():
        for i in range(60, len(hist) - 13):   # P1-5: hold_days=10 + buffer
            row = hist.iloc[i]
            if pd.isna(row['RSI']) or pd.isna(row['MA50_dev']): continue
            if row['ATR_pct'] < 2.0: continue
            if row['AvgTurnover'] < (1e9 if is_japan else 1e7): continue

            if (row['RSI'] >= 50 and
                3.0 <= row['MA50_dev'] <= 15.0 and
                row['New52wHigh'] and
                row['Close'] > row['MA50'] and
                row['VolRatio'] >= 1.2):

                pnl, exit_reason, hold = simulate_trade(hist, i, hold_days=10, trail_days=5,
                                                       is_japan=is_japan)
                trades.append({'ticker': ticker, 'pnl': pnl, 'exit': exit_reason, 'hold': hold,
                               'date': hist.iloc[i]['Date'].strftime('%Y-%m-%d')})
    return trades

def backtest_gap_down(data, is_japan=False):
    """ギャップダウン戦略バックテスト。P1-5: hold_days=7 + 1。"""
    trades = []

    for ticker, hist in data.items():
        for i in range(60, len(hist) - 9):   # P1-5: hold_days=7 + buffer
            row = hist.iloc[i]
            if pd.isna(row['Gap']) or pd.isna(row['ATR_pct']): continue
            if row['ATR_pct'] < 2.0: continue
            if row['AvgTurnover'] < (1e9 if is_japan else 1e7): continue

            if (row['Gap'] <= -3.0 and
                row['VolRatio'] >= 1.5):

                pnl, exit_reason, hold = simulate_trade(hist, i, hold_days=7, stop_atr_multiplier=2.0,
                                                       is_japan=is_japan)
                trades.append({'ticker': ticker, 'pnl': pnl, 'exit': exit_reason, 'hold': hold,
                               'date': hist.iloc[i]['Date'].strftime('%Y-%m-%d')})
    return trades

def backtest_event_driven_post(data, is_japan=False):
    """イベントドリブン後バックテスト（±5%以上の急騰急落）。P1-5: hold_days=10 + 1。"""
    trades = []

    for ticker, hist in data.items():
        for i in range(60, len(hist) - 13):   # P1-5: hold_days=10 + buffer
            row = hist.iloc[i]
            if pd.isna(row['Change']) or pd.isna(row['ATR_pct']): continue
            if row['ATR_pct'] < 2.0: continue
            if row['AvgTurnover'] < (1e9 if is_japan else 1e7): continue

            # 急落後リバウンド狙い
            if (row['Change'] <= -5.0 and row['VolRatio'] >= 3.0):
                pnl, exit_reason, hold = simulate_trade(hist, i, hold_days=10, is_japan=is_japan)
                trades.append({'ticker': ticker, 'pnl': pnl, 'exit': exit_reason, 'hold': hold,
                               'type': '急落後', 'date': hist.iloc[i]['Date'].strftime('%Y-%m-%d')})

    return trades

def summarize(trades, strategy_name):
    if len(trades) < 10:
        return {'strategy': strategy_name, 'trades': len(trades), 'note': '件数不足'}

    pnls = [t['pnl'] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    stops = sum(1 for t in trades if t['exit'] == 'ストップロス')
    trails = sum(1 for t in trades if t['exit'] == 'トレーリング')
    timeouts = sum(1 for t in trades if t['exit'] == 'タイムストップ')

    return {
        'strategy': strategy_name,
        'trades': len(trades),
        'win_rate': round(len(wins) / len(trades) * 100, 1),
        'avg_pnl': round(sum(pnls) / len(pnls), 2),
        'avg_win': round(sum(wins) / len(wins), 2) if wins else 0,
        'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0,
        'max_win': round(max(pnls), 2),
        'max_loss': round(min(pnls), 2),
        'profit_factor': round(abs(sum(wins) / sum(losses)), 2) if losses and sum(losses) != 0 else 999,
        'stop_rate': round(stops / len(trades) * 100, 1),
        'trail_rate': round(trails / len(trades) * 100, 1),
        'timeout_rate': round(timeouts / len(trades) * 100, 1),
        'avg_hold': round(sum(t['hold'] for t in trades) / len(trades), 1),
        'total_pnl': round(sum(pnls), 2)
    }

def print_summary(results):
    print("\n" + "=" * 65)
    print("バックテスト結果（新設計・5戦略）")
    print("=" * 65)
    for r in results:
        if r.get('note'):
            print(f"\n【{r['strategy']}】件数不足（{r['trades']}件）")
            continue
        print(f"""
【{r['strategy']}】
  トレード数:       {r['trades']}回
  勝率:             {r['win_rate']}%
  平均損益:         {r['avg_pnl']:+.2f}%
  平均利益:         {r['avg_win']:+.2f}%  /  平均損失: {r['avg_loss']:+.2f}%
  最大利益:         {r['max_win']:+.2f}%  /  最大損失: {r['max_loss']:+.2f}%
  プロフィットF:    {r['profit_factor']}
  エグジット内訳:   ストップ{r['stop_rate']}% / トレーリング{r['trail_rate']}% / タイム{r['timeout_rate']}%
  平均保有日数:     {r['avg_hold']}日
  累計損益:         {r['total_pnl']:+.2f}%""")

if __name__ == "__main__":
    print("=" * 65)
    print("ALMANAC バックテスト（新設計・過去3年）")
    print("=" * 65)

    print("\n【S&P500銘柄】")
    sp_data = load_data(SP500_TICKERS, period_years=3)

    print("\n【日経225銘柄】")
    nk_data = load_data(NIKKEI_TICKERS, period_years=3)

    print("\n各戦略をバックテスト中...")
    results = []

    print("  逆張り(US)...")
    results.append(summarize(backtest_mean_reversion(sp_data, is_japan=False), "逆張り(US)"))
    print("  逆張り(JP)...")
    results.append(summarize(backtest_mean_reversion(nk_data, is_japan=True), "逆張り(JP)"))
    print("  モメンタム(US)...")
    results.append(summarize(backtest_momentum(sp_data, is_japan=False), "モメンタム(US)"))
    print("  モメンタム(JP)...")
    results.append(summarize(backtest_momentum(nk_data, is_japan=True), "モメンタム(JP)"))
    print("  ギャップダウン(US)...")
    results.append(summarize(backtest_gap_down(sp_data, is_japan=False), "ギャップダウン(US)"))
    print("  ギャップダウン(JP)...")
    results.append(summarize(backtest_gap_down(nk_data, is_japan=True), "ギャップダウン(JP)"))
    print("  イベントドリブン後(US)...")
    results.append(summarize(backtest_event_driven_post(sp_data, is_japan=False), "イベントドリブン後(US)"))
    print("  イベントドリブン後(JP)...")
    results.append(summarize(backtest_event_driven_post(nk_data, is_japan=True), "イベントドリブン後(JP)"))

    print_summary(results)

    output_path = os.path.expanduser('~/portfolio-bot/backtest_results.json')
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\n結果保存: {output_path}")
