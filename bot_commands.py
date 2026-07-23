import os
import json
import requests
import yfinance as yf
from datetime import datetime
from generate_dashboard import generate as update_dashboard
import time

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
HOLDINGS_FILE = os.path.expanduser('~/portfolio-bot/holdings.json')

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    })

def load_holdings():
    if not os.path.exists(HOLDINGS_FILE):
        return {}
    with open(HOLDINGS_FILE) as f:
        return json.load(f)

def save_holdings(holdings):
    with open(HOLDINGS_FILE, 'w') as f:
        json.dump(holdings, f, indent=2)

def get_account_info():
    """口座情報を読み込む"""
    filepath = os.path.expanduser('~/portfolio-bot/account.json')
    if not os.path.exists(filepath):
        return {"balance": 0, "risk_per_trade": 0.1}
    with open(filepath) as f:
        return json.load(f)

def save_account_info(info):
    filepath = os.path.expanduser('~/portfolio-bot/account.json')
    with open(filepath, 'w') as f:
        json.dump(info, f, indent=2)

def cmd_setbalance(parts):
    """/setbalance 3000000 → 口座残高を設定"""
    if len(parts) < 2:
        return "使い方: /setbalance 金額\n例: /setbalance 3000000"
    try:
        balance = float(parts[1].replace(',', ''))
    except:
        return "金額は数字で入力してください"
    
    info = get_account_info()
    info['balance'] = balance
    save_account_info(info)
    return f"✅ 口座残高を設定しました\n💴 残高: ¥{balance:,.0f}\n📊 1トレード上限: ¥{balance * info['risk_per_trade']:,.0f}（残高の{info['risk_per_trade']*100:.0f}%）"

def cmd_setrisk(parts):
    """/setrisk 10 → 1トレードのリスク割合を設定（%）"""
    if len(parts) < 2:
        return "使い方: /setrisk パーセント\n例: /setrisk 10"
    try:
        risk = float(parts[1]) / 100
        if risk <= 0 or risk > 0.5:
            return "リスクは1〜50%の間で設定してください"
    except:
        return "数字で入力してください"
    
    info = get_account_info()
    info['risk_per_trade'] = risk
    save_account_info(info)
    return f"✅ 1トレードのリスクを設定しました\n📊 リスク: {risk*100:.0f}%\n💴 1トレード上限: ¥{info['balance'] * risk:,.0f}"

def calc_position_size(price_jpy, account_info):
    """推奨株数を計算"""
    balance = account_info.get('balance', 0)
    risk_ratio = account_info.get('risk_per_trade', 0.1)
    if balance == 0 or price_jpy == 0:
        return None
    max_amount = balance * risk_ratio
    shares = int(max_amount / price_jpy)
    return max(1, shares)

def record_trade(action, ticker, price, shares, pnl_pct=None, pnl_amount=None):
    """売買履歴をCSVに記録"""
    import csv
    filepath = os.path.expanduser('~/portfolio-bot/trade_history.csv')
    file_exists = os.path.exists(filepath)
    
    with open(filepath, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['日時', 'アクション', 'ティッカー', '価格', '株数', '損益%', '損益額'])
        writer.writerow([
            datetime.now().strftime('%Y-%m-%d %H:%M'),
            action, ticker, price, shares,
            f'{pnl_pct:.1f}%' if pnl_pct is not None else '',
            f'${pnl_amount:+,.0f}' if pnl_amount is not None else ''
        ])

def cmd_buy(parts):
    """例: /buy AMZN 204.79 5"""
    if len(parts) < 4:
        return "使い方: /buy ティッカー 価格 株数\n例: /buy AMZN 204.79 5"
    ticker = parts[1].upper()
    try:
        entry_price = float(parts[2])
        shares = float(parts[3])
    except:
        return "価格と株数は数字で入力してください"
    
    holdings = load_holdings()
    # シグナルログから詳細情報を引き継ぎ
    signal_info = {}
    try:
        log_path = os.path.expanduser('~/portfolio-bot/signals_log.json')
        if os.path.exists(log_path):
            with open(log_path) as f:
                logs = json.load(f)
            signal_info = logs.get(ticker, {})
    except:
        pass

    holdings[ticker] = {
        "entry_price": entry_price,
        "shares": shares,
        "entry_date": datetime.now().strftime('%Y-%m-%d'),
        "target_price": signal_info.get('target_price', '-'),
        "stop_loss": signal_info.get('stop_loss', '-'),
        "reason": signal_info.get('reason', ''),
        "holding_period": signal_info.get('holding_period', '-'),
        "score": signal_info.get('score', '-')
    }
    save_holdings(holdings)
    record_trade("BUY", ticker, entry_price, shares)
    try:
        update_dashboard()
    except:
        pass
    
    total = entry_price * shares
    return f"✅ <b>{ticker} 登録完了</b>\n💰 エントリー: ${entry_price} × {shares}株\n💴 合計: ${total:,.0f}"

