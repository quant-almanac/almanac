#!/bin/bash
# ALMANAC 再起動スクリプト
# Usage: ./restart_v5.sh

cd "$(dirname "$0")"

echo "=== ALMANAC 再起動 ==="
echo ""

# [1/3] FastAPI (uvicorn) を停止
echo "[1/3] FastAPI 停止中..."
pkill -f "uvicorn api.main:app" 2>/dev/null
sleep 1
echo "      完了"

# [2/3] Next.js LaunchAgent を再起動
echo "[2/3] Next.js 再起動中..."
launchctl stop com.almanac.nextjs 2>/dev/null
sleep 1
launchctl start com.almanac.nextjs 2>/dev/null
echo "      完了"

# [3/3] FastAPI を再起動
echo "[3/3] FastAPI 起動中..."
sleep 1
exec bash "$(dirname "$0")/start_v5.sh"
