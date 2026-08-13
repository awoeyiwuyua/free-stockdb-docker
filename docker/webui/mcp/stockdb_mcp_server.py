#!/usr/bin/env python3
"""stockdb_mcp_server — free-stockdb 只读查询的 MCP server（stdio / HTTP 双传输）

本脚本是一个长期驻留的 Model Context Protocol (MCP) server，由 ZCode /
Claude Desktop 等 MCP 客户端通过 stdin/stdout（stdio）或 HTTP（NAS 容器部署）
拉起。它把局域网部署的 free-stockdb（默认 100.66.1.1:7899，Tailscale 地址）
的 HTTP API 封装成 MCP 工具，供 agent 直接查询真实行情。只读，不写任何数据，
不连接项目 SQLite。

依赖：纯 Python 标准库（urllib/json/sys/os/http.server/threading），零第三方包。
传输：
  - stdio transport = 换行分隔 JSON-RPC（每行一个 JSON 对象）
  - HTTP transport = ThreadingHTTPServer，POST /mcp 收 JSON-RPC 请求返回 JSON 响应，
    GET / 返回健康检查文本（stockdb-mcp ok）

Usage:
    STOCKDB_HOST=100.66.1.1 STOCKDB_PORT=7899 \
        uv run python mcp/stockdb_mcp_server.py              # stdio（MCP 客户端自动拉起）
    uv run python mcp/stockdb_mcp_server.py --self-check     # 连通性自检
    uv run python mcp/stockdb_mcp_server.py --http \
        --host 0.0.0.0 --port 8080                           # HTTP（NAS 容器部署）

环境变量:
    STOCKDB_HOST   free-stockdb 服务地址，默认 100.66.1.1（NAS Tailscale）
    STOCKDB_PORT   free-stockdb 服务端口，默认 7899
    STOCKDB_TIMEOUT  HTTP 查询超时（秒），默认 15

暴露的工具（只读）:
    get_kline           日K/分钟K 行情，支持区间与字段投影
    get_stock_list      全市场 A 股代码列表
    get_adjust_factors  复权因子
    get_market_snapshot 指定交易日多只股票的单日行情快照
    get_board_open_effect_history
                        全市场“昨日非一字板涨停、今日开盘溢价”时序
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import urllib.parse
import urllib.request

# 保证同目录（mcp/）在 sys.path，使 `from board_metrics import ...` 在两种运行方式下都能找到：
# 1) 直接 `python mcp/stockdb_mcp_server.py`（sys.path[0] 已是脚本目录）
# 2) 作为包导入 `from mcp import stockdb_mcp_server`（sys.path 是仓库根，需补 mcp/）
_MCP_DIR = Path(__file__).resolve().parent
if str(_MCP_DIR) not in sys.path:
    sys.path.insert(0, str(_MCP_DIR))

from board_metrics import (  # noqa: E402  - 需先插入 sys.path 再导入同目录模块
    DailyBar,
    is_supported_a_share_code,
    compute_board_open_effect_details,
)

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "stockdb-native"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "100.66.1.1"
DEFAULT_PORT = 7899
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8080

# 重型工具串行化锁：query_board_open_effect_history 内部会嵌套调用
# query_fullmarket_daily_snapshot（两者都会开线程池拉全市场），用 RLock 避免同线程二次获取死锁。
_HEAVY_LOCK = threading.RLock()


# === HTTP 客户端 ===


def _base_url() -> str:
    host = os.environ.get("STOCKDB_HOST", DEFAULT_HOST)
    port = os.environ.get("STOCKDB_PORT", str(DEFAULT_PORT))
    return f"http://{host}:{port}"


def _timeout() -> float:
    try:
        return float(os.environ.get("STOCKDB_TIMEOUT", "15"))
    except ValueError:
        return 15.0


def _http_get(cmd: str, table: str) -> object:
    """Query free-stockdb HTTP API: /?cmd=<cmd>&t=<table>."""
    url = f"{_base_url()}/?cmd={cmd}&t={urllib.parse.quote(table)}"
    with urllib.request.urlopen(url, timeout=_timeout()) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    if not raw.strip():
        return None
    return json.loads(raw)


def _normalize_rows(data: object) -> list:
    """Flatten free-stockdb responses to a list of dicts."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        return [data]
    return []


