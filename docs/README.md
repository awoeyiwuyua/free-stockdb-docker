# docs 索引

> 一页看懂每份文档管什么。**现行制度**在上，**历史归档**在 `history/` 不再维护。
> 防漂移锚点：任何新需求先对照 `ROADMAP.md` §4「不该做什么」。

## 阅读顺序（按角色）

- **新接手/回顾架构**：ROADMAP（定位与边界）→ architecture（总纲）→ development-guide（本机怎么跑）
- **日常开发**：development-guide（配方与坑）+ release-policy（发版纪律）
- **改数据链路**：runtime-model（并发治理检查清单）+ 对应 design/ 文档
- **验收发版**：release-policy + acceptance/ 最新记录 + deployments 台账

## 现行文档

| 文档 | 职责 |
|---|---|
| `ROADMAP.md` | 定位/路线图/防漂移锚点（该做与不该做） |
| `architecture.md` | 架构总纲：边界/分层/决策 D1-D12 存档 |
| `development-guide.md` | 本机开发：目录关系（**代码在 stockdb-ai/，数据在仓库根 data/**）/运行配方/排查手册 |
| `release-policy.md` | 发布纪律：版本语义/流程/验证要求 |
| `runtime-model.md` | 后端并发治理：熔断/信号量/单飞 + 新功能运行时影响检查清单 |
| `data-source.md` | 上游数据源与镜像同步协议（manifest/SHA256） |
| `deployments.md` | NAS docker 部署台账（每次部署后更新） |
| `design/` | 领域设计：application-layer（四层，已实施存档）、auction-collector（打板口径）、research-store（0.9.5）、sdk-mcp-bridge（0.9.0）、warehouse（0.10.0 列式仓库） |
| `acceptance/` | 验收记录（异源签字/实测证据，如 warehouse-live-20260822） |
| `history/` | **归档不再维护**：0.6~0.8 时代过程文档（SPA 重构计划/导读笔记/回归清单/涨停口径 CSV 证据） |

## 常见困惑速查

- **找数据文件（Parquet/SQLite/jsonl）**：不在 `stockdb-ai/storage/`（那是数据层**代码**），
  在 DATA_DIR——本机开发 = 仓库根 `data/`（warehouse/ Parquet+DuckDB、research/
  SQLite、records/ jsonl），生产 = `/data` 卷
- **找发布流程**：`release-policy.md`；版本号在 `stockdb-ai/config.py` 的 `WEBUI_VERSION`
- **找某决策的为什么**：`architecture.md` §4 决策表（D1~D12）
