"""storage.warehouse.engine — DuckDB 查询/计算引擎（0.10.0 W3，D12 第 2/3 层）。

连接策略：每 root 一个常驻连接 + threading.Lock 全程串行（沿 mydb _rd_lock 模式）——
单进程（D9）内 MCP/HTTP/调度线程共用；个人研究量级无瓶颈。

SQL 面（读写全开 + 三护栏，用户拍板"最大权限"）：
  1. 单语句：一次 run_sql 只允许一条语句（信封结果形态唯一）
  2. facts 只读：语句文本含 facts/ 路径即拒（事实区唯一写入口是 sink；视图读不受影响）
  3. 行数上限/超时：SELECT 超 cap 截断（truncated 标记由信封承载）；
     超时经 watchdog 线程 interrupt()（尽力而为，见 docstring 已知限制）

视图：v_daily（日K 全分区）/ v_adjust（最新快照去重）/ v_daily_fq（ASOF 复权拼接）/
v_codes（代码表）。指标 = 表宏（PARTITION BY code 保证时序窗口正确性；
ta_ma/ta_rsi/ta_macd），口径见 docs/design/warehouse.md。
"""
from __future__ import annotations

import threading
from pathlib import Path

import config

from . import catalog, layout


class WarehouseUnavailable(RuntimeError):
    """duckdb 缺失或仓库关闭（上层映射 DEPENDENCY_UNAVAILABLE）。"""


class GuardrailError(ValueError):
    """run_sql 护栏拒绝（上层映射 INVALID_ARGUMENT）。"""


_DAILY_GLOB = "daily/*/*/date=*.parquet"
_ADJUST_GLOB = "adjust/snapshot=*.parquet"

_DAILY_EMPTY_COLUMNS = [
    ("code", "TEXT"), ("date", "DATE"), ("name", "TEXT"), ("is_st", "BOOLEAN"),
    ("open", "DOUBLE"), ("high", "DOUBLE"), ("low", "DOUBLE"), ("close", "DOUBLE"),
    ("prev_close", "DOUBLE"), ("volume", "DOUBLE"), ("amount", "DOUBLE"),
]


