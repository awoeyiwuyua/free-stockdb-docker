# 导读笔记 M3 — Phase 5.1 LuCI 重组（"一页一职责，信息优先"）

> 配套里程碑：Phase 5.1（LuCI 经验版）——SPA 从"十页平铺"重组为"菜单树 + 一页一职责"
> 阅读对象：想跟着学的你
> 配套资料：Element Plus 菜单/分组文档（https://element-plus.org/zh-CN/component/menu.html）、
> Vue Router 重定向（https://router.vuejs.org/zh/guide/essentials/redirect-and-alias.html）、
> ECharts 双 Y 轴配置（https://echarts.apache.org/zh/option.html#yAxis）

## 这一轮做了什么

1. **菜单树重组（LuCI 模式）**：`layout/nav.js` 从 M1 的"4 组 10 页平铺"改成"顶层单页 + 分组子菜单"。
   `TOP_ITEMS` 只有总览一项（对应 LuCI 的 Status→Overview 单入口），`NAV_GROUPS` 是两个可折叠分组
   ——**系统运维 7 项**（数据同步/私有存储/系统健康/诊断中心/日志中心/通知中心/MCP 观测）+ **模拟盘 3 项**，
   共 11 页。`SideNav.vue` 用 `el-sub-menu` 渲染分组，子项 `el-menu-item` 点即跳路由；nav.js 仍是
   唯一配置源——路由表、侧边栏、旧路径重定向全部从它生成。
2. **一页一职责（页的"拆迁"）**：旧页按职责拆、并、迁，每页只干一件事：
   - 数据同步页搬家（`/data/sync` → `/ops/sync`），并把**容器日志、重启按钮**迁去系统健康页；
   - 私有存储页搬家（`/data/mydb` → `/ops/mydb`）；
   - 原"系统"页扩成**系统健康页**（`/ops/health`），容器/磁盘/健康/同步能力/日志/重启全收一处；
   - **版本页退役**（`/ops/version` → 重定向到总览），版本卡并入总览页；
   - 新增**诊断中心**（`/ops/diag`）与**日志中心**（`/ops/logs`）两个专职页面。
3. **`/api/diag` 诊断聚合端点（后端 Phase 5.1 新增）**：一次 GET 拿全"六项检查 + 环境信息"。
   六检查 = 上游 GitHub / 妙想模拟盘 API / stockdb 服务 / pybao 计算模块 / 磁盘 / 交易日历，
   每项 `{name,label,ok,note}`；env 块给 python 版本/架构/镜像 tag/启动时长/数据目录/数据最新日；
   单块失败只降级该块，整体始终 200（`all_ok` 汇总全绿与否）。前端 `api/diag.js` 一行封装，
   `OpsDiag.vue` 渲染成六张状态卡 + 环境描述表。
4. **日志中心三源聚合 + 前端过滤**：`OpsLogs.vue` 把**同步日志**（`/api/log`）、**容器日志**
   （`/api/container/logs`）、**模拟盘事件**（`/api/paper/events`）三个接口并行拉进一页，
   每个源独立管理 loading/error/数据——一个源挂了另外两个照常展示（分块错误隔离）。
   页头一个关键字输入框，`computed` 在**前端实时过滤**（文本按行 contains、事件表按
   event/detail/timepoint/level/trade_date 字段匹配），不发新请求。
5. **数据同步趋势图**：`OpsSync.vue` 页顶新增"同步耗时趋势"卡片——**直接复用 `/api/history`
   的同一份数组做图，不额外请求接口**：x=同步时间，左轴=耗时（秒），右轴=下载文件数（个），
   双折线 + 渐变面积；该次同步没有数值（运行中/异常中断）就留断点，图表与表格永远同源。
6. **密度哲学落地（LuCI 经验）**：全站统一——表格一律 `size="small"` + `border`、卡片内边距
   收紧到 12-14px、页头用 `.page-head` 紧凑式（小标题在左 + 操作在右）、说明性文字压到
   12px muted 色、`el-descriptions` 用 `border size="small"`。信息优先，不铺大留白。
7. **顶栏精简**：`StatusBar.vue` 只保留**数据新鲜度 / 模拟盘 / 告警红点**三个高频项 +
   右侧"刷新时间/主题/时钟"；MCP 健康率、webui 版本从顶栏撤下——它们各自有了专职页面
   （MCP 观测页、总览版本卡），顶栏只放"全局都想知道"的信息。
8. **旧路由重定向兜底**：`LEGACY_REDIRECTS` 六条映射（`/data/sync`→`/ops/sync`、
   `/data/mydb`→`/ops/mydb`、`/ops/system`→`/ops/health`、`/ops/version`→`/overview`、
   `/data`→`/ops/sync`、`/alerts`→`/ops/alerts`），`router/index.js` 把每条渲染成 `redirect`
   路由——老书签/旧链接不 404，浏览器地址栏直接跳到新地址。
