"""storage.warehouse.sink — 事实写入（0.10.0 W2；facts/ 的唯一写入口）。

不变量（docs/design/warehouse.md）：
  - facts/ 只增不改：分区文件已存在即跳过（幂等），不存在"改写历史"路径
  - 原子可见：临时文件 → COPY → 原子 rename；失败清理临时文件，绝不留半文件
  - 护栏：数值列非有限（NaN/Inf）或缺失的行拒写并计数（沿 research_store 教训）
  - watermark 随写推进（经 catalog，只前进）

数值语义：字段值按引擎原样镜像（不换算单位），仓库不做任何口径加工。
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from . import catalog, layout

# 日K列定义（引擎日K字段原样：date 转 DATE 类型便于 SQL 区间/年份运算）
_DAILY_COLUMNS = (
    ("code", "TEXT"),
    ("date", "DATE"),
    ("name", "TEXT"),
    ("is_st", "BOOLEAN"),
    ("open", "DOUBLE"),
    ("high", "DOUBLE"),
    ("low", "DOUBLE"),
    ("close", "DOUBLE"),
    ("prev_close", "DOUBLE"),
    ("volume", "DOUBLE"),
    ("amount", "DOUBLE"),
)
_NUMERIC_FIELDS = ("open", "high", "low", "close", "prev_close", "volume", "amount")

_ADJUST_COLUMNS = (
    ("code", "TEXT"),
    ("date", "DATE"),
    ("factor", "DOUBLE"),
    # 快照版本列（真实列而非 hive 解析：DuckDB 不解析文件名段的 key=value）
    ("snapshot", "DATE"),
)


def _finite(value) -> bool:
    """数值字段护栏：None / NaN / Inf 拒写。"""
    if value is None:
        return False
    if isinstance(value, float) and not math.isfinite(value):
        return False
    return True


def _normalize_rows(rows: list[dict], columns):
    """dict 行 → 元组行：数值护栏、列序对齐（date 字段由调用方注入行内）。返回 (rows, dropped)。"""
    out, dropped = [], 0
    numeric = {name for name, _ in columns if name in _NUMERIC_FIELDS or name == "factor"}
    names = [name for name, _ in columns]
    for r in rows:
        if any(not _finite(r.get(f)) for f in numeric):
            dropped += 1
            continue
        row = []
        for name in names:
            v = r.get(name)
            if name in ("date", "snapshot"):
                row.append(layout.iso_date(v))
            elif name == "is_st":
                row.append(bool(v) if v is not None else None)
            else:
                row.append(v)
        out.append(tuple(row))
    return out, dropped


def _write_parquet_atomic(rows: list[tuple], columns, target: Path) -> None:
    """元组行 → 排序写入临时文件 → 原子 rename。失败清理临时文件。"""
    import duckdb

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    col_defs = ", ".join(f"{n} {t}" for n, t in columns)
    names = [n for n, _ in columns]
    try:
        con = duckdb.connect()
        try:
            con.execute(f"CREATE TABLE t ({col_defs})")
            if rows:
                placeholders = ", ".join("?" for _ in names)
                con.executemany(f"INSERT INTO t VALUES ({placeholders})", rows)
            order_by = "code" if "code" in names else names[0]
            con.execute(
                f"COPY (SELECT * FROM t ORDER BY {order_by}) TO '{tmp.as_posix()}' (FORMAT PARQUET)"
            )
        finally:
            con.close()
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()


def write_daily(root: Path, date, rows: list[dict]) -> dict:
    """沉淀一个交易日的全市场日K（按市场分分区文件，幂等）。

    返回 {status: written|skipped|empty, markets, rows, dropped_nonfinite, watermark}。
    """
    date = layout.normalize_date(date)
    by_market: dict[str, list[tuple]] = {}
    dropped = 0
    for r in rows:
        market = layout.market_of(r.get("code", ""))
        row = {"date": date, **r}  # 快照行不带日期，由任务层日期注入
        normalized, d = _normalize_rows([row], _DAILY_COLUMNS)
        dropped += d
        if normalized:
            by_market.setdefault(market, []).append(normalized[0])

    if not by_market:
        # 完全无有效行也推进 watermark（空交易日不阻塞后续任务判断）
        advanced = catalog.set_watermark(root, "daily", date)
        return {"status": "empty", "markets": [], "rows": 0,
                "dropped_nonfinite": dropped, "watermark_advanced": advanced}

    written_markets = []
    total = 0
    for market, market_rows in sorted(by_market.items()):
        target = layout.daily_partition(root, date, market)
        if target.exists():
            continue  # 只增不改：已存在分区跳过（幂等）
        _write_parquet_atomic(market_rows, _DAILY_COLUMNS, target)
        written_markets.append(market)
        total += len(market_rows)

    advanced = catalog.set_watermark(root, "daily", date)
    return {
        "status": "written" if written_markets else "skipped",
        "markets": written_markets,
        "rows": total,
        "dropped_nonfinite": dropped,
        "watermark_advanced": advanced,
    }


def write_adjust_snapshot(root: Path, date, rows: list[dict]) -> dict:
    """复权因子全量快照（版本化追加：snapshot=YYYYMMDD.parquet，幂等）。

    快照语义：每次刷新写入当日版本文件，视图读 catalog 指针指向的最新快照
    （facts 仍只增不改；旧快照留档可审计）。
    """
    date = layout.normalize_date(date)
    target = layout.adjust_snapshot_path(root, date)
    rows = [{"date": date, "snapshot": date, **r} for r in rows]
    normalized, dropped = _normalize_rows(rows, _ADJUST_COLUMNS)
    if not normalized:
        return {"status": "empty", "rows": 0, "dropped_nonfinite": dropped}
    if target.exists():
        return {"status": "skipped", "rows": len(normalized), "dropped_nonfinite": dropped}
    _write_parquet_atomic(normalized, _ADJUST_COLUMNS, target)
    catalog.set_meta(root, "adjust:snapshot", date)
    return {"status": "written", "rows": len(normalized), "dropped_nonfinite": dropped}


def write_codes(root: Path, rows: list[dict]) -> dict:
    """代码表全量刷新（warehouse.duckdb 内表，非 facts——它是"当前状态"不是"事实"）。"""
    import duckdb

    con = duckdb.connect(str(layout.duckdb_path(root)))
    try:
        con.execute("CREATE TABLE IF NOT EXISTS codes (code TEXT PRIMARY KEY, name TEXT)")
        con.execute("DELETE FROM codes")
        data = [(str(r.get("code", "")).strip(), r.get("name")) for r in rows
                if str(r.get("code", "")).strip()]
        if data:
            con.executemany("INSERT INTO codes VALUES (?, ?)", data)
    finally:
        con.close()
    return {"status": "written", "rows": len(data)}
