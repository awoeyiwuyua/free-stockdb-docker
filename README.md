# free-stockdb-docker

[free-stockdb](https://github.com/hello245m/free-stockdb) 的 Docker 容器化封装，定位为**本地量化数据基座**：把上游引擎稳定跑在 NAS 上，供研究侧与 AI 取数。本仓库不改上游 C++ 源码，Sync fork 与上游保持同步。

## 架构（单镜像，一容器两端口）

- `stockdb` 服务端（7899 行情 HTTP API）+ `数据更新` 同步器 + pybao + webui 运维面板（8080）
- 进程级控制（pidfile + SIGTERM），**不挂载 docker.sock**；webui 崩溃自动重启不影响数据服务
- 镜像 `ghcr.io/awoeyiwuyua/free-stockdb:latest`

## webui 运维面板（`http://<NAS_IP>:8081`）

- 数据同步：网页一键「立即热更新」（reload 零中断）/「停服同步」（故障兜底）+ 定时计划
- 健康监控：数据最新日期 / stockdb 进程 / 存储 / 同步能力
- mydb 私有存储：港股日K 拉取（东财/腾讯）、AI 写入接口
- 查询台：直查任意表；`/mcp` 路由 = AI 取数入口（9 个只读工具：行情/复权/快照/指标/板块/选股/私有库）

## 版本

- 镜像 tag = 上游发布包版本（`docker/Dockerfile` 的 `ARG VERSION`），面板版本 = `WEBUI_VERSION`（`docker/webui/app.py`），compose 用 `:latest`

## 文档

- 部署 / 日常更新 / 升级 / 回滚：[`docker/README.md`](docker/README.md)
- 上游引擎能力（本地量化引擎 / 39 指标 / 五种调用方式）：[hello245m/free-stockdb](https://github.com/hello245m/free-stockdb)
