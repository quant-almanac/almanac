# analyzer.pyのscreen_candidates関数を修正版に置き換え

SCREEN_CODE = '''
def screen_candidates(stocks):
    """一次フィルタリング：市場環境に合わせた条件"""
    candidates = []
    for data in stocks:
        if data is None:
            continue
        
        # 条件1：極端な売られすぎ（RSI25以下）→ 出来高条件を緩める
        if data['rsi'] < 25:
            candidates.append((data, "極度売られすぎ"))
        
        # 条件2：RSI35以下 かつ 出来高1.0倍以上
        elif data['rsi'] < 35 and data['volume_ratio'] > 1.0:
            candidates.append((data, "売られすぎ"))
        
        # 条件3：モメンタム強い かつ 出来高1.1倍以上
        elif abs(data['mom_1m']) > 8 and data['volume_ratio'] > 1.1:
            candidates.append((data, "モメンタム"))
    
    # RSIが低い順に上位5件
    candidates.sort(key=lambda x: x[0]['rsi'])
    return [c[0] for c in candidates[:5]]
'''

print("現在の市場でスクリーニング結果をプレビュー:")
import yfinance as yf

stocks_data = []
for ticker in ['NVDA', 'AMD', 'AVGO', 'META', 'MSFT', 'AAPL', 'TSLA', 'AMZN', 'QQQ', 'SPY', 'SOXL', 'TQQQ']:
    hist = yf.Ticker(ticker).history(period='3mo')
    if hist.empty: continue
    delta = hist['Close'].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rsi = round(100 - (100 / (1 + gain / loss)).iloc[-1], 1)
    vol_ratio = round(hist['Volume'].iloc[-1] / hist['Volume'].iloc[-20:].mean(), 2)
    mom_1m = round((hist['Close'].iloc[-1] - hist['Close'].iloc[-22]) / hist['Close'].iloc[-22] * 100, 1)
    stocks_data.append({"ticker": ticker, "price": round(hist['Close'].iloc[-1], 2),
                        "change_pct": round((hist['Close'].iloc[-1]-hist['Close'].iloc[-2])/hist['Close'].iloc[-2]*100,2),
                        "rsi": rsi, "mom_1m": mom_1m, "volume_ratio": vol_ratio})

candidates = []
for data in stocks_data:
    if data['rsi'] < 25:
        candidates.append((data, "極度売られすぎ"))
    elif data['rsi'] < 35 and data['volume_ratio'] > 1.0:
        candidates.append((data, "売られすぎ"))
    elif abs(data['mom_1m']) > 8 and data['volume_ratio'] > 1.1:
        candidates.append((data, "モメンタム"))

candidates.sort(key=lambda x: x[0]['rsi'])

print(f"\n候補銘柄: {len(candidates)}件")
for c, reason in candidates[:5]:
    print(f"  {c['ticker']}: RSI {c['rsi']} | 出来高比 {c['volume_ratio']} | 理由: {reason}")