# === 业务查询（对应 HTTP API） ===


def _month_prefixes(start: str, end: str) -> list[str]:
    """YYYYMM 前缀序列，覆盖 [start, end] 涉及的每个自然月（用于区间查询）。"""
    try:
        y0, m0 = int(start[:4]), int(start[4:6])
        y1, m1 = int(end[:4]), int(end[4:6])
    except ValueError:
        return []
    prefixes = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        prefixes.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return prefixes


def _range_prefixes(start: str, end: str) -> list[str]:
    """短区间按月、长区间按年查询，最终仍由客户端严格过滤日期。"""
    months = _month_prefixes(start, end)
    if len(months) <= 3:
        return months
    return [f"{year:04d}" for year in range(int(start[:4]), int(end[:4]) + 1)]


def query_daily_kline(code: str, start: str, end: str | None, fields: str | None) -> object:
    """日K查询。start 必填：单点(start)或区间(start,end)。

    HTTP 层不支持 start<end 区间语法，只有单点与日期前缀通配。
    短区间按自然月、长区间按自然年前缀通配 vals，最后在客户端过滤 [start, end]。
    """
    if not start:
        return {"error": "get_kline: 日K 查询需要 start（8位日期，如 20260620）"}
    if end:
        rows = []
        for prefix in _range_prefixes(start, end):
            data = _http_get("vals", f"日k:{code}:{prefix}*")
            rows.extend(_normalize_rows(data))
        rows = [row for row in rows if isinstance(row, dict)]
        s, e = int(start), int(end)
        rows = [r for r in rows if s <= int(r.get("date") or 0) <= e]
        rows.sort(key=lambda r: int(r.get("date") or 0))
    else:
        data = _http_get("get", f"日k:{code}:{start}")
        rows = _normalize_rows(data)
    if fields:
        keys = [k.strip() for k in fields.split(",")]
        rows = [{k: r.get(k) for k in keys} for r in rows]
    return rows


def query_minute_kline(code: str, datetime_ts: str) -> object:
    """分钟K查询。datetime_ts 为 14 位时间戳（如 20260625145200）。"""
    if not datetime_ts:
        return {"error": "get_kline: 分钟K 查询需要 datetime（14位，如 20260625145200）"}
    table = f"分钟k:{code}:{datetime_ts}"
    data = _http_get("get", table)
    return _normalize_rows(data)


def query_stock_list() -> object:
    """全市场股票代码。返回 {total, codes}。"""
    data = _http_get("get", "股票代码")
    codes: list[str] = []
    if isinstance(data, dict):
        for group in data.values():
            if isinstance(group, list):
                codes.extend(str(c) for c in group)
    codes = sorted(set(codes))
    return {"total": len(codes), "codes": codes}


def query_adjust_factors(code: str, date_pattern: str) -> object:
    """复权因子。date_pattern 支持通配，如 2026* 或 *。"""
    table = f"复权:{code}:{date_pattern or '*'}"
    data = _http_get("get", table)
    rows: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list) and len(item) == 2:
                key, factors = item[0], item[1]
                date = str(key).split(":")[-1]
                rows.append({"date": date, **factors})
    return rows


def query_market_snapshot(date: str, codes: list[str]) -> object:
    """指定交易日多只股票的单日行情。HTTP 层不支持 code 通配，逐只查询。"""
    results: list[dict] = []
    errors: list[dict] = []
    for code in codes:
        code = code.strip()
        if not code:
            continue
        try:
            data = _http_get("get", f"日k:{code}:{date}")
            rows = _normalize_rows(data)
            if rows:
                results.append(rows[0])
            else:
                errors.append({"code": code, "error": "无数据"})
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            errors.append({"code": code, "error": str(exc)})
    return {"results": results, "errors": errors}


