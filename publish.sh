#!/bin/bash
# publish.sh — 每日采集额度 → 本地快照/底账 → 生成公开版页面 → 推送 GitHub Pages
set -u
NVM_BIN="$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | tail -1)"
export PATH="$HOME/.grok/bin:$HOME/.local/bin:${NVM_BIN}:/opt/homebrew/bin:/usr/bin:/bin"
export TERM=xterm-256color
cd "$(dirname "$0")"

python3 quota_report.py --log --html --public-html docs/index.html || exit 1

git add docs/index.html
if ! git diff --cached --quiet; then
  git commit -q -m "chore: 每日额度快照 $(date +%F_%H%M)"
  GIT_TERMINAL_PROMPT=0 git push https://github.com/insistgang/ai-quota-monitor.git main
fi
