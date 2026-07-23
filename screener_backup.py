import yfinance as yf
import json
import os
import time
from datetime import datetime

TICKERS_FILE = os.path.expanduser('~/portfolio-bot/tickers.json')
RESULTS_FILE = os.path.expanduser('~/portfolio-bot/screen_results.json')

def load_tickers():
    with open(TICKERS_FILE) as f:
        return json.load(f)['all']

def quick_screen(ticker):
    try:
        hist = yf.Ticker(ticker).history(period="3mo")
        if hist.empty or len(hist) < 20:
            return None

        current = float(hist['Close'].iloc[-1])
        prev = float(hist['Close'].iloc[-2])

        # RSI計算
        delta = hist['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi = float(100 - (100 / (1 + gain / loss)).iloc[-1])

        # 出来高
        vol_ratio = float(hist['Volume'].iloc[-1] / hist['Volume'].iloc[-20:].mean())

        # モメンタム
        mom_1m = float((current - hist['Close'].iloc[-22]) / hist['Close'].iloc[-22] * 100) if len(hist) >= 22 else 0
        mom_3m = float((current - hist['Close'].iloc[0]) / hist['Close'].iloc[0] * 100)
        change_pct = float((current - prev) / prev * 100)

        # 52週高値からの乖離（モメンタム用）
        high_52w = float(hist['Close'].max())
        from_high = float((current - high_52w) / high_52w * 100)

        # 日本株判定
        is_japan = ticker.endswith('.T')

        strategy = None
        score = 0

        # --- 逆張り戦略（米国株：RSI<20 / 日本株：RSI<25） ---
        rsi_threshold = 25 if is_japan else 20
        if rsi < rsi_threshold:
            strategy = "逆張り"
            reason = "極度売られすぎ(JP)" if is_japan else "極度売られすぎ"
            score = (rsi_threshold - rsi) * 2
            if change_pct > 0: score += 10
            if vol_ratio > 1.2: score += 5
            if mom_3m > -40: score += 5

        elif not is_japan and rsi < 30 and vol_ratio > 0.8:
            strategy = "逆張り"
            reason = "売られすぎ"
            score = (30 - rsi) * 1.5
            if change_pct > 0: score += 8
            if vol_ratio > 1.2: score += 5

        # --- 順張り戦略（米国株：最適化済み Mom1M>12%・Mom3M>20%） ---
        elif not is_japan and rsi > 60 and mom_1m > 12 and mom_3m > 20 and vol_ratio > 1.2:
            strategy = "順張り"
            reason = "強いモメンタム(US)"
            score = mom_1m * 1.5 + mom_3m * 0.5
            if vol_ratio > 1.5: score += 10
            if rsi < 75: score += 10
            if from_high > -10: score += 15

        # --- 順張り戦略（日本株：独自基準） ---
        elif is_japan and rsi > 60 and mom_1m > 8 and mom_3m > 15 and vol_ratio > 1.1:
            strategy = "順張り"
            reason = "強いモメンタム(JP)"
            score = mom_1m * 1.2 + mom_3m * 0.4
            if vol_ratio > 1.3: score += 8
            if rsi < 80: score += 8
            if from_high > -5: score += 12

        # --- ギャップダウン戦略（最適化済み：Gap<-2%・Vol>2.0x・損切-3%） ---
        elif change_pct <= -2.0 and vol_ratio > 2.0 and 30 < rsi < 60:
            strategy = "ギャップダウン"
            reason = f"急落{change_pct:.1f}%・出来高{vol_ratio:.1f}倍"
            score = abs(change_pct) * 3 + vol_ratio * 5
            if rsi < 45: score += 10
            if mom_3m > -10: score += 10

        # --- イベントドリブン戦略（決算直後の急落） ---
        elif change_pct <= -5.0 and vol_ratio > 2.0 and rsi < 45:
            strategy = "イベントドリブン"
            reason = f"急落{change_pct:.1f}%・出来高{vol_ratio:.1f}倍"
            score = abs(change_pct) * 2 + vol_ratio * 3
            if rsi < 35: score += 15
            if mom_3m > -20: score += 10

        if not strategy:
            return None

        return {
            "ticker": ticker,
            "strategy": strategy,
            "price": round(current, 2),
            "change_pct": round(change_pct, 2),
            "rsi": round(rsi, 1),
            "volume_ratio": round(vol_ratio, 2),
            "mom_1m": round(mom_1m, 1),
            "mom_3m": round(mom_3m, 1),
            "from_high": round(from_high, 1),
            "reason": reason,
            "score": round(score, 1)
        }
    except:
        return None

def run_full_screen():
    print(f"[{datetime.now().strftime('%H:%M:%S')}] 全市場スクリーニング開始...")
    from insider_restrictions import filter_allowed_tickers
    tickers = filter_allowed_tickers(load_tickers())
    print(f"対象: {len(tickers)}銘柄")

    mean_reversion = []
    momentum = []
    gap_down = []
    event_driven = []

    for i, ticker in enumerate(tickers):
        result = quick_screen(ticker)
        if result:
            s = result['strategy']
            if s == '逆張り': mean_reversion.append(result)
            elif s == '順張り': momentum.append(result)
            elif s == 'ギャップダウン': gap_down.append(result)
            elif s == 'イベントドリブン': event_driven.append(result)
            print(f"  ✅ {ticker}: RSI {result['rsi']} | {s} | {result['reason']}")

        if (i + 1) % 50 == 0:
            print(f"  進捗: {i+1}/{len(tickers)}...")

        time.sleep(0.1)

    # スコア順にソート
    for lst in [mean_reversion, momentum, gap_down, event_driven]:
        lst.sort(key=lambda x: x['score'], reverse=True)

    # 戦略別上位選出：逆張り2・順張り1・ギャップダウン1・イベントドリブン1（計5件）
    top_mr = mean_reversion[:2]
    top_mo = momentum[:1]
    top_gd = gap_down[:1]
    top_ed = event_driven[:1]
    candidates = top_mr + top_mo + top_gd + top_ed

    print(f"\n完了: 逆張り{len(mean_reversion)}件 / 順張り{len(momentum)}件 / ギャップダウン{len(gap_down)}件 / イベントドリブン{len(event_driven)}件")
    for label, lst in [("逆張り上位2件", top_mr), ("順張り上位1件", top_mo),
                        ("ギャップダウン上位1件", top_gd), ("イベントドリブン上位1件", top_ed)]:
        if lst:
            print(f"\n【{label}】")
            for c in lst:
                print(f"  {c['ticker']}: RSI {c['rsi']} | score {c['score']} | 1M: {c['mom_1m']}% | {c['reason']}")

    output = {
        "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M'),
        "total_screened": len(tickers),
        "mean_reversion_count": len(mean_reversion),
        "momentum_count": len(momentum),
        "gap_down_count": len(gap_down),
        "event_driven_count": len(event_driven),
        "candidates": mean_reversion[:8] + momentum[:5] + gap_down[:5] + event_driven[:5]
    }
    with open(RESULTS_FILE, 'w') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    return candidates

if __name__ == "__main__":
    run_full_screen()