def _parse_yyyymmdd(value: str, *, field: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 8 位日期 YYYYMMDD") from exc


def query_fullmarket_daily_snapshot(
    start: str,
    end: str,
    *,
    codes: list[str] | None = None,
    limit: int = 0,
    workers: int = 16,
    warmup_days: int = 0,
) -> tuple[dict, dict]:
    """拉取日 K 并组装 DailyBar 快照，同时返回完整性诊断。"""
    with _HEAVY_LOCK:
        start_dt = _parse_yyyymmdd(start, field="start")
        end_dt = _parse_yyyymmdd(end, field="end")
        if start_dt > end_dt:
            raise ValueError("start 不能晚于 end")
        workers = max(1, min(int(workers), 32))
        warmup_start = (start_dt - timedelta(days=max(0, warmup_days))).strftime("%Y%m%d")

        universe = query_stock_list()
        raw_codes = [
            str(code).strip()
            for code in ((universe or {}).get("codes") or [])
            if str(code).strip()
        ]
        target_codes = sorted({
            code for code in raw_codes if is_supported_a_share_code(code)
        })
        source_codes = target_codes if codes is None else codes
        requested_codes = list(dict.fromkeys(
            str(code).strip() for code in source_codes if str(code).strip()
        ))
        limit_applied = limit > 0
        if limit > 0:
            requested_codes = requested_codes[:limit]

        def fetch_one(code: str) -> tuple[str, list, str | None]:
            last_error: Exception | None = None
            for _ in range(3):
                try:
                    rows = _normalize_rows(
                        query_daily_kline(code, warmup_start, end, None) or []
                    )
                    return code, rows, None
                except Exception as exc:  # noqa: BLE001 - 保留单股失败诊断
                    last_error = exc
            return code, [], str(last_error)

        code_rows: list[tuple[str, list]] = []
        failed: list[dict[str, str]] = []
        empty_codes: list[str] = []
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(fetch_one, code): code for code in requested_codes}
            for future in as_completed(futures):
                code, rows, error = future.result()
                if error is not None:
                    failed.append({"code": code, "error": error})
                elif not rows:
                    empty_codes.append(code)
                else:
                    code_rows.append((code, rows))

        snapshot: dict[str, list[DailyBar]] = {}
        raw_row_count = 0
        point_in_time_state_unknown_codes: set[str] = set()

        def parse_is_st(value: object) -> bool | None:
            if isinstance(value, bool):
                return value
            normalized = str(value or "").strip().lower()
            if normalized in {"1", "true", "yes"}:
                return True
            if normalized in {"0", "false", "no"}:
                return False
            return None

        for code, rows in sorted(code_rows):
            history_close: float | None = None
            for row in sorted(rows, key=lambda item: int(item.get("date") or 0)):
                try:
                    day = str(row.get("date"))
                    if len(day) != 8:
                        continue
                    close = float(row.get("close"))
                    raw_prev_close = row.get("pre_close")
                    prev_close = (
                        float(raw_prev_close)
                        if raw_prev_close not in (None, "", 0, "0")
                        else history_close
                    )
                    is_st = parse_is_st(row.get("is_st"))
                    if is_st is None:
                        point_in_time_state_unknown_codes.add(code)
                    bar = DailyBar(
                        code=code,
                        close=close,
                        high=float(row.get("high")),
                        low=float(row.get("low")),
                        amount=float(row.get("amount") or 0.0),
                        prev_close=prev_close,
                        open=float(row.get("open")) if row.get("open") not in (None, "") else None,
                        is_st=False if is_st is None else is_st,
                    )
                except (TypeError, ValueError):
                    continue
                date_str = f"{day[:4]}-{day[4:6]}-{day[6:]}"
                snapshot.setdefault(date_str, []).append(bar)
                history_close = close
                raw_row_count += 1

        selected_set = set(requested_codes)
        target_set = set(target_codes)
        scope_is_partial = limit_applied or selected_set != target_set
        coverage_is_complete = (
            not failed
            and not empty_codes
            and {code for code, _ in code_rows} == selected_set
        )
        partial_reasons: list[str] = []
        if limit_applied:
            partial_reasons.append("LIMIT_APPLIED")
        if codes is not None and selected_set != target_set:
            partial_reasons.append("EXPLICIT_CODES_PARTIAL")
        elif scope_is_partial and "LIMIT_APPLIED" not in partial_reasons:
            partial_reasons.append("TARGET_UNIVERSE_MISMATCH")
        if failed:
            partial_reasons.append("SOURCE_REQUEST_FAILED")
        if empty_codes:
            partial_reasons.append("EMPTY_CODE_UNCLASSIFIED")
        if point_in_time_state_unknown_codes:
            partial_reasons.append("POINT_IN_TIME_STATE_UNKNOWN")
        formal_usable = (
            not scope_is_partial
            and coverage_is_complete
            and not point_in_time_state_unknown_codes
        )
        metadata = {
            "raw_instrument_count": len(set(raw_codes)),
            "universe_count": len(target_codes),
            "requested_code_count": len(requested_codes),
            "fetched_code_count": len(code_rows),
            "empty_code_count": len(empty_codes),
            "failed_code_count": len(failed),
            "raw_row_count": raw_row_count,
            "scope_is_partial": scope_is_partial,
            "coverage_is_complete": coverage_is_complete,
            "partial_reasons": partial_reasons,
            "formal_usable": formal_usable,
            "is_partial": scope_is_partial,
            "failed_codes": failed[:100],
            "empty_codes": empty_codes[:100],
            "point_in_time_state_unknown_codes": sorted(
                point_in_time_state_unknown_codes
            )[:100],
            "failed_codes_truncated": len(failed) > 100,
            "empty_codes_truncated": len(empty_codes) > 100,
        }
        return snapshot, metadata


