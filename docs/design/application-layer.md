# 设计文档：应用层四层架构框架（0.9.1 立框架 / 0.9.2 搬迁）

> 用户拍板（2026-08-16）：0.9.1 先把四层框架搭好，0.9.2 进行搬迁。
> 背景：app.py 3266 行单体——接口/服务/存储/横切混居。逻辑上已有层（mcp/ 是接口层、
> board_metrics 是领域层），物理上没有墙。
> 原则：**0.9.1 不搬代码、不改行为**（224 单测保持全绿）；框架交付"图纸 + 墙"。

## 1. 目标架构（四层 + 横切）

```
stockdb-ai/
├── app.py                    # 入口：装配 + 启动（组合根，不属于任何层）
├── config.py                 # 配置集中解析（各层共享）
├── interfaces/               # 接口层（0.9.8 统一收拢；web/ HTTP + mcp/ MCP）
│   ├── web/                  #   HTTP 侧：routes.py（路由表）/ handlers.py（Handler）
│   └── mcp/                  #   MCP 侧：stockdb_mcp_server / sdk_bridge / pybao_tools
├── services/                 # 应用服务层（用例编排）
│   ├── __init__.py           # 层契约 docstring
│   └── auction_tasks.py      # 打板三用例 + 同步/日检（0.9.2 批次 4）
├── core/                     # 领域层（纯规则，不依赖任何外部）
│   ├── __init__.py
│   └── board_metrics / auction_metrics / auction_list / calendar_xshg
├── storage/                  # 基础设施层（数据读写；providers 多源抽象）
│   ├── __init__.py
│   └── providers/（free_stockdb / mydb_store）+ records + research_store
├── ops/                      # 横切关注点（各层共用）
│   ├── __init__.py
│   └── alerts / logging（0.9.2 批次 2）
└── test_*.py                 # 层依赖纪律测试（0.9.1 就位；不属于任何层）
```

## 2. 层职责契约（0.9.1 的"图纸"——现有代码归属映射）

