#!/bin/bash
# フロントエンドをビルドしてLaunchAgentを再起動するスクリプト
# Usage: ./rebuild_frontend.sh

cd "$(dirname "$0")/frontend"

echo "=== フロントエンド再ビルド ==="
npm run build

if [ $? -eq 0 ]; then
    echo "✅ ビルド完了"
    echo "🔄 LaunchAgent再起動中..."
    launchctl stop com.almanac.nextjs 2>/dev/null
    sleep 2
    launchctl start com.almanac.nextjs 2>/dev/null
    echo "✅ Next.js 再起動完了"
else
    echo "❌ ビルド失敗"
    exit 1
fi
