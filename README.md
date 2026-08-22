# stockdb-ai（AI 原生数据后端）

fork 自 [hello245m/free-stockdb](https://github.com/hello245m/free-stockdb) 的
**私有研究成果仓库**（原名 free-stockdb-docker，2026-08-16 更名 stockdb-ai）。
开发主线 = **本机 Windows 原生引擎模式**，docker 镜像封装为可选发布物（版本成熟后才构建）。

## 定位（2026-08-16 升级定稿）

**AI 原生数据后端**。核心产品 = **AI 友好的数据接口**：
- **MCP 服务器**（/mcp，56 个工具：53 只读 + 3 仓库（0.10.0 D12：warehouse_run_sql
  读写 SQL/list_tables/status），统一契约信封 + 8 错误码）——AI 客户端（Claude 等）直接接入
- **HTTP API**（webui 路由）——脚本/程序取数
- **打板情绪指标**（涨停池 → 开盘溢价 → 60 日分位 → 强弱标签，异源验收签字）
- **列式仓库层**（0.10.0 D12：日K 沉淀 Parquet + DuckDB SQL 分析/研究自建表，设计见
  `docs/design/warehouse.md`）

**数据层定位**：上游 free-stockdb 引擎是**数据层的一部分**（当前唯一的行情 provider），
不是架构依赖——数据层按多数据源抽象设计（0.9.2），上游引擎、mydb 自持存储、将来的
自建数据源都是可替换的 provider。webui 运维面板为辅助驾驶舱（冻结）。
定位与路线图见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

## 目录关系（本机）

| 目录 | 角色 |
|---|---|
| `C:\Users\75393\Desktop\stockdb` | 上游引擎运行时（`stockdb.exe` + LevelDB 行情 + pybao 扩展），**只读数据源**（数据层 provider 之一），不 git 化 |
| 本仓库 | 全部研究成果代码与文档，唯一版本化对象（分支 + PR） |

数据流：`应用层 → pybao 扩展 → 引擎 127.0.0.1:7899 → LevelDB（data/ 行情 + mydb/ 私有存储）`。
两个目录互不写文件。详见 [`docs/development-guide.md`](docs/development-guide.md)（运行配方 + 排查手册）。

## 仓库结构（0.9.1 起：四层架构框架）

- `stockdb-ai/` — 应用层（后端主体）：`config.py`（配置单一入口）+ `interfaces/`（接口层：
  web/ HTTP + mcp/ MCP）+ `services/ core/ storage/ ops/` 四层（0.9.2 搬迁，0.9.8 严格分层）
  + 打板模块 + 单测（261）
- `docker/` — 可选 docker 封装（`Dockerfile`/`docker-compose.yml`/`entrypoint.sh`）
- `docs/` — 文档区（索引见 `docs/README.md`：架构/开发/发布纪律等现行制度 + `design/` 领域设计
  + `acceptance/` 验收记录 + `history/` 历史归档）
- `.github/workflows/` — `test.yml`（PR 门禁）+ `build-image.yml`（镜像构建，仅手动、成熟后启用）
- `CHANGELOG.md` — 版本记录（版本号 = `stockdb-ai/config.py` 的 `WEBUI_VERSION`）

上游内容（`cpp/` 引擎源码、`pybao/` 扩展拷贝、`调用方式/` 文档、演示文件等）已从仓库移除，
需要时直接看原生目录或上游仓库。

## 版本

- 面板版本 = `WEBUI_VERSION`（`stockdb-ai/config.py`），发布流程见
  [`docs/release-policy.md`](docs/release-policy.md)
- 镜像 tag = 上游发布包版本（`docker/Dockerfile` 的 `ARG VERSION`）；镜像 `ghcr.io/awoeyiwuyua/stockdb-ai` **仅成熟版本发布**

## 文档

- 开发指南（目录关系 / 运行配方 / 排查手册）：[`docs/development-guide.md`](docs/development-guide.md)
- 应用层四层架构设计（0.9.1 框架 / 0.9.2 搬迁）：[`docs/design/application-layer.md`](docs/design/application-layer.md)
- 打板采集设计（含涨停判定终定口径）：[`docs/design/auction-collector.md`](docs/design/auction-collector.md)
- 部署台账：[`docs/deployments.md`](docs/deployments.md)
- docker 部署细节：[`docker/README.md`](docker/README.md)
