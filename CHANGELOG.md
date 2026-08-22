# CHANGELOG

本项目面板版本号 = `WEBUI_VERSION`（`stockdb-ai/config.py`，0.9.1 起收敛至 config），
镜像 tag 跟随上游引擎版本。发布纪律见 `docs/release-policy.md`；
部署记录见 `docs/deployments.md`；本机目录关系与运行配方见 `docs/development-guide.md`。

## [0.10.0] — 2026-08-22（D12：列式仓库层——Parquet 事实沉淀 + DuckDB 读写仓库）

用户拍板的三层存储架构落地（设计 `docs/design/warehouse.md`；**显式推翻 0.9.4
「Parquet/DuckDB 不做」**，决策存档 architecture.md D12）：

- **存储持久层（W2）**：`storage/warehouse/{layout,sink,catalog}.py`——
  facts/daily 按年/市场/日分区（只增不改：临时文件+原子 rename、已存在即跳过）；
  adjust 版本化快照（snapshot 真实列，DuckDB 不解析文件名段 hive 分区——实测结论）；
  watermark 只前进，元数据唯一存 warehouse.duckdb meta（C4）
- **查询与计算层（W3）**：`engine.py/queries.py`——v_daily/v_adjust/v_daily_fq
  （ASOF 复权拼接）/v_codes + 指标宏 ta_ma/ta_rsi/ta_macd（窗口按 code 分区、
  窗口不满为 NULL）；`run_sql` 读写全开（用户拍板最大权限）+ 三护栏
  （单语句/facts 只读/行数上限+超时）
- **每日沉淀编排（W4）**：`services/warehouse_tasks.py` 第四条调度线程（16:40，
  就绪门 data_latest>=今日，未就绪 10 分钟重试至 20:00）；TRADED 快照 → sink →
  对账三板斧（行数/同源字段回读/异源开盘昨收）→ records+告警；services 经注入访问
  （C3，层边界测试增补）；HTTP 运维口 `GET /api/warehouse/status` +
  `POST /api/warehouse/run {"days":1-5}`（小范围测试通道，历史回填延后）
- **MCP 工具面（W5）**：warehouse 组 3 工具（run_sql/list_tables/status，
  工具数 53→56、组 6→7；C2 收敛：指标一律 SQL 宏不加单指标工具）；known_at=watermark；
  降级 DEPENDENCY_UNAVAILABLE（duckdb 缺失/WAREHOUSE_ENABLED=0 不伤既有工具）
- **依赖（uv 首次真正启用）**：duckdb 1.5.5 = 首个第三方依赖（不用 pyarrow）；
  CI 换 setup-uv + `uv sync --frozen`；Dockerfile amd64 pip install（与 uv.lock
  手动对齐），arm64 alpine 无 musllinux wheel → 不装走降级
- **C1 双轨收敛**：MCP `_http_get` 收敛为数据层闸口薄委托（free_stockdb.fetch 增
  base/block 参数——独立运行模式默认 host 透传、查询路径排队语义，行为保持）
- 回滚演练开关：`WAREHOUSE_ENABLED=0`（调度线程不启动，全链路无感知）
- **仓库治理批（代码/数据分区 + 文档重组）**：
  - `data/` 可见化——dev 数据目录由隐藏 `stockdb-ai/.dev-data` 上提仓库根（gitignore；
    dev.sh/gitignore/docker README 同步），仓库根四区自解释（代码 stockdb-ai/、数据 data/、
    文档 docs/、部署 docker/）；`storage/` = 数据层**代码**、`data/` = 数据落盘的关系
    写入 storage/__init__ 与 development-guide（消除语义误会）
  - DATA_DIR 按存储类型收纳——research.db 迁 `DATA_DIR/research/`（备份随行；旧根路径
    **粘性兼容**，NAS 升级无缝不自动搬移，避 WAL 伴生文件风险）；布局约定入 warehouse.md §2
  - docs 重组——历史过程文档（SPA 计划/导读笔记×4/回归清单/0.6.1 验收/涨停 CSV×4）
    归档 `docs/history/`；architecture/release-policy/runtime-model 上提 docs 根
    （runtime-model 顺带修 0.9.2 已搬迁的落点引用）；命名统一小写连字符
    （development-guide/data-source/deployments；ROADMAP 原名不动）；新增 `docs/README.md`
    一页索引；全量交叉引用同步（docs + 代码注释）
  - storage/ 代码层不动文件位置；__init__ 三处滞后修缮（补 warehouse/、删 0.9.1 状态行、
    悬空的「统一接口随 M5」承诺改为如实描述）；mydb_store 职责注释更新（0.9.5 后收窄）
- 发版门：310 测试全绿（W1→W5 每批验收通过 + 治理批路径测试）；**待实数据执行**：连续 ≥3 交易日
  沉淀对账签字（首日实测已过，见 docs/acceptance/warehouse-live-20260822.md）+ 指标宏 vs
  pybao 异源对账 + 性能下限回归（首日实测：横截面 1ms/单标的 2ms/自连接 3ms，大幅达标）
- 文档：warehouse.md 新增；ROADMAP/README/architecture/development-guide 同步

## [0.9.13] — 2026-08-17（帧交错剩余通道封死：指标计算与 rd 读写统一锁 + 线程栈诊断）

0.9.12 部署实证：点击多了仍卡顿 + RAM 不断膨胀 + 日志无异常——指向进程级冻结
（C 扩展持 GIL）而非崩溃/异常：

- **pybao 锁统一（根因）**：`_PYBAO_LOCK`（zhibiao.jisuan 指标计算）与 `_RD_LOCK`
  （rd.get/keys）此前是两把独立锁——但 zhibiao 指标计算与 mydb 读写复用同一引擎
  socket（zhibiao.rd / get_default_raw_rd 同源），两把锁分别串行仍会并发帧交错
  （2026-08-16 事故：协议帧交错 → C 扩展持 GIL → 全进程冻结 → 冻结线程不释放
  → RAM 膨胀）。统一为同一把锁：`_PYBAO_LOCK = _RD_LOCK`（同进程容器与 app 侧
  `mydb_store._rd_lock` 共享，全进程单锁，无死锁——RLock）
- **SIGUSR1 线程栈转储**：app.py 启动注册 faulthandler——冻结时执行
  `docker exec stockdb sh -c 'kill -USR1 $(pgrep -f "python /opt/webui/app.py")'`
  即可把全部线程栈打到 docker logs（定位卡点，无需重启）

Python 295 全绿。
## [0.9.12] — 2026-08-17（webui 并发风暴修复：有界 HTTP 并发 + 备份/SQLite 锁收口）

0.9.11 部署实证：点击多了 webui 全站请求超时（进程活着、无错误日志，请求排队/
阻塞累积而非崩溃）。三个确定性病灶：

