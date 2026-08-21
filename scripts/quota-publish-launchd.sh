#!/bin/bash
# launchd 入口必须安装到 Documents 之外；Python 可读取仓库源码，而 launchd
# 直接让 /bin/bash 打开 Documents 内的 .sh 会被 macOS TCC 拒绝。
set -euo pipefail

TASK_USER_HOME="${HOME:?HOME is required}"
REPO="${QUOTA_MONITOR_HOME:-$TASK_USER_HOME/Documents/ai-quota-monitor}"
PYTHON_BIN="${QUOTA_PYTHON_BIN:-}"

if [[ -z "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" || ! -x "$PYTHON_BIN" ]]; then
  printf '%s\n' "quota monitor python3 executable missing" >&2
  exit 1
fi
if [[ ! -f "$REPO/publish_runtime.py" ]]; then
  printf '%s\n' "quota monitor runtime missing: $REPO/publish_runtime.py" >&2
  exit 1
fi

exec "$PYTHON_BIN" "$REPO/publish_runtime.py"
