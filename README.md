# stockdb-private（free-stockdb 研究成果仓库）

fork 自 [hello245m/free-stockdb](https://github.com/hello245m/free-stockdb) 的
**私有研究成果仓库**（原名 free-stockdb-docker）。开发主线 = **本机 Windows 原生引擎模式**，
docker 镜像封装为可选发布物（版本成熟后才构建）。

## 定位（2026-08 定稿）

本地量化数据基座。核心产品 = **HTTP + MCP 数据接口**（只读、可信契约、可审计）
+ **打板情绪指标**（涨停池 → 开盘溢价 → 60 日分位 → 强弱标签，异源验收签字）。
webui 运维面板为辅助驾驶舱。定位与路线图见 [`docs/ROADMAP.md`](docs/ROADMAP.md)。

## 目录关系（本机）

| 目录 | 角色 |
|---|---|
| `C:\Users\75393\Desktop\stockdb` | 上游原生引擎运行时（`stockdb.exe` + LevelDB 行情 + pybao 扩展），**只读数据源**，不 git 化 |
| 本仓库 | 全部研究成果代码与文档，唯一版本化对象（分支 + PR） |

数据流：`私有代码 → pybao 扩展 → 引擎 127.0.0.1:7899 → LevelDB（data/ 行情 + mydb/ 私有存储）`。
两个目录互不写文件。详见 [`docs/DEVELOPMENT-GUIDE.md`](docs/DEVELOPMENT-GUIDE.md)（运行配方 + 排查手册）。

## 仓库结构（0.8.18 精简后：只保留研究成果）

- `docker/webui/` — webui 运维面板（`app.py`）+ 打板模块（`auction_collect/metrics/list`）+ MCP 服务（`mcp/`）+ 单测
- `docker/` — 可选 docker 封装（`Dockerfile`/`docker-compose.yml`/`entrypoint.sh`，构建从官方 release 下载引擎，不依赖仓库内上游源码）
- `docs/` — 设计文档（`design/auction-collector.md` 终定口径）、验收记录（`acceptance/`、`DEPLOYMENTS.md`）、发布纪律、SPA 指南
- `.github/workflows/` — `test.yml`（PR 门禁）+ `build-image.yml`（镜像构建，仅手动、成熟后启用）
- `CHANGELOG.md` — 版本记录（版本号 = `docker/webui/app.py` 的 `WEBUI_VERSION`）

上游内容（`cpp/` 引擎源码、`pybao/` 扩展拷贝、`调用方式/` 文档、演示文件等）已从仓库移除，
需要时直接看原生目录或上游仓库。

## 版本

- 面板版本 = `WEBUI_VERSION`（`docker/webui/app.py`），发布流程见
  [`docs/webui-spa/release-policy.md`](docs/webui-spa/release-policy.md)
- 镜像 tag = 上游发布包版本（`docker/Dockerfile` 的 `ARG VERSION`）；镜像 `ghcr.io/awoeyiwuyua/free-stockdb` **仅成熟版本发布**

## 文档

- 开发指南（目录关系 / 运行配方 / 排查手册）：[`docs/DEVELOPMENT-GUIDE.md`](docs/DEVELOPMENT-GUIDE.md)
- 打板采集设计（含涨停判定终定口径）：[`docs/design/auction-collector.md`](docs/design/auction-collector.md)
- 部署台账：[`docs/DEPLOYMENTS.md`](docs/DEPLOYMENTS.md)
- docker 部署细节：[`docker/README.md`](docker/README.md)
- 上游引擎能力：[hello245m/free-stockdb](https://github.com/hello245m/free-stockdb)
