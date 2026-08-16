# 导读笔记 M2+M3 — 十页搬迁与数据流（"控制台能干活了"）

> 配套里程碑：Phase 5 SPA 重构 M2（十页搬迁）+ M3（补位页与收尾）
> 阅读对象：想跟着学的你
> 配套资料：Vue 生命周期钩子（https://cn.vuejs.org/guide/essentials/lifecycle.html）、
> ECharts 配置项手册（https://echarts.apache.org/zh/option.html）、
> Element Plus Message / MessageBox / Tabs 文档（https://element-plus.org/zh-CN/）

## 这一轮做了什么

1. **10 个占位页全部换成真页面**：M1 的 `PlaceholderView` 一次性退役，`src/views/` 下新增
   `Overview / DataSync / MyDb / Paper / PaperAudit / PaperSignal / OpsSystem / OpsAlerts / OpsMcp / OpsVersion`
   十个页面组件，全部用 `script setup` 组合式 API 编写，逐页对照旧面板 `static/legacy/index.html`
   的行为做回归（旧页是行为基线）。
2. **两张补位页**：审计报告（`/paper/audit`）与信号体检（`/paper/signal`）在旧面板里**从来没有 UI**
   （Phase 4.5 只交付了后端端点 + Python 测试），本轮按 `test_ops.py` 的 payload 断言补齐渲染——
   这是 M2 的补位任务，字段以 `GET /api/paper/audit`、`GET /api/paper/signal-status` 实际返回为准。
3. **api/ 四域封装**：接口按业务域拆成 `status.js / data.js / paper.js / ops.js` 四个模块（另加 M1 的
   `overview.js` 聚合域），页面只 import 需要的函数；后端路径、查询参数、POST 体全部收敛在这四
   个文件里，接口改动一处生效。
4. **EChart 按需封装**：`components/EChart.vue` 用 `echarts/core` 按需注册折线/柱状/饼图与常用
   组件，只打包用到的图表类型；内部自动 `init / setOption / resize / dispose`，页面传一个
   `option` 对象 + `height` 字符串即可。
5. **三态齐备**：每个页面都实现了「加载态（`el-skeleton` 骨架屏）→ 空态（`EmptyState`）→
   错误态（顶部 `ElAlert` 文案 / 重试按钮）」；轮询中途失败时"保留旧数据 + 弱提示"，页面绝不崩。
6. **危险操作二次确认**：容器重启、清空告警、暂停/恢复模拟盘、清除 apikey、以及一切写入操作
   （启动同步/保存定时/港股同步/写私有表/手动单步/保存 apikey）统一走 `ElMessageBox.confirm`，
   用户点取消就静默返回；结果用 `ElMessage` 提示 success/error/warning。
7. **轮询纪律统一**：页面级数据一律 `onMounted` 首拉 + `setInterval` 30s + `onUnmounted`
   `clearInterval` 清理；总览相关字段优先读全局 store（App 层已轮询 `/api/overview`），页面不重复请求。
8. **接线待办（重要）**：本轮交付的是**页面组件与 API 封装**；`router/index.js`（M1 状态）目前
   仍把所有路径指向 `PlaceholderView`。要让页面真正可访问，需要把路由表里每个 path 的
   `component` 换成对应视图（建议懒加载 `() => import('../views/Xxx.vue')`）——这一步在
   验收清单里是待勾选项，接线前访问各页看到的仍是占位页。

## 十页各自的职责与数据流

统一数据流模式（每页都一样，记住它就能读懂任何一页）：

```
浏览器页面 (src/views/Xxx.vue)
   │  import 具体函数，不直接 fetch
   ▼
api/ 四域模块 (src/api/*.js)      ← 后端路径只写在这里
   │  getJson / postJson（20s 超时、非 2xx 抛 ApiError）
   ▼
后端 app.py handler (/api/...)    ← 字段以实际返回为准
```

