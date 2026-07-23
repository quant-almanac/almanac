#!/bin/zsh
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
source_dir="$repo_dir/launchagents"
target_dir="$HOME/Library/LaunchAgents"

mkdir -p "$target_dir"
for plist in "$source_dir"/*.plist; do
  plutil -lint "$plist"
  cp "$plist" "$target_dir/"
  echo "installed (not loaded): $target_dir/${plist:t}"
done

cat <<'EOF'

No agents were loaded. Review the installed files, then run the explicit
launchctl bootstrap commands in docs/runbook_user_actions_2026_06.md.
EOF
