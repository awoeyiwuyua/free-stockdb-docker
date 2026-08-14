# free-stockdb Docker 部署（极空间 Q4）

free-stockdb 官方发行包是**静态链接的独立二进制**（服务端 `stockdb` + 更新器 `数据更新`），
本目录把它容器化到 Linux Docker（极空间 Q4），多架构镜像（amd64 + arm64）一次构建，
NAS 拉取时自动匹配自身 CPU 架构。

- 上游：https://github.com/hello245m/free-stockdb （MIT，fork 于 `awoeyiwuyua/free-stockdb-docker`）
- 本仓库定位：**只做 docker 化封装，不改上游 C++ 源码**，Sync fork 与上游零冲突

## 版本约定（重要）

- **镜像 tag = 上游 stockdb 发布包版本号**（当前 `0.3.1`）：workflow 手动触发时不填
  version 输入，就从 `docker/Dockerfile` 的 `ARG VERSION` 打 tag，如
  `ghcr.io/awoeyiwuyua/free-stockdb:0.3.1` 与 `:latest`。
- **webui 面板内部版本 = `WEBUI_VERSION`**（当前 0.5.1，见 `docker/webui/app.py`），
  仅用于面板显示，**不是镜像 tag**。二者是两个维度，不要混用。
- 迭代节奏：上游发新版 → 升 `ARG VERSION`（镜像 tag 跟着变）；webui 面板改动 →
  升 `WEBUI_VERSION`（面板显示）。compose 建议用 `:latest`，无需每次改配置。

## 目录说明

| 文件 | 作用 |
|------|------|
| `Dockerfile` | 多阶段（0.5.0 起**单镜像**）：下载官方发布包（服务端+同步器+pybao）+ SHA256 校验 + webui 运维面板，一容器含 stockdb(7899)+webui(8080) |
| `docker-compose.yml` | 单 service `stockdb`（端口 7899 + 8081，挂载 `./data` `./mydb`），不再挂载 docker.sock |
| `stockdb.conf` | 容器内配置模板：`server.ip: 0.0.0.0`；pidfile `/data/stockdb.pid`、log `/data/log.txt`；首次启动拷到 `/data/stockdb.conf` 供编辑 |
| `webui/entrypoint.sh` | 容器入口（0.5.0）：数据卷准备 → 可选首次同步（`STOCKDB_SYNC_FIRST=1`）→ 后台监督 stockdb 进程存活 → 前台循环拉起 webui（崩溃自动重启） |
| `.github/workflows/build-image.yml` | GitHub Actions：手动触发，buildx 构建 amd64+arm64 单镜像推 ghcr.io |

---

## 一、首次部署

### 1. 前置
- 极空间 ZOS 应用中心已安装 **Docker**，并支持「项目（compose v2）」粘贴
- 极空间局域网 IP（记为 `<NAS_IP>`），从极空间管理界面或路由器获取
- 外网可达 GitHub（构建镜像时）与数据源 `http://a.123128.xyz`（同步数据时）
- 确认 Q4 CPU 架构（`uname -m`：`x86_64`→manylinux-x64 / `aarch64`→alpine-arm64）——多架构镜像已自动覆盖，仅作知情

### 2. 构建镜像（二选一）

**A. GitHub Actions（推荐，需一次性配置）**
1. 生成 GitHub PAT（权限勾选 `write:packages`）→ 本 fork 仓库 `Settings → Secrets and variables → Actions` → 新增 Secret，名字 `GH_PAT`，粘贴 token
2. 仓库 `Actions` 页 → 左侧 `Build & Push stockdb image` → `Run workflow`（手动触发）
3. 等构建完成（约 5–10 分钟），镜像出现在 `ghcr.io/awoeyiwuyua/free-stockdb:0.3.1`

**B. 本机/极空间构建（备选）**
```bash
# Mac（已装 Docker Desktop）或极空间 Docker 内，在 docker/ 目录：
docker build -t ghcr.io/awoeyiwuyua/free-stockdb:0.3.1 .
# 极空间导入镜像：docker save ... | 极空间导入 tar
```

