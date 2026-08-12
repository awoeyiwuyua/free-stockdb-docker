# free-stockdb Docker 部署（极空间 Q4）

free-stockdb 官方发行包是**静态链接的独立二进制**（服务端 `stockdb` + 更新器 `数据更新`），
本目录把它容器化到 Linux Docker（极空间 Q4），多架构镜像（amd64 + arm64）一次构建，
NAS 拉取时自动匹配自身 CPU 架构。

- 上游：https://github.com/hello245m/free-stockdb （MIT，fork 于 `awoeyiwuyua/free-stockdb-docker`）
- 本仓库定位：**只做 docker 化封装，不改上游 C++ 源码**，Sync fork 与上游零冲突

## 目录说明

| 文件 | 作用 |
|------|------|
| `Dockerfile` | 多阶段：下载官方发布包 + SHA256 校验 + 静态二进制打包进 alpine |
| `docker-compose.yml` | `stockdb` 服务（常驻）+ `stockdb-sync`（一次性增量同步，sync profile）+ `webui/`（自建管理台，行情查询+一键同步） |
| `stockdb.conf` | 容器内配置模板：`server.ip: 0.0.0.0`（默认 127.0.0.1，容器内必须改才能映射）；pidfile/log 绝对路径落 `/data` 卷；首次启动拷到 `/data/stockdb.conf` 供编辑 |
| `entrypoint.sh` | 容器入口：切到 `/data` 可写卷 → 拷贝 conf/sync_url 模板 → 以绝对路径 conf 启动 `stockdb`（发行版要求 `stockdb /path/to/conf`） |
| `sync.sh` | 同步封装：切到 `/data` → 读 `/data/sync_url.txt` 数据源 → 运行 `数据更新` 增量同步 |
| `.github/workflows/build-image.yml` | GitHub Actions：手动触发，buildx 构建 amd64+arm64 推 ghcr.io |

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
2. compose 会在项目目录下自动创建 `data/`、`mydb/`、`research/`、`log/` 子目录（数据持久化）
3. **首次同步数据**（断点续传，一次可能数 GB，需较长时间，可反复运行）——二选一：

   **A. 有主机终端**（SSH / Docker 项目终端，同步须停服务）：
   ```bash
   docker compose stop stockdb
   docker compose --profile sync run --rm stockdb-sync
   docker compose start stockdb
   ```

   **B. 无主机终端（只有图形界面）——容器 sync-first 模式（推荐给极空间）**：
   - 极空间 Docker → 项目 → stockdb 容器 → **编辑环境变量**，加 `STOCKDB_SYNC_FIRST=1`
   - **重启容器**：容器启动时先自动增量同步（此时服务未起，满足"同步须停服务"），
     同步完成后自动启动服务
   - 首次同步数 GB：**同步期间容器显示"运行中"但端口未监听属正常**，耐心等待；
     中途中断可再次重启续传
   - 同步完成后可删除该变量（或保留：每次重启都自动增量同步，数据保持最新，
     只是启动多花几分钟）

4. 启动服务（方式 A 已含；方式 B 重启容器即完成同步+启动）：
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
   webui 管理台（行情查询 + 一键同步）：浏览器打开 `http://<NAS_IP>:18080`
   （若 18080 被占用，改 `docker-compose.yml` 里 `18080:8080` 的宿主端口即可，
   容器内 8080 不变）。

### 3.5 本地迭代 webui（Mac 等有 python3 的机器）

`webui` 是纯 Python 标准库单文件应用，读功能（行情/K线/健康度/自选/状态）可完全在本地开发调试：

```bash
docker/webui/dev.sh            # 默认直连 Tailscale 上极空间的 100.66.1.1:7899
STOCKDB_HOST=192.168.1.5 ./docker/webui/dev.sh   # 指定其他 stockdb 实例
WEBUI_PORT=18080 ./docker/webui/dev.sh           # 换本地端口
# 浏览器打开 http://127.0.0.1:8080
```

- 本地数据（自选/历史/日志）落在 `docker/webui/.dev-data/`（已 gitignore），不碰 NAS 数据卷
- docker 操控与同步依赖容器内 `/opt/stockdb/数据更新`，本地不挂载 docker socket，对应接口自动降级为"不可用"提示——这些改动需推到 NAS 重建 webui 镜像后验证
- 改完 `app.py` 后 `docker compose up -d --build webui` 可重新构建（也可用 GH Actions / `docker build` 流程，见上文）

**webui 功能速览**（轻量 NAS 运维控制台风格，深色 + 状态优先 + 响应式）：

| 页签 | 能力 |
|------|------|
| 概览 | 自选股快照（点击看 K 线）、加自选 |
| 行情 | ECharts K 线（日K/分钟K/前复权/后复权/MA）、原始查询代理 |
| 数据同步 | 主状态区（数据是否最新 + 立即热更新 + 更多操作→停服同步备用）、同步进度（阶段/耗时/进度条）、自动同步设置卡（多时间点、仅交易日、失败自动重试）、数据概况（股票/ETF 数量、覆盖范围）、最近同步（桌面表格 + 手机卡片、失败原因展开）、日志（运行中/失败自动展开） |
| 系统 | 健康检查面板（行情服务延迟 / Docker 连接 / 自动任务三卡）、存储空间条、运行信息（镜像/状态/时长/节点）、运维工具（容器日志、重启 stockdb，描边警告样式）、Docker 不可用警告卡 |

> 同步主流程为**热更新**（同步器检测到新数据文件后自动重启 stockdb 加载新快照，
> 重启窗口约 1-2 秒）；**停服同步**为故障兜底（「更多操作 → 停服同步」）。

