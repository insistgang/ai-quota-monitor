# 项目进度

## 当前状态

- 通过 SSH 读取 6 个 Sub2API 账号（OpenAI ×3、Kimi ×2、MiniMax），通过本机 `agy` CLI 读取 Gemini、Claude / GPT-OSS 两个共享额度池，私有映射位于 `~/.cache/ai-quota-monitor/sub2api.json`（权限 `0600`）。说明见 [Sub2API 额度接入](docs/sub2api-quota.md)。
- 旧本机采集器（豆包 Chrome DOM、Grok、本机 CLI 采集及 `QUOTA_SOURCE=local` 分支）已全部移除；历史底账中停用/隐藏账号通过展示层过滤保留历史隔离。
- 财务盘点与公开脱敏映射统一由 `~/.cache/ai-quota-monitor/finance.json`（权限 `0600`）驱动：公开页面（概览、每日消耗、订阅账单）对邮箱、实名、银行名与私有账号标识全量脱敏，源码不含硬编码昵称。
- 订阅账单页卡片与近期到期 KPI 已纳入非 AI 服务续费监测（如 `ikuuu` 到期提醒）。

## 最近完成

- 2026-11-22 15:25 先只查（`scripts/quota-check.sh`，不写底账、不推送）后手动执行 `scripts/quota-check.sh --publish` 完成一次采集与公开发布：9 个来源（Sub2API 6 个账号 + Antigravity CLI 3 个额度组）全部新鲜（数据时间 15:25），无失败来源、无旧快照兜底；生成 15:25 快照，推送提交 `05b38b5`，GitHub Pages 构建状态 `built`，线上三页与本地发布产物 SHA-256 逐一一致。仍有两处源端缺项：Gemini · 4 Argon 无 5h 窗口数据（Antigravity CLI 对该组不提供 5h 桶）、Codex · phwdrvocd09162@hotmail.com 无 5h 行（上游未提供），均非采集失败。本次未改动采集代码。

- 2026-11-22 13:26 手动执行 `scripts/quota-check.sh --publish` 完成一次采集与公开发布：9 个来源（Sub2API 6 个账号 + Antigravity CLI 3 个额度组）全部新鲜（数据时间 13:24），无失败来源、无旧快照兜底；生成 13:24 快照，推送提交 `0fc5c4d`，GitHub Pages 构建状态 `built`，线上三页与本地发布产物哈希一致。本次未改动采集代码。

- 2026-11-22 02:44 提交并推送 `ROADMAP.md` 后再次手动发布：生成 02:43 快照，推送提交 `85c253b`，9 个来源全部新鲜；GitHub Pages 构建状态 `built`，线上三页与本地发布产物逐字一致。

- 2026-11-22 02:12 手动执行 `scripts/quota-check.sh --publish` 完成一次采集与公开发布：9 个来源（Sub2API 6 个账号 + Antigravity CLI 3 个额度组）全部新鲜，无失败来源、无旧快照兜底；生成 02:10 快照，推送提交 `c0a29fd`，GitHub Pages 构建状态 `built`，线上三页与本地发布产物逐字一致。本次未改动采集代码。

- 2026-11-21 00:35 按本人确认处理订阅口径：豆包会员保留使用至 2026-12-15、到期不续，订阅页恢复显示其到期提醒（额度卡片与每日消耗仍过滤）；Google AI Pro（土区）12-05 到期不续，到期时再定。拆分订阅页与额度页的过滤规则，新增回归测试。

- 2026-11-18 21:52 按本人确认更新 Gemini 会员口径：土区 Google AI Pro 到期日改为 **2026-12-05**；11-18 凌晨免费兑换的 18 个月 Gemini Ultra 20 于同日 16:00 掉费后切回 Pro，未实际生效。订阅页不再显示 2028-03-11。