### 3. 极空间部署
1. 极空间 Docker → 项目 → 新建 → 上传/粘贴 `docker-compose.yml`，项目目录建议 `/vol1/docker/stockdb/`
2. compose 会在项目目录下自动创建 `data/`、`mydb/` 子目录（数据持久化）
3. **首次同步数据**（断点续传，一次可能数 GB，需较长时间，可反复运行）——二选一：

   **A. 有主机终端**（SSH / Docker 项目终端，同步须停服务）：
   ```bash
   # 首次：先同步再启动（服务未起，天然满足「同步须停服务」）
   docker run --rm -v "$PWD/data:/data" -v "$PWD/mydb:/mydb" \
     ghcr.io/awoeyiwuyua/free-stockdb:0.3.1 /bin/sh -c \
     "cd /data && /opt/stockdb/数据更新"
   ```
   （或直接跳过：启动后用网页一键热更新同步）

   **B. 无主机终端（只有图形界面）——容器 sync-first 模式（推荐给极空间）**：
   - 极空间 Docker → 项目 → stockdb 容器 → **编辑环境变量**，加 `STOCKDB_SYNC_FIRST=1`
   - **重启容器**：容器启动时先自动增量同步（此时服务未起，满足"同步须停服务"），
     同步完成后自动启动服务
   - 首次同步数 GB：**同步期间容器显示"运行中"但端口未监听属正常**，耐心等待；
     中途中断可再次重启续传
   - 同步完成后可删除该变量（或保留：每次重启都自动增量同步，数据保持最新，
     只是启动多花几分钟）

4. 启动服务：
   ```bash
   docker compose up -d stockdb
   ```
5. **验证（验收点）**：
   ```bash
   # 在局域网任意机器上：
   curl "http://<NAS_IP>:7899/?cmd=get&t=股票代码"          # 应返回全市场代码列表
   curl "http://<NAS_IP>:7899/?cmd=get&t=日k:600633:20260810" # 应返回日K
   docker compose ps                                        # stockdb 状态应为 running(healthy)
   ```
   webui 运维面板（同步管理 + 健康监控 + AI 查询）：浏览器打开 `http://<NAS_IP>:8081`
   （若 8081 被占用，改 `docker-compose.yml` 里 `8081:8080` 的宿主端口即可，
   容器内 8080 不变）。

### 3.5 本地迭代 webui（Mac 等有 python3 的机器）

`webui` 是纯 Python 标准库单文件应用，读功能（健康度/状态/查询/港股拉取）可完全在本地开发调试：

```bash
docker/webui/dev.sh            # 默认直连 Tailscale 上极空间的 100.66.1.1:7899
STOCKDB_HOST=192.168.1.5 ./docker/webui/dev.sh   # 指定其他 stockdb 实例
WEBUI_PORT=18080 ./docker/webui/dev.sh           # 换本地端口
# 浏览器打开 http://127.0.0.1:8080
```

- 本地数据（同步历史/日志）落在 `docker/webui/.dev-data/`（已 gitignore），不碰 NAS 数据卷
- 同步依赖容器内 `/opt/stockdb/数据更新`；本地无该二进制时同步接口自动降级为"不可用"提示——这些改动需推到 NAS 重建镜像后验证
- 改完 `app.py` 后重新构建单镜像（GH Actions / `docker build` 流程，见上文），极空间上 `docker compose up -d stockdb` 拉新镜像重启

**webui 功能速览**（轻量 NAS 运维控制台风格，深色 + 状态优先 + 响应式）：

| 页签 | 能力 |
|------|------|
| 数据同步 | 主状态区（数据是否最新 + 立即热更新 + 更多操作→停服同步备用）、同步进度（阶段/耗时/进度条）、自动同步设置卡（多时间点、仅交易日、失败自动重试）、数据概况（股票/ETF 数量、覆盖范围）、最近同步（桌面表格 + 手机卡片、失败原因展开）、日志（运行中/失败自动展开） |
| 系统 | 健康检查面板（行情服务延迟 / stockdb 进程 / 自动任务三卡）、存储空间条、运行信息（数据卷/进程状态/进程时长/节点）、运维工具（stockdb 日志、重启 stockdb，描边警告样式）、stockdb 不可控警告卡、开发工具（原始查询代理，直查 stockdb 任意表） |
| 私有存储（同步页子页签） | 港股日K 手动拉取（东财/腾讯，写入 `hk日k` 表）、AI 写入接口（自定义表，表名与上游保留表隔离） |

