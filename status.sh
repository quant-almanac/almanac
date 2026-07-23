#!/bin/zsh
# ALMANAC v5.0 稼働状態確認

cd ~/portfolio-bot
echo "=== ALMANAC v5.0 状態確認 ==="
echo "$(date)"
echo ""

# プロセス確認
echo "【稼働プロセス】"
for name in "uvicorn api.main:app" "next-server" "telegram_bot.py" "alert.py"; do
  pid=$(pgrep -f "$name" | head -1)
  if [ -n "$pid" ]; then
    echo "  ✅ $name (PID: $pid)"
  else
    echo "  ❌ $name: 停止中"
  fi
done

echo ""
echo "【crontab 環境変数】"
crontab -l | grep -E "^(ANTHROPIC|TELEGRAM)" | sed 's/=.*/=***/' || echo "  未設定"

echo ""
echo "【最新ログ（各5行）】"
echo "--- analyzer (最終実行) ---"
tail -5 log.txt 2>/dev/null || echo "  ログなし"
echo "--- data_fetcher ---"
tail -3 data_fetcher_log.txt 2>/dev/null || echo "  ログなし"
echo "--- FastAPI ---"
tail -3 fastapi_log.txt 2>/dev/null || echo "  ログなし"
echo "--- Next.js ---"
tail -3 logs/nextjs.log 2>/dev/null || echo "  ログなし"

echo ""
echo "【データファイル 更新日時】"
for f in holdings.json account.json ai_portfolio_analysis.json regime_state.json screen_results.json long_term_screen_results.json guard_state.json; do
  if [ -f "$f" ]; then
    echo "  $f: $(stat -f '%Sm' -t '%m/%d %H:%M' $f)"
  else
    echo "  $f: なし"
  fi
done

echo ""
echo "ダッシュボード: http://localhost:3000"
echo "API:          http://localhost:8000"
