#!/bin/bash
# ALMANAC v5.0 起動スクリプト
# Usage: ./start_v5.sh
# Note: Next.js は LaunchAgent (com.almanac.nextjs) が管理するため
#       このスクリプトは FastAPI のみ起動する

cd "$(dirname "$0")"

mkdir -p logs

echo "=== ALMANAC v5.0 ==="
echo ""

# P0-3: API キー読込（書き込み系エンドポイントの認証に使用）
if [ -f "$HOME/.config/almanac/api_key" ]; then
    export ALMANAC_API_KEY="$(cat "$HOME/.config/almanac/api_key")"
    echo "[AUTH] ALMANAC_API_KEY loaded from ~/.config/almanac/api_key"
else
    echo "[AUTH] WARN: ~/.config/almanac/api_key missing. Generate via: mkdir -p ~/.config/almanac && python -c 'import secrets; print(secrets.token_urlsafe(32))' > ~/.config/almanac/api_key && chmod 600 ~/.config/almanac/api_key"
fi
# P0-2: ALLOW_UNAUTH の自動有効化を廃止。
# 認証バイパスが必要な場合は明示的に `ALLOW_UNAUTH=1 ./start_v5.sh` で起動すること。
if [ "${ALLOW_UNAUTH:-0}" = "1" ]; then
    echo "[AUTH] ⚠️  ALLOW_UNAUTH=1（明示指定）— 認証スキップ中、トラブルシュート用途のみで使用すること"
fi

# FastAPI (Background) — 127.0.0.1 バインド（LAN 全開を防止）
echo "[1/2] FastAPI バックエンド起動中... (http://127.0.0.1:8000)"
source venv/bin/activate
ulimit -n 65536  # FDリーク防止（--reloadのkqueueウォッチャー対策）
uvicorn api.main:app --reload --reload-dir api/ --host 127.0.0.1 --port 8000 &
FASTAPI_PID=$!
echo "      PID: $FASTAPI_PID"

sleep 2

# Next.js は LaunchAgent が管理（next start / 本番モード）
# 手動でも起動したい場合は: cd frontend && npm run start
echo "[2/2] Next.js は LaunchAgent が管理 (http://localhost:3000)"
echo "      launchctl list com.almanac.nextjs で状態確認"

echo ""
echo "✅ 起動完了"
echo "   FastAPI: http://localhost:8000"
echo "   Next.js: http://localhost:3000 (LaunchAgent管理)"
echo ""
echo "停止: kill $FASTAPI_PID"
echo "(または Ctrl+C で終了)"

# Wait
trap "kill $FASTAPI_PID 2>/dev/null; exit 0" INT TERM
wait $FASTAPI_PID