9. **测试守门**：`nav.test.js`（10 例）把菜单树钉死——顶层恰好 1 项、分组恰好 2 组且顺序固定、
   每组条数 7/3、展平 11 项、path 全唯一、badge 只挂通知中心、六条重定向与目标地址合法性；
   `diag.test.js`（3 例）锁死 `/api/diag` 是只读 GET 且不带 body。
10. **轮询纪律与例外**：默认统一 30s + `onUnmounted` 清理；本轮出现两处**写注释的例外**——
    日志中心 15s（日志讲究新鲜）、诊断中心 60s（体检是"人点一下"的静默动作）。约定的意义
    就在于例外也是明说的，不是悄悄改的。

## 十一页各自的职责与唯一数据源

统一数据流模式没变（页面 → api/ 封装 → 后端），但本轮的纪律升级为**一页一职责**：
每页只回答一个问题，数据只从自己声明的来源拿，页面之间不重复请求。

| 页面（文件） | 路由 | 唯一数据源 | 职责（一句话） | 轮询 | 学习要点 |
|---|---|---|---|---|---|
| 总览 `Overview.vue` | `/overview` | 全局 store（`/api/overview`）+ 页面私有的 `getSignalStatus()` / `getVersion()` | 一眼看全：5 指标卡 + 4 紧凑区块 | store 由 App 30s 刷；信号/版本两源页面自管 30s | 一页混用"共享数据 + 私有数据"两种来源 |
| 数据同步 `OpsSync.vue` | `/ops/sync` | `/api/status` `/api/history` `/api/schedule` `/api/log` + 写操作 | 同步状态/趋势/定时/操作/港股 | 30s（4 只读并行） | **趋势图复用 history 同一份数组**；schDirty 防吞草稿 |
| 私有存储 `OpsMydb.vue` | `/ops/mydb` | `/api/data/tables` `/api/data/read` `/api/data/write` `/api/query` | 私有表：清单/读取/写入/查询台 | 30s（只刷表清单） | 写入操作二次确认 |
| 系统健康 `OpsHealth.vue` | `/ops/health` | `/api/health` `/api/status`(container/disk/sync_cap) `/api/diag`(env) `/api/container/logs` | 判断"系统行不行、要不要动它" | 30s（busy 互斥） | 三源并行、各自降级；重启二次确认 |
| 诊断中心 `OpsDiag.vue` | `/ops/diag` | `/api/diag` | 人点一下的体检：六检查 + 环境信息 | **60s**（例外） | `all_ok` 派生汇总徽标；失败卡红边框 |
| 日志中心 `OpsLogs.vue` | `/ops/logs` | `/api/log` `/api/container/logs` `/api/paper/events` | 三源日志聚合 + 关键字过滤 | **15s**（例外） | 前端 contains 过滤；分块错误隔离；智能滚动 |
| 通知中心 `OpsAlerts.vue` | `/ops/alerts` | `/api/alerts` + `/api/alerts/clear` | 告警列表 + 清空 | 30s | 清空后调 `store.refresh()` 联动顶栏红点归零 |
| MCP 观测 `OpsMcp.vue` | `/ops/mcp` | `/api/mcp/stats` `/api/mcp/calls` | MCP 调用健康与明细 | 30s（busy 互斥） | 统计卡 + 柱状图 + 明细表互补 |
| 模拟盘 `Paper.vue` | `/paper` | `/api/paper/*`（status/overview/snapshot/decisions/orders/events/pause/run-now/apikey/connectivity） | 模拟盘驾驶舱：账户/净值/单步/明细 | 30s（status + 5 明细并行） | 引擎不可用 501 → 整块降级 |
| 审计报告 `PaperAudit.vue` | `/paper/audit` | `/api/paper/audit` | 回放审计：统计 + 明细 + 净值 vs 基准 | 30s | 双序列 ECharts；空库降级 |
| 信号体检 `PaperSignal.vue` | `/paper/signal` | `/api/paper/signal-status` | 7 项信号校验卡片 | 30s | 通过绿/失败红；文件缺失降级 |

> 变更速记：**退役** = `/ops/version`（并入总览）；**搬家** = 数据同步/私有存储/系统；**新增** =
> `/ops/diag`（诊断中心）、`/ops/logs`（日志中心）；**瘦身** = 原"数据"页的容器日志与重启迁去系统健康页。

## 每个文件干什么

**页面（src/views/，11 个）**