- **HTTP 并发无上限**：ThreadingHTTPServer 每请求一线程，慢操作排队时线程无限
  堆积（最终新请求挂起、全站"无法访问"）→ 新增 `_BoundedHTTPServer`（信号量限
  并发 64，超限快速断开保活；`daemon_threads=True`；listen backlog 64），点击/
  客户端风暴不再拖死 webui
- **backup VACUUM INTO 持业务锁**：主连接 `self._lock` 内复制全库（NAS 磁盘慢时
  数十秒），期间 MCP 快速通道等全部 SQLite 访问阻塞排队 → 备份改独立连接执行
  （WAL 模式在线备份由文件级锁保证一致性，不占业务锁）
- **mydb_read 全表列取连续持锁**：500 键逐键 get 持 `_rd_lock` 全程（引擎慢时
  25s+），MCP 快速通道/打板任务等全部 rd 访问排队 → 改细粒度持锁（keys 一次、
  逐键各一次），面板展示对中间态不敏感
- **旧研究成果导入**：0.9.5 存储迁移（引擎 mydb → /data/research.db）后旧数据
  不自动导入，打板序列（60 日分位分母）从零积累约 60 个交易日——
  `migrate_from_engine` 改**仅补缺失**（不覆盖 0.9.11+ 新采集含 daily 的载荷），
  新增 `POST /api/research/migrate` 入口（幂等可重跑）

本地压力实测：100 并发 /api/status 全部 200 快速完成、0 tracebacks。
Python 295 全绿。

## [0.9.11] — 2026-08-17（整体迭代：循环导入致命修复 + 常见 bug 类型扫描收口）

0.9.10 部署实证 webui 启动即崩（循环导入），本轮整体迭代修复 + 按 14 类常见 bug
清单对全部模块做系统扫描（5 组并行审计 40+ 发现，全部 P1 + 高价值 P2 已修）：

**致命修复（0.9.10 部署回归）**

- **app.py ↔ handlers.py 循环导入**：Handler 从模块级移到 `main()` 延迟装配——
  脚本方式（`python /opt/webui/app.py`，容器 entrypoint）执行时 `sys.modules` 无
  `app` → handlers 顶层 `import app` 触发循环重载 → ImportError 启动即崩。模块
  导入方式（`import app`/测试）此前掩盖了该问题；0.9.11 起脚本方式实测启动通过，
  `test_research` 基线红随之修绿

**P1 并发/数据正确性**

- **pybao rd 单连接无锁并发**（2026-08-16 协议帧交错冻结事故同类）：`mydb_store._rd_lock`
  改 RLock 并收敛全进程——pybao_tools 新增加锁辅助 `rd_get/rd_keys`（同进程容器与
  app 侧共享同一把锁），`query_mydb`、MCP 快速通道/合并路径、research_store 迁移
  路径全部改经加锁访问；`_mydb_rd` 初始化、`research_store._connect`、
  `research_factory` 单例创建均入锁（消除双连接泄漏与双实例）
- **records.recent 遇缺日 break** → continue：跨周末/节假日/失败日后面板历史记录
  不再被截断到当天
- **screen_stocks indicator_cross 显式 date 窗口退化单日** → start 恒为 date 前推
  warmup_days（EMA/MACD/OBV 收敛窗口恢复，金叉/死叉信号不再失真/全无）
- **SDK 单日区间 start==end 空区间**（开区间契约）：`_fullmarket_sdk_outcomes` end
  顺延一日并客户端过滤——全市场单日快照不再整批空返回被静默判 SUSPENDED
- **config/pybao_tools 环境变量坏值崩溃**：`STOCKDB_PORT`/`WEBUI_PORT`/
  `STOCKDB_MAX_CONCURRENCY` 经 `_env_int` 回退告警（此前 import 即崩）；
  pybao_tools 坏端口不再废掉整个 pybao 子系统

**P2 健壮性/可观测性**

- 清单键跨周末失配：明日清单写"下一交易日"键（周五写周一键，此前恒走全市场兜底）
- 收口分位口径：rank 窗口 = 严格"此前 60 观测"（先算分位再 append，与竞价版一致，
  消除 1/60 偏差与提前出 rank）
- 序列读-改-写原子化（`_series_lock`）：收口 append 与回填整体覆盖互斥防丢更新
- 回填单飞"检查+置位"持锁原子（防并发双份分钟级全市场回填）
- 手动重跑采集不再抹掉当日已收口对账结论（reconciled 行保留）
- 休市表覆盖护栏（2027+ 未收录年份拒绝任务/快速通道回退慢路径，防休市日全 None
  指标行污染）
- 调度时间补零规范化（`9:26` 此前使采集 10 点后永不触发）
- handlers：请求体上限 16MB/负长度防护/socket 超时 30s（防读阻塞挂线程）、
  `_read_json` 非 dict/非法 UTF-8 防护、`_log`/`_container_logs`/`days` int 防护、
  `_hk_sync` 入参校验、`hot` 布尔显式解析、`_sync` 锁所有权移交防"假启动"、
  BrokenPipe 短路、500 不回显内部异常
- MCP：排序 key 容错（单条脏 date 不再中断整批 5000 只快照）、引擎通道仅补缺
  （迁移期遗留数据不覆盖权威 store）、响应 NaN/Inf → null 清洗（合法 JSON 输出）
- app：镜像日期失败也缓存（不再 7×24 无意义外呼）、data_coverage 全历史扫描单飞
  （多标签页不再各跑一份 5 分钟扫描）、code_stats 补 hk 键、MCP 模块导入失败留痕
- storage：mydb_read 复合键解析（`split(":", 1)` 保留代码段）、records.append
  捕获 TypeError（坏记录不再击穿任务）、migrate 全失败不再误标已迁移、
  backup 文件名唯一化 + SQL 引号转义
- 熔断器计数加锁 + 仅探针路径复位；entrypoint 进程身份校验（readlink /proc/pid/exe，
  防 stale pid 复用误判存活）；ops.log 写失败静默（不再炸调度线程）

Python 295 全绿（含 test_research 基线红转绿）；脚本方式启动实测（webui 200、
非法入参 400/回退、后台线程零 traceback）。

## [0.9.10] — 2026-08-17（打板读取链路工程化收口：键契约修正 + 预计算快速通道）

验收复盘（0.8.16 异源验收）：核心计算正确，但 HTTP MCP「能算、数也对，读取链路未
收口」——每次查询全市场重扫约 37s、带分布 20s 超时、写入键与读取键不对齐、15 个
空代码导致 `formal_usable=false`。本版按 P0/P1/P2 修复：

**P0 正确性与快速读取**

- **键契约统一（核心 bug）**：写入端（`打板指标:<YYYYMMDD>` 表 + `metrics` 键）与
  读取端（曾用 `打板指标` 表 + `<YYYYMMDD>` 键）不对齐，预计算指标读不到、异常被
  静默吞掉。读端改为双通道双键形：① research_store（0.9.5 抽象，写端同源，
  默认 SqliteResearchStore / mydb 回滚适配）② 引擎 mydb rd 直读（新契约
  `打板指标:<date>`/`metrics` 优先，旧契约 `打板指标`/`<date>` 回退，0.8.x 数据兼容）；
  快照读取同步精确键形（`竞价快照:<date>`/`code`）
