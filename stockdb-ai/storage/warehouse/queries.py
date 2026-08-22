"""storage.warehouse.queries — 仓库查询门面（0.10.0 W3）。

服务层/接口层统一入口：availability 检查 + 单例引擎委托。
异常约定（上层映射契约错误码）：
  - WarehouseUnavailable → DEPENDENCY_UNAVAILABLE
  - GuardrailError / duckdb 参数类异常 → INVALID_ARGUMENT
  - TimeoutError → INTERNAL_ERROR（附超时说明）
"""
from __future__ import annotations

from storage import warehouse as _wh
from . import catalog, layout
from .engine import GuardrailError, WarehouseEngine, WarehouseUnavailable, get_engine, reset_engine

__all__ = [
    "GuardrailError", "WarehouseUnavailable", "WarehouseEngine",
    "get_engine", "reset_engine", "run_sql", "status", "list_objects", "known_at",
]


def _require_engine() -> WarehouseEngine:
    ok, note = _wh.availability()
    if not ok:
        raise WarehouseUnavailable(note)
    return get_engine()


def run_sql(sql: str) -> dict:
    """执行单条 SQL（读写全开，护栏见 engine.run_sql）。"""
    return _require_engine().run_sql(sql)


def status() -> dict:
    """仓库状态（watermark/分区数/快照/代码数/duckdb 版本）。"""
    info = _require_engine().status()
    info["available"] = True
    return info


def list_objects() -> dict:
    """仓库对象清单（表/视图 + 指标宏）。"""
    return _require_engine().list_objects()


def known_at() -> str | None:
    """仓库可信时点（daily watermark；空仓返回 None）。"""
    return catalog.get_watermark(layout.root_dir(), "daily")