def cmd_sell(parts):
    """例: /sell AMZN"""
    if len(parts) < 2:
        return "使い方: /sell ティッカー\n例: /sell AMZN"
    ticker = parts[1].upper()
    holdings = load_holdings()
    
    if ticker not in holdings:
        return f"❌ {ticker} は保有リストにありません"
    
    entry = holdings[ticker]['entry_price']
    shares = holdings[ticker]['shares']
    
    try:
        current = yf.Ticker(ticker).fast_info['lastPrice']
        pnl_pct = (current - entry) / entry * 100
        pnl_amount = (current - entry) * shares
        emoji = "📈" if pnl_pct >= 0 else "📉"
    except:
        current = 0
        pnl_pct = 0
        pnl_amount = 0
        emoji = "📊"
    
    del holdings[ticker]
    save_holdings(holdings)
    record_trade("SELL", ticker, current, shares, pnl_pct, pnl_amount)
    try:
        update_dashboard()
    except:
        pass
    
    return f"""✅ <b>{ticker} 売却・削除完了</b>
{emoji} 損益: {pnl_pct:+.1f}% (${pnl_amount:+,.0f})
💰 エントリー: ${entry} → 現在値: ${current:.2f}"""

def cmd_holdings():
    """/holdings：保有一覧"""
    holdings = load_holdings()
    if not holdings:
        return "📋 現在の保有銘柄なし"
    
    lines = ["📋 <b>現在の保有銘柄</b>\n━━━━━━━━━━━━━━"]
    total_pnl = 0
    
    for ticker, info in holdings.items():
        try:
            current = yf.Ticker(ticker).fast_info['lastPrice']
            pnl_pct = (current - info['entry_price']) / info['entry_price'] * 100
            pnl_amount = (current - info['entry_price']) * info['shares']
            total_pnl += pnl_amount
            emoji = "🟢" if pnl_pct >= 0 else "🔴"
            lines.append(f"{emoji} <b>{ticker}</b> {pnl_pct:+.1f}%\n  ${info['entry_price']} → ${current:.2f} ({info['shares']}株)")
        except:
            lines.append(f"⚪ <b>{ticker}</b> (データ取得エラー)")
    
    lines.append(f"━━━━━━━━━━━━━━")
    lines.append(f"💴 合計損益: ${total_pnl:+,.0f}")
    return "\n".join(lines)

def cmd_analyze(parts):
    """/analyze NVDA → 任意銘柄を即時分析"""
    if len(parts) < 2:
        return "使い方: /analyze ティッカー\n例: /analyze NVDA"
    
    ticker = parts[1].upper()
    # ALMANAC: telegram disabled — ai_analysis only
    # send_telegram(f"🔍 {ticker} を分析中... 2〜3分お待ちください")
    
    try:
        import sys
        sys.path.insert(0, os.path.expanduser('~/portfolio-bot'))
        from analyzer import get_stock_data, analyze_with_agents, get_macro_score, format_signal_message
        
        stock = get_stock_data(ticker)
        if not stock:
            return f"❌ {ticker} のデータを取得できませんでした"
        
        macro = get_macro_score()
        judgment = analyze_with_agents(stock, macro)
        
        if not judgment:
            return f"❌ {ticker} の分析に失敗しました"
        
        return format_signal_message(stock, judgment, macro)
    except Exception as e:
        return f"❌ エラー: {e}"

def cmd_help():
    return """🤖 <b>使えるコマンド</b>

/buy ティッカー 価格 株数
  例: /buy AMZN 204.79 5

/sell ティッカー
  例: /sell AMZN

/holdings
  保有銘柄一覧と損益確認

/analyze ティッカー
  例: /analyze NVDA
  任意の銘柄を即時分析

/setbalance 金額
  例: /setbalance 3000000
  マネックス口座の残高を設定

/setrisk パーセント
  例: /setrisk 10
  1トレードの最大投資割合を設定

/help
  このメッセージを表示"""

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"timeout": 30}
    if offset:
        params["offset"] = offset
    res = requests.get(url, params=params)
    return res.json()

def main():
    print("コマンド受付開始...")
    # ALMANAC: telegram disabled — ai_analysis only
    # send_telegram("🤖 コマンドBot起動\n\n" + cmd_help())
    
    offset = None
    while True:
        try:
            updates = get_updates(offset)
            for update in updates.get("result", []):
                offset = update["update_id"] + 1
                
                msg = update.get("message", {})
                text = msg.get("text", "")
                chat_id = str(msg.get("chat", {}).get("id", ""))
                
                # 自分のチャットIDのみ受け付ける
                if chat_id != TELEGRAM_CHAT_ID:
                    continue
                
                parts = text.strip().split()
                if not parts:
                    continue
                
                cmd = parts[0].lower()
                if cmd == "/buy":
                    reply = cmd_buy(parts)
                elif cmd == "/sell":
                    reply = cmd_sell(parts)
                elif cmd == "/holdings":
                    reply = cmd_holdings()
                elif cmd == "/analyze":
                    reply = cmd_analyze(parts)
                elif cmd == "/setbalance":
                    reply = cmd_setbalance(parts)
                elif cmd == "/setrisk":
                    reply = cmd_setrisk(parts)
                elif cmd == "/help":
                    reply = cmd_help()
                else:
                    continue
                
                # ALMANAC: telegram disabled — ai_analysis only
                # send_telegram(reply)
        except Exception as e:
            print(f"エラー: {e}")
            time.sleep(5)

if __name__ == "__main__":
    main()