- 2026-11-18 16:55 Antigravity 改为按 CLI 实际返回的模型组逐组成卡：Gemini、Claude / GPT-OSS 共享池各带周窗与 5 小时窗，Gemini 4 Argon 作为独立周额度单独显示。

- 2026-11-18 14:49 Antigravity 改用本机 CLI 原生额度命令；不再查询已移除的服务器账号。两个共享池分别显示周窗与 5 小时窗，旧分模型历史单独保留。

- 2026-11-17 19:19 按根目录 `日记.md` 的 11-17 记录新增 CodeBuddy（WorkBuddy 接入）年费 672 元：订阅页同步年费、2027-11-17 推算到期日和“自动续费未确认”边界，不与已有 Kimi 年费重复计收；AI 固定月均更新为 996 元、含非 AI 合计 1036 元，历史台账增至 25 笔。同步知识库订阅盘点，新增订阅解析回归测试并更新中英文测试计数。

- 2026-11-15 13:51 按 README 编写规范重写中英文 README（面向第一次了解项目的读者）：新增「主要功能」「配置」「技术栈与运行环境」三节，补全部 CLI 参数表、私有配置与环境变量表、测试按文件分布明细与「文档最后核对」时间；逐项对照代码后修正 `finance.json` 缺失时的行为描述（实为公开页生成失败，而非只缺订阅内容），并补写 launchd 入口依赖 `QUOTA_MONITOR_HOME` 指向仓库根目录的前提。

- 2026-11-15 06:23 重拍 README 三张页面截图：由 1280×900 视口截图改为整页截图，额度概览从缺 6 张卡片变为完整页面（含 KPI 条、快刷新提醒、9 张卡片与最近两日消耗），每日消耗与订阅账单同样改为整页；为符合公众号图片不超过 2000px 的约束，概览缩放为 1421×2000、订阅账单 1145×2000、每日消耗保持 1440×1799，均为 82 质量 JPEG。

- 2026-11-15 06:14 从私有财务笔记移除 Grok 记录并重新发布：删除订阅表 Grok 行、2026-08-27 的 217.50 元算力充值台账行、两处散文提及与一个 2026-11-02 前关注 SuperGrok 到期的待办；随后执行 `./publish.sh`，生成 06:13 快照（9 个账号）并推送提交 `bbf6925`，GitHub Pages 已构建，线上三页与本地发布产物逐字一致。

- 2026-11-15 06:12 从对外文案中移除 Grok：GitHub 仓库简介改为「Kimi/Codex/MiniMax/Antigravity」，README 中英文版与 `docs/project-design.md` 同步删除 Grok 提及。采集端自 2026-11-03 已无 Grok 取数代码（`sub2api_quota.py`、服务器侧快照脚本、发布流程与私有映射 `sub2api.json` 7 个账号均无 Grok）；保留 `quota_report.py` 的展示层名称过滤与其回归测试，避免历史底账中的停用账号重新出现在公开页。

- 2026-11-15 06:02 重写 README：新增英文版 `README_EN.md` 并与中文版顶部互链（共用 `README_SYNC` 标记），首屏补 Star 引导与 Stars／Python／License／Issues／Last Commit 徽章，正文重排为「是什么 → 不用它做什么 → 线上看板 → 快速开始 → 数据源 → 定时 → 隐私 → 口径 → 结构」；修正过期口径（当前 9 个账号、117 项测试、纯标准库、9:30–23:30 共 15 次），补项目结构树与本地测试说明；新增仓库 `LICENSE`（MIT 官方原文），使 README 声明与仓库实际一致。