def query_board_open_effect_history(
    start: str,
    end: str,
    *,
    codes: list[str] | None = None,
    limit: int = 0,
    workers: int = 16,
    include_distribution: bool = False,
) -> dict:
    """基于 stockdb 全市场日 K 返回可审计的打板开盘溢价时序。"""
    with _HEAVY_LOCK:
        full_a_share_request = codes is None
        if full_a_share_request:
            raw_universe = query_stock_list()
            codes = [
                code for code in (raw_universe.get("codes") or [])
                if is_supported_a_share_code(str(code))
            ]

        snapshot, metadata = query_fullmarket_daily_snapshot(
            start,
            end,
            codes=codes,
            limit=limit,
            workers=workers,
            warmup_days=20,
        )
        details = compute_board_open_effect_details(snapshot)
        start_iso = _parse_yyyymmdd(start, field="start").date().isoformat()
        end_iso = _parse_yyyymmdd(end, field="end").date().isoformat()
        days = []
        for trade_date, row in sorted(details.items()):
            if not start_iso <= trade_date <= end_iso:
                continue
            item = dict(row)
            if not include_distribution:
                item.pop("distribution", None)
            days.append(item)
        return {
            "source_name": "free-stockdb",
            "source_contract_version": "board-open-effect-stockdb-v4.1",
            "start": start_iso,
            "end": end_iso,
            **metadata,
            "methodology": {
                "sample": "T-1沪深非ST、非一字板收盘涨停股",
                "one_word": "T-1 open=high=low=close=涨停价",
                "limit_price": "前收×(1+10%/20%)，0.01元 ROUND_HALF_UP",
                "value": "(T日开盘价/T-1收盘价-1)×100%",
            },
            "known_limitations": [
                "stockdb 当前未提供可直接校验的上市日期/无涨跌幅限制标记；"
                "新股无涨跌幅限制期内若收盘价恰好等于理论涨停价，日K单源无法完全识别",
            ],
            "days": days,
        }


# === MCP 工具定义 ===