class WarehouseEngine:
    """单连接 DuckDB 引擎：视图/宏注册 + run_sql 护栏。线程安全（内部锁串行）。"""

    def __init__(self, root: Path):
        import duckdb

        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._con = duckdb.connect(str(layout.duckdb_path(self.root)))
        self._duckdb = duckdb
        self.refresh_views()

    # ---- 视图与宏 ----

    def refresh_views(self) -> None:
        """（重）注册视图与宏。沉淀任务写入后调用；glob 视图查询期求值，新分区自动可见。"""
        con = self._con
        facts = layout.facts_dir(self.root)
        daily_glob = (facts / _DAILY_GLOB).as_posix()
        adjust_glob = (facts / _ADJUST_GLOB).as_posix()
        has_daily = bool(list((facts / "daily").rglob("date=*.parquet"))) if (facts / "daily").is_dir() else False
        has_adjust = bool(list((facts / "adjust").glob("snapshot=*.parquet"))) if (facts / "adjust").is_dir() else False

        # 空仓期给类型正确的空视图（沉淀后 refresh 换成 parquet 视图）
        if has_daily:
            con.execute(
                f"CREATE OR REPLACE VIEW v_daily AS "
                f"SELECT * FROM read_parquet('{daily_glob}', hive_partitioning=true)"
            )
        else:
            # 空仓期给类型正确的空视图（列名加引号：name/is_st 等易撞关键字）
            cols = ", ".join(f"NULL::{t} \"{n}\"" for n, t in _DAILY_EMPTY_COLUMNS)
            con.execute(f"CREATE OR REPLACE VIEW v_daily AS SELECT {cols} WHERE FALSE")

        if has_adjust:
            con.execute(
                f"CREATE OR REPLACE VIEW v_adjust AS "
                f"SELECT code, date, factor FROM ("
                f"  SELECT *, row_number() OVER (PARTITION BY code, date ORDER BY snapshot DESC) rn"
                f"  FROM read_parquet('{adjust_glob}')) WHERE rn = 1"
            )
        else:
            con.execute("CREATE OR REPLACE VIEW v_adjust AS "
                        "SELECT NULL::VARCHAR code, NULL::DATE date, NULL::DOUBLE factor WHERE FALSE")

        con.execute(
            "CREATE OR REPLACE VIEW v_daily_fq AS "
            "SELECT d.*, a.factor AS adj_factor, "
            "       d.open * a.factor AS open_fq, d.high * a.factor AS high_fq, "
            "       d.low * a.factor AS low_fq, d.close * a.factor AS close_fq "
            "FROM v_daily d ASOF LEFT JOIN v_adjust a ON d.code = a.code AND d.date >= a.date"
        )
        con.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, name TEXT)")
        con.execute("CREATE OR REPLACE VIEW v_codes AS SELECT * FROM codes")
        # 用户研究区默认可用（工具文档建议放 research schema——实测首建前直接
        # CREATE TABLE research.x 会 CatalogException，故初始化即预建）
        con.execute("CREATE SCHEMA IF NOT EXISTS research")
        self._register_macros()

    def _register_macros(self) -> None:
        con = self._con
        # 移动平均：n 日简单均线（窗口按 code 分区、date 排序；
        # 窗口不满 n 日为 NULL——对齐 pandas rolling 语义，避免"伪 MA5"）
        con.execute("""
            CREATE OR REPLACE MACRO ta_ma(n) AS TABLE
            SELECT code, date, close,
                   CASE WHEN count(close) OVER (
                            PARTITION BY code ORDER BY date
                            ROWS BETWEEN n - 1 PRECEDING AND CURRENT ROW) >= n
                        THEN avg(close) OVER (
                            PARTITION BY code ORDER BY date
                            ROWS BETWEEN n - 1 PRECEDING AND CURRENT ROW)
                   END AS ma
            FROM v_daily
        """)
        # RSI：n 日简单版（滚动均值比，非 Wilder 平滑——口径差异见设计文档）；
        # 需 n 个价差 → 窗口 n PRECEDING，计满 n 个非空 diff 才有值
        con.execute("""
            CREATE OR REPLACE MACRO ta_rsi(n) AS TABLE
            WITH d AS (
                SELECT code, date, close,
                       close - lag(close) OVER w AS diff
                FROM v_daily
                WINDOW w AS (PARTITION BY code ORDER BY date))
            SELECT code, date, close,
                   CASE WHEN count(diff) OVER g < n THEN NULL
                        WHEN avg(greatest(-diff, 0)) OVER g = 0
                             THEN CASE WHEN avg(greatest(diff, 0)) OVER g > 0 THEN 100.0 END
                        ELSE 100.0 - 100.0 / (1 + avg(greatest(diff, 0)) OVER g
                                                     / avg(greatest(-diff, 0)) OVER g)
                   END AS rsi
            FROM d
            WINDOW g AS (PARTITION BY code ORDER BY date
                         ROWS BETWEEN n PRECEDING AND CURRENT ROW)
        """)
        # MACD：双 EMA（递归 CTE）+ 信号线 EMA；fast/slow/sig 为周期参数
        con.execute("""
            CREATE OR REPLACE MACRO ta_macd(fast, slow, sig) AS TABLE
            WITH RECURSIVE
            o AS (SELECT code, date, close,
                         row_number() OVER (PARTITION BY code ORDER BY date) rn
                  FROM v_daily),
            e AS (
                SELECT code, date, close, rn,
                       close::DOUBLE AS ef, close::DOUBLE AS es
                FROM o WHERE rn = 1
                UNION ALL
                SELECT o.code, o.date, o.close, o.rn,
                       2.0 / (fast + 1) * o.close + (1 - 2.0 / (fast + 1)) * e.ef,
                       2.0 / (slow + 1) * o.close + (1 - 2.0 / (slow + 1)) * e.es
                FROM e JOIN o ON o.code = e.code AND o.rn = e.rn + 1),
            m AS (SELECT code, date, close, rn, ef - es AS macd FROM e),
            s AS (
                SELECT code, date, close, rn, macd, macd::DOUBLE AS signal
                FROM m WHERE rn = 1
                UNION ALL
                SELECT m.code, m.date, m.close, m.rn, m.macd,
                       2.0 / (sig + 1) * m.macd + (1 - 2.0 / (sig + 1)) * s.signal
                FROM s JOIN m ON m.code = s.code AND m.rn = s.rn + 1)
            SELECT code, date, close, macd, signal, macd - signal AS hist FROM s
        """)

    # ---- run_sql ----

    def run_sql(self, sql: str) -> dict:
        """执行单条 SQL（读写全开）。

        返回 {"kind": "rows"|"count", "columns", "rows", "row_count",
              "truncated", "statement_type"}；
        护栏/超时/语法错误分别抛 GuardrailError / TimeoutError / duckdb 异常。
        """
        statements = self._duckdb.extract_statements(sql)
        if len(statements) != 1:
            raise GuardrailError("run_sql 仅接受单条语句")
        stmt = statements[0]
        stmt_sql = stmt.query
        if "facts/" in stmt_sql.lower().replace("\\", "/"):
            raise GuardrailError("facts/ 为不可变事实区：只可经视图读取（v_daily/v_adjust），写入仅经沉淀任务")

        timeout = max(1, int(config.WAREHOUSE_QUERY_TIMEOUT))
        timer = threading.Timer(timeout, self._con.interrupt)
        try:
            with self._lock:
                timer.start()  # 拿到锁后再计时，避免误打断他人持锁查询
                result = self._con.execute(stmt_sql)
                columns = [d[0] for d in (result.description or [])]
                if columns == ["Count"]:
                    row = result.fetchone()
                    n = row[0] if row else 0  # DDL 亦返回 Count 形态但无行
                    return {"kind": "count", "columns": ["count"], "rows": [[n]],
                            "row_count": 1, "truncated": False,
                            "statement_type": str(getattr(stmt, "type", "unknown"))}
                cap = max(1, int(config.WAREHOUSE_ROW_CAP))
                rows = result.fetchmany(cap + 1)
                truncated = len(rows) > cap
                if truncated:
                    rows = rows[:cap]
                rows = [list(r) for r in rows]  # tuple → list（JSON 序列化友好）
                return {"kind": "rows", "columns": columns, "rows": rows,
                        "row_count": len(rows), "truncated": truncated,
                        "statement_type": str(getattr(stmt, "type", "unknown"))}
        except self._duckdb.InterruptException as exc:
            raise TimeoutError(f"query exceeded {timeout}s (interrupted)") from exc
        finally:
            timer.cancel()

    # ---- 状态与清单 ----

    def status(self) -> dict:
        dates = layout.list_daily_dates(self.root)
        return {
            "root": str(self.root),
            "watermark_daily": catalog.get_watermark(self.root, "daily"),
            "sedimented_dates": len(dates),
            "first_date": dates[0] if dates else None,
            "latest_date": dates[-1] if dates else None,
            "adjust_snapshot": catalog.get_meta(self.root, "adjust:snapshot"),
            "codes": self._count("codes"),
            "duckdb_version": self._duckdb.__version__,
        }

    def list_objects(self) -> dict:
        tables = [r[0] for r in self._con.execute("SHOW TABLES").fetchall()]
        macros = self._con.execute(
            "SELECT function_name, parameters FROM duckdb_functions() "
            "WHERE function_type IN ('macro', 'table_macro') "
            "  AND function_name LIKE 'ta\\_%' ESCAPE '\\' "
            "ORDER BY function_name"
        ).fetchall()
        return {"tables": tables,
                "macros": [{"name": n, "parameters": p} for n, p in macros]}

    def _count(self, table: str) -> int:
        with self._lock:
            return self._con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._con.close()


# ---- 模块级单例（组合根/接口层用；测试自建 WarehouseEngine(tmpdir)） ----

_engine: WarehouseEngine | None = None
_engine_lock = threading.Lock()


def get_engine() -> WarehouseEngine:
    """按 config.WAREHOUSE_DIR 的惰性单例（动态读 config，测试可 patch 后 reset）。"""
    global _engine
    with _engine_lock:
        if _engine is not None and str(_engine.root) == str(layout.root_dir()):
            return _engine
        if _engine is not None:
            _engine.close()
            _engine = None
        _engine = WarehouseEngine(layout.root_dir())
        return _engine


def reset_engine() -> None:
    """测试隔离：关闭并清空单例。"""
    global _engine
    with _engine_lock:
        if _engine is not None:
            _engine.close()
            _engine = None