- **预计算快速通道**：`query_board_open_effect_history` 在 `_HEAVY_LOCK` 之外先读
  预计算结果——区间内每个交易日都有 daily 行 → 直读返回（`load_path=mydb`、
  `cache_hit=true`，正常查询 <1s、带分布 <3s，不扫 5200 只）；任一交易日缺失才回退
  全市场重算（`cache_hit=false` + `fallback_reason` 注明），当日段仍用预计算覆盖
- **回归测试**：真实键形 mock（`打板指标:20260817`/`metrics`、`竞价快照:20260817`/
  `<code>`），键契约/快速通道/双键形回退/慢路径覆盖全绿

**P1 响应内容**

- **完整日级结果持久化**：09:26 竞价版与 16:30 K线版写 `payload.daily` 子载荷——
  样本数、正/平/负数量、均值、成功率、分位数、完整分布（open_return_pct +
  sample_codes）、候选数/采集数/缺价数（coverage）、known_at、value_source
  （`core.auction_metrics.build_daily_row`，与日K 行同构）
- **`include_distribution` 语义兑现**：false → 摘要（快速通道直读，<1s）；
  true → 直接读已持久化的样本分布（不再全市场扫描，<3s）

**P2 质量审计**

- **空代码分类**：15 个空代码不再一刀切——按 `query_point_snapshot` 时点判定分类为
  停牌（EMPTY_SUSPENDED）/ 退市未上市（EMPTY_DELISTED_OR_NOT_LISTED）/ 未发布 /
  无法分类（EMPTY_CODE_UNCLASSIFIED）；前两类不否决正式可用性，只有真实失败与
  未分类才否决
- **双覆盖率拆分**：`candidate_coverage`（候选识别完整性，决定 formal_usable）与
  信封 `sample_coverage`（赚钱效应样本覆盖：候选数/采集数/缺价数）；`formal_usable`
  不再被"区间无 bar 但分类为停牌/退市"的代码否决
- **信封增强**：`load_path` / `cache_hit` / `precomputed_days` / `fallback_reason` /
  `sample_coverage` / `empty_code_breakdown`；`source_contract_version` v4.1 → v4.2

Python 239 全绿（MCP 120 + 采集/领域 119）。

## [0.9.9] — 2026-08-16（D11 落位：采集执行迁数据层 storage/providers/quote_sources.py）

架构决策 D11 落地（采集执行归数据层、编排归服务层）：

- **`auction_collect.py` → `storage/providers/quote_sources.py`**（git mv，契约与行为
  完全不变）：腾讯主源批量 + 东财备源降级采集器归位数据层——文件边界按 provider
  划分（free_stockdb 引擎 / quote_sources 腾讯东财 / mydb_store 研究产出）
- **编排侧同步**：services/auction_tasks.py 改从 quote_sources 导入 fetch_quotes
  （惰性导入降级语义不变）；services/__init__.py 职责描述更新
- **测试随迁**：test_auction_collect → test_quote_sources（模块名对齐，用例全保留）
- **Dockerfile**：auction_collect.py 单独 COPY 行移除（storage/ 已整体 COPY，
  quote_sources.py 随 storage/providers 入镜像）
- CI：test.yml / build-image.yml unittest 命令 test_auction_collect →
  test_quote_sources；DEVELOPMENT-GUIDE 配方同步
- docs：application-layer 归属表、architecture D11 状态、ROADMAP 演进更新
- Python 261 全绿（D11 落位后重新验证）

## [0.9.8] — 2026-08-16（严格分层：接口层统一收拢 interfaces/，目录 = 架构图）

用户拍板（学习诉求：目录结构严格按"层"定义）：

- **接口层收拢**：`web/` + `mcp/` → `interfaces/web/`（HTTP）+ `interfaces/mcp/`（MCP）——
  目录树 = 架构文档 §3 分层表的物理投影（interfaces/services/core/storage/ops）
- **组合根与测试归位说明**：app.py/config.py（装配+配置，不属于任何层）与 test_*.py
  （横切验证）保持根目录——这也是分层的一部分（组合根不是层、测试不是层）
- **领域 shim 清零**：根目录 auction_metrics/auction_list + mcp/board_metrics/
  calendar_xshg 四个兼容转发删除；interfaces/mcp server 与 services 改 import core.*
  （领域逻辑单一真身）
- **sys.path 机制适配**：server 直接运行方式补插仓库根（core/ 可导入），包导入方式不变
- **层边界测试升级**：FORBIDDEN 集合 {web,mcp} → {interfaces}，接口层整体禁被下层依赖；
  新增 test_interfaces_clean（接口层可依赖一切下层）
- **Dockerfile 修正**（顺带发现）：此前只 COPY mcp/，web/services/core/storage/ops 均缺
  （镜像非主线未暴露）——0.9.8 按层整体 COPY 后端主体
- CI：unittest 命令 `mcp.test_*` → `interfaces.mcp.test_*`
- Python 261 全绿（严格分层后重新验证）

## [0.9.7] — 2026-08-16（目录重组：应用层上提 stockdb-ai/，docker/ 只留部署物）

用户拍板：仓库本质是**后端**（D1 AI 原生数据后端），webui 只是它暴露的 HTTP
面板接口之一——目录名与项目名一致，不再用局部视角的 webui 命名：

- **`docker/webui/` → `stockdb-ai/`**（git mv，历史保留）：app.py + 四层
  （web/services/core/storage/ops）+ mcp + spa + static + 全部单测，即后端主体
- **`docker/` 只留部署物**：Dockerfile / docker-compose.yml / entrypoint.sh /
  stockdb.conf / README
- **Docker 构建适配**：build context 改为仓库根（`COPY stockdb-ai/...`），
  新增根 `.dockerignore`（排除 .git/.venv/docs 等非镜像物）
- **引用全量同步**：43 处 `docker/webui` → `stockdb-ai`（README/docs/CHANGELOG/
  pyproject/CI/代码注释/测试头注释），grep 清零
- CI：test.yml working-directory 与触发路径、build-image.yml context 与
  cache-dependency-path 同步；release-policy 版本号位置更正（config.py）
- Python 261 全绿（路径搬迁后重新验证）

## [0.9.6] — 2026-08-16（应用层瘦身一：Handler 迁 web/handlers.py + 备份断言）

用户采纳建议 1a（拆分 app.py）与 3（备份断言）：

- **app.py 拆分**：HTTP Handler 类 + 静态服务辅助（_static_file/_ui_index/
  version_payload/_process_rss_mb）整体迁 `web/handlers.py`（AST 逐行搬迁，
  对外契约不变）；app.py 1791 行（拆分前 2412+），组合根职责收敛——
  依赖方向 app → web.handlers（app 末尾导入打破循环，handlers 顶部 `from app`
  集中取依赖）