| 层 | 职责 | 禁止 | 现有代码归属（0.9.2 搬） |
|---|---|---|---|
| **接口层** interfaces/（web/ + mcp/） | 收参数、校验、分发、组装响应（信封/错误码）；不碰业务 | 不写业务规则；不直接碰存储 | interfaces/mcp/stockdb_mcp_server.py（TOOLS/_call_tool/契约）；interfaces/web/handlers.py（Handler：do_GET/do_POST） |
| **应用服务层** services/ | 用例编排：拉数据→算→存；控制降级与告警触发 | 不写纯规则（公式）；不直接拼 SQL/键 | app.py 的 auction_run_collect/close/backfill、run_sync、auction_scheduler_loop 的任务体 |
| **领域层** core/ | 纯规则：涨停判定/指标公式/分位/日历；纯函数、可独立测 | **不 import 任何其他层**（接口/服务/存储都不行） | core/board_metrics.py、auction_metrics.py、auction_list.py、calendar_xshg.py（0.9.8 去 mcp/ 兼容转发） |
| **基础设施层** storage/ | 存取：mydb 读写、文件、外部 HTTP（引擎/腾讯/东财）、pybao 加载 | 不写业务规则；不知道"调用者是谁" | app.py 的 mydb_write/read/tables、stockdb_fetch、_auction_series_*；auction_collect.fetch_quotes；pybao_tools；sdk_bridge（部分） |
| **横切 ops/** | 日志/告警/调度/健康——各层可用 | 不承载业务用例 | app.py 的 log/tail_log、Alerts、notify_alert、scheduler_loop、health_status、_diag |

**依赖铁律（单向）**：接口层 → 应用服务层 → 领域层 / 基础设施层；领域层不依赖任何人。
禁止：core/ 与 storage/ 之间互相 import；任何层反向 import 接口层。

## 3. 0.9.1 交付物（框架本身）

1. **目录 + 空壳**：web/ services/ core/ storage/ ops/ 五个包，`__init__.py` 写层契约
   docstring（职责/禁止/依赖方向）；不建空文件（避免"为了结构而结构"）——只在
   各层 __init__ 里写明"0.9.2 将承载什么"
2. **层契约文档**：本文件 §2 映射表即契约（每个现有模块/函数 → 目标层）
3. **依赖纪律物理检查（核心交付）**：新增 `tests/test_layer_boundaries.py`——
   用 ast 静态扫描各层包内 import，断言：
   - core/ 不 import web/ services/ storage/ ops/ mcp/
   - services/ 不 import web/（可 import core/ storage/ ops/）
   - storage/ 不 import services/ core/（可 import ops/）
   - 违规即测试红（"墙"的物理保证，防未来越界）
4. **config.py 骨架**：0.9.1 先建文件 + 迁移**读取逻辑**（STOCKDB_HOST/PORT/PYBAO_DIR/
   AUCTION_*_TIME/DATA_DIR 的解析函数），app.py 改为 `from config import ...`——
   **唯一允许的"搬迁"**（纯提取无行为变化，224 测试兜底）
5. **测试**：新增层边界测试 + config 提取后全量回归 224 全绿

## 4. 0.9.2 搬迁计划（概览，批次数以实际为准）

| 批次 | 内容 | 验收 |
|---|---|---|
| 1 | config.py 完成装配接入（app.py 用 config） | 224 绿 |
| 2 | ops/：Alerts + notify_alert + log/tail_log 搬入 | 224 绿 |
| 3 | storage/：mydb_write/read/tables + _auction_series_* + stockdb_fetch 搬入（**含多源抽象，见 §4.1**） | 224 绿 |
| 4 | services/：auction_run_collect/close/backfill + run_sync 任务体搬入（app.py 留薄壳转发） | 224 绿 |
| 5 | core/：board_metrics/auction_metrics/auction_list/calendar_xshg 归位（mcp/ 留兼容转发或改 import） | 224 绿 |
| 6 | web/：Handler 路由表拆分 routes.py/handlers.py | 224 绿 |
| 7 | app.py 瘦身收尾 + 全量回归 + 本机引擎冒烟 | 224 绿 + 冒烟 |

每批独立 PR、独立可回滚；**不改变任何对外契约**（HTTP 路径/MCP 工具名/信封/错误码
一律不动——它们是"对外承诺"）。

## 4.1 storage/ 多数据源抽象（2026-08-16 用户拍板：上游只是数据层的一部分）

**原则**：应用层（services/）不感知数据从哪来——数据访问统一走 storage/ 的 provider
接口；上游 free-stockdb 引擎是第一个（当前唯一）行情 provider，mydb 自持存储是第二个。

```
storage/
├── providers/            # 数据源适配器（每个 provider 一个模块）
│   ├── free_stockdb.py   # 上游引擎（引擎 HTTP + pybao 扩展）——当前唯一行情源
│   └── mydb_store.py     # mydb 读写（研究成果自持：打板指标/序列/清单/快照）
├── __init__.py           # 对外统一入口（0.9.2 先保持函数直调，抽象接口随 M5 引入）
└── records.py            # 日检/运行记录（可观测性落点）
```

**0.9.2 落地范围（克制）**：搬迁期**只做目录归位 + 模块拆分**，不引入 provider 抽象
接口（那是 M5 的活）；但**文件边界按 provider 划分**——mydb 相关函数进
`mydb_store.py`、引擎访问进 `free_stockdb.py`，为 M5 的抽象接口留好墙。
M5（排期待定）再定义统一数据访问接口（如 `quote_provider` / `research_store`），
应用层切换数据源零改动。

## 5. 可观测性三件套（0.9.1 延后，随 0.9.2 落地）

原规划 A（打板日检）/B（调度探活+失败告警）/C（结构化日志）排 0.9.2，与搬迁同步：
ops/ 就位后日检记录落在 ops/records.py、告警补全落在 ops/alerts.py——**先有墙，再往墙里装仪表**。

## 6. 验收标准（0.9.1）

- [ ] 五包目录 + 层契约 docstring 就位
- [ ] config.py 提取完成，app.py 改用 config（唯一代码搬迁）
- [ ] tests/test_layer_boundaries.py 层依赖检查通过（含故意违规样例验证检查有效）
- [ ] Python 224 全绿（+ 新增层边界测试）
- [ ] 本机引擎冒烟（MCP 12+41 工具可用性抽查）
- [ ] WEBUI_VERSION 0.9.0 → 0.9.1；CHANGELOG 记录

## 7. 风险

| 风险 | 应对 |
|---|---|
| 空框架价值低（"为了架构而架构"） | 交付物含映射表+物理检查+config 提取——框架即图纸非空壳 |
| ast 检查误报（动态 import/别名） | 检查器只扫包内顶层 import 语句；误报率低，样例校准 |
| mcp/ 归属争议（接口层 vs 混合） | 0.9.1 不动 mcp/；0.9.2 批次 5 单独决策（board_metrics 迁 core 时） |
| config 提取引入回归 | 纯提取+测试兜底；若 config 解析有环境依赖（NO_PROXY 等）保持行为一致 |