> A股休市表（`app.py` 的 `XSHG_HOLIDAYS`）取自 [exchange_calendars](https://github.com/gerrymanoim/exchange_calendars) XSHG 日历，数据截至 2026 年；官方次年放假安排公布后，用 `docker/webui/scripts/extract_xshg_holidays.py` 重新提取更新（webui 运行时零依赖，判定不依赖外部服务）。

### 4. 本地 ZCode 接入
本机 `scripts/stockdb_mcp_server.py`（只读 MCP，连 `STOCKDB_HOST:7899`）：
```bash
# 连通性自检（替换为你的 NAS 地址）：
STOCKDB_HOST=<NAS_IP> uv run python scripts/stockdb_mcp_server.py --self-check
# 通过后，把 ~/.zcode/cli/config.json 的 stockdb-native 加 env：
#   "env": {"STOCKDB_HOST": "<NAS_IP>"}
```

---

## 二、日常数据更新（增量同步，须停服务）

官方要求**同步期间停止服务**（`docs/DATA_SOURCE.md`：同步应先停服务、完成后重启）。两步：

```bash
# 1. 停服务
docker compose stop stockdb
# 2. 增量同步（可反复运行直到无新文件；断点续传，数据只追增量）
docker compose --profile sync run --rm stockdb-sync
# 3. 重启
docker compose start stockdb
```

> 极空间可在「计划任务」里把上述三步做成定时脚本（如每日收盘后），实现自动更新。
> 同步源在 `/data/sync_url.txt`（一行一个镜像根目录；`always` 后缀 = 每次都强制校验该源）。

---

## 三、上游版本升级

1. **拉上游**：本 fork 仓库 GitHub 页 → `Sync fork` → `Update branch`（上游发新版时）
2. **改版本号**：`docker/Dockerfile` 顶部 `ARG VERSION=0.3.1` → 新版号；**SHA256 必须同步更新**（上游 Releases 页面 `.SHA256.txt`）；若上游 tag 名变化，同步改 `ARG GH_TAG_ENCODED`
3. **重建镜像**：`Actions → Run workflow`（构建 `:新版本` tag；旧 tag 保留）
4. **NAS 升级**：compose 里 `image: ghcr.io/awoeyiwuyua/free-stockdb:<新版本>` → `docker compose pull && docker compose up -d`（`data/` 卷不动，数据不丢）

---

## 四、回滚

```bash
# 把 compose 里 image tag 改回旧版本，然后：
docker compose pull && docker compose up -d
# 旧 tag 一直保留在 ghcr，天然回滚点
```

---

## 市场复盘 SQLite 研究库

WebUI 使用独立 SQLite 文件 `/research/market_research.sqlite3` 管理每日市场复盘。compose 将宿主机 `./research` 挂载到 `/research`；数据库生命周期由 WebUI 统一管理，不写入 StockDB 的 `./mydb`。

启动时 WebUI 会自动：

1. 创建数据库与表结构；
2. 启用 WAL、`synchronous=NORMAL`、30 秒 busy timeout 和外键；
3. 如果数据库为空且存在旧 `market_snapshot_*.json`，迁移最新一份有效快照并升级 schema；
4. 在系统页显示数据库路径、大小与最新日期。

每次市场研究重算使用一个 SQLite 事务写入：

| 表 | 内容 |
|---|---|
| `market_daily` | 每日核心市场指标及完整快照 JSON |
| `breadth_daily` | 每日上涨比例、MA20 广度、成交额、新高/新低 |
| `return_distribution_daily` | 七档涨跌幅分布 |
| `sector_daily` | 全部申万一级行业的中位涨跌、上涨比例与量能变化 |
| `methodology` | 按研究 Schema 版本保存的计算公式 |

常用查询：

```bash
sqlite3 ./research/market_research.sqlite3 \
  'SELECT date,up_ratio,ma20_ratio,breadth_gap_pp FROM market_daily ORDER BY date DESC LIMIT 20;'

sqlite3 ./research/market_research.sqlite3 \
  'SELECT date,industry,median_pct,up_ratio FROM sector_daily ORDER BY date DESC,median_pct DESC LIMIT 50;'
```

快照内的 `methodology` 字段保存计算口径：

| 指标 | 计算逻辑 |
|---|---|
| A/D | 上涨家数 ÷ 下跌家数 |
| 上涨－MA20 | 上涨家数占比 − 站上 MA20 占比，单位为百分点 |
| 1日/5日广度变化 | 当日上涨占比 − N 个交易日前上涨占比 |
| 较前5日/20日均额 | 当日成交额 ÷ 此前 N 日平均成交额 − 1；基准不含当日 |
| 涨跌分布 | ≥5%、2~5%、0~2%、平盘、0~-2%、-2~-5%、≤-5% |
| 行业强弱 | 申万一级成分股等权涨跌中位数；同时计算上涨比例与成交额日变化 |

`≥9.5%` 与 `≤−9.5%` 仅标记为“大涨/大跌”，不直接视为涨停/跌停，避免忽略 ST、科创板、创业板、北交所等不同涨跌停规则。

当前行情源不含独立指数表，`日k:000001:*` 是平安银行而非上证指数，因此复盘页不伪造三大指数卡片。接入真实指数表后再补充指数当日/5日涨跌、MA20/MA60 位置与量能。

---

## 后续扩展（本版不启用，需求浮现后再加）

- **AI MCP 容器化**：官方 `调用方式/ai_mcp/stockdb_full_mcp.py`（需容器带 Python + pybao C 扩展），或继续用本仓库 `scripts/stockdb_mcp_server.py`（HTTP 只读，NAS 部署后改 `STOCKDB_HOST` 即可，无需容器化）
- **webui 增强**：当前 webui 保持纯标准库后端，已包含市场复盘、因子排行、K 线与多周期视图；后续新增研究模块继续复用本地 ECharts 与 StockDB HTTP API。
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
