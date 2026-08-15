# free-stockdb-docker

[free-stockdb](https://github.com/hello245m/free-stockdb) 的 Docker 容器化封装，定位为**本地量化数据基座**：把上游引擎稳定跑在 NAS 上，供研究侧与 AI 取数。本仓库不改上游 C++ 源码，Sync fork 与上游保持同步。

## 架构（单镜像，一容器两端口）

- `stockdb` 服务端（7899 行情 HTTP API）+ `数据更新` 同步器 + pybao + webui 运维面板（8080）
- 进程级控制（pidfile + SIGTERM），**不挂载 docker.sock**；webui 崩溃自动重启不影响数据服务
- 镜像 `ghcr.io/awoeyiwuyua/free-stockdb:latest`

## webui 运维面板（`http://<NAS_IP>:8081`）

- 前端为 **Vue 3 SPA**（0.6.1 起：LuCI 风格菜单树——总览驾驶舱 / 系统运维 7 子页（数据同步、私有存储、系统健康、诊断中心、日志中心、通知中心、MCP 观测）/ 模拟盘 3 子页，每页一职责；源码 `docker/webui/spa/`，构建产物随镜像分发，Node 仅构建期存在）
- 旧面板完整保留在 `/legacy`（逃生通道）；环境变量 `WEBUI_UI=legacy` 可把根路径整体切回旧面板，`spa`（默认）为新面板
- 数据同步：网页一键「立即热更新」（reload 零中断）/「停服同步」（故障兜底）+ 定时计划 + 趋势图
- 系统健康：数据最新日期 / stockdb 进程 / 存储 / 容器日志与重启；诊断中心一键体检（上游 GitHub/妙想 API/stockdb 服务/pybao/磁盘/交易日历）
- mydb 私有存储：港股日K 拉取（东财/腾讯）、AI 写入接口
- 模拟盘：固定策略合同（159915 目标仓位模型，妙想模拟盘接入，默认 trading_enabled=false）+ 审计报告 + 信号体检
- 查询台：直查任意表；`/mcp` 路由 = AI 取数入口（12 个只读工具：行情/复权/快照/指标/板块/选股/私有库/交易日历/状态/时点快照）

## 版本

- 镜像 tag = 上游发布包版本（`docker/Dockerfile` 的 `ARG VERSION`），面板版本 = `WEBUI_VERSION`（`docker/webui/app.py`），compose 用 `:latest`

## 文档

- 部署 / 日常更新 / 升级 / 回滚：[`docker/README.md`](docker/README.md)
- 前端重构方案（Phase 5 架构与实施路径）：[`docs/phase5-spa-plan.md`](docs/phase5-spa-plan.md)
- SPA 开发与学习导读（M0 起逐里程碑更新）：[`docs/webui-spa/guide-m0.md`](docs/webui-spa/guide-m0.md)
- 上游引擎能力（本地量化引擎 / 39 指标 / 五种调用方式）：[hello245m/free-stockdb](https://github.com/hello245m/free-stockdb)
