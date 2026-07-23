#!/bin/zsh
# ALMANAC v5.0 起動スクリプト（launchd 管理プロセスとの競合回避）
# alert.py / telegram_bot.py は launchd (KeepAlive) で管理されているため
# このスクリプトからは起動しない
cd ~/portfolio-bot

echo "=== ALMANAC v5.0 起動 ==="
echo "$(date)"

# v4.0 の旧プロセスが残っていれば停止（launchd管理分は対象外）
pkill -f "bot_commands.py" 2>/dev/null && echo "bot_commands.py: 旧プロセス停止"
pkill -f "streamlit run" 2>/dev/null && echo "streamlit: 旧プロセス停止"

sleep 1

echo ""
echo "=== 稼働確認 ==="
ps aux | grep -E "alert\.py|telegram_bot\.py|uvicorn|next-server" | grep -v grep | awk '{print $11, $12, "PID:"$2}'
echo ""
echo "ダッシュボード: http://localhost:3000 (Next.js)"
echo "API:          http://localhost:8000 (FastAPI)"
echo "FastAPI手動起動: ./start_v5.sh"
echo "Next.js再起動:   launchctl kickstart -k gui/$(id -u)/com.almanac.nextjs"
echo "=== 起動完了 ==="
