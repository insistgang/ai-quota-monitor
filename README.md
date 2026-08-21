# 🤖 AI Quota Monitor（AI 额度监控）

一条命令，把这台 Mac 上所有 AI 编程工具的**本周已用额度**汇总成一张表，附带一个本地卡片式仪表盘。

**📊 线上快照（每天 9:30–23:30 每小时更新）**：https://insistgang.top/ai-quota-monitor/

线上页面分为三个页签：额度概览（含最近两天摘要）、每日消耗（7/30 天切换与账号筛选）和订阅账单。

## 支持的数据源

| 源 | 取数方式 |
|---|---|
| Kimi for Coding（双账号） | `api.kimi.com/coding/v1/usages` API 直连（key 从 KimiCodeBar 配置读取） |
| Codex（Mac） | tmux 驱动 `codex` TUI 的 `/status` 状态栏（实时）；重置时间用会话里的官方 `resets_at` |
| Codex（Win / 远程机） | `ssh` 到远程机读取最新会话的 rate_limits |
| Grok（SuperGrok） | tmux 驱动 `grok` TUI 的 `/usage` 面板截屏解析 |
| MiniMax | `mmx quota show`（官方 CLI，JSON 输出） |
| Antigravity（Gemini 组 / Claude·GPT 组） | tmux 驱动 `agy` TUI 的 `/usage` 面板截屏解析 |

## 用法

```bash
python3 quota_report.py            # 终端表格
python3 quota_report.py --json     # 机器可读输出
python3 quota_report.py --log      # 追加快照到 quota-log.csv（攒每日底账）
python3 quota_report.py --html     # 生成 quota-dashboard.html 仪表盘
python3 quota_report.py --serve    # 本地服务模式：http://127.0.0.1:8788 页面上可点按钮实时刷新
./publish.sh                       # 采集 + 生成公开版页面 + 推送 GitHub Pages（定时任务跑的就是它）
```

仪表盘长这样：每张卡 = 一个模型，大号百分比 + 进度条，按用量变色（🟢<50% / 🟡50-80% / 🟠80-95% / 🔴>95%），Kimi/Antigravity 附 5 小时窗子条。

## 定时自动跑（macOS launchd）

```bash
cp examples/com.leo.quota-report.plist ~/Library/LaunchAgents/
# 把 plist 里的 /Users/YOURNAME 改成你的路径
launchctl load ~/Library/LaunchAgents/com.leo.quota-report.plist
```

默认每天 **9:30–23:30 每小时一次**（共 15 次）。launchd 调用 `~/.local/bin/quota-publish`（不放在文稿目录，避免 macOS TCC 拦截），再进入仓库跑采集、写 CSV、刷 HTML、推公开页。电脑睡着错过会在唤醒后补跑。

查询本身**不消耗对话/生成额度**：Kimi / MiniMax 走只读用量接口；Grok / Codex / Antigravity 只开隐藏 TUI 发 `/usage`、`/status` 截屏，不向模型发任务。

## 依赖

- macOS + Python 3.10+（只用标准库）
- `tmux`（Grok / Codex-Mac / Antigravity 的 TUI 探测）
- 各工具的官方 CLI 已登录：`kimi`（经 KimiCodeBar）、`codex`、`grok`、`mmx`、`agy`
- Codex 远程机：配置好免密 ssh（示例用 `ssh desktop`，可在脚本里改）

## 隐私与安全

- **全部本机只读取数**：不改任何配置、不发消息、不消耗对话额度
- **永不打印/存储任何 key 或 token**；凭据只从各 CLI 自己的本地配置里读出来用
- `quota-log.csv`、`quota-dashboard.html` 含你的用量数据，已在 .gitignore 排除，不会误传

## 实现说明

有些工具（Grok、Codex、Antigravity）没有公开的非交互查询口，本项目的做法是**用 tmux 开一个隐藏的 TUI 会话，发送 `/usage`、`/status` 等本地命令，截屏解析文本**。不消耗任何模型额度，探测完自动杀掉临时会话。

## License

MIT