| 文件 | 作用 | 关键概念 |
|---|---|---|
| `Overview.vue` | 总览（LuCI 驾驶舱版）：5 指标卡 + 4 区块卡，数据以 store 为主 | 共享 store + 页面私有轮询混用 / 紧凑卡片 |
| `OpsSync.vue` | 数据同步：状态总览、**耗时趋势图**、操作、定时、历史、日志、港股 | 趋势图复用 history / 阶段→百分比 / schDirty |
| `OpsMydb.vue` | 私有存储：表清单、读取、写入、查询台 | 任意 JSON 归一化成表格 / 写入二次确认 |
| `OpsHealth.vue` | 系统健康：健康卡、容器（日志/重启）、磁盘、同步能力 | 三源并行各自降级 / busy 互斥 |
| `OpsDiag.vue` | 诊断中心：六检查卡片 + 环境描述表 + 汇总徽标 | `/api/diag` 聚合 / all_ok 派生 / 60s 静默轮询 |
| `OpsLogs.vue` | 日志中心：三源日志聚合 + 全局关键字过滤 | 前端 contains 过滤 / 分块降级 / 15s 轮询 / 智能滚动 |
| `OpsAlerts.vue` | 通知中心：告警表格 + 清空（二次确认） | 清空后联动 store 红点归零 |
| `OpsMcp.vue` | MCP 观测：统计卡 + 按工具分布 + 调用明细 | ECharts 柱状图 + 表格互补 / 失败行红 |
| `Paper.vue` | 模拟盘：状态、账户、净值、手动单步、明细三 tab、apikey | el-tabs / seq 防旧请求覆盖 |
| `PaperAudit.vue` | 审计报告：统计卡 + 明细 + 净值 vs 基准双曲线 | 双序列 ECharts / 归一化对比 |
| `PaperSignal.vue` | 信号体检：概况 + 7 项校验卡片 | 通过/失败色区分 / 文件缺失降级 |

**基础设施（layout / api / router / 测试）**

| 文件 | 作用 | 关键概念 |
|---|---|---|
| `src/layout/nav.js` | 导航唯一配置源：`TOP_ITEMS` + `NAV_GROUPS` + 展平 `NAV_ITEMS` + `LEGACY_REDIRECTS` | 单一数据源 / 纯数据可单测 |
| `src/layout/SideNav.vue` | 侧边栏：`el-sub-menu` 渲染分组树，子项 router 模式跳路由，badge 挂红点 | el-sub-menu 分组 / collapse |
| `src/layout/StatusBar.vue` | 顶栏（精简）：数据/模拟盘/告警 + 刷新时间 + 主题 + 时钟 | 顶栏只放高频信息 |
| `src/router/index.js` | 从 `NAV_ITEMS` 生成路由 + 把 `LEGACY_REDIRECTS` 渲染成 redirect + `afterEach` 标题 | 单一数据源 / 重定向兜底 |
| `src/api/diag.js` | `getDiag()` 一行封装 `GET /api/diag` | 一域一模块 |
| `src/layout/nav.test.js` | 菜单树与重定向契约（10 例）：分组条数/path 唯一/badge/重定向合法性 | 测试当守门员 |
| `src/api/diag.test.js` | `/api/diag` 契约（3 例）：GET、无 body、payload 解析 | 契约测试 |

> 后端对应：`stockdb-ai/app.py` 新增 `GET /api/diag` handler（六检查 + env，单块降级不 500），
> `test_ops.py` 补了对应契约断言——前端字段以它实际返回为准。

## 关键概念对照表

| 名词 | 白话 | 在本项目哪里 |
|---|---|---|
| 菜单树与分组导航 | 导航不再是一排平铺页签，而是"顶层单页 + 可折叠分组"，像 LuCI 侧栏一样按职责分区 | `TOP_ITEMS`（总览单页）+ `NAV_GROUPS`（系统运维 7 / 模拟盘 3）+ `SideNav.vue` 的 `el-sub-menu` |
| 一页一职责 | 每页只回答一个问题、只用自己的数据源；页多了不怕，职责不重叠就行 | 十一页各管各的（见上表"唯一数据源"列）；版本页并入总览、容器日志归健康页 |
| 聚合端点 | 后端把多次轮询合成一次请求，前端少发请求、数据天然一致 | `/api/overview`（M1，五块聚合）与 `/api/diag`（本轮，六检查 + env） |
| 前端日志聚合 | 多个日志/事件接口并行拉进一页，各自降级，一个挂了不拖垮别的 | `OpsLogs.vue` 三源：`getLog` / `getContainerLogs` / `getEvents` |
| 关键字过滤 | 过滤在浏览器里做（computed 实时 contains），不动后端、不重发请求 | `OpsLogs.vue` 的 `kw` + `filterLogText` / `filteredEvents` |
| 信息密度 | 同样的屏幕放下更多信息：小表格、小内边距、紧凑标题行、弱化说明文字 | 全站 `size="small"` 表格、卡片 padding 14px、`.page-head`、12px muted hint |
| 重定向兜底 | 老路径不 404：路由表声明 from→to，访问旧地址自动跳新地址 | `LEGACY_REDIRECTS` 六条 → `router/index.js` 的 `redirect` 路由 |
| 轮询节拍例外 | 约定 30s 是默认值，个别页面按业务改节拍时要写注释说明为什么 | `OpsLogs` 15s（日志要新）/ `OpsDiag` 60s（体检是人点一下） |
| 断点折线 | 数据缺某一点时折线断开而不是连错，如实反映"那次没产生数值" | `OpsSync.vue` 趋势图：`duration_sec/downloads` 为 null 的同步记录留断点 |

