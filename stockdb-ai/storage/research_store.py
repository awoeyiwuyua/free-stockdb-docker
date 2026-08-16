"""storage.research_store — 研究成果产出自持（0.9.5，M5 架构总纲 D8）。

ResearchStore 抽象接口（防腐层，用户 Repository 模式采纳）+ SQLite（WAL）实现
+ 引擎 mydb 回滚适配。应用层只依赖注入的接口，不感知存储实现（D3 兑现）。

存储：DATA_DIR/research.db，WAL 模式，busy_timeout 5s；NaN/Inf 写前护栏沿用。
"""
from __future__ import annotations

import json
import math
import sqlite3
import threading
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

import config  # 模块引用（config.DATA_DIR 动态读取，测试 patch 生效）

DB_FILE = "research.db"
BACKUP_DIR = "backups"
BACKUP_KEEP = 14          # 备份保留份数
MIGRATION_KEY = "migrated_at"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metrics (
    date TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS series (
    metric TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lists (
    date TEXT PRIMARY KEY, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    date TEXT NOT NULL, code TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (date, code)
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
"""


def _has_nan_inf(value) -> bool:
    """递归检查 value 中是否含 NaN/Inf 浮点（写前护栏，与 mydb_store 同语义）。"""
    if isinstance(value, float):
        return math.isnan(value) or math.isinf(value)
    if isinstance(value, list):
        return any(_has_nan_inf(v) for v in value)
    if isinstance(value, dict):
        return any(_has_nan_inf(v) for v in value.values())
    return False


class ResearchStore(ABC):
    """研究成果仓储接口（防腐层：领域/服务层只依赖本接口）。"""

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
    def migrate_from_engine(self) -> dict:
        """从引擎 mydb 全量导入既有研究成果（幂等可重跑）。"""

    @abstractmethod
    def backup(self) -> Path | None:
        """备份当前库（返回备份文件路径；失败 None）。"""


class SqliteResearchStore(ResearchStore):
    """SQLite（WAL）实现：DATA_DIR/research.db。

    单连接 + 线程锁（写串行，读并行——SQLite 读天然并发）；WAL 模式解决
    读写互锁；busy_timeout 5s 防写锁竞争报错。
    """

    def __init__(self, db_path: Path | None = None):
        self._path = db_path or (Path(config.DATA_DIR) / DB_FILE)
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    # ---- 连接管理 ----
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self._path), timeout=5.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            self._conn = conn
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    # ---- 写（串行锁 + NaN 护栏）----
    def _write_row(self, table: str, keys: tuple, payload: dict) -> int:
        """写入单行；NaN/Inf 拦截返回 0（不落盘），成功返回 1。"""
        if _has_nan_inf(payload):
            return 0
        conn = self._connect()
        placeholders = ", ".join("?" for _ in keys) + ", ?"
        sql = f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})"
        with self._lock:
            conn.execute(sql, (*keys, json.dumps(payload, ensure_ascii=False)))
            conn.commit()
        return 1

    def write_metrics(self, date: str, payload: dict) -> None:
        self._write_row("metrics", (date,), payload)

    def write_series(self, metric: str, payload: dict) -> None:
        self._write_row("series", (metric,), payload)

    def write_list(self, date: str, payload: dict) -> None:
        self._write_row("lists", (date,), payload)

    def write_snapshots(self, date: str, rows: dict[str, dict]) -> None:
        for code, row in (rows or {}).items():
            self._write_row("snapshots", (date, code), row)

    # ---- 读 ----
    def _read_row(self, table: str, cols: list[str], keys: tuple) -> dict | None:
        conn = self._connect()
        where = " AND ".join(f"{c}=?" for c in cols)
        with self._lock:
            cur = conn.execute(f"SELECT payload FROM {table} WHERE {where}", keys)
            row = cur.fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except ValueError:
            return None

    def read_metrics(self, date: str) -> dict | None:
        return self._read_row("metrics", ["date"], (date,))

    def read_series(self, metric: str) -> dict | None:
        return self._read_row("series", ["metric"], (metric,))

    def read_list(self, date: str) -> dict | None:
        return self._read_row("lists", ["date"], (date,))

    def read_snapshots(self, date: str) -> dict[str, dict]:
        conn = self._connect()
        with self._lock:
            cur = conn.execute(
                "SELECT code, payload FROM snapshots WHERE date=?", (date,))
            rows = cur.fetchall()
        out: dict[str, dict] = {}
        for code, raw in rows:
            try:
                out[code] = json.loads(raw)
            except ValueError:
                continue
        return out

    # ---- 迁移（从引擎 mydb 全量导入，幂等）----
    def migrate_from_engine(self) -> dict:
        from storage.providers import mydb_store as _mydb

        counts = {"metrics": 0, "series": 0, "lists": 0, "snapshots": 0}
        # 前缀通配逐表读取（禁止 keys("*") 全表扫描——引擎串行处理会挂）
        for prefix, kind in (("打板指标:", "metrics"), ("打板序列:", "series"),
                             ("清单:", "lists"), ("竞价快照:", "snapshots")):
            try:
                keys = _mydb_rd_keys(prefix)
            except Exception:  # noqa: BLE001 - 单表失败不影响其余
                continue
            for key in keys or []:
                table_full = str(key)
                # 键三段式（实测引擎语义）：表:日期/名:子键
                #   打板指标:20260522:metrics / 打板序列:premium_mean:series /
                #   清单:20260815:limitup_non_yizi / 竞价快照:20260814:<code>
                parts = table_full.split(":")
                if len(parts) < 3:
                    continue
                try:
                    value = _mydb._rd_to_py(
                        _mydb._mydb_rd().get(":".join(parts[:-1]), parts[-1]))
                except Exception:  # noqa: BLE001 - 单键失败跳过
                    continue
                if not isinstance(value, dict):
                    continue
                if kind == "metrics":
                    self.write_metrics(parts[1], value)
                    counts["metrics"] += 1
                elif kind == "series":
                    self.write_series(parts[1], value)
                    counts["series"] += 1
                elif kind == "lists":
                    self.write_list(parts[1], value)
                    counts["lists"] += 1
                elif kind == "snapshots":
                    self.write_snapshots(parts[1], {parts[2]: value})
                    counts["snapshots"] += 1
        self._set_meta(MIGRATION_KEY, datetime.now().isoformat(timespec="seconds"))
        return {"ok": True, "counts": counts,
                "migrated_at": self._get_meta(MIGRATION_KEY)}

    def _set_meta(self, key: str, value: str) -> None:
        conn = self._connect()
        with self._lock:
            conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))
            conn.commit()

    def _get_meta(self, key: str) -> str | None:
        conn = self._connect()
        with self._lock:
            cur = conn.execute("SELECT value FROM meta WHERE key=?", (key,))
            row = cur.fetchone()
        return row[0] if row else None

    # ---- 备份（VACUUM INTO，在线安全；保留 N 份）----
    def backup(self) -> Path | None:
        try:
            backup_dir = Path(config.DATA_DIR) / BACKUP_DIR
            backup_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            target = backup_dir / f"research-{stamp}.db"
            conn = self._connect()
            with self._lock:
                conn.execute(f"VACUUM INTO '{target.as_posix()}'")
            # 保留最近 BACKUP_KEEP 份
            backups = sorted(backup_dir.glob("research-*.db"))
            for old in backups[:-BACKUP_KEEP]:
                old.unlink(missing_ok=True)
            return target
        except Exception:  # noqa: BLE001 - 备份失败静默（不阻塞日检）
            return None


def _mydb_rd_keys(prefix: str) -> list:
    """引擎 mydb 前缀键枚举：rd.keys(表前缀, "*") 返回完整键（"表:子键" 形态）。

    以表前缀为 table 参数（如 "打板指标"），引擎按前缀匹配所有该命名空间表；
    禁止 keys("*")（全库扫描，引擎串行处理会挂）。
    """
    from storage.providers import mydb_store as _mydb
    table = prefix.rstrip(":")
    if not table:
        return []
    try:
        return _mydb._mydb_rd().keys(table, "*")
    except Exception:  # noqa: BLE001 - 引擎不可用 → 空
        return []


class MydbResearchStore(ResearchStore):
    """引擎 mydb 回滚适配（RESEARCH_STORE=mydb）：语义方法映射回旧读写路径。"""

    def _r(self) -> object:
        from storage.providers import mydb_store as _mydb
        return _mydb

    def write_metrics(self, date: str, payload: dict) -> None:
        self._r().mydb_write(f"打板指标:{date}", [("metrics", payload)])

    def read_metrics(self, date: str) -> dict | None:
        v = self._r().mydb_read(f"打板指标:{date}", "metrics").get("value")
        return v if isinstance(v, dict) else None

    def write_series(self, metric: str, payload: dict) -> None:
        self._r().mydb_write(f"打板序列:{metric}", [("series", payload)])

    def read_series(self, metric: str) -> dict | None:
        v = self._r().mydb_read(f"打板序列:{metric}", "series").get("value")
        return v if isinstance(v, dict) else None

    def write_list(self, date: str, payload: dict) -> None:
        self._r().mydb_write(f"清单:{date}:limitup_non_yizi", [("list", payload)])

    def read_list(self, date: str) -> dict | None:
        v = self._r().mydb_read(f"清单:{date}:limitup_non_yizi", "list").get("value")
        return v if isinstance(v, dict) else None

    def write_snapshots(self, date: str, rows: dict[str, dict]) -> None:
        self._r().mydb_write(f"竞价快照:{date}",
                             [(c, row) for c, row in (rows or {}).items()])

    def read_snapshots(self, date: str) -> dict[str, dict]:
        res = self._r().mydb_read(f"竞价快照:{date}", "")
        out: dict[str, dict] = {}
        for code, raw in (res.get("values") or {}).items():
            if isinstance(raw, dict):
                out[str(code).split(":")[-1]] = raw
        return out

    def migrate_from_engine(self) -> dict:
        return {"ok": True, "counts": {}, "note": "回滚模式无需迁移"}

    def backup(self) -> Path | None:
        return None  # 引擎侧存储由引擎自身负责
