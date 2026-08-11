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
2. compose 会在项目目录下自动创建 `data/`、`mydb/`、`log/` 子目录（数据持久化）
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

## 后续扩展（本版不启用，需求浮现后再加）

- **AI MCP 容器化**：官方 `调用方式/ai_mcp/stockdb_full_mcp.py`（需容器带 Python + pybao C 扩展），或继续用本仓库 `scripts/stockdb_mcp_server.py`（HTTP 只读，NAS 部署后改 `STOCKDB_HOST` 即可，无需容器化）
- **webui 增强**：当前 webui 是纯标准库最小版（状态+同步+查询代理）；如需 K 线图表/多周期视图，在 `docker/webui/app.py` 的页面里扩展（ECharts 静态引入即可，无需后端框架）
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
