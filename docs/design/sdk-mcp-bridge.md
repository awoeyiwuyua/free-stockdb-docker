# 设计文档：SDK API 整合 MCP（0.9.0 主线 M4）

> 用户需求（2026-08-16 拍板）：把上游 stock_sdk 的 45 个工具整合到 MCP。
> 事实基线：上游官方 MCP 服务器 `调用方式/ai_mcp/stockdb_full_mcp.py`（原生目录）
> 注册 **41 个 `@mcp.tool()`**（用户记忆的 45 口径）；`from stock_sdk import *`
> 实际暴露 70 个可调用对象（含类型/异常，纯业务 API 约 55 个）。
> 本设计 = 把这 41 个工具以**本仓库 MCP 契约体系**（信封/8 错误码/参数校验）整合，
> 禁止引入第二套口径。

## 1. 技术事实（已实测验证）

- 底层 API 是 C 扩展**远程代理**（`stockdb.pyd` 的 RemoteProxy → 引擎 7899 socket 执行），
  无 Python 签名；参数契约以 `stock_sdk` 顶层函数与上游 MCP 包装函数签名为准
- 依赖模块：`stock_sdk`（含 `from stockdb import *`）、`zb_core`、`zhibiao`——全部来自
  原生目录 `pybao/`（本机 PYBAO_DIR / docker /opt/stockdb/pybao），**无第三方依赖**
- 本机实测：`get_all_trade_days()` 0.52s、`get_security_info("000001")` 0.15s，
  远程通道可用（引擎在线时必须）
- 上游 MCP 包装函数（stockdb_full_mcp.py）返回 **字符串 JSON**（`_format_result`），
  无错误码体系、无契约信封——只作参数归一化参考，**不直接复用其返回格式**
- **2026-08-16 实现期实测**：41 工具全量本机引擎冒烟 **41/41 通过**（安全参数表）；
  `get_call_auction` 返回 `[{code, time, ...}]` 形态（time=ISO 时间戳），引擎内置
  历史竞价数据确认存在；MCP 注册数 = 12 现有 + 41 SDK = **53**（设计预估 54 按
  现有 13 计，实际现有 12）；信封 source="sdk" / contract="sdk-bridge-v1"

## 2. 目标

1. 41 个上游工具全量注册进本仓库 MCP（`mcp/stockdb_mcp_server.py`），工具名对齐上游
   （`get_bars` 等，去掉 stockdb_ 前缀以符合本仓库命名习惯？——**决策见 §5**）
2. 统一契约：`{envelope, data}` 信封 + 8 错误码 + 参数校验（沿用 `_call_tool` /
   `_apply_contract` 机制）
3. 无 pybao / 引擎不可达 → 明确降级（DEPENDENCY_UNAVAILABLE，与现有工具一致）
4. 与现有 13 工具重叠处**标注口径差异**，不合并、不替代（防口径漂移，0.8.14 教训）

## 3. 架构：上游函数复用 + 契约外壳（2026-08-16 策略拍板）

> 决策背景（用户问"是否重复造轮子"后拍板）：上游 `stockdb_full_mcp.py` 已实现
> 41 个工具的调用逻辑——**业务逻辑零重复**，直接复用其 `stockdb_*` 函数；
> 我们只做**契约外壳**（信封/错误码/截断/注册），契约层是本仓库 0.8.x 验收资产，
> 不可用上游 raw 形态替代。

```
docker/webui/mcp/
   ├─ stockdb_full_mcp.py   # 上游原文件拷贝（MIT，文件头注明来源与上游版本；
   │                        #   仅用其 stockdb_* 函数，不启动其服务器）
   ├─ sdk_bridge.py         # 新增契约外壳（纯标准库，~200 行）
   │     ├─ _SDK_TOOLS: {tool_name: ToolSpec}   # 41 个工具注册表
   │     │    ToolSpec = {name, description, params_schema, sdk_fn}
   │     ├─ 参数校验（逐工具 schema：类型/必填/枚举/日期格式）
   │     ├─ 调用封装：sdk_fn(**args, df=False) → 错误归一化（→ 8 错误码）
   │     └─ 结果契约化：原生 list/dict → 信封（known_at / source / errors）
   └─ stockdb_mcp_server.py # 现有 dispatch：_call_tool 增加 SDK 工具路由
```

- **上游文件拷贝策略**：从原生目录 `调用方式/ai_mcp/stockdb_full_mcp.py` 拷贝
  （docker 镜像无此文件，必须随仓库分发）；文件头注明来源与拷贝时的上游版本；
  上游升级后重拷 + 冒烟回归（§8）