- **patch 面修复**：DATA_DIR / fetch_upstream_release 改模块引用（app.* 动态
  解析），测试 patch.object(app, ...) 依旧生效（from-import 会在导入期拷贝
  引用导致 patch 失效——沿用 ops.DATA_DIR 教训）
- **备份断言**：新增 test_backup_file_independently_readable——备份文件独立
  连接打开、数据读回与备份时刻快照一致（VACUUM INTO 产物可离线恢复）
- 测试 +1；Python 261 全绿

## [0.9.5] — 2026-08-16（M5 研究成果产出自持：引擎死不影响研究数据）

架构总纲 D8 落地（设计 docs/design/research-store.md，PR #91 评审合并）：

- **SqliteResearchStore**（storage/research_store.py）：研究成果迁出引擎 mydb →
  自建 SQLite（DATA_DIR/research.db，WAL 模式 + busy_timeout 5s）；表：
  metrics / series / lists / snapshots / meta（迁移记录）；NaN/Inf 写前护栏沿用
- **ResearchStore 抽象接口**（用户 Repository 模式采纳）：write/read metrics・
  series・lists・snapshots + migrate_from_engine + backup；两个实现——
  SqliteResearchStore（主线）+ MydbResearchStore（回滚适配，语义方法映射旧路径）
- **工厂切换**：RESEARCH_STORE 环境变量（默认 sqlite；mydb = 回滚 0.9.4 行为），
  组合根注入 services（应用层只依赖接口，零改动感知——层边界测试新增
  services 禁 import storage.providers 物理规则）
- **一次性迁移**：migrate_from_engine 前缀枚举引擎 mydb 研究成果表（禁 keys("*")
  全表扫描）→ SQLite 事务批量；幂等可重跑；引擎数据保留不删（回滚预案）
- **读兼容**：get_mydb_data 研究成果表路由 research store（打板指标→metrics 等
  表名映射，返回结构与引擎路径一致——对外契约不变）；未知前缀仍走引擎
- **自动备份**：日检写盘后 VACUUM INTO backups/（保留 14 份，失败静默）
- **本机真实迁移验证**：引擎 mydb 60 天回填 → SQLite，读回基线逐位命中
  （08-14 = 47 / 0.0121129886 / 0.4893617、序列 60/60）；备份生成正常
- 测试 +11（SQLite CRUD/WAL/NaN 护栏/迁移幂等/备份保留/工厂切换/回滚适配/
  query_mydb 路由）；Python 260 全绿

## [0.9.4] — 2026-08-16（存储层优化：日检日度 Rotate + 写前 NaN/Inf 护栏）

用户存储层优化拍板（分级评估：Rotate/护栏做；SQLite WAL + Repository 接口 + 备份
随 M5；Parquet/DuckDB 明确不做、PostgreSQL 明确否）：

- **定位澄清（用户拍板）**：free-stockdb 引擎本地 LevelDB 即"自建行情存储"——
  架构红线精确化为"不重造行情获取管线"；Parquet 镜像冗余，M6 降级为纯备胎预案
  （architecture.md 已更新）
- **records 日度 Rotate**：日检按天分文件（records/YYYY-MM-DD.jsonl），天然按日期
  索引（Trace ID 检索友好），保留 90 天自动清理；recent 跨天扫描 + 兼容 0.9.2
  旧单文件
- **mydb_write 写前护栏（Pre-Commit Validation）**：递归检测 NaN/Inf 浮点——
  含脏数值的条目剔除并计数（skipped_invalid），不落盘（拦截污染数据，防 pybao
  序列化失败）；全部被拦截 → ValueError（调用方告警）；停牌日 open=0/None 属合法
  形态不误伤
- 测试：records 重写 7 例（日度/跨天/清理/兼容）+ 护栏 1 例；Python 249 全绿

## [0.9.3] — 2026-08-16（MCP 工具分组 Gateway + Trace ID 可观测性）

用户优化拍板（四维度评估：1a/4a 做，2a-2b/3a-3b/4b 记候选，Redis/队列不做）：

**1a MCP 工具分组（Gateway Pattern）**——解决 53 工具占满 LLM 上下文（约 5 万 tokens）：
- 全部工具带 `group` 元数据，6 组：market_data(20) / fundamental(11) / factor_analysis(10) /
  market_structure(7) / system_health(3) / research(2)（组名与描述见 TOOL_GROUPS）
- `/mcp?group=<组名>` 接入 = 只注册该组工具（tools/list 过滤）；不传 = 全量（向后兼容）
- dispatch/_handle_request 支持 group 参数（stdio 无分组）；sdk_bridge.SDK_TOOL_GROUPS
  维护 41 个 SDK 工具分组；HTTP 端点解析 query 传参

**4a Trace ID**——异常可按键诊断：
- tools/call 每次生成 trace_id（uuid4 前 12 位），贯穿 JSON-RPC result（顶层附加键，
  信封 8 键不动——对外契约未破坏）、工具调用日志（jsonl 加 trace_id 字段）
- 日检记录（storage/records）自动附加 trace_id——AI 可凭响应中的 trace_id 在
  /api/auction/daily 定位同一次执行上下文

验证：246 全绿（237 + 9 分组/Trace）；本机引擎冒烟（53 工具 6 组 + 分组过滤 + trace_id）

## [0.9.2] — 2026-08-16（四层搬迁完成 + 可观测性三件套之打板日检）

按 docs/design/application-layer.md 完成 7 批次增量搬迁（每批独立 commit、测试全绿、
对外契约零变化——HTTP 路径/MCP 工具/信封/错误码一律不动）：

| 批次 | 内容 |
|---|---|
| 2 | ops/alerts.py（告警中心）+ ops/logging.py（日志）从 app.py 迁出，app 保留同名转发 |
| 3 | storage/providers/mydb_store.py（mydb 读写 + 序列薄封装）+ free_stockdb.py（引擎闸口：熔断/信号量）——**多源抽象文件边界**（D3） |
| 4 | services/auction_tasks.py（打板采集/收口/回填 + 调度线程）——**组合根注入**（app.py 装配 query_snapshot/data_latest/is_fq_event/is_trading_day），服务层不 import 接口层（层纪律）；_auction_prev_trade_date 增 400 日防御上限 |
| 5 | 领域归位 core/：board_metrics/auction_metrics/auction_list/calendar_xshg（旧位置兼容转发）；修正 auction_list 越界 import（core 内同层引用） |
| 6 | web/routes.py 路由表外置（GET 18 项 + POST 7 项），Handler 按表分发；/api/auction/status 提取为方法 |

可观测性（三件套之 A 落地）：
- storage/records.py：打板日检 jsonl 存储（上限滚动/损坏容错）；采集/收口每次执行写
  结构化日检记录（ok/结果快照/reason）
