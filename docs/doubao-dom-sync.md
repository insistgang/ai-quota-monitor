# 豆包官网 DOM 额度接入

## 采用的方法

数据源固定为豆包官网的“订阅与额度管理”页：

`https://www.doubao.com/member/quota-management`

采集器只读取页面中用户本来就能看到的文本，并提取：

- 套餐名称与免费体验截止日；
- “当前时段”的已用百分比和重置时间；
- “近 7 天”的已用百分比和重置时间。

套餐名称只接受“订阅与额度管理”标题紧邻的套餐字段；账号昵称等页面文本不会作为套餐名称缓存或发布。百分比必须位于 0–100，重置时间也必须是有效日期和时间，否则整次同步失败并回退到已有缓存。

它不进入豆包客户端进程，不读取 Cookie、localStorage、登录 Token 或 Chrome 配置，也不点击和导航。原始 DOM 不落盘；缓存文件只含归一化字段，位于：

`~/.cache/ai-quota-monitor/doubao-quota.json`

文件权限固定为 `0600`。快照超过 6 小时会在仪表盘显示“快照过期”，并停止用这份旧数据产生刷新提醒；可通过 `DOUBAO_QUOTA_MAX_AGE_HOURS` 调整阈值。

## Chrome 自动同步

1. 在 Chrome 登录豆包，并保持额度管理页打开（可以固定标签页）。
2. 在 Chrome 菜单“视图 → 开发者”中开启“允许来自 Apple 事件的 JavaScript”。
3. 首次执行时，若 macOS 询问是否允许终端或自动任务控制 Chrome，请允许。
4. 运行：

```bash
python3 quota_report.py --sync-doubao-chrome
```

成功后会输出已归一化的 JSON，并更新本地缓存。普通的 `python3 quota_report.py`、`--json`、`--html` 和 `publish.sh` 都会把豆包作为一个额度源；采集时会优先刷新 Chrome DOM，失败则使用缓存并显示新鲜度。

“允许来自 Apple 事件的 JavaScript”是 Chrome 的全局开发者选项，并非只对本脚本生效。只应给可信的本地自动化程序授予 Chrome 控制权限；不再使用自动同步时可以关闭该选项。

## Codex 或其他 Agent 接入

浏览器 Agent 只要能读取该页的可见文本，就不依赖 Codex 专用能力。把 `document.body.innerText` 的结果交给以下入口即可：

```bash
python3 quota_report.py --import-doubao-dom -
```

命令从标准输入接收文本，解析成功后只写入最小快照。这条入口适合 Codex、其他带浏览器扩展的 Agent，或人工从开发者工具复制可见文本。Agent 不应导出 Cookie、存储区或网络鉴权头。

## 故障提示

- `Chrome 未运行`：打开 Chrome 后重试；脚本不会自行启动浏览器。
- `Chrome 中未打开豆包额度管理页`：在已登录的 Chrome 打开上面的固定 URL。
- `Chrome 未允许来自 Apple 事件的 JavaScript`：启用 Chrome 的对应开发者菜单项。
- `macOS 未授权自动化控制 Chrome`：在“系统设置 → 隐私与安全性 → 自动化”中允许当前运行环境控制 Chrome。
- 页面结构变化：导入会明确失败，不会把不完整文本当成有效额度。