TOOLS: list[dict] = [
    {
        "name": "get_kline",
        "description": (
            "获取A股股票K线数据。frequency 支持 '1d'(日K)、'5m'/'15m'/'30m'/'60m'(分钟K，"
            "分钟K 需传 14 位 datetime 时间戳)。日K 传 8 位日期 start（可加 end 构成开区间 "
            "start..end，按自然月拆分查询）。fields 为逗号分隔投影字段，如 date,open,close,volume。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位股票代码，如 600633"},
                "frequency": {
                    "type": "string",
                    "enum": ["1d", "5m", "15m", "30m", "60m"],
                    "description": "K 线周期；1d=日K，其余为分钟K",
                    "default": "1d",
                },
                "start": {
                    "type": "string",
                    "description": "日K：8位起始日期(如 20260620)；分钟K：14位时间戳(如 20260625145200)",
                },
                "end": {"type": "string", "description": "日K 结束日期(可选，与 start 构成开区间 start<end)"},
                "fields": {"type": "string", "description": "投影字段，逗号分隔（可选）"},
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_stock_list",
        "description": "获取全市场 A 股股票代码列表，返回 {total, codes}。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_adjust_factors",
        "description": "获取股票复权因子。返回 [{date, div, give, trans, mult, cum}, ...]。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位股票代码，如 600633"},
                "date_pattern": {
                    "type": "string",
                    "description": "日期通配，如 2026* 或 *（默认 *）",
                    "default": "*",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "get_market_snapshot",
        "description": "获取指定交易日多只股票的单日行情快照（日K）。返回 {results, errors}。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "交易日，8 位日期，如 20260625"},
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "股票代码列表，如 ['600633', '000001']",
                },
            },
            "required": ["date", "codes"],
        },
    },
    {
        "name": "get_board_open_effect_history",
        "description": (
            "按交易日计算‘T-1非一字板收盘涨停股在T日开盘的溢价’。"
            "默认遍历 stockdb 全市场，返回样本数、匹配数、成功率、均值和分位数。"
            "codes/limit 仅用于调试，使用后 is_partial=true，不得当作市场结论。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "8位起始日期，如 20230101"},
                "end": {"type": "string", "description": "8位结束日期，如 20261231"},
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "可选调试股票列表；传入即为部分市场",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "可选调试上限；0=不截断",
                    "default": 0,
                },
                "workers": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 32,
                    "default": 16,
                },
                "include_distribution": {
                    "type": "boolean",
                    "description": "是否返回每日全部个股溢价序列",
                    "default": False,
                },
            },
            "required": ["start", "end"],
        },
    },
]


def _call_tool(name: str, args: dict) -> dict:
    """Dispatch a tools/call request to a handler. Returns MCP result dict."""
    if name == "get_kline":
        code = str(args.get("code", ""))
        frequency = str(args.get("frequency", "1d"))
        start = str(args.get("start", "") or "")
        end = str(args.get("end", "") or "")
        fields = str(args.get("fields", "") or "") or None
        if frequency == "1d":
            result = query_daily_kline(code, start, end, fields)
        elif frequency in ("5m", "15m", "30m", "60m"):
            result = query_minute_kline(code, start)
        else:
            return {"content": [{"type": "text", "text": json.dumps(
                {"error": f"不支持的周期: {frequency}"}, ensure_ascii=False)}], "isError": True}
    elif name == "get_stock_list":
        result = query_stock_list()
    elif name == "get_adjust_factors":
        result = query_adjust_factors(str(args.get("code", "")), str(args.get("date_pattern", "*")))
    elif name == "get_market_snapshot":
        codes = args.get("codes") or []
        if not isinstance(codes, list):
            return {"content": [{"type": "text", "text": json.dumps(
                {"error": "get_market_snapshot: codes 必须为数组"}, ensure_ascii=False)}], "isError": True}
        result = query_market_snapshot(str(args.get("date", "")), [str(c) for c in codes])
    elif name == "get_board_open_effect_history":
        codes = args.get("codes")
        if codes is not None and not isinstance(codes, list):
            return {"content": [{"type": "text", "text": json.dumps(
                {"error": "get_board_open_effect_history: codes 必须为数组"}, ensure_ascii=False)}], "isError": True}
        result = query_board_open_effect_history(
            str(args.get("start", "")),
            str(args.get("end", "")),
            codes=None if codes is None else [str(code) for code in codes],
            limit=int(args.get("limit", 0) or 0),
            workers=int(args.get("workers", 16) or 16),
            include_distribution=bool(args.get("include_distribution", False)),
        )
    else:
        return {"content": [{"type": "text", "text": json.dumps(
            {"error": f"未知工具: {name}"}, ensure_ascii=False)}], "isError": True}
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


# === MCP 协议（换行分隔 JSON-RPC，stdio / HTTP 共用同一份 dispatch） ===


def _write_message(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle_request(msg: dict) -> dict:
    """核心分发：把一条带 id 的 JSON-RPC 请求映射为响应 dict（id/result 或 id/error）。"""
    method = msg.get("method")
    request_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        client_version = params.get("protocolVersion")
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "protocolVersion": client_version if isinstance(client_version, str) else PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32602, "message": "Invalid params: arguments must be an object"},
            }
        try:
            result = _call_tool(str(tool_name), arguments)
        except Exception as exc:  # noqa: BLE001 - 工具异常转为 MCP 错误响应
            result = {
                "content": [{"type": "text", "text": json.dumps(
                    {"error": str(exc)}, ensure_ascii=False)}],
                "isError": True,
            }
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    else:
        # Speculative/unknown request methods get a JSON-RPC method-not-found error.
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def dispatch(msg: dict) -> dict | None:
    """JSON-RPC 分发纯函数：通知（无 id 或已知通知方法）返回 None，否则返回响应 dict。

    stdio 与 HTTP 共用：请求返回 dict 写回；notification 返回 None 不回写。
    """
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    if method in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
        return None
    if "id" not in msg:
        # 无 id 的未知消息按 JSON-RPC 通知处理（不期待响应）
        return None
    return _handle_request(msg)


