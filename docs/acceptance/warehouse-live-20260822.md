# 仓库层实数据验收记录 — 2026-08-22（0.10.0 发版门 · 第 1 次实测）

- 环境：本机 Windows 原生模式，引擎 127.0.0.1:7899（STOCKDB_HOST 需显式设置——
  MCP 独立默认 host 为 100.66.1.1，本次实测踩坑记录），数据落 `data/warehouse/`（0.10.0 治理批前为 .dev-data/）
- 方式：手动 `warehouse_run(days=3, reconcile_sample=30)`（等价 `POST /api/warehouse/run`），
  非调度线程自动触发（今日周六非交易日；自动链路的 ≥3 连续交易日窗口仍待观察）

## 1. 沉淀（写入路径）

| 交易日 | 状态 | 行数 | 市场 | 拒写 | 对账 |
|---|---|---|---|---|---|
| 20260819 | written | 5179 | sh/sz | 0 | 全绿（行数+30 只字段级） |
| 20260820 | written | 5178 | sh/sz | 0 | 全绿 |
| 20260821 | written | 5179 | sh/sz | 0 | 全绿 |

- 3 日总耗时 55.8s（≈19s/日，远低于 5 分钟预算）；watermark=20260821；codes=5179
- **观察**：引擎股票池仅沪深（无北交所标的），仓库忠实镜像；引擎日K 的扩展字段
  （turnover/pct_chg/amplitude/vol_ratio/total_mv 等）快照通道不透出，列为后续数据集候选

## 2. 读取路径（回验 + 第二通道抽查 + 性能门）

- v_daily 总量 15,536 行；按市场 sh 2302 / sz 2877（0821）
- **引擎第二通道抽查**：600000@20260821 逐字段（open/high/low/close/pre_close/volume/
  amount/name）vs 引擎 `日k:600000:20260821` 单点 get——**PASS（精确相等）**
- ta_ma(3)@600000：(None, None, 9.08)——窗口不满 NULL 语义正确，均值=mean(9.08,9.11,9.05) ✓
- 横截面单日聚合 5179 行 **1ms**；单标的指标查询 **2ms**；15.5k 行自连接聚合 **3ms**
  （性能下限 200ms/500ms/2s 全部大幅达标；全历史量级待回填后复测）
- SQL 写入面：research schema **修复后**直写可用（`research.spot` close>100 → 210 只）；
  修复内容：engine 初始化预建 research schema（实测首建前 CREATE TABLE research.x 报
  CatalogException，与工具文档不符——已修 + 测试覆盖）
- 横截面应用例：0821 成交额 Top3 = 中际旭创 266.9 亿 / 新易盛 207.9 亿 / 天孚通信 140.6 亿

## 3. 结论与遗留

- 写入/读取/对账/性能：**实测通过**（发版门第 1/3 交易日证据）
- 待办：① 调度线程连续 ≥3 交易日自动沉淀记录（下个交易周观察）；② 指标宏 vs pybao
  get_indicators 异源对账签字（需 ≥20 日数据，待回填或自然累积）；③ 全历史量级性能复测
  （回填后）
- 本机运行配方补充：`STOCKDB_HOST=127.0.0.1 NO_PROXY=127.0.0.1,localhost`（见
  development-guide 既有条目，本次再验证）
