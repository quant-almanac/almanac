import yfinance as yf
from datetime import datetime, timedelta
import json

def get_earnings_soon(ticker, days=7):
    """決算発表が直近7日以内かチェック"""
    try:
        stock = yf.Ticker(ticker)
        cal = stock.calendar
        if cal is None or cal.empty:
            return False
        earnings_date = cal.iloc[0, 0]
        if hasattr(earnings_date, 'date'):
            earnings_date = earnings_date.date()
        today = datetime.now().date()
        diff = (earnings_date - today).days
        return 0 <= diff <= days
    except Exception:
        return False

def check_event_risk(ticker):
    """イベントリスクをチェック"""
    risks = []
    
    # 決算チェック
    if get_earnings_soon(ticker, days=7):
        risks.append("⚠️ 決算発表が1週間以内")
    elif get_earnings_soon(ticker, days=14):
        risks.append("📅 決算発表が2週間以内")
    
    return risks

def filter_by_events(candidates):
    """決算直前銘柄をフィルタリング"""
    filtered = []
    excluded = []
    
    for c in candidates:
        risks = check_event_risk(c['ticker'])
        has_critical_risk = any("1週間以内" in r for r in risks)
        
        if has_critical_risk:
            excluded.append((c['ticker'], risks))
        else:
            c['event_risks'] = risks
            filtered.append(c)
    
    if excluded:
        print(f"決算直前のため除外: {[e[0] for e in excluded]}")
    
    return filtered

if __name__ == "__main__":
    # テスト
    test_tickers = ['AMZN', 'NVDA', 'AAPL', 'TMO', 'INTU']
    print("決算チェック結果:")
    for ticker in test_tickers:
        risks = check_event_risk(ticker)
        if risks:
            print(f"  {ticker}: {', '.join(risks)}")
        else:
            print(f"  {ticker}: イベントなし")
