"""storage.warehouse.catalog — 仓库元数据（0.10.0 W2；收敛点 C4：唯一存于 warehouse.duckdb meta 表）。

watermark 语义：dataset 已沉淀到的最新日期（YYYYMMDD），**只前进不回退**——
查询侧 known_at 由 watermark 派生（W3/W5），是仓库可信度的唯一时点声明。

连接策略：短生命周期（用完即关）。DuckDB 同进程按路径缓存数据库实例，
sink/engine 的多个连接共享同一实例，元数据即时互见；写入频率为日级，无争用。
"""
from __future__ import annotations

from pathlib import Path

from . import layout

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
)
"""


def _connect(root: Path):
    import duckdb

    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(layout.duckdb_path(root)))
    con.execute(_SCHEMA)
    return con


def get_meta(root: Path, key: str, default=None):
    con = _connect(root)
    try:
        rows = con.execute("SELECT value FROM meta WHERE key = ?", [key]).fetchall()
        return rows[0][0] if rows else default
    finally:
        con.close()


def set_meta(root: Path, key: str, value) -> None:
    con = _connect(root)
    try:
        con.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            [key, str(value)],
        )
    finally:
        con.close()


def get_watermark(root: Path, dataset: str = "daily"):
    """dataset 已沉淀到的最新日期（YYYYMMDD；未沉淀返回 None）。"""
    return get_meta(root, f"watermark:{dataset}")


def set_watermark(root: Path, dataset: str, date) -> bool:
    """推进 watermark；仅当新值更新（字典序比较 8 位日期）才写。返回是否推进。"""
    d = layout.normalize_date(date)
    current = get_watermark(root, dataset)
    if current is not None and d <= current:
        return False
    set_meta(root, f"watermark:{dataset}", d)
    return True