- 新增 GET /api/auction/daily（最近 N 条日检，limit 1-100）
- 测试 +5（records 追加/读取/损坏容错/上限滚动/缺失）；Python 237 全绿
- 待后续版本：B 调度探活与失败告警补全、C 结构化运行日志全面化（ops/ 已就位）

app.py 3266 → 约 2400 行（告警/日志/mydb/引擎闸口/打板用例/领域/路由全部迁出）；
层边界检查（test_layer_boundaries）持续守护依赖方向。

## [0.9.1] — 2026-08-16（应用层四层架构：立框架，不搬代码）

用户拍板：0.9.1 先搭四层框架，0.9.2 搬迁（设计见 docs/design/application-layer.md）：
- **五层包骨架**：web/（接口层）/ services/（应用服务层）/ core/（领域层）/
  storage/（基础设施层）/ ops/（横切关注点）——每包 `__init__.py` 载明层职责、
  依赖纪律与 0.9.2 搬迁目标（现有代码归属映射）
- **依赖纪律物理检查**（框架核心）：`test_layer_boundaries.py` 用 ast 静态扫描各层
  import——core/ 零外部依赖、services/ 禁依赖接口层、storage/ 禁依赖业务层、
  ops/ 禁依赖用例层；违规即测试红（防未来越界）；含检查器有效性样例
- **config.py 配置单一入口**（唯一代码搬迁）：STOCKDB_HOST/PORT、DATA_DIR、
  LISTEN_PORT、STOCKDB_PIDFILE/PAUSE/LOG_FILE、WEBUI_VERSION、打板调度触发点、
  并发闸门从 app.py 收敛至 config.py；app.py 改为引用 config，行为不变
- 版本号 WEBUI_VERSION 收敛到 config.py（0.9.0 → 0.9.1）；CI 测试命令加入层边界检查
- 验证：Python 232 全绿（224 + 8 层边界）；本机引擎冒烟（MCP 53 工具可用）
- 0.9.2 计划：7 批次增量搬迁（ops→storage→services→core→web）+ 可观测性三件套

## [0.9.0] — 2026-08-16（功能里程碑：SDK 41 工具整合 MCP + 打板链路语义修正）

**M4：上游 stock_sdk 41 工具全量整合进本仓库 MCP**（用户拍板核心需求，设计见
`docs/design/sdk-mcp-bridge.md`）：
- 策略（用户拍板"不造轮子"）：拷贝上游 `stockdb_full_mcp.py` 进 `stockdb-ai/mcp/`
  （MIT，文件头注明来源与版本），复用其 `stockdb_*` 函数；新增 `sdk_bridge.py`
  契约外壳（纯标准库）：参数 schema 自动生成（inspect.signature + docstring）、
  df/panel 强制 JSON 形态、三级结果解析（json → literal_eval → 原文）、大结果
  截断 + truncated、8 错误码映射、无 pybao 时 DEPENDENCY_UNAVAILABLE 降级
- 工具清单：行情/竞价（get_bars/get_price/get_ticks/get_call_auction 等 10 个）、
  融资融券/龙虎榜/板块（5 个）、基本面/估值/解禁（11 个）、因子/alpha/MACD/指标
  （9 个）、期货/指数（4 个）、财务/债券/期权表查询（run_query 白名单安全边界，
  2 个）——MCP 注册数 12 → **53**
- 契约：SDK 工具族信封 source="sdk" / contract="sdk-bridge-v1"；known_at 兼容
  ISO/8 位日期；`_CONTRACT_BY_TOOL` 自动注册
- 验收：**本机引擎 41/41 全量冒烟通过**（安全参数表，含 get_call_auction 返回
  [{code,time}] 形态确认——引擎内置历史竞价 = 打板链路潜在第三异源，三方对账
  待 08-17 采集首跑后执行）；单测 +16（降级/schema/调用封装/错误映射/信封/
  集成）；Python 224 全绿

**M1：打板链路语义修正（边界 c）+ 采集/收口就绪**
- missing_open_count 计数语义落地：板日涨停但指标日无 (open, prev_close) 有效对
  的股票计入计数（0.9.0 前静默丢弃）；守恒检查 `候选 = n_samples + missing_open_count`
  （回填 + 收口两路径）；不影响指标值；单测 +2（回填/收口守恒）
- 08-17 周一起 09:26 采集 / 16:30 收口将在本机原生模式真实首跑（含对账偏差监测）；
  回填数据基线已在本机引擎重跑核对（08-14 = 47 / 0.012113 / 0.4894、序列 60/60）

**其他**：修复 PR #81 head 误用导致空 diff 的失误（重新以正确 head 合并 PR #82）；
`log()/tail_log()` 改为动态读 DATA_DIR（原用 import 时求值的 SYNC_LOG 常量——测试
patch DATA_DIR 后仍写默认 /data，CI Linux 不可写导致收口路径 PermissionError，
本地 Windows 因 C:\data 可写从未暴露）；上游文件维护约定：升级后重拷
stockdb_full_mcp.py + 冒烟回归（设计文档 §8）

## [0.8.18] — 2026-08-16（仓库治理：只留研究成果 + 原生模式为主线 + docker 降级可选）
- 方向（用户拍板）：① 明确两个目录关系；② 精简仓库只保留研究成果；③ docker 镜像非主线
- 新增 `docs/development-guide.md`：本机两个目录（原生引擎运行时 vs 研究成果仓库）职责
  边界、数据流、运行配方（STOCKDB_HOST/PYBAO_DIR/NO_PROXY）、已知坑排查手册
- 删除上游继承内容（git 历史可找回）：`cpp/`（引擎 C++ 源码）、`pybao/`（扩展拷贝，
  docker 构建从官方 release 下载不依赖）、`调用方式/`、`数据网页版.html`/`更新运行图.png`/
  `数据库运行图.png`/`网页版示范.png`/`先看！这个！！使用说明.txt`、顶层 `stockdb.conf`/
  `sync_url.txt`；保留 LICENSE（上游 MIT，合规必需）与 `docs/data-source.md`（同步源机制说明）
- README 重写为研究成果仓库定位（native-first）；docker/README 的 `调用方式` 引用改上游链接
- 发布纪律修订（release-policy.md）：主线 = 本机原生模式验证 + tag；镜像 = 可选发布物
  （仅成熟版本手动 build-image.yml）
- 本机验证：206 单测全绿；60 天打板回填在本机引擎重跑，验收基线全命中
  （08-14 = 47 / 0.012113 / 0.4894、序列 60/60）——Windows 原生模式接续成立

## [0.8.17] — 2026-08-16（同步链路修复：认证失败识别 + None>0 崩溃）
- 事故：命理档案触发手动热更新 → 数据源 auth failed（同步器退出码 0）→ webui
  崩溃 "'>' not supported between instances of 'NoneType' and 'int'"（exit_code=-1）
- 根因一：`counts.get("downloads", 0) > 0`——同步器未打印"待下载资源数"时
  downloads=None（key 存在），dict.get 默认值不生效 → None > 0 抛异常