- 2026-11-15 05:18 为 README 增加额度概览、每日消耗和订阅账单三张公开页面截图，图片放在 `docs/assets/`。
- 2026-11-15 05:13 通过已安装的 `~/.local/bin/quota-publish` 再次手动触发采集与发布，生成 05:12 快照（9 个账号），推送提交 `f0fc389`，GitHub Pages 已完成构建。
- 2026-11-15 04:06 手动执行 `./publish.sh`，采集 9 个来源并生成本地看板、底账与三张公开页面；快照提交 `49f72a2` 已推送，GitHub Pages 构建完成。
- 2026-11-14 完成项目架构清理、全量脱敏与历史重写：
  - 移除豆包 DOM 及旧本机 CLI 采集器、旧测试基线、`quota-log.csv.bak-before-free-account-removal` 与 `.tmp` 临时产物。
  - 修复 5h 短窗口未激活误计、`/serve` 并发竞争、写缓存 `0600` 权限与 CSV 去重性能；消除源码中硬编码私有昵称并由 `finance.json` 统一脱敏三个公开页面。
  - 订阅页与近期到期 KPI 接入非 AI 服务（`ikuuu`）到期展示（114 项测试与发布脚本校验通过）；重写 Git 历史清理历史敏感字段。
- 2026-11-03 23:52 未付费的免费 Codex 账号（`Codex · 1***@qq.com`）不再接入监控：本机 `unmapped_accounts` 清空、底账移除该账号 264 行历史，终端、看板、历史与公开页均不再出现。`public-hidden-accounts.json` 保留为历史数据兜底。
- 2026-11-03 23:23 下线 Grok 来源。采集端删除本机 Grok 日志／TUI／Win 远程采集与 Sub2API `grok` 平台支持，本机私有映射移除 Grok 账号（8 → 7）；展示层统一按名称过滤，历史底账中的 Grok 记录不再出现在概览与每日消耗页。运行时 PATH 移除 `.grok/bin`。
- 2026-10-28 00:12 修复定时发布的容错预算缺陷。此前 `--max-unhealthy 1` 的额度被配置里永久「未接入 Sub2API」的免费账号整份占满（`retryable=False` 仍被计入异常），任何一次瞬时失败都会让整轮写入取消；10-27 23:30 因 SSH 整体不可达，线上冻结在 22:38 快照逾 1.5 小时。
  - `_unhealthy_rows` 不再把 `retryable=False` 的既定状态计入异常预算，容错额度回到真实可用的 1。
  - 新增最近成功快照缓存 `~/.cache/ai-quota-monitor/last-good.json`：整轮读取失败时，把失败来源替换为上次成功值并标注「本次刷新失败，沿用上次快照」，页面以「使用缓存」样式发布，取代整轮放弃。
  - 仅当某来源既失败又无缓存可用时才仍以退出码 2 阻断写入，保留「不发布垃圾」的原有保护。缓存行在底账记为 `stale`，仍被每日消耗统计排除。
- 2026-10-25 22:14 完成 Sub2API 额度接入并在本机启用。采用账号实际窗口、来源时间、单账号失败／旧快照重试；保护空窗口、过期短窗、CN 超额值及配置错误后的缓存显示。OpenAI 使用服务器现有凭据 GET 官方用量，避开模型探针和自动重置路径；凭据不复制到本机。生成本地看板并追加一份真实快照，旧 CSV 前缀逐字保留。


## 最近验证

- 2026-11-22 15:25 只查与 `--publish` 两轮采集均未出现来源失败或「沿用上次快照」提示，9 个来源数据时间同为 11-22 15:25；底账增至 11598 行（本次快照新增 9 行）。`gh api` 确认最新 Pages 构建 commit 为 `05b38b5`、状态 `built`（15:25:44 building → 15:26:19 built）；`curl` 抓取 https://insistgang.top/ai-quota-monitor/ 三页与 `docs/index.html`、`docs/history.html`、`docs/subscriptions.html` 的 SHA-256 逐一一致，线上概览快照时间为 2026-11-22 15:25。提交前工作区仅剩本 ROADMAP 一处待提交改动，已随本次提交一并写入。

- 2026-11-22 02:44 `85c253b` 推送后轮询 Pages 最新构建约 1 分钟转为 `built`；`curl` 抓取 https://insistgang.top/ai-quota-monitor/ 三页与 `docs/` 对应产物逐字一致，线上概览快照时间为 2026-11-22 02:43，底账增至 11508 行，工作区干净、无未推送提交。

