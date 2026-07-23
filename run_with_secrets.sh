#!/bin/sh
# ALMANAC ラッパー: secrets をロードして実行
# LaunchAgent / crontab のどちらからも使用可能
#
# Usage: run_with_secrets.sh <command> [args...]
# Example: run_with_secrets.sh /path/to/python portfolio_analyst.py --force

if [ -f "$HOME/.almanac_secrets" ]; then
  . "$HOME/.almanac_secrets"
elif [ -f "$HOME/.nexustrader_secrets" ]; then
  . "$HOME/.nexustrader_secrets"
else
  echo "run_with_secrets.sh: missing ~/.almanac_secrets (legacy fallback ~/.nexustrader_secrets also missing)" >&2
  exit 1
fi

exec "$@"