- **调用约定**：所有上游函数调用强制 `df=False`（纯 JSON 契约，上游默认 df=True
  返回 DataFrame 不可序列化）；`panel` 仅显式 true 时返回 {code: rows}
- **懒加载**：`import stockdb_full_mcp`（→ stock_sdk）仅在首个 SDK 工具被调用时执行；
  无 pybao 环境可正常导入服务器（DEPENDENCY_UNAVAILABLE 降级）
- **单连接**：复用 `pybao_tools.get_sdk_client()` 连接管理，不新建连接池
- **参数 schema 来源**：上游 `stockdb_*` 函数签名 + 上游接口文档；RemoteProxy 无
  introspection，参数表在 `sdk_bridge.py` 显式维护（41 张表，可评审）

## 4. 工具全量清单（41 个，上游对齐）

| # | 工具名（上游函数） | 分类 | 关键参数 | 与本仓库现有工具重叠 |
|---|---|---|---|---|
| 1 | get_industry | 板块 | security, date | 无（新增） |
| 2 | get_data | 行情 | 底层 rd.get_data 全量 | get_kline（重叠，标注） |
| 3 | get_all_securities | 股票池 | types, date | get_stock_list（重叠，标注） |
| 4 | get_security_info | 基础 | code, date | 无 |
| 5 | get_trade_days | 日历 | start_date, end_date, count | get_trading_days（来源不同：引擎 vs 本地休市表，**口径标注**） |
| 6 | get_money_flow | 资金 | security_list, start_date, end_date, fields, count | 无 |
| 7 | get_ticks | tick | security, start_dt, end_dt, count, fields | 无 |
| 8 | get_last_tick | tick | security, count, fields | 无 |
| 9 | get_bars | 行情 | security, count, unit, fields, include_now, end_dt, fq_ref_date | get_kline（重叠，**复权口径差异标注**） |
| 10 | get_price | 行情 | security, start_date, end_date, frequency, fields, fq, panel, fill_paused | get_kline / get_market_snapshot（重叠，标注） |
| 11 | get_marginsec_stocks | 融资融券 | date | 无 |
| 12 | get_margincash_stocks | 融资融券 | date | 无 |
| 13 | get_mtss | 融资融券 | security_list, start_date, end_date, fields, count | 无 |
| 14 | get_extras | 辅助 | info, security_list, start_date, end_date, df, count | 无 |
| 15 | **get_call_auction** | **竞价** | security, start_date, end_date, fields | **打板采集（腾讯/东财）——潜在第三竞价源，§6 专项** |
| 16 | get_billboard_list | 龙虎榜 | stock_list, start_date, end_date, count | 无 |
| 17 | bk_get | 板块 | x, category, fields | 无 |
| 18-22 | get_fundamentals_valuation/income/generic/cash_flow/indicator_legacy | 基本面 | security, fields, date/count 等 | 无 |
| 23 | get_history_fundamentals | 基本面 | security, fields, watch_date, stat_date, count, interval, stat_by_year | 无 |
| 24 | get_valuation | 估值 | security_list, start_date, end_date, fields, count | 无 |
| 26 | get_fundamentals | 基本面（主） | security, fields, start_date, end_date, count | 无 |
| 27 | get_locked_shares | 解禁 | stock_list, start_date, end_date, forward_count | 无 |
| 28 | get_future_contracts | 期货 | underlying_symbol, date | 无 |
| 29 | get_dominant_future | 期货 | underlying_symbol, date, end_date | 无 |
| 30 | get_index_weights | 指数 | index_id, date | 无 |
| 31 | get_index_stocks | 指数 | index_symbol, date | 无 |
| 32 | get_all_alpha_101 | 因子 | date, code, alpha | 无 |
| 33 | get_all_alpha_191 | 因子 | date, code, alpha | 无 |
| 34 | alpha | 因子 | date, index, fq, alpha | 无 |
| 35 | get_factor_kanban_values | 因子 | （见上游签名） | 无 |
| 36 | MACD | 指标 | security_list, check_date, SHORT, LONG, MID, unit, include_now | 无（zhibiao 指标族） |
| 37 | get_factor_values | 因子 | securities, factors, start_date, end_date, count | 无 |
| 38 | get_factor_values_legacy | 因子 | 同上 | 无 |
| 39 | get_index_style_exposure | 指数 | index, factors, start_date, end_date, count | 无 |
| 40 | list_query_tables | 表查询 | root | 无 |
| 41 | run_query | 表查询 | root/table/fields/filter/order | 无（财务/债券/期权大表，§6 安全边界） |

## 5. 关键设计决策

1. **工具命名**：保留上游函数名（去 stockdb_ 前缀，如 `get_bars`）；与现有工具
   （get_kline 等）**不合并**——数据语义与口径不同源，合并必然引入口径漂移。
   文档中明确"同一数据域双工具并存"的适用场景（AI 按需选择）。