- 根因二：同步器对认证/连接失败返回退出码 0，webui 无失败识别 → 误入验证流程
- 修复：
  - `_sync_failure_reason(stdout)`：识别 auth failed / 连接失败 → 明确 fail_reason
    + 跳过验证与重启（数据保持原状，恢复认证后重试）
  - downloads=None 比较安全化（None 走保守重启分支）
- 测试 +4（auth/连接失败识别、正常输出、None 比较回归），Python 206 全绿
- 待办（上游侧）：数据源 a.123128.xyz 认证恢复后重跑同步；补齐 002310 05-06~05-12
  缺失历史（命理档案样本外复验 1 条差异的唯一根因）；候选计数语义（板日涨停/
  指标日无 bar 计入 missing_open）后续修正

## [0.8.16] — 2026-08-16（验收 CSV 首日边界修正 + MCP server_version 同步）
- 0.8.15 复验：线上 lag-close 主路径抽查通过（05-25 板日 05-22：候选 114 = 同花顺
  115 - 1 ST - 3 一字板）；验收 CSV 87 条不一致全部集中在窗口首日 05-22
- 根因：导出脚本交易日表从 05-22 起且 enumerate 负索引——days[-1]（08-14）被当
  作 05-22 的前一日，lag close 全错 → 成组翻转（18 条非涨停进找回、69 条真涨停
  进剔除）。服务端主算法用日历 prev_trade_date 无此问题
- 修复：导出脚本交易日表前移一天预热 05-21 + 排除 i=0 负索引
- 修正后清单：找回 **286**（241 已确认 + 45 条 05-22 新增）、剔除 **4**（与命理
  档案确认的 000937/601128/301023/002083 逐位一致）
- MCP server_version 0.1.0 → 0.8.16（与 WEBUI_VERSION 同步，命理档案建议）
- Python 202 全绿；部署后 initialize 返回 serverInfo.version=0.8.16

## [0.8.15] — 2026-08-16（涨停判定参考价改为 lag close：命理档案验收修正）
- 事故：0.8.14 统一因子反推被命理档案验收驳回（严重不通过）——517 条"剔除"中
  **513 条误删**（真实涨停被删），104 条找回正确
- 根因：污染不均匀——同一只股票（如 000012）部分历史行 pre_close 是真实前收
  （未污染），但因子表有未来事件 → 0.8.14 无条件乘 cum_latest/cum_D 放大参考价
  → 真实涨停 4.40 被误判非涨停（000012 05-22 实证）
- 正确口径（命理档案方案，0.8.15 实施）：
  - **普通日：参考价 = 上一实际成交日未复权收盘（lag close）**——与污染与否无关，
    close 字段是未复权真实价，天然免疫污染
  - **除权日（因子表当日有事件）：参考价 = 当日 pre_close**（法定除权参考价，可信）
  - 停牌跨日/lag 缺失：原值兜底
- 实现：MCP 全市场快照组装用 history_close 逐日追踪（已有逻辑）+
  `pybao_tools.is_fq_event_date`；app.py 回填/收口/兜底改为
  `_auction_apply_reference(points, date, lag_close_by_code)`（回填多拉一日 t2 快照；
  收口复用 0.8.13 的 prev_points）
- 全量实测（60 日 vs 污染判定）：找回 **259**（0.8.14 的 104 ⊆ 259，接近命理档案
  281 口径）、剔除 **73**（含命理档案确认的 4 条真误收；**除权日误剔 0**）
- 0.8.14 的 rebuild_limit_reference_price/get_fq_cum 保留兼容（不再用于主路径）
- 测试 +2（除权日保持 pre_close / lag 提取），Python 202 全绿
- 部署后重跑回填；重新导出找回/剔除清单供命理档案复验（同花顺终态池为最终裁判）

## [0.8.14] — 2026-08-16（涨停判定参考价重建：pre_close 除权污染修复）
- 事故：命理档案异源核验（同花顺历史终态涨停池 + 未复权 OHLC，20260522~20260814）
  发现 318 个候选差异——基座漏判真涨停、误收伪涨停；我此前 60 天交叉核对为
  "同源自洽"（基座存储 vs 生产工具同源实现），掩盖了该问题
- 根因：引擎历史 K 线 pre_close 被**未来除权除息按最新复权因子回溯重算**
  （000100 实测恒低 1.92%；000026 恒低 0.55%）——涨停判定直接用 pre_close
  系统性漏判/误收（除权股），且污染方向恒为"调低"（cum 单调递增）
- 实测验证污染模型：pre_close_engine(D) = 真实法定参考价(D) × cum_D / cum_latest
  - 000100：4.207 × 3.079/3.019 = 4.289 ≈ 真实昨收 4.29 ✓；除权日 4.58 原样 ✓
- 修复（三路径统一）：
  - 新增 `rebuild_limit_reference_price`（board_metrics）：ref = pre_close × cum_latest/cum_D
  - 新增 `pybao_tools.get_fq_cum(code, date)`：SDK 预加载因子表二分查询
  - 回填/收口/采集兜底（app.py `_auction_fix_limit_reference`）+ MCP 全市场快照组装
    （query_fullmarket_daily_snapshot）判定前重建参考价
  - 未除权股票（无因子事件）原样通过；因子表不可用降级原值
- 全量实测（5182 只因子表 + 60 日逐日重算）：找回漏判涨停 **104**（78 只）、
  剔除误收 **517**（除权日误剔 **0**——除权日参考价保持正确，33 个除权日真涨停
  不受影响）；抽查 000668 案例：真实参考价下 close=20.59 非涨停（+9.2%）✓
- 字段契约（对齐命理档案建议）：涨停判定专用"当日法定涨跌停参考价"（重建值），
  不再直接使用模糊的 pre_close；pre_close 仅作原始证据保留
- 测试 +4（重建公式/因子查询/应用验证/降级），Python 200 全绿
- 部署后重跑回填；验收基准 = 命理档案同花顺终态池（异源外部权威）

## [0.8.13] — 2026-08-16（溢价分母 = T-1 收盘价：除权除息日口径修复）
- 事故：0.8.12 部署回填后交叉核对（存储 vs 生产工具）60 天中 9 天均值不一致
  （样本数 60/60 一致 → 不是涨停池问题，是溢价数值问题）
- 根因：溢价分母误用 T 日 bar 的 pre_close 字段——除权除息日该字段是交易所
  **调整后昨收**（如 000100：T-1 实际收盘 6.12，T 日 pre_close=5.82），把分红
  混进打板溢价（+2.7% vs 真实 -2.3%）；用户公式字面定义是"t-1 日的收盘价"
- 修复三处统一改用 T-1 收盘价：
  - 回填溢价日：T-1 快照 close（prev_close_by_code）
  - 16:30 收口 K线权威版：补拉 T-1 快照取 close
  - MCP 当日段合并：用 T-1 bar.close（不再用快照 prev_close）