- 2026-11-22 02:12 只查与 `--publish` 两轮采集均未出现来源失败或「沿用上次快照」提示，9 个来源数据时间同为 11-22 02:10；首次线上比对时 Pages 仍为 `building`（线上是 23:30 旧快照），等待构建完成后重新抓取 https://insistgang.top/ai-quota-monitor/ 三页，与 `docs/index.html`、`docs/history.html`、`docs/subscriptions.html` 逐字一致（`cmp`）；`gh api` 确认最新 Pages 构建 commit 为 `c0a29fd`、状态 `built`。

- 2026-11-18 14:49 本机 `agy` 1.5.10 的 `/usage` 返回 0 回合、0 token。14:44 快照含 6 个 Sub2API 账号和 2 个 CLI 共享池，均读取成功，原底账前缀不变。126 项测试、双语 README 校验和 launchd 入口检查通过。

- 2026-11-18 16:55 CLI 的 JSON 同时返回第三个组 Gemini 4 Argon（独立周额度 99.0%），旧实现按固定组名过滤导致漏采。改为动态映射组名后，16:54 快照为 6 个 Sub2API 账号加 3 个额度组，全部新鲜，底账前缀不变；129 项测试与双语 README 校验通过。

- 2026-11-17 19:19 `python3 -B -m unittest discover -s tests -q`：118 项通过；实际财务笔记解析确认年费、日期、合计和公开页内容。提交 `bc82f73` 推送后 GitHub Pages 构建状态 `built`，正式 `subscriptions.html` 可见 CodeBuddy、672.00 元、2027-11-17 到期、996 元/月、1036 元/月及 25 笔台账。本次只更新订阅页，不重新采集或改写其他额度快照。

- 2026-11-15 13:51 `validate_readme_pair.py --primary README.md --secondary README_EN.md`：PASS；`python3 -B -m unittest discover -s tests`：117 项通过（10 + 60 + 30 + 17，按文件核对与 README 描述一致）；README 中的命令、参数、路径与环境变量逐项对照 `quota_report.py`、`sub2api_quota.py`、`publish_runtime.py`、`scripts/*.sh` 与 `examples/com.leo.quota-report.plist` 核验；README 标注的文档最后核对时间为 2026-11-15。

- 2026-11-15 06:23 三张截图由 Playwright 对线上 https://insistgang.top/ai-quota-monitor/ 三个页面以 1440×900 视口、`--full-page` 整页拍摄，逐张人工查看确认内容完整、邮箱已打码、页面无 Grok 字样；像素尺寸用 JPEG SOF 标记解析复核，最大边均不超过 2000px；`validate_readme_pair.py`：PASS。

- 2026-11-15 06:14 线上 https://insistgang.top/ai-quota-monitor/ 三页实测 Grok 命中均为 0；`docs/index.html`、`docs/history.html`、`docs/subscriptions.html` 与线上响应逐字一致（`cmp`），页面生成时间为 2026-11-15 06:13；订阅页消费台账由 25 笔变为 24 笔，历史消费台账 KPI 同步为 24 笔。发布前已备份财务笔记到 `.tmp/finance-note-grok-removal/`，笔记所在 `~/Documents/knowledge` 为 git 仓库可回滚。

- 2026-11-15 06:12 `validate_readme_pair.py --primary README.md --secondary README_EN.md`：PASS；`python3 -B -m unittest discover -s tests`：117 项通过；`git grep -i grok` 在 README.md、README_EN.md、docs/project-design.md 已无命中，采集端四个文件 grep grok 无命中；仓库简介经 `gh api` 复核已更新。代码内剩余 Grok 字样仅三处：展示层名称过滤、订阅页图标映射与对应回归测试，均有保留理由。

