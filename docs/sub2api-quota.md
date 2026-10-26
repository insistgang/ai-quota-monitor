# Sub2API 额度接入

监控器从已有 Sub2API 部署取得账号的供应商额度，保留看板名称、CSV 历史、订阅账单和公开账号过滤。网关 Key 的余额、请求费用与供应商套餐额度各自独立；`/v1/usage` 的账单统计不参与套餐百分比计算。

## 数据流与权限

本机采集器通过已有 SSH 登录，把 `scripts/sub2api_quota_snapshot.py` 作为一次性 Python 脚本发送到服务器执行。服务器需有 Python 3、Docker，以及读取 `sub2api-db` 的管理权限；应用接口为同机 `127.0.0.1:8080`。无需额外服务、公开端口或定时任务。

SQL 连接强制只读。脚本读取数据库持久化的 JWT 签名密钥和管理员身份摘要，在服务器进程内生成 180 秒有效的认证。管理员认证、模型凭据及密码摘要不写入输出，不复制到 Mac。请求限制为固定 GET 路径、固定官方用量地址，不接受任意 URL；不跟随跳转，并保持 HTTPS 证书验证。

Kimi、MiniMax、Grok、Antigravity 使用 Sub2API 账号查询接口。这些查询可能由 Sub2API 更新自身额度缓存或维护 Grok OAuth 凭据，账号刷新仍由 Sub2API 管理。

OpenAI 单独在服务器读取 Sub2API 当前账号的访问令牌，向官方 `/backend-api/wham/usage` 发一次 GET。这样可避开管理面板查询可能通知自动重置、通用 usage 可能发送模型探针的路径。监控器不刷新 OpenAI 令牌，也不调用额度重置、重置券消费或模型生成接口。当前部署使用 RN 直连出口；此读取路径不复制本机代理或 OAuth 缓存。

## 私有配置

配置默认位于 `~/.cache/ai-quota-monitor/sub2api.json`，权限应为 `0600`。文件只保存 SSH 别名、账号 ID、平台和原看板名称，不保存密钥。可用 `QUOTA_SUB2API_CONFIG` 指向其他绝对路径。

以下仅为结构示例，账号 ID、SSH 别名和名称需按实际部署填写：

```json
{
  "version": 1,
  "enabled": true,
  "ssh_host": "YOUR_SERVER",
  "accounts": [
    {"id": 101, "platform": "openai", "collector": "codex_auth", "name": "Codex · account@example.invalid"},
    {"id": 102, "platform": "minimax", "collector": "minimax", "name": "MiniMax"}
  ],
  "unmapped_accounts": []
}
```

支持的 collector 为 `codex_auth`、`kimi_mine`、`kimi_andy`、`grok`、`minimax`、`antigravity`。同一个 Codex collector 可对应多个账号。实际接入时，应先核对服务器账号身份，再复用现有 CSV 中的完整名称；历史仍按名称分组，改名会影响历史曲线的连续性。

未迁入服务器但需保留本地显示的账号放入 `unmapped_accounts`，每项含 `name` 和 `collector`。这些行显示“未接入”，不刷新本机过期令牌；公开过滤名单继续生效。一个 collector 被配置覆盖后，必须列全该来源需要展示的账号。

## 窗口与失败语义

- OpenAI 按明确的 18000／604800 秒窗口区分 5h 与 7 天，不按 primary／secondary 的位置猜测。只有周窗的套餐不会出现虚构的 5h 值。
- Kimi／MiniMax 只采用本次返回的 `tiers`，不把旧 `extra` 中已缺失的周窗当作刷新成功。允许供应商返回超出 100% 的真实已用值；剩余显示为 0，进度条限制在 100% 内。
- Grok 使用官方周账单更新时间。只有月账单、网关消费余额或未知窗口时，不转换成周额度。
- MiniMax 仅有 5h 滑动窗口无周额度，卡片直接展示 5h 窗口并计算剩余，避免展示空置的周额度占位符。
- Antigravity 只取 Gemini 文本模型，排除图像及 Claude/GPT 项，以最高模型使用率作为周额度与周重置窗口。
- 未返回的周额度显示 `—`，不填成 0%。没有任何可识别窗口、认证失败或数据无效时显示异常。
- 超过五分钟、时间未知、时间明显超前或周窗口已过期的样本标为旧数据。过期短时窗口不会覆盖仍有效的周数据。
- 只重试失败或过期账号，新鲜成功账号不会重复请求。已配置账号读取失败时不静默切回本机 CLI；未被配置覆盖的来源沿用原采集器。
- 配置损坏时命令行停止写入；实时看板保留旧数据并标为缓存，不更新采集时间或追加历史。

## 使用与回退

```bash
python3 quota_report.py --json
python3 quota_report.py --html
QUOTA_SOURCE=sub2api python3 quota_report.py --json
QUOTA_SOURCE=local python3 quota_report.py --json
```

默认 `auto` 模式在私有配置存在且启用时接入 Sub2API；`sub2api` 模式要求有效配置；`local` 显式使用原本机采集器。也可以把私有配置的 `enabled` 改为 `false`。不要覆盖 CSV、原账号文件或公开过滤名单。

本地静态页与公开预览沿用邮箱打码和原有公开授权规则。更换数据源不修改现有 launchd 计划；公开页面继续走原发布流程。

## 验证入口

```bash
python3 -B -m unittest discover -s tests -p 'test_sub2api*.py' -q
python3 -B -m unittest discover -s tests -q
python3 -m py_compile quota_report.py sub2api_quota.py scripts/sub2api_quota_snapshot.py
```

服务器实现与路由核对基于 Sub2API `2380713bfaa6e49783c28d604bc9c2200602e7cc`。升级 Sub2API 后，应重新核对认证与查询副作用，不能把 GET 方法本身当作无模型请求、无重置行为的保证。