| 页面（文件） | 路由 | 数据流（接口 → 页面块） | 轮询 | 学习要点 |
|---|---|---|---|---|
| 总览 `Overview.vue` | `/overview` | `GET /api/overview`（聚合）→ 全局 store；`GET /api/paper/signal-status` → 情绪投递卡 | 总览读 store 不重复轮询；**信号卡独立 30s 轮询** | 一个页面同时用"共享 store 数据"与"页面私有数据"两种来源 |
| 数据同步 `DataSync.vue` | `/data/sync` | `GET /api/status /api/history /api/schedule /api/log?n=80` + `POST /api/sync /api/container/restart /api/hk/sync` | 30s（4 个只读接口并行） | 同步进度条、定时计划防吞草稿（schDirty）、懒加载容器日志 |
| 私有存储 `MyDb.vue` | `/data/mydb` | `GET /api/data/tables /api/data/read` + `POST /api/data/write` + `GET /api/query?t=` | 30s（只刷表清单） | 任意 JSON 查询结果归一化成表格；单条/批量两种写入 |
| 模拟盘 `Paper.vue` | `/paper` | `GET /api/paper/status` + 5 个明细接口（overview/snapshot/decisions/orders/events）+ `POST /api/paper/pause /run-now /apikey /connectivity` | 30s（status + 5 明细并行） | 引擎不可用时明细接口全 501 → 整块降级；seq 防旧请求覆盖新数据 |
| 审计报告 `PaperAudit.vue` | `/paper/audit` | `GET /api/paper/audit`（mode=ro 只读） | 30s | **补位页**：4 张统计卡 + 3 块明细表 + 净值 vs 基准双折线（净值归一化到首日 1.0） |
| 信号体检 `PaperSignal.vue` | `/paper/signal` | `GET /api/paper/signal-status` | 30s | **补位页**：7 项 checks 逐项卡片，通过绿/失败红；交易日从文件路径正则提取 |
| 系统 `OpsSystem.vue` | `/ops/system` | `GET /api/health` + `GET /api/status` + `GET /api/container/logs` + `POST /api/container/restart` | 30s（busy 互斥） | Promise.all 并行拉两接口；重启按钮在进程不可控时禁用 |
| 通知中心 `OpsAlerts.vue` | `/ops/alerts` | `GET /api/alerts?limit=200` + `POST /api/alerts/clear` | 30s | 清空后调 `store.refresh()` 让顶栏红点立刻归零（跨组件联动） |
| MCP 观测 `OpsMcp.vue` | `/ops/mcp` | `GET /api/mcp/stats` + `GET /api/mcp/calls?limit=50` | 30s（busy 互斥） | 统计卡 + by_tool 柱状图/表格 + 明细表（失败行红色） |
| 版本 `OpsVersion.vue` | `/ops/version` | `GET /api/version` | 30s（busy 互斥） | stale 高亮 + 上游 release 外链（`target=_blank` + `rel=noopener`）+ `/legacy` 逃生通道 |

> 补位页字段速记：
> `audit` → `replay_mismatches{count,examples}` · `duplicate_intents` · `illegal_transitions{count,examples}`
> · `slippage{avg_slippage,n}` · `order_status_counts` · `nav_series` · `benchmark_series`（基准已按首日归一化）
> `signal-status` → `exists / parsed / error / path / fields` · `checks{key:{ok,reason}}`（7 项，与引擎 DATA_NOT_QUALIFIED 同规则）

## 每个文件干什么

**页面（src/views/，10 个）**

| 文件 | 作用 | 关键概念 |
|---|---|---|
| `Overview.vue` | 总览仪表盘：5 张 StatCard + 时间轴/告警/数据与 MCP/情绪投递/版本 5 张信息卡 | store getters / 页面独立轮询混用 |
| `DataSync.vue` | 数据同步：状态总览、同步操作、定时计划、历史、日志、容器、港股 | 阶段→百分比映射 / schDirty 防吞草稿 |
| `MyDb.vue` | 私有存储：表清单、读取、写入（单条/批量）、查询台 | 任意 JSON 归一化成表格 / allow-create 输入新表名 |
| `Paper.vue` | 模拟盘：状态、账户、净值曲线、手动单步、明细三 tab、apikey | el-tabs / reactive 集中状态 / 分块错误互不影响 |
| `PaperAudit.vue` | 审计报告（补位）：统计卡 + 明细 + 净值 vs 基准双曲线 | 双序列 ECharts / 归一化对比 / 空库降级 |
| `PaperSignal.vue` | 信号体检（补位）：概况 + 7 项校验卡片 + 字段摘要 | 通过/失败色区分 / 文件缺失降级态 |
| `OpsSystem.vue` | 系统：健康卡 + 进程/磁盘/同步能力分块 + 容器日志 + 重启 | busy 互斥 / 并行 Promise.all |
| `OpsAlerts.vue` | 通知中心：告警表格 + 清空（二次确认） | 清空后联动 store 红点归零 |
| `OpsMcp.vue` | MCP 观测：统计卡 + 按工具分布 + 调用明细 | ECharts 柱状图 + 表格互补 / 失败行 :deep 红 |
| `OpsVersion.vue` | 版本：版本信息 + stale 高亮 + 旧面板入口 | 外链安全习惯 / upstream 为 null 容错 |

