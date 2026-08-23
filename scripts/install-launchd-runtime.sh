#!/bin/bash
# 将版本化 launchd 入口和日志放到 Documents 之外，避免 macOS TCC 拒绝启动。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOURCE="$SCRIPT_DIR/quota-publish-launchd.sh"
TARGET_DIR="${QUOTA_RUNTIME_BIN_DIR:-${HOME:?}/.local/bin}"
TARGET="$TARGET_DIR/quota-publish"
LOG_DIR="${HOME:?}/Library/Logs/ai-quota-monitor"

if [[ ! -f "$SOURCE" ]]; then
  printf '%s\n' "launchd entrypoint source missing: $SOURCE" >&2
  exit 1
fi

mkdir -p "$TARGET_DIR" "$LOG_DIR"
install -m 0755 "$SOURCE" "$TARGET"
printf '%s\n' "installed: $TARGET"
printf '%s\n' "launchd logs: $LOG_DIR"