- 2026-11-15 06:02 `validate_readme_pair.py --primary README.md --secondary README_EN.md`：PASS（语言互链、同步标记、代码块一致、链接集合一致、代码内技术 Token 一致、本地相对目标均存在）；`python3 -B -m unittest discover -s tests`：117 项通过；README 中的账号数（9）、底账快照区间（2026-08-19 起 1224 次）与测试数量均取自本地实际数据与运行结果。

- 2026-11-15 05:18 Playwright 以 1280×900 截取三个公开页面并逐张检查可读性与打码；README 的三条相对图片链接均指向有效 JPEG，`git diff --check` 通过。仅更新文档和图片，未改采集代码。
- 2026-11-15 05:13 已安装触发入口退出码为 0；本地底账最新快照 05:12:10 共 9 行，三张公开页面无完整邮箱或已移除的免费账号；GitHub Pages 构建状态 `built`，线上三个 HTML 均与本地发布产物逐字一致。
- 2026-11-15 04:06 `python3 -B -m unittest discover -s tests -q`：117 项通过；发布脚本语法检查、三份私有配置 `0600` 与 JSON 结构检查通过。底账最新快照为 04:05:43、共 9 行；GitHub Pages 构建状态 `built`，线上三个 HTML 与本地发布产物逐字一致，完整邮箱及已移除的免费账号均未出现。
- 2026-10-28 00:12 新增 7 项回归测试通过（异常预算排除既定未接入、真实故障仍计数、缓存替换、无缓存仍阻断、降级发布、成功快照落盘）；全量 127 项中 120 项通过，5 项失败与改动前 120 项基线中的失败名称完全相同，均为原豆包策略与旧测试预期冲突。`git diff --check` 与语法编译通过。
- 2026-10-28 00:12 端到端实测：把 `ssh_host` 指向不可达主机模拟整体故障，命令以退出码 0 降级发布，8 个来源全部显示上次成功数值并逐一标注「本次刷新失败，沿用上次快照」，既定未接入的免费账号保持原样；`last-good.json` 在健康轮次正确落盘 8 个来源。线上已推送 `08e3001`，冻结快照解除。
- 2026-10-25 22:14 新增来源、远端读取与集成测试 39 项通过；全量 119 项中 114 项通过，5 项失败与改动前 80 项基线中的失败名称完全相同，均为原豆包策略与测试预期冲突。Python 编译及 `git diff --check` 通过；未改动原发布器及其已有未提交修改。
- 2026-10-25 22:14 已启用配置的主命令真实读取 8 个新鲜账号，另 1 个本地免费账号明确标为未接入；最终采集约 8.3 秒，追加 9 行底账，历史前缀校验一致。浏览器实测公开预览 8 张卡片、两项未知周额度正确显示、进度条无越界、无横向溢出；本地及三个公开预览均无完整邮箱或凭据模式。
- 2026-10-25 22:14 既有 launchd 已加载，安装入口只读 smoke 返回 `INSTALL_RUNTIME_PASS`；SSH 在不使用 agent 时仍能无交互连接 RN。原计划上次因 3 个异常来源退出，新采集已通过原写入门槛。未新增计划、未执行手动公开发布。

- 2026-10-24 11:12 GitHub Pages 已构建 `b82ddd2`；三个线上 HTML 与本地发布产物逐字一致。真实采集 9 个来源全部正常，公开概览保留 8 张卡片并隐藏配置指定的账号；Grok 数据时间从 08:04 更新为 11:06。浏览器验证新快照原位更新、旧响应不覆盖新值、HTTP 503 保留现有显示；本次推送连接失败后自动重试成功。
- 2026-10-24 修复新增 12 项回归测试通过，覆盖过期日志、主动刷新、Codex 重试、缓存历史隔离及发布重试。全量 80 项中 75 项通过，另 5 项为修复前已存在的豆包停用策略与旧测试预期冲突；本轮保留原有账号展示策略，未改写这些测试。公开页隐私扫描及 `git diff --check` 通过。
