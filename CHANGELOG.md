# CHANGELOG

本项目面板版本号 = `WEBUI_VERSION`（`docker/webui/app.py`），镜像 tag 跟随上游引擎版本。
发布纪律见 `docs/webui-spa/release-policy.md`；部署记录见 `docs/DEPLOYMENTS.md`。

## [0.8.2] — 2026-08-15（回填异步化）
- 修复：同步回填在请求线程内跑 60 天全市场扫描，连接中断即夭折且可并发叠加打满上游
- auction_run_backfill_async：后台线程执行 + 单飞防重（进行中再触发→拒绝）
- GET /api/auction/status 查询回填状态与日级守卫；Python 177 全绿

## [0.8.1] — 2026-08-15（打板序列冷启动修复）
- 新增 auction_run_backfill：历史 K 线重算过去 N 个交易日业务指标，初始化 打板序列（60 日分母）
  与逐日 打板指标（kline 口径 + 当日可得滚动分位，无未来函数）
- POST /api/auction/run {"task":"backfill","days":60} 手动触发，幂等可重跑
- 部署后先跑一次回填 → 周一 09:26 首次采集的分位当场成立；Python 175 全绿

## [0.8.0] — 2026-08-15（移除模拟盘：数据基座收敛）
- 用户拍板砍掉整个模拟盘：模拟盘模块（paper_core / paper_db / mx_client / paper_engine）移出镜像
  （Dockerfile 删除对应 COPY 行）；模拟盘 / 审计 / 信号验收文档标注「0.8.0 已移除」
- 并入 0.7.1 打包修复：auction 三模块（auction_collect / auction_metrics / auction_list）COPY 保留，
  打板竞价采集链路完整不受影响
- 原因：数据基座收敛——执行 / 研究移出基座，基座只保留「可信数据接口」（HTTP + MCP 12 只读工具）

## [0.7.1] — 2026-08-15（打包修复）
- 修复：Dockerfile 遗漏 COPY auction_collect/auction_metrics/auction_list → 镜像内 ModuleNotFoundError
- CI 加固：verify-pybao job 在镜像内同时 import 三个采集模块（打包缺口从源头拦截）

## [0.7.0] — 2026-08-15（打板竞价采集：数据基座首个自取能力）
- 新增打板竞价采集链路（设计：docs/design/auction-collector.md）：
  - 采集器 auction_collect.py：腾讯主源批量（≤50/批、限流 1req/s）+ 东财备源降级，9:25 竞价价=当日开盘价口径
  - auction_list.py：T-1 K线算"非一字板涨停"清单（5%/10%/20% 三档 + ST）
  - auction_metrics.py：业务指标（溢价均值/成功率）+ 滚动 60 交易日分位（语义对齐 emotion-v1）
  - 调度：09:26 采集（快照→业务值→分位落 mydb）；16:30 收口（同步校验→明日清单→对账回写→K线权威指标→序列追加）；POST /api/auction/run 手动触发
  - MCP get_board_open_effect_history 双源合并：历史 K线 / 当日 mydb 竞价快照，known_at 标注来源
  - mydb 保留前缀：竞价快照:/打板指标:/打板序列:/清单:（AI 勿写）
- 测试：Python 260 全绿（+20 采集/指标/清单用例）；前端 77 全绿（未改动）

## [0.6.6] — 2026-08-15（稳定性收官）
- 空载荷安全性全量加固：PaperSignal/OpsSync 等页在后端瞬时失败（载荷 null）时不再渲染崩溃
- 新增回归防线：10 个页面「所有 API 拒绝」状态下的挂载测试（views-null-safety.test.js，10 例）
- 前端测试 77 例 / Python 240 例全绿

## [0.6.5] — 2026-08-15
- 修复：/ops/sync 在 /api/status 失败（status=null）时裸读 sync_running 导致渲染异常（错误兜底捕获的真凶）

## [0.6.4] — 2026-08-15
- stockdb 上游访问闸口：熔断器（探针连续失败→降级 5 分钟）+ 信号量（并发≤8，超出降级不排队）
- 舱壁隔离：控制路径（同步校验/用户查询/基准）只过信号量，不受探针熔断牵连
- 新增《运行时模型》文档（Little's Law 三因子/依赖地图/失败矩阵/新增功能评审清单）

## [0.6.3] — 2026-08-15
- 修复多标签切页打瘫后端：data_latest_date 失败结果缓存 8s + 探测单飞锁 + 前端在途请求去重

## [0.6.2] — 2026-08-15
- 全局错误兜底：未捕获异常不再白屏，弹提示带错误文案（定位根因的窗口）

## [0.6.1] — 2026-08-15
- Phase 5.1 LuCI 风格面板重组：菜单树 3 组 11 页（一页一职责），诊断中心（/api/diag 一键体检）、
  日志中心（三源聚合+搜索）、数据同步趋势图、系统健康页（含环境信息卡）
- 全站密度收紧、顶栏精简、6 条旧路径重定向

## [0.6.0] — 2026-08-15
- Phase 5 SPA 重构（M0→M3）：前端重写为 Vue 3 + Vite + Element Plus + ECharts
- 十页搬迁（含旧面板缺失 UI 的审计报告/信号体检补位）、api 四域封装、EChart 按需封装
- 路由懒加载 + vendor 分包（index 主包 1.2MB→16KB）
- 双轨底座：/legacy 逃生通道 + WEBUI_UI 开关 + /api/overview 聚合
- 新增轻量测试门禁 test.yml（push/PR 只跑测试不建镜像）

## [0.5.6] — 2026-08-14
- Phase 4.5 运营支撑 + 面板优化：数据新鲜度告警、情绪投递状态卡、策略验收报告、
  全局状态条、通知中心、MCP 调用观测、模拟盘页增强、上游版本检查卡

## [0.5.0] — 2026-08-13
- Phase 4 模拟盘：固定策略合同（emotion-trend-159915-v1）、SQLite WAL 审计账本（9 表）、
  妙想模拟盘接入、7 时点时间轴、T+1、幂等键、trading_enabled=false 默认

## 更早
- 0.4.x：数据契约（统一信封/8 错误码/时点快照/交易日历）
- 0.3.x：MCP 工具集（12 只读工具）、screen_stocks、get_mydb_data
- 0.2.x：get_indicators（39 指标）、get_board_members、get_kline 升级
- 0.1.x：单镜像 Docker 封装（stockdb + updater + webui + pybao + MCP）
