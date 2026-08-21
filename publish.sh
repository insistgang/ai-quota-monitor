#!/bin/bash
# publish.sh — 每日采集额度 → 本地快照/底账 → 生成公开版页面 → 推送 GitHub Pages
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${QUOTA_PYTHON_BIN:-python3}"

exec "$PYTHON_BIN" "$SCRIPT_DIR/publish_runtime.py"
