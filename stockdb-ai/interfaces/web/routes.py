"""web.routes — HTTP 路由表（接口层，0.9.2 批次 6）。

URL → Handler 方法名映射（GET/POST 各一张表）；Handler 按表分发，新增端点
= 加一行表项 + 一个方法。行为与 0.9.2 前的 if/elif 链完全一致（对外契约不变）。

注：/mcp 与 /api/auction/run 为 POST；/api/version-check 是 /api/version 别名。
"""
from __future__ import annotations

GET_ROUTES: dict[str, str] = {
    "/api/status": "_status",
    "/api/history": "_history",
    "/api/schedule": "_schedule",
    "/api/health": "_health",
    "/api/log": "_log",
    "/api/query": "_query",
    "/api/container/logs": "_container_logs",
    "/api/data/tables": "_data_tables",
    "/api/data/read": "_data_read",
    "/api/hk/sync": "_hk_sync",
    "/api/overview": "_overview",
    "/api/auction/status": "_auction_status",
    "/api/auction/daily": "_auction_daily",
    "/api/warehouse/status": "_warehouse_status",
    "/api/diag": "_diag",
    "/api/alerts": "_alerts",
    "/api/alerts/summary": "_alerts_summary",
    "/api/mcp/stats": "_mcp_stats",
    "/api/mcp/calls": "_mcp_calls",
    "/api/version": "_version",
    "/api/version-check": "_version",  # /api/version 别名（前端旧路径兼容）
}

POST_ROUTES: dict[str, str] = {
    "/api/sync": "_sync",
    "/api/container/restart": "_container_restart",
    "/api/data/write": "_data_write",
    "/api/hk/sync": "_hk_sync",
    "/api/alerts/clear": "_alerts_clear",
    "/api/auction/run": "_auction_run",
    "/api/warehouse/run": "_warehouse_run",
    "/mcp": "_mcp",
}