> **0.5.0 单镜像架构**：stockdb（7899）与 webui（8080）同容器，进程级控制（pidfile + SIGTERM），
> 不再挂载 docker.sock。webui 崩溃自动重启，不影响 stockdb 数据服务。
> 0.4.0 起 webui 已瘦身为**运维面板**（数据基座管理），行情/自选/K线等展示功能已移除。

> 同步主流程为**热更新**（同步器检测到新数据文件后自动重启 stockdb 加载新快照，
> 重启窗口约 1-2 秒）；**停服同步**为故障兜底（「更多操作 → 停服同步」）。

> A股休市表（`app.py` 的 `XSHG_HOLIDAYS`）取自 [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) XSHG 日历，数据截至 2026 年；官方次年放假安排公布后，用 `docker/webui/scripts/extract_xshg_holidays.py` 重新提取更新（webui 运行时零依赖，判定不依赖外部服务）。

### 4. 本地 ZCode 接入
只读 MCP server 已迁入本仓库 `docker/webui/mcp/stockdb_mcp_server.py`（纯标准库，连 `STOCKDB_HOST:7899`），
随 webui 镜像一起分发，由 webui 的 `POST /mcp` 路由承载（与 stdio 共用同一份 dispatch）。

现共 **9 个只读工具**：

| 工具 | 能力 |
|------|------|
| `get_kline` | A 股 K 线（日K/分钟K，支持 1m/1w/1M 周期、fq 复权、批量 codes、字段投影、limit） |
| `get_stock_list` | 全市场 A 股代码列表 |
| `get_adjust_factors` | 复权因子 |
| `get_market_snapshot` | 指定交易日多只股票的单日行情快照 |
| `get_board_open_effect_history` | 板块开盘效应历史（涨停股次日开盘溢价统计） |
| `get_indicators` | 技术指标计算（39 项，含 zhishu 指数） |
| `get_board_members` | 板块 ↔ 股票 双向查询 |
| `screen_stocks` | 全市场条件选股（板块过滤 + 指标金叉/死叉 + 流通市值 + 剔除 ST） |
| `get_mydb_data` | mydb 私有库只读（港股日K / AI 自定义表） |

> **pybao 依赖**：`get_indicators` / `get_board_members` / `screen_stocks` / `get_mydb_data`，
> 以及 `get_kline` 的复权（fq）、1m/1w/1M 周期、批量 codes 能力，均依赖容器内 pybao
> （镜像自带 `/opt/stockdb/pybao`，无需额外配置）。
> 本机 dev 开发（dev.sh）需把 macOS 版 pybao 放到 `/tmp/pybao_mac` 或设置 `PYBAO_DIR` 环境变量，
> 否则这几类能力返回**明确降级错误**（其余工具不受影响）。

**A. stdio（本机 ZCode）**
```bash
# 连通性自检（替换为你的 NAS 地址）：
STOCKDB_HOST=<NAS_IP> uv run python docker/webui/mcp/stockdb_mcp_server.py --self-check
# 通过后，在 ZCode Settings → MCP 加 stockdb-native，command 指向本文件：
#   "env": {"STOCKDB_HOST": "<NAS_IP>"}
```

**B. HTTP（NAS 容器部署，推荐）**
webui 容器已内嵌 `/mcp` 路由（无需单独 mcp 容器），走 8081：
```json
{"type":"http","url":"http://<NAS_IP>:8081/mcp"}
```
健康检查：`curl http://<NAS_IP>:8081/api/health`。

> **安全注意**：`/mcp` 与 webui 的只读查询接口一致，均无鉴权（面向内网信任环境）。
> 由于 webui 同时暴露同步/重启 stockdb 容器等写操作接口，若通过 `type:http` 把 webui
> 暴露给公网 agent，务必修整：改走 Tailscale（`100.66.1.1`）等内网地址、或在前加反向
> 代理鉴权，否则等于把可操控容器的高权限接口裸奔在公网。

---

## 二、日常数据更新（增量同步）

官方要求**同步期间停止服务**（`docs/DATA_SOURCE.md`）。0.5.0 起三种方式任选：