- 实测 9/9 除权日逐位命中生产工具（如 07-01：1.9422% ✅）
- 注：09:26 竞价版仍用采集源昨收（供即时决策），16:30 K线版为权威口径
- 测试夹具升级：T 日溢价点 prev_close 置毒值 999，证明分母确为 T-1 收盘；Python 196 全绿

## [0.8.12] — 2026-08-16（涨停样本口径对齐用户 SQL：与生产路径同源）
- 对账：用户仓库 SQL（dwd/board_open_feedback_members.sql）基准实测 08-14——
  生产 get_board_open_effect_history 输出逐位一致（59 候选/12 一字板/47 有效/
  +1.2113%/48.9%），而回填/清单路径（auction_list）旧口径为 51 只/+1.24%（差 4 只）
- 根因：旧规则用"涨幅带近似"（10cm 9.5~10.5% / 20cm 19~21%）+ ST 5% 档 +
  未识别代码按 10% 兜底 → 炸板股/ST/北交所/异常样本混入
- 修复：auction_list 判定改为与生产 board_metrics **同源复用**（0.8.12）：
  - 涨停 = 收盘价 == round(昨收×(1+10%/20%), 2) 精确封板判定（分位容差）
  - ST / 北交所（4/8/92）/ 非沪深 A 股代码排除
  - 一字板严格定义：昨开/高/低/收全部等于涨停价（T 字板保留）
- 实测复现：08-14 → 47 只 / 高开23 平开5 低开19 / +1.2113% / 成功率 48.94%，逐位命中用户基准
- 测试重写 test_auction_list（T字板保留/带边缘炸板排除/北交所排除/ST排除），Python 196 全绿
- 部署后重跑一次回填：60 天序列全部按新口径重算（08-17 首个分位的分母池随之修正）

## [0.8.11] — 2026-08-16（分位口径改为用户公式 + 强弱标签）
- 分位口径用户拍板：`rank = (此前 60 个有效观测中严格低于当日值的天数) / 60`
  - 严格小于：等值不计入（旧 count_equal 折半计入废弃）；分母固定 60
  - 不足 60 个有效观测 → rank=null（定义不适用，不硬算）；回填 60 天恰好构成
    08-17 首个满分母日（历史回填日 rank=null 属预期，不重复回填历史）
- 强弱标签：`strength_60d` per metric——strong（rank≥0.90，≥54 个历史观测低于当日）
  / weak（rank≤0.10，≤6 个）/ neutral；rank=null → 标签 null
- 载荷新增 strength_60d（打板指标:<日期>），MCP get_board_open_effect_history 透传；
  HTTP 采集/收口/回填返回同步带出
- 回填第二遍前值窗口 [-59:] → [-60:]（配合固定 60 分母）
- 测试 +5（严格分位×4 / 强弱标签×2 / 载荷满窗标签），Python 193 全绿

## [0.8.10] — 2026-08-16（rd 单连接加固：锁 + 自愈重连 + 值归一化 + RSS 遥测）
- 事故：0.8.9 部署后回填成功（60 天全绿），但 /api/data/read + /api/data/tables 并发探测
  触发全进程冻结（TCP 可连、零响应）+ NAS 内存暴增 3.8GB，需重启容器恢复
- 根因：`_mydb_rd()` 全局单连接无锁，多线程并发写同一 socket → 协议帧交错 →
  C 扩展阻塞持 GIL → 全进程冻结（MCP 层早有 _PYBAO_LOCK，app.py 的 rd 面一直裸奔）
- 修复一：全部 rd 读写（mydb_read/write/tables + hk_sync_codes/hk_klines）持 `_rd_lock` 串行化
- 修复二：rd 调用异常 → `_mydb_rd_reset()` 丢弃缓存连接，下次调用重新 init（自愈楔死连接）
- 修复三：`_to_py` 改为 `_rd_to_py`，与 MCP `_auction_value_to_dict` 语义对齐：
  QueryResult/JSON 串统一转 dict，转换失败按缺失（旧实现原样返回 QueryResult，
  /api/data/read 报 "Object of type QueryResult is not JSON serializable"）
- 遥测：/api/diag env 新增 rss_mb（进程常驻内存，内存类事故可远程观察）
- 测试 +8（归一化×4 / 并发串行化 / 失败自愈 / 全表列出 / hk 读），Python 188 全绿

## [0.8.9] — 2026-08-16（溢价日显式清单 >200 只分块修复）
- 修复：打板溢价日回填对清单股显式查快照，清单 >200 只时 MCP 显式路径报
  ValueError "codes 数量必须为 1-200"（0.8.8 全市场口径修复后清单实测 256 只触发）
- 新增 `_auction_points_for_codes`：按 200/批分块拉取 + by_code 合并去重，回填统一走该路径
- 测试 +1：250 只 → 2 批（200+50）合并 250 点；Python 180 全绿
- 部署后重跑一次回填（预期 55~60 天有效样本）

## [0.8.8] — 2026-08-15（limit=0 全量语义修复 + pipeline 分块 50）
- 修复：limit 解析用 `or` 兜底 → 显式 0 被吞成默认 50，0.8.3 的全量修复从未生效，
  打板清单/回填/对账一直只在 50 只子集上计算（47 天怪象的最终根因）
- pipeline 分块 1000→50（实测 pybao 单次响应上限 50 条）
- 测试改用 60 只股票（>截断上限），截断回归从此无处藏身；真机复验单日 5182 点/51 只涨停/1.6s
- Python 179 全绿

## [0.8.7] — 2026-08-15（回填算法提速：全市场批量 pipeline）
- 全市场快照改用 pybao SDK pipeline 批量（1000 只/批，一次往返）：回填 45 分钟 → 秒级；
  16:30 每日收口同受益（5200 次请求 → 6 批）
- fq=None 取不复权原始价（涨停判定口径正确）；批内缺失代码补一轮重试
- SDK 不可用自动回退逐只 HTTP（保留节流）；测试 +2（批量路径 fq/全量 + 回退节流），Python 179 全绿

## [0.8.6] — 2026-08-15（mydb 存值类型修复）
- 修复：打板全链路 mydb_write 存了 JSON 字符串，pybao 只认原生对象 → 实际存成空
  （回填"成功"但序列/指标读出 {} 即此因）
- 全部改存原生 dict；读取侧（清单/序列/指标/MCP 合并）本就兼容双形态
- 部署后重跑一次回填即得真实序列；Python 177 全绿

## [0.8.5] — 2026-08-15（打板工具全扫路径补节流）
- 修复同类问题：get_board_open_effect_history 的全市场拉取路径自带 16 线程无节流池
  （与 query_point_snapshot 同因的端口耗尽风险）；统一 8 并发 + 50ms 节流
- 全仓库线程池排查完毕：仅此三处全扫路径，均已治理