**基础设施（api / components / utils）**

| 文件 | 作用 | 关键概念 |
|---|---|---|
| `src/api/http.js` | 统一 fetch 总机：超时（AbortController）、JSON 解析、非 2xx 抛 `ApiError` | 封装统一错误面 |
| `src/api/status.js` | 数据同步/系统域：status/health/history/schedule/log/container/sync/hk | 一域一模块 |
| `src/api/data.js` | 私有存储域：tables/read/write/query | 一域一模块 |
| `src/api/paper.js` | 模拟盘域：status/overview/snapshot/decisions/orders/events/audit/signal/pause/run-now/apikey/connectivity | 隐私：apikey 只提交只掩码 |
| `src/api/ops.js` | 运维域：alerts/clear/mcp/version | 一域一模块 |
| `src/components/EChart.vue` | ECharts 按需封装：init/setOption/resize/dispose 全包办 | 按需引入 / ResizeObserver |
| `src/components/StatCard.vue` | 指标卡（props 驱动，tone 限 ok/warn/err/brand） | 复用 / validator 校验 |
| `src/components/EmptyState.vue` | 空态占位（icon/title/description + 默认插槽） | 职责单一 |
| `src/utils/format.js` | fmtYMD/fmtMoney/fmtPct/fmtElapsed 纯函数 | 全站数字展示统一入口 |

## 关键概念对照表

| 名词 | 白话 | 在本项目哪里 |
|---|---|---|
| 三态设计 | 页面永远分三种状态渲染：加载中（骨架屏）、成功但没数据（空态）、出错（文案+重试），任何时候都不裸奔 | 十个页面的 `el-skeleton` / `EmptyState` / `ElAlert` 三分支 |
| ECharts option | 一张"图怎么画"的纯 JS 配置对象（坐标轴/系列/颜色），数据变了就重传一份 | `Paper.vue` 的 `chartOption`、`PaperAudit.vue` 的双序列 option |
| ResizeObserver | 浏览器 API：监听到容器尺寸变化就回调——图表容器被拉宽/侧边栏折叠时自动重绘 | `EChart.vue` 里 `ro.observe(el)` → `chart.resize()` |
| 按需引入与代码分包 | 只 import 用到的模块（tree-shaking），构建产物更小；首屏按路由拆包更快 | `EChart.vue` 从 `echarts/core` 注册；`/tmp` 构建看 chunk 大小 |
| 危险操作确认 | 点按钮不立刻执行：先弹 `ElMessageBox.confirm`，用户明确点"确认"才真正调接口 | 重启/清空/暂停恢复/清除 apikey/一切写入操作 |
| 多标签 el-tabs | 同一块区域放多个页签，数据多而不乱；每个页签独立加载/独立报错 | `Paper.vue` 的 决策/订单/事件 三个 tab |
| 轮询清理 | 定时器用完必须 `clearInterval`，离开页面还挂着会泄漏、白费请求 | 每页 `onUnmounted` 里的清理 + `Paper.vue` 的 seq 防串扰 |
| busy 互斥 | 上次请求没回来就跳过本轮，防止 30s 轮询与慢接口叠加堆积 | `OpsSystem / OpsAlerts / OpsMcp / OpsVersion` 的 `if (busy) return` |
| 聚合接口 | 后端把多次轮询合成一次请求，前端少发请求、数据天然一致 | `/api/overview` → 全局 store → 顶栏 + 总览共用 |
| 补位页 | 旧面板从没渲染过的功能，本轮按后端 payload 断言补齐 UI | `PaperAudit.vue` / `PaperSignal.vue` |
| 掩码隐私 | apikey 永远只提交、只展示掩码 `****`，前端任何地方不缓存原文 | `Paper.vue` apikey 卡 + `api/paper.js` 注释约定 |

