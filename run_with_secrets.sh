#!/bin/sh
# ALMANAC ラッパー: secrets をロードして実行
# LaunchAgent / crontab のどちらからも使用可能
#
# Usage: run_with_secrets.sh <command> [args...]
# Example: run_with_secrets.sh /path/to/python portfolio_analyst.py --force

set -a
if [ -f "$HOME/.almanac_secrets" ]; then
  . "$HOME/.almanac_secrets"
elif [ -f "$HOME/.nexustrader_secrets" ]; then
  . "$HOME/.nexustrader_secrets"
else
  set +a
  echo "run_with_secrets.sh: missing ~/.almanac_secrets (legacy fallback ~/.nexustrader_secrets also missing)" >&2
  exit 1
fi
set +a

# launchd/cron の子プロセスは maxfiles=256 で起動する (対話シェルの
# 1048576 とは桁が違う)。681銘柄の並列ダウンロードや LLM の並列呼び出しは
# 容易にこれを使い切り、2026-08-05 には次の実害が出ていた:
#   - short_screener が候補算出後に short_candidates.json を書けず、
#     しかも _save_candidates が例外を握り潰すため無症状で 5 日間 stale
#   - 朝の分析で beliefs/agent_beliefs.json 更新が OSError(24) で失敗
#   - yfinance の一括DLが "Too many open files" で部分的に欠損
# ハードリミットは unlimited なので、ソフトリミットだけ引き上げる。
# ulimit が拒否された環境でも実行自体は継続させる (|| true)。
ulimit -n 4096 2>/dev/null || true

exec "$@"