def run_stdio() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # 忽略非法帧，不崩溃
        if not isinstance(msg, dict):
            continue  # 畸形消息
        try:
            response = dispatch(msg)
        except Exception as exc:  # noqa: BLE001 - 单条消息失败不影响 server 存活
            response = {
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }
        if response is not None:
            _write_message(response)


# === HTTP 传输（NAS 容器部署主用） ===


class _MCPRequestHandler(http.server.BaseHTTPRequestHandler):
    """只读 JSON-RPC over HTTP：POST /mcp 返回 JSON 响应，GET / 健康检查。"""

    server_version = f"{SERVER_NAME}/{SERVER_VERSION}"

    def do_GET(self) -> None:  # noqa: N802 - 覆写 BaseHTTPRequestHandler 约定命名
        if self.path in ("/", "/health"):
            body = b"stockdb-mcp ok\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - 覆写 BaseHTTPRequestHandler 约定命名
        if self.path != "/mcp":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.strip():
                raise ValueError("空请求体")
            msg = json.loads(raw.decode("utf-8"))
            if not isinstance(msg, dict):
                raise ValueError("请求体必须是 JSON 对象")
            response = dispatch(msg)
        except Exception as exc:  # noqa: BLE001 - 解析失败返回 JSON-RPC parse error
            response = {
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        if response is None:
            # 通知：无 JSON-RPC 响应，返回 202 空体
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        body = json.dumps(response, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        # 走标准日志（stderr），容器日志可观测；与默认行为一致，保留
        super().log_message(fmt, *args)


def run_http(host: str, port: int) -> None:
    """启动 ThreadingHTTPServer（每请求一个线程，多 agent 并发安全）。"""
    httpd = http.server.ThreadingHTTPServer((host, port), _MCPRequestHandler)
    print(
        f"{SERVER_NAME} HTTP server listening on http://{host}:{port} "
        f"(POST /mcp, GET / 健康检查)",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def self_check() -> int:
    """Standalone connectivity smoke test; never presented as market analysis."""
    print(f"free-stockdb base: {_base_url()}")
    try:
        stock_list = query_stock_list()
        print("get_stock_list 自检:", json.dumps(stock_list, ensure_ascii=False)[:200])
    except Exception as exc:  # noqa: BLE001
        print(f"get_stock_list 自检失败: {exc}")
        return 1
    try:
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        sample_codes = list(stock_list.get("codes") or [])[:3]
        sample = {
            code: query_daily_kline(code, start, end, "date,open,close")
            for code in sample_codes
        }
        print(
            "get_kline 连通性抽样（仅自检，非市场分析）:",
            json.dumps(sample, ensure_ascii=False)[:500],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"get_kline 自检失败: {exc}")
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="free-stockdb 只读 MCP server（stdio / HTTP）")
    parser.add_argument(
        "--self-check", action="store_true",
        help="不进入 MCP 循环，仅做连通性自检后退出",
    )
    parser.add_argument(
        "--http", action="store_true",
        help="以 HTTP 模式运行（NAS 容器部署），否则用 stdio 模式",
    )
    parser.add_argument(
        "--host", default=DEFAULT_HTTP_HOST,
        help=f"HTTP 监听地址（默认 {DEFAULT_HTTP_HOST}）",
    )
    parser.add_argument(
        "--port", type=int, default=DEFAULT_HTTP_PORT,
        help=f"HTTP 监听端口（默认 {DEFAULT_HTTP_PORT}）",
    )
    args = parser.parse_args()
    if args.self_check:
        sys.exit(self_check())
    if args.http:
        run_http(args.host, args.port)
    else:
        run_stdio()


if __name__ == "__main__":
    main()