2. **信封**：`_apply_contract` 自动派生 `known_at`（用数据内最大日期）/`source`="sdk"/
   `errors`（8 错误码：NO_DATA / INVALID_ARGUMENT / DEPENDENCY_UNAVAILABLE /
   INTERNAL_ERROR 等）。
3. **大结果截断**：run_query / get_all_securities 等可能返回超大结果 → 统一
   `limit` 截断 + `truncated` 标记（对齐 get_point_snapshot 先例）。
4. **df/panel 参数**：上游默认 df=True/panel=False 返回 DataFrame——MCP 工具强制
   `df=False`（纯 JSON 契约），`panel` 仅允许显式 true（返回 {code: rows} 形态）。
5. **安全边界（run_query）**：只允许上游 SAFE_ROOTS（valuation/income/cash_flow/
   indicator/balance）+ RUN_QUERY_ALLOWED_TABLES 白名单（bond/finance/opt 表清单），
   路径正则校验（SAFE_PATH_RE 同上游）；**不开放任意表名**。

## 6. 专项：get_call_auction 与打板链路的第三竞价源

- 上游引擎内置竞价数据（`get_call_auction(security, start_date, end_date)`）——若返回
  内容为历史逐日竞价价（非 9:26 即时），则与现有腾讯/东财即时采集**互补不冲突**：
  - 即时采集（09:26，腾讯/东财）继续是当日决策源（现有契约不动）
  - 引擎竞价可作为**历史回填/对账的第三异源**（口径核对：采集 open_price vs 引擎竞价价）
- 验收动作（M4 实施时）：实测 get_call_auction 返回形态 → 与 09:26 采集 + K线 open
  三方对账 → 结论记入本文档与 auction-collector.md（若差异显著，另立"竞价异源对账"
  设计）

## 7. 实施顺序（全量 41 个，按依赖分组）

1. **基础组（5）**：get_security_info / get_all_securities / get_trade_days /
   get_industry / get_data —— 先打通 SDK 桥骨架与信封
2. **行情组（8）**：get_bars / get_price / get_ticks / get_last_tick /
   get_money_flow / get_mtss / get_extras / get_call_auction
3. **融资融券/龙虎榜（4）**：get_marginsec_stocks / get_margincash_stocks /
   get_billboard_list / bk_get
4. **基本面组（10）**：get_fundamentals 系列（6）+ get_valuation +
   get_history_fundamentals + get_locked_shares + get_fundamentals_continuously
5. **因子/指标组（8）**：alpha 系列（3）+ get_factor_values（2）+
   get_factor_kanban_values + get_index_style_exposure + MACD
6. **期货/指数（4）**：get_future_contracts / get_dominant_future /
   get_index_weights / get_index_stocks
7. **表查询（2）**：list_query_tables / run_query（安全白名单先行评审）

## 8. 测试与验收

- **单测（离线，mock SDK）**：41 工具 × 参数校验（非法类型/缺失/日期格式）、错误码
  映射（SDK 异常 → INTERNAL_ERROR / NO_DATA）、信封字段齐全、截断标记、降级
  （无 pybao → DEPENDENCY_UNAVAILABLE）——沿用 test_ops / mcp 测试夹具（_FakeRd 扩展）
- **本机冒烟（真实引擎）**：41 工具逐个调用（安全只读参数），核对返回形态与信封；
  `get_call_auction` 专项三方对账
- **契约验收**：MCP initialize 返回 tool_count = 13 + 41 = 54；AI 客户端可用性抽查
- **回归**：现有 206 单测全绿（SDK 桥不得影响现有工具路径）

## 9. 风险

| 风险 | 应对 |
|---|---|
| 上游 full_mcp.py 依赖 native_mcp 框架（import 副作用） | 实现时验证仅 import 函数不启动服务器；必要时剥离框架依赖（薄包装） |
| RemoteProxy 无签名 → schema 凭文档维护，可能有偏差 | 41 张参数表进设计评审；冒烟阶段逐个核对 |
| run_query 大表/深查询拖慢引擎 | 白名单 + limit + 超时（沿用 STOCKDB_TIMEOUT） |
| 上游 API 行为随引擎版本变化 | 重拷上游文件 + 冒烟回归（§8） |
| 与现有工具口径混淆 | §5.1 命名与文档标注 + 测试断言"双工具并存" |

## 10. 关联变更

- `docs/ROADMAP.md`：0.9.0 计划（M1 链路 + M4 SDK 整合）
- `docs/design/auction-collector.md`：M1 边界 c（missing_open 计数语义）更新
- `CHANGELOG.md`：0.9.0 版本记录