## 你可以怎么玩

```bash
cd stockdb-ai/spa
npm run dev        # 打开 http://localhost:5173，热更新（/api 已代理到本地 webui 8080）
npm run test       # 前端单测（当前 63 例全绿）
npm run build      # 编译出 dist/
```

改一改试试（保存后浏览器立即生效）：

1. **在 nav.js 加一个分组子项看菜单树变化**：打开 `src/layout/nav.js`，在「模拟盘」分组里加一项
   比如 `{ path: '/paper/log', title: '复盘日志', icon: 'Memo' }` → 侧边栏「模拟盘」下拉里立刻多
   出一项（路由是导航生成的，页面没写的话点击会 404——这正好说明"菜单只管入口，组件还得自己写"）。
   然后跑 `npm run test`：`nav.test.js` 会**报错**（它钉死了每组条数和总页数）——这不是 bug，而是
   测试在提醒你"结构变了，去同步断言"。这就是"测试当守门员"的日常。
2. **把日志中心轮询 15s 改 5s**：打开 `src/views/OpsLogs.vue`，找到
   `const POLL_MS = 15_000`，改成 `5000` → 三块日志的"xx 更新"时间跳得更勤。注意 `OpsLogs.vue`
   顶部注释写着"15s（日志要新）"——改完记得改回来，并想想：为什么其他页都是 30s，唯独这里更密？
3. **访问旧路径看重定向**：在浏览器地址栏直接敲 `http://localhost:5173/data/sync` 回车 →
   地址栏立刻变成 `/ops/sync` 且页面正常加载（不是 404）。再试试 `/ops/system` → `/ops/health`、
   `/ops/version` → `/overview`。路由表里这几条 `redirect` 来自 `LEGACY_REDIRECTS`——老书签从此
   不失效。
4. **看诊断中心的"静默轮询"**：打开 `src/views/OpsDiag.vue`，点「立即体检」按钮看按钮转圈 + 成功
   提示；再把 `setInterval(() => load(), 60000)` 改成 `30000`，等半分钟看"上次体检"时间自己往前走
   ——轮询成功是静默的，不打扰你；失败才在顶部给一行弱提示。
5. **看分块错误隔离**：进入日志中心后把后端 webui 进程关掉 → 三块各自出现"读取失败"文案与重试按钮，
   但页面不崩；再启动后端点重试，各块逐个恢复。对比诊断中心：整页只有一个接口，失败就是整页空态——
   单源页面与多源页面的降级策略完全不同。

## 验收记录

- [x] 前端 Vitest 全绿（8 文件 / 63 例；含 nav 结构断言 + diag 契约 3 例）
- [x] `npx vite build` 通过（11 页按路由分包 + vendor 三件独立缓存，index 主包 16.9KB）
- [x] Python 232 全绿（test_ops 含 `/api/diag` 3 例新增）
- [x] 11 深链冒烟：11 条路由全 200 + 六条旧路径重定向注册生效（主线程 QC 实测）
- [ ] NAS `pull && up -d` 后实测 + 新旧面板逐页并排核对 + `/legacy` 回滚演练（用户侧，Phase 5 收口时执行）

## 下个里程碑（Phase 5 收口）

本地 11 深链冒烟（`app.py` + `dist` 双通道）→ NAS 部署实测与 `/legacy` 逃生通道演练 → 功能清单回归表
（`docs/history/m2-regression-checklist.md` 风格）按新菜单结构重新逐项勾选收口。后续可玩的方向：
日志中心加"级别筛选"（DEBUG/INFO/WARN/ERROR 下拉）与一键导出；诊断中心记录历史体检结果画趋势；
趋势图把"下载量"换成速率轴；顶栏再考虑"可配置隐藏"。
