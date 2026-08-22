"""storage.warehouse — 列式仓库层（0.10.0，架构决策 D12）。

Parquet 事实沉淀 + DuckDB 查询/计算，物理落位数据层：
  - layout.py：分区路径规则（facts/<dataset>/year=YYYY/market=xx/date=YYYYMMDD.parquet）
  - sink.py：事实写入（临时文件 + 原子 rename，幂等，只增不改）
  - catalog.py：watermark 与分区清单（唯一存于 warehouse.duckdb meta 表，C4）
  - engine.py：DuckDB 连接管理 + 视图/宏注册 + run_sql 护栏（W3）
  - queries.py：供服务层/MCP 的查询封装（W3）

不变量：
  - 事实区（facts/）只由 sink 写入；run_sql 拒绝指向 facts/ 的 COPY TO
  - 引擎 = 当日权威，仓库 = 派生副本；可信度由 watermark（known_at）承载

依赖纪律：同 storage 层（可依赖 ops/config + 第三方 duckdb；禁依赖 services/core/interfaces）。
duckdb 缺失时 availability 探针返回 False，上层按 DEPENDENCY_UNAVAILABLE 降级（镜像无
musllinux wheel 等场景），不影响 53 个既有工具。
"""
from __future__ import annotations

import config


def availability() -> tuple[bool, str]:
    """duckdb 可用性探针：返回 (可用, 说明)。缺失时上层走 DEPENDENCY_UNAVAILABLE 降级。"""
    if not config.WAREHOUSE_ENABLED:
        return False, "warehouse disabled (WAREHOUSE_ENABLED=0)"
    try:
        import duckdb  # noqa: F401
    except Exception as exc:  # ImportError 或 ABI 加载失败一律降级
        return False, f"duckdb unavailable: {exc}"
    return True, "ok"