## 你可以怎么玩

```bash
cd stockdb-ai/spa
npm run dev        # 打开 http://localhost:5173，热更新（/api 已代理到本地 webui 8080）
npm run test       # 前端单测（当前 56 例全绿）
npm run build      # 编译出 dist/
```

改一改试试（保存后浏览器立即生效）：

1. **在总览页把轮询改快看刷新**：打开 `src/views/Overview.vue`，找到情绪投递卡的
   `setInterval(loadSignal, 30_000)`，把 `30_000` 改成 `5000` → 右上角"上次刷新"时间明显跳得更勤；
   再试试 `src/App.vue` 里的 `POLL_FAST = 30_000`——那是整个 `/api/overview` 的节拍，改小后顶栏
   状态条也变勤快。改完记得改回来。
2. **改 audit 图 height**：打开 `src/views/PaperAudit.vue`，找到
   `<EChart :option="chartOption" height="320px" />`，把 `320px` 改成 `480px` → 净值 vs 基准
   双曲线变高，y 轴刻度更展开——`height` 就是 EChart 组件的第二个 prop，改的是"画布高度"。
3. **给告警列表加 limit 下拉**：打开 `src/views/OpsAlerts.vue`，现在 `load()` 里写死
   `getAlerts(200)`。试着加一个 `const limit = ref(50)`，页头放
   `<el-select v-model="limit">`（选项 50/100/200），把 `getAlerts(200)` 改成 `getAlerts(limit.value)`
   ——注意 `getAlerts` 已经接收 limit 参数（`src/api/ops.js`），页面层加个控件即可，这就是
   "接口封装好了，页面只管交互"的体现。
4. **看三态切换**：打开任一页后把后端 webui 进程关掉 → 页面出现"接口不可用"错误文案与重试按钮，
   但页面不崩；再点重试恢复。对比 `Paper.vue`：引擎不可用时账户/曲线/明细整块变成降级空态，只有
   状态卡和 apikey 卡还活着——这就是"分块错误互不影响"。
5. **体验二次确认**：在数据同步页点"重启 stockdb"或"立即热更新"，观察 `ElMessageBox.confirm`
   弹窗；点"取消"时什么都不发生，点"确认"后看 `ElMessage` 的成功/失败提示——确认逻辑就是
   `try { await ElMessageBox.confirm(...) } catch { return }` 这几行。

## 验收记录（已通过项）

- [x] 前端 Vitest 全绿（7 文件 / 56 例；M3 起逐页补组件/逻辑测试）
- [x] `npx vite build` 通过（含手动分包：index 主包 16KB，vendor 三件独立缓存）
- [x] Python 229 全绿（补位页契约以 test_ops.py 断言为准）
- [x] 功能清单回归表 `docs/webui-spa/m2-regression-checklist.md` 通用要求 + 十页生成完成（逐项人工勾选随 NAS 实测）
- [x] 路由接线：`router/index.js` 10 个 path 懒加载指向对应视图，10 深链冒烟 200
- [x] 端到端冒烟：本地 app.py 静态服务（+ vite dev 通道此前已验证）
- [ ] NAS `pull && up -d` 后实测 + 新旧面板逐页并排核对（用户侧，Phase 5 收口时执行）

## 下个里程碑（M3）

路由接线让 10 页全部可达 → 逐页补前端组件测试（当前 7 个测试文件只覆盖 api/store/utils/nav，
页面逻辑还没有单测）→ 端到端双通道冒烟（`app.py` + `dist`；`vite dev` 代理）→ NAS 部署实测与
`/legacy` 回滚演练。ECharts 后续还可以做"主题切换时图表颜色联动"与按路由拆包优化。
