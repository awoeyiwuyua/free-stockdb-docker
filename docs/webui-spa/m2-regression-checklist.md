# M2 功能清单回归表（Phase 5 SPA 页面搬迁验收标准）

> 生成依据（2026-08-15）：旧面板 `static/legacy/index.html` 全部 API 调用逐条提取 +
> `app.py` 路由表（do_GET/do_POST）。M2 每页完成后逐项打勾，**任何一项未打勾不得宣称该页完成**。
>
> 重要发现：旧面板前端从未渲染过 **审计报告（/api/paper/audit）** 与 **信号体检
> （/api/paper/signal-status）** 的 UI（Phase 4.5 只交付了后端端点 + 测试）。这两页是
> M2 的**补位任务**，验收契约以 `test_ops.py` 中 payload 断言为准。

## 通用要求（每页适用）

- [ ] 加载态（skeleton）、空态（EmptyState）、错误态（错误码/中文降级文案）三态齐备
- [ ] 轮询页面统一 30s 节拍 + 手动刷新按钮；危险操作（重启/清空/交易开关）二次确认
- [ ] 隐私：apikey 永不回显（仅掩码 `****`）、交易写操作仅在 webui 前端发起

## 总览 `/overview`（新页，无 legacy 对照，契约 = /api/overview）

- [ ] 数据新鲜度卡：`health.latest / lag_days / status / note`，滞后着色（≤1 ok / 2 warn / >2 err）
- [ ] 告警摘要卡：`alerts.count` + `recent` 列表（级别/时间/文案）
- [ ] 模拟盘时间轴卡：`paper.timeline` + `next_runs` + 状态徽标（观察期/交易开启/暂停）
- [ ] MCP 健康卡：`mcp.total / ok_rate / p95_ms`
- [ ] 版本卡：`version.webui.version / stale / upstream.tag_name`
- [ ] 手动刷新 + `lastRefresh` 时间展示

## 数据同步 `/data/sync`（legacy「数据同步」页签 source 子页）

- [ ] 立即热更新 `POST /api/sync`（进度条 / 阶段 / 退出码 / 日志回显）
- [ ] 停服同步 `POST /api/sync`（故障兜底模式）
- [ ] 同步历史 `GET /api/history`（表格）
- [ ] 定时计划查看 `GET /api/schedule` + 保存 `POST /api/schedule?action=save`（时间点编辑、trading_only、空时间点校验提示）
- [ ] 状态总览 `GET /api/status`（container / sync_running / sync_phase / data_latest / code_stats / coverage / sync_cap / mirror / disk / scheduler_alive / trading_today）
- [ ] 同步日志 `GET /api/log?n=80`
- [ ] 容器日志 `GET /api/container/logs?tail=150`（展开/收起）
- [ ] 容器重启 `POST /api/container/restart`（危险操作二次确认）

## 私有存储 `/data/mydb`（legacy mydb 子页）

- [ ] 表清单 `GET /api/data/tables`
- [ ] 读取 `GET /api/data/read?table&key`
- [ ] 写入 `POST /api/data/write`（单条 key/value + 批量 items）
- [ ] 港股日K 同步 `POST /api/hk/sync`（codes/years 表单 + 结果反馈）
- [ ] 查询台 `GET /api/query?t=`（直查 stockdb 任意表，SQL 输入）

## 模拟盘 `/paper`

- [ ] 状态卡 `GET /api/paper/status`（configured / trading_enabled / paused / engine_available / masked_key / next_runs / scheduler_alive / modules_ok / reason 降级文案）
- [ ] 暂停/恢复 `POST /api/paper/pause`
- [ ] 手动单步 `POST /api/paper/run-now`（7 时点选择器，数据/交易时点分组提示）
- [ ] 账户总览 `GET /api/paper/overview`（余额 / 可用资金 / 持仓 / pnl / model_nav）
- [ ] 净值曲线 `GET /api/paper/snapshot?limit=60`（ECharts）
- [ ] 决策列表 `GET /api/paper/decisions?limit`（交易日/信号/目标/理由/状态）
- [ ] 订单列表 `GET /api/paper/orders?limit`（状态/数量/价格/时间）
- [ ] 事件时间轴 `GET /api/paper/events?limit`（过滤/明细）
- [ ] apikey 面板 `POST /api/paper/apikey`（保存/清除；**仅显示掩码**）
- [ ] 连通自检 `POST /api/paper/connectivity`（结果展示）

## 审计报告 `/paper/audit`（**legacy 无 UI，M2 补位**）

- [ ] `GET /api/paper/audit` 全字段渲染：replay_mismatches / duplicate_intents / illegal_transitions / slippage（统计 + 明细行）
- [ ] 净值 vs 基准双曲线（nav_series + benchmark_series，ECharts 双序列）
- [ ] 空库 / 数据库缺失降级态（全 0 / [] 不抛错）

## 信号体检 `/paper/signal`（**legacy 无 UI，M2 补位**）

- [ ] `GET /api/paper/signal-status`：exists / parsed / error / fields 摘要
- [ ] 7 项 checks 逐项渲染（history_count / current_rank / metric_value / formal_usable / contract_supported / known_at / previous_rank），通过/失败色区分
- [ ] 信号文件缺失降级提示（exists=false + error 文案）

## 系统 `/ops/system`（legacy「系统」页签）

- [ ] 健康卡 `GET /api/health`（latest / lag_days / mirror / status / note）
- [ ] 进程/存储/同步能力卡（status 内 container / disk / sync_cap 分块展示）

## 通知中心 `/ops/alerts`

- [ ] 告警列表 `GET /api/alerts?limit=200`（级别色条 / 时间 / 文案）
- [ ] 清空全部 `POST /api/alerts/clear`（二次确认）
- [ ] 顶栏红点联动 `GET /api/alerts/summary`（M1 已接，M2 保持）

## MCP 观测 `/ops/mcp`

- [ ] 统计总览 `GET /api/mcp/stats`（total / ok_rate / avg_ms / p95_ms，StatCard + ECharts）
- [ ] 按工具分布 `by_tool`（表格或柱状图）
- [ ] 调用明细 `GET /api/mcp/calls?limit=50`（时间/工具/状态/耗时/参数摘要）

## 版本 `/ops/version`

- [ ] `GET /api/version`（webui.version / image.tag / upstream / stale / msg / ui_mode）
- [ ] stale 高亮 + 上游 release 链接
- [ ] `/legacy` 旧面板入口链接（逃生通道可达性）

---

## 端到端验收（M2 全部勾选后）

- [x] 前端 Vitest 全绿（7 文件 / 56 例）
- [x] `npm run build` 通过（vendor 分包，index 主包 16KB）
- [x] Python 229 全绿
- [x] 本地冒烟（app.py 静态服务：10 深链 200 + vendor immutable 缓存；vite dev 通道此前已验证）
- [ ] 新旧面板逐页并排核对（`/legacy` 对照）
- [ ] NAS `pull && up -d` 后实测 + `/legacy` 回滚演练
