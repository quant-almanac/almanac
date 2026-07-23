import os
import requests
import anthropic

# Telegram通知関数
def send_telegram(message):
    token = os.environ['TELEGRAM_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(url, data={"chat_id": chat_id, "text": message, "parse_mode": "HTML"})

# Claude APIテスト
client = anthropic.Anthropic()
response = client.messages.create(
    model="claude-sonnet-4-6",
    max_tokens=200,
    messages=[{"role": "user", "content": "資産管理Botのテストです。一言挨拶してください。"}]
)

message = response.content[0].text
send_telegram(f"🤖 Bot起動テスト成功！\n\n{message}")
print("Telegram通知を送信しました")
