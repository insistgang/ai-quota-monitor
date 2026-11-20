#!/bin/sh
# 触发额度采集并把结果打印到终端。
#   quota-check              只查一次并打印结果（不写底账、不改网页、不推送）
#   quota-check --publish    完整跑一遍：写底账 + 刷新三个公开页 + 提交推送
#   quota-check publish      同上（两种写法都接受）
set -eu

REPO_DIR="${QUOTA_MONITOR_HOME:-$HOME/Desktop/个人工具/ai-quota-monitor}"
PYTHON_BIN="${QUOTA_PYTHON_BIN:-python3}"
MODE="check"

for arg in "$@"; do
  case "$arg" in
    --publish|publish) MODE="publish" ;;
    --check|check|"") ;;
    *) printf '未知参数：%s（可用：--publish）\n' "$arg" >&2; exit 2 ;;
  esac
done

if [ ! -x "$PYTHON_BIN" ] && ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  printf '找不到 python 解释器：%s\n' "$PYTHON_BIN" >&2
  exit 2
fi

if [ ! -f "$REPO_DIR/quota_report.py" ]; then
  printf '找不到额度监控仓库：%s\n' "$REPO_DIR" >&2
  exit 2
fi

cd "$REPO_DIR"

if [ "$MODE" = "publish" ]; then
  # 走项目唯一的发布入口：采集 → 写底账 → 生成三页 → 提交 → 推送
  exec "$PYTHON_BIN" "$REPO_DIR/publish_runtime.py"
fi

# 只查：采集一次并打印终端表格，不写底账、不改公开页
exec "$PYTHON_BIN" "$REPO_DIR/quota_report.py" --retry-failures 2 --retry-delay 15