**方式 1（推荐，网页一键热更新）**：浏览器打开 `http://<NAS_IP>:8081` → 点「立即热更新」。
同步期间 stockdb 保持运行（reload 热重载零中断），同步失败自动重启进程兜底。

**方式 2（停服同步，故障兜底）**：网页「更多操作 → 停服同步」，webui 会停 stockdb 进程 →
运行同步器 → 重启进程，全程网页操作无需终端。

**方式 3（命令行/计划任务，极空间计划任务可定时）**：
```bash
# 停 stockdb 进程 → 增量同步（断点续传，可反复运行直到无新文件）→ 重启进程
docker compose stop stockdb
docker run --rm -v "$PWD/data:/data" -v "$PWD/mydb:/mydb" \
  ghcr.io/awoeyiwuyua/free-stockdb:0.3.1 /bin/sh -c "cd /data && /opt/stockdb/数据更新"
docker compose start stockdb
```
> 同步源在 `/data/sync_url.txt`（一行一个镜像根目录；`always` 后缀 = 每次都强制校验该源）。

---

## 三、上游版本升级

1. **拉上游**：本 fork 仓库 GitHub 页 → `Sync fork` → `Update branch`（上游发新版时）
2. **改版本号**：`docker/Dockerfile` 顶部 `ARG VERSION=0.3.1` → 新版号；**SHA256 必须同步更新**（上游 Releases 页面 `.SHA256.txt`）；若上游 tag 名变化，同步改 `ARG GH_TAG_ENCODED`
3. **重建镜像**：`Actions → Run workflow`（构建 `:新版本` + `:latest` 双 tag；旧 tag 保留）
4. **NAS 升级**：compose 已用 `:latest`，无需改配置，直接 `docker compose pull && docker compose up -d` 即拉取最新（`data/` 卷不动，数据不丢）。若想固定某版本，把 compose 里 `image: ...:latest` 改回 `:<具体版本>` 即可

---

## 四、回滚

```bash
# 把 compose 里 image tag 改回旧版本（如 :0.3.0 / :0.3.1），然后：
docker compose pull && docker compose up -d
# 版本 tag 一直保留在 ghcr，天然回滚点；:latest 仅指向最近一次构建
```

---

## 后续扩展（本版不启用，需求浮现后再加）

- **AI MCP 容器化**：官方 `调用方式/ai_mcp/stockdb_full_mcp.py`（需容器带 Python + pybao C 扩展），或继续用本仓库 `docker/webui/mcp/stockdb_mcp_server.py`（HTTP 只读，已随 webui 容器的 `/mcp` 路由承载，NAS 部署后走 `http://<NAS_IP>:8081/mcp`）
- **webui 增强**：0.4.0 起 webui 为运维面板（同步/健康/查询/私有存储），行情展示功能已移除；数据接入统一走 stockdb HTTP（7899）与 `/mcp` 路由。
- **定时同步**：极空间计划任务，或 webui 内加定时（后续版本）

## 风险与备忘

- 同步必须停服务，否则 LevelDB 文件可能损坏（官方明确要求）
- ghcr 国内拉取不稳：备选 `docker save` 导出 tar 到极空间导入
- 默认数据源 `http://a.123128.xyz` 走公网：不可达则改 `sync_url.txt` 指向内网镜像/自建源
- 镜像构建需 GitHub 可访问；如网络受限，走「备选：本机/极空间构建」路径

### 排障：容器一直重启（Restarting / Exit 循环）

`stockdb` 发行版的行为约束（已实测二进制确认）：
- **conf 必须作为命令行参数**：用法 `stockdb [-d] /path/to/conf`，不传 conf 会 `error loading conf file` 直接退出
- **pidfile/log 必须可写**：conf 里 `pidfile`/`output` 若指向只读路径（镜像层）会写失败退出
- **数据目录硬编码绝对路径** `/data`（行情）、`/mydb`（私有库），由 compose 卷挂载

entrypoint 已按上述约束实现（`cd /data` + 绝对路径 conf 启动）。若仍重启：
1. `docker compose logs stockdb` 看退出原因（conf 报错 / pidfile 报错 / 端口占用）
2. 端口被占：compose 里宿主端口换 `17899:7899`，访问改 `http://<NAS_IP>:17899`
3. 数据卷权限：确认 `./data` 目录容器可写（极空间共享目录需在 Docker 里映射为可写）
