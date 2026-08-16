# 设计文档：研究成果产出自持（0.9.5，M5）

> 架构总纲 D8（用户拍板提前）：打板指标/序列/清单/竞价快照等**研究成果**从引擎
> mydb 迁出至**自建存储**——引擎进程死亡不再影响研究成果可读性；引擎降级为
> 纯行情 provider。用户 0.9.4 拍板补充：存储选型 SQLite（WAL），Repository 接口
> 模式采纳（`save_limit_up_records` 等语义命名），自动化备份随 M5 落地。

## 1. 目标

1. 研究成果（打板指标/序列/清单/竞价快照）读写全部走自建 SQLite（WAL 模式），
   **应用层（services/）零改动感知**（通过 Repository 接口 + 工厂注入）
2. 一次性迁移：引擎 mydb 既有研究成果全量导出 → SQLite（幂等，可重跑）
3. 自动备份：日检写盘后触发 SQLite 备份（保留 N 份）
4. 可回滚：`RESEARCH_STORE=mydb` 环境变量切回引擎 mydb（旧行为，零迁移）
5. MCP `get_mydb_data` 读研究成果走 research store（对外语义不变：读研究成果表）

## 2. 存储设计（SQLite，research.db）

```
storage/providers/research_store.py     # SQLite 实现（WAL）
storage/providers/mydb_store.py         # 引擎 mydb 实现（遗留，回滚用）
storage/research_factory.py             # RESEARCH_STORE 工厂（sqlite 默认 / mydb 回滚）
```

**Schema**（单文件 `DATA_DIR/research.db`，WAL 模式）：

| 表 | 键 | 值 | 对应 mydb 旧布局 |
|---|---|---|---|
| metrics | date(8位) | payload JSON | 打板指标:<date> / 键 metrics |
| series | metric | payload JSON | 打板序列:<metric> / 键 series |
| lists | date | payload JSON | 清单:<date>:limitup_non_yizi / 键 list |
| snapshots | date, code | row JSON | 竞价快照:<date> / 键 <code> |
| meta | key | value | 迁移版本/时间（迁移幂等记录） |

- 开启 WAL：`PRAGMA journal_mode=WAL`（读写并发不互锁；`busy_timeout=5000`）
- 连接：单连接 + 线程锁（复用 mydb_store._rd_lock 模式，全进程串行写；读可并行）
- NaN/Inf 护栏：沿用 mydb_write 的 `_has_nan_inf`（写入前剔除计数）

## 3. Repository 接口（防腐层，用户模式采纳）

```python
class ResearchStore(ABC):  # storage/research_store.py 内定义
    @abstractmethod
    def write_metrics(self, date: str, payload: dict) -> None: ...
    @abstractmethod
    def read_metrics(self, date: str) -> dict | None: ...
    @abstractmethod
    def write_series(self, metric: str, payload: dict) -> None: ...
    @abstractmethod
    def read_series(self, metric: str) -> dict | None: ...
    @abstractmethod
    def write_list(self, date: str, payload: dict) -> None: ...
    @abstractmethod
    def read_list(self, date: str) -> dict | None: ...
    @abstractmethod
    def write_snapshots(self, date: str, rows: dict[str, dict]) -> None: ...
    @abstractmethod
    def read_snapshots(self, date: str) -> dict[str, dict]: ...
    @abstractmethod
    def migrate_from_engine(self) -> dict: ...   # 从引擎 mydb 全量导入（幂等）
    @abstractmethod
    def backup(self) -> Path | None: ...          # 备份当前库
```

- **实现**：SqliteResearchStore（0.9.5 主线）+ MydbResearchStore（薄适配：把上述语义
  方法映射回现有 mydb_write/read 调用——回滚路径即旧行为）
- **工厂**：`get_research_store() -> ResearchStore`——按 `RESEARCH_STORE` 环境变量
  （默认 sqlite；mydb = 回滚）；惰性单例
- **注入**：services/auction_tasks 的写路径通过模块级注入点 `research_store`（app.py
  组合根装配 `get_research_store()`），沿用 0.9.2 注入模式——**应用层不感知存储实现**

## 4. 迁移（migrate_from_engine）

- 遍历引擎 mydb 研究成果表：`打板指标:*` / `打板序列:*` / `清单:*` / `竞价快照:*`
  （rd.keys 前缀通配，**禁止 keys("*") 全表扫描**——引擎串行处理会挂）
- 逐表逐键读 → 写 SQLite（事务批量）；meta 记 `migrated_at` / 表计数
- 幂等：重跑覆盖写；迁移完成前 get_mydb_data 双读（SQLite 优先，mydb 兜底）
- 迁移后引擎 mydb 数据**保留不删**（回滚预案）

## 5. 读兼容（get_mydb_data）

- MCP `get_mydb_data` 保持对外语义（读研究成果），内部表名映射：
  `打板指标:<date>` → metrics(date)；`打板序列:<metric>` → series(metric)；
  `清单:<date>:limitup_non_yizi` → lists(date)；`竞价快照:<date>` → snapshots(date)
- 未知前缀 → 读引擎 mydb（遗留兼容）；mydb 回滚模式 → 全走引擎

## 6. 备份（日检后自动）

- 日检 append 成功后触发 `research_store.backup()`：
  - SQLite：`VACUUM INTO 'backups/research-YYYYMMDD-HHMMSS.db'`（在线安全，
    不锁主库）或 WAL checkpoint 后文件拷贝
  - 保留最近 14 份，超出删除最旧
- 备份目录 `DATA_DIR/backups/`；失败静默（不阻塞日检）

## 7. 验收

- [ ] 249 测试全绿 + 新增（SQLite CRUD / WAL / 迁移幂等 / 备份保留 / 工厂切换 /
      get_mydb_data 兼容）
- [ ] **本机真实迁移**：引擎 mydb 现有 60 天回填 → SQLite，读回核对验收基线
      （08-14 = 47 / 0.012113 / 0.4894、序列 60/60）
- [ ] RESEARCH_STORE=mydb 回滚验证（行为与 0.9.4 一致）
- [ ] 应用层零改动：services 代码不出现 sqlite/mydb 具体类型（仅经注入的接口）
- [ ] WEBUI_VERSION 0.9.4 → 0.9.5；CHANGELOG 记录

## 8. 风险

| 风险 | 应对 |
|---|---|
| SQLite 单文件损坏 | WAL + busy_timeout + 备份链；备份本身可回滚恢复 |
| 迁移遗漏表 | migrate 幂等 + meta 计数 + 验收基线核对（08-14 逐位） |
| get_mydb_data 语义漂移 | 映射表显式维护 + 兼容测试（前缀路由） |
| services 误依赖具体实现 | 层边界测试扩展：services 禁 import storage.providers 具体类 |
| 引擎升级/未来存储演进 | Repository 接口即抽象——加 provider 即可（D3 兑现） |