## [0.8.4] — 2026-08-15（全扫连接卫生）
- 修复：全市场快照 16 并发无节流 → NAS 临时端口耗尽（Errno 99 Cannot assign requested address）
- 全扫改 8 并发 + 每请求 50ms 节流（约 160 req/s，TIME_WAIT 存量远低于端口池）
- 回填溢价日只查清单股（显式小清单）：120 次全扫 → 60 全扫 + 60 小扫
- Python 177 全绿

## [0.8.3] — 2026-08-15（全市场快照修复）
- 修复：内部 query_point_snapshot 默认截断 50 条（上限 200）→ 打板清单/回填/对账全部
  只在 50 只子集上计算（回填仅 31 天有效样本即此因）
- 内部语义新增 limit=0 = 全量不截断（工具 schema 对外仍限 1..200 不变）；打板四调用点全部传 0
- Python 177 全绿；部署后重跑一次回填（预期 55~60 天有效样本）

## [0.8.2] — 2026-08-15（回填异步化）
- 修复：同步回填在请求线程内跑 60 天全市场扫描，连接中断即夭折且可并发叠加打满上游
- auction_run_backfill_async：后台线程执行 + 单飞防重（进行中再触发→拒绝）
- GET /api/auction/status 查询回填状态与日级守卫；Python 177 全绿

## [0.8.1] — 2026-08-15（打板序列冷启动修复）
- 新增 auction_run_backfill：历史 K 线重算过去 N 个交易日业务指标，初始化 打板序列（60 日分母）
  与逐日 打板指标（kline 口径 + 当日可得滚动分位，无未来函数）
- POST /api/auction/run {"task":"backfill","days":60} 手动触发，幂等可重跑
- 部署后先跑一次回填 → 周一 09:26 首次采集的分位当场成立；Python 175 全绿

## [0.8.0] — 2026-08-15（移除模拟盘：数据基座收敛）
- 用户拍板砍掉整个模拟盘：模拟盘模块（paper_core / paper_db / mx_client / paper_engine）移出镜像
  （Dockerfile 删除对应 COPY 行）；模拟盘 / 审计 / 信号验收文档标注「0.8.0 已移除」
- 并入 0.7.1 打包修复：auction 三模块（auction_collect / auction_metrics / auction_list）COPY 保留，
  打板竞价采集链路完整不受影响
- 原因：数据基座收敛——执行 / 研究移出基座，基座只保留「可信数据接口」（HTTP + MCP 12 只读工具）

## [0.7.1] — 2026-08-15（打包修复）
- 修复：Dockerfile 遗漏 COPY auction_collect/auction_metrics/auction_list → 镜像内 ModuleNotFoundError
- CI 加固：verify-pybao job 在镜像内同时 import 三个采集模块（打包缺口从源头拦截）

## [0.7.0] — 2026-08-15（打板竞价采集：数据基座首个自取能力）
- 新增打板竞价采集链路（设计：docs/design/auction-collector.md）：
  - 采集器 auction_collect.py：腾讯主源批量（≤50/批、限流 1req/s）+ 东财备源降级，9:25 竞价价=当日开盘价口径
  - auction_list.py：T-1 K线算"非一字板涨停"清单（5%/10%/20% 三档 + ST）
  - auction_metrics.py：业务指标（溢价均值/成功率）+ 滚动 60 交易日分位（语义对齐 emotion-v1）
  - 调度：09:26 采集（快照→业务值→分位落 mydb）；16:30 收口（同步校验→明日清单→对账回写→K线权威指标→序列追加）；POST /api/auction/run 手动触发
  - MCP get_board_open_effect_history 双源合并：历史 K线 / 当日 mydb 竞价快照，known_at 标注来源
  - mydb 保留前缀：竞价快照:/打板指标:/打板序列:/清单:（AI 勿写）
- 测试：Python 260 全绿（+20 采集/指标/清单用例）；前端 77 全绿（未改动）

## [0.6.6] — 2026-08-15（稳定性收官）
- 空载荷安全性全量加固：PaperSignal/OpsSync 等页在后端瞬时失败（载荷 null）时不再渲染崩溃
- 新增回归防线：10 个页面「所有 API 拒绝」状态下的挂载测试（views-null-safety.test.js，10 例）
- 前端测试 77 例 / Python 240 例全绿

## [0.6.5] — 2026-08-15
- 修复：/ops/sync 在 /api/status 失败（status=null）时裸读 sync_running 导致渲染异常（错误兜底捕获的真凶）

## [0.6.4] — 2026-08-15
- stockdb 上游访问闸口：熔断器（探针连续失败→降级 5 分钟）+ 信号量（并发≤8，超出降级不排队）
- 舱壁隔离：控制路径（同步校验/用户查询/基准）只过信号量，不受探针熔断牵连
- 新增《运行时模型》文档（Little's Law 三因子/依赖地图/失败矩阵/新增功能评审清单）

## [0.6.3] — 2026-08-15
- 修复多标签切页打瘫后端：data_latest_date 失败结果缓存 8s + 探测单飞锁 + 前端在途请求去重

## [0.6.2] — 2026-08-15
- 全局错误兜底：未捕获异常不再白屏，弹提示带错误文案（定位根因的窗口）

## [0.6.1] — 2026-08-15
- Phase 5.1 LuCI 风格面板重组：菜单树 3 组 11 页（一页一职责），诊断中心（/api/diag 一键体检）、
  日志中心（三源聚合+搜索）、数据同步趋势图、系统健康页（含环境信息卡）
- 全站密度收紧、顶栏精简、6 条旧路径重定向

## [0.6.0] — 2026-08-15
- Phase 5 SPA 重构（M0→M3）：前端重写为 Vue 3 + Vite + Element Plus + ECharts
- 十页搬迁（含旧面板缺失 UI 的审计报告/信号体检补位）、api 四域封装、EChart 按需封装
- 路由懒加载 + vendor 分包（index 主包 1.2MB→16KB）
- 双轨底座：/legacy 逃生通道 + WEBUI_UI 开关 + /api/overview 聚合
- 新增轻量测试门禁 test.yml（push/PR 只跑测试不建镜像）

## [0.5.6] — 2026-08-14
- Phase 4.5 运营支撑 + 面板优化：数据新鲜度告警、情绪投递状态卡、策略验收报告、
  全局状态条、通知中心、MCP 调用观测、模拟盘页增强、上游版本检查卡

## [0.5.0] — 2026-08-13
- Phase 4 模拟盘：固定策略合同（emotion-trend-159915-v1）、SQLite WAL 审计账本（9 表）、
  妙想模拟盘接入、7 时点时间轴、T+1、幂等键、trading_enabled=false 默认

## 更早
- 0.4.x：数据契约（统一信封/8 错误码/时点快照/交易日历）
- 0.3.x：MCP 工具集（12 只读工具）、screen_stocks、get_mydb_data
- 0.2.x：get_indicators（39 指标）、get_board_members、get_kline 升级
- 0.1.x：单镜像 Docker 封装（stockdb + updater + webui + pybao + MCP）
