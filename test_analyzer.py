import os
import anthropic
import yfinance as yf
import requests
import json
import re
import sys

if "pytest" in sys.modules:
    import pytest
    pytest.skip("manual Anthropic/Telegram integration script", allow_module_level=True)

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
client = anthropic.Anthropic()

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    })

def get_stock_data(ticker):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="3mo")
        if hist.empty:
            return None
        current_price = hist['Close'].iloc[-1]
        prev_price = hist['Close'].iloc[-2]
        change_pct = (current_price - prev_price) / prev_price * 100
        delta = hist['Close'].diff()
        gain = delta.where(delta > 0, 0).rolling(14).mean()
        loss = -delta.where(delta < 0, 0).rolling(14).mean()
        rsi = 100 - (100 / (1 + gain / loss)).iloc[-1]
        avg_volume = hist['Volume'].iloc[-20:].mean()
        volume_ratio = hist['Volume'].iloc[-1] / avg_volume
        mom_1m = (current_price - hist['Close'].iloc[-22]) / hist['Close'].iloc[-22] * 100
        return {
            "ticker": ticker,
            "price": round(current_price, 2),
            "change_pct": round(change_pct, 2),
            "rsi": round(rsi, 1),
            "mom_1m": round(mom_1m, 1),
            "volume_ratio": round(volume_ratio, 2)
        }
    except Exception as e:
        print(f"{ticker} エラー: {e}")
        return None

# NVDAだけで強制テスト
print("NVDAのデータ取得中...")
stock = get_stock_data("NVDA")
print(f"データ: {stock}")

if stock:
    print("マルチエージェント分析開始...")
    context = f"銘柄: {stock['ticker']}, 現在値: ${stock['price']}, RSI: {stock['rsi']}, 前日比: {stock['change_pct']}%"

    bull = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="強気派アナリストとして、この銘柄を買うべき理由を3つ簡潔に述べてください。",
        messages=[{"role": "user", "content": context}]
    ).content[0].text

    bear = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="慎重派アナリストとして、この銘柄を買ってはいけない理由を3つ簡潔に述べてください。",
        messages=[{"role": "user", "content": context}]
    ).content[0].text

    skeptic = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=300,
        system="リスク管理の専門家として、最悪のシナリオを簡潔に述べてください。",
        messages=[{"role": "user", "content": f"{context}\n強気派:{bull}\n慎重派:{bear}"}]
    ).content[0].text

    final = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=600,
        system="""ヘッジファンドマネージャーとして最終判断をJSON形式で返してください：
{"signal": "買い" or "様子見", "score": 1-5, "entry_price": 数値, "target_price": 数値, "stop_loss": 数値, "reason": "理由", "holding_period": "期間"}""",
        messages=[{"role": "user", "content": f"{context}\n強気派:{bull}\n慎重派:{bear}\nリスク派:{skeptic}"}]
    ).content[0].text

    print(f"Opus判断: {final}")

    json_match = re.search(r'\{.*\}', final, re.DOTALL)
    if json_match:
        j = json.loads(json_match.group())
        signal_emoji = "🟢" if j['signal'] == "買い" else "🟡"
        msg = f"""
{signal_emoji} <b>NVDA テスト分析</b>
━━━━━━━━━━━━━━
💰 現在値: ${stock['price']} ({stock['change_pct']:+.1f}%)
📊 RSI: {stock['rsi']}

🎯 エントリー: ${j.get('entry_price', '-')}
📈 目標株価: ${j.get('target_price', '-')}
🛑 損切り: ${j.get('stop_loss', '-')}
⏱ 保有期間: {j.get('holding_period', '-')}

💡 {j.get('reason', '-')}
⭐ 信頼度: {j.get('score', 0)}/5
"""
        send_telegram(msg)
        print("Telegram送信完了")
