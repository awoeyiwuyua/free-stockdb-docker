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
    get_kline           K线（日K/分钟K，支持 1m/1w/1M 周期、fq 复权、批量 codes、limit）
    get_stock_list      全市场 A 股代码列表
    get_adjust_factors  复权因子
    get_market_snapshot 指定交易日多只股票的单日行情快照
    get_board_open_effect_history
                        全市场“昨日非一字板涨停、今日开盘溢价”时序
    get_indicators      技术指标计算（pybao，39 项指标，含 zhishu 指数）
    get_board_members   板块 ↔ 股票 双向查询（pybao）
    screen_stocks       全市场条件选股（pybao：板块过滤 + 指标金叉/死叉 + 流通市值 + 剔除ST）
    get_mydb_data       只读 mydb 私有库（pybao：港股日K / AI 自定义表）
    get_trading_days    A股交易日历（休市表覆盖 2024-2026，来源 exchange_calendars XSHG）
    get_data_status     数据基座状态（最新交易日/滞后天数/pybao 可用性/版本/日历覆盖）

    prompts（能力）:
        screen-workflow     条件选股工作流
        limit-up-review     涨停复盘工作流

pybao 为可选外部依赖（容器 /opt/stockdb/pybao，本机 /tmp/pybao_mac 或 PYBAO_DIR）：
get_indicators / get_board_members / screen_stocks / get_mydb_data，以及 get_kline 的
复权/1m/1w/1M/批量能力依赖它；缺失时相关能力返回明确降级错误，其余工具不受影响。
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
import time
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

import calendar_xshg  # noqa: E402  - A 股交易日历（同目录模块，休市表与 app.py 保持一致）

try:  # noqa: E402  - pybao_tools 为同目录模块（_MCP_DIR 已插入 sys.path）
    import pybao_tools
except ImportError:  # 防御：模块缺失时 get_indicators/get_board_members 返回明确错误而非崩溃
    pybao_tools = None  # type: ignore[assignment]

try:  # noqa: E402  - query_mydb / screen_stocks 由 Task A2 加入 pybao_tools（Phase 2）
    from pybao_tools import query_mydb, screen_stocks
except ImportError:  # 防御：与 pybao_tools=None 同语义，相关工具返回明确降级错误
    query_mydb = None  # type: ignore[assignment]
    screen_stocks = None  # type: ignore[assignment]

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "stockdb-native"
SERVER_VERSION = "0.1.0"

DEFAULT_HOST = "100.66.1.1"
DEFAULT_PORT = 7899
DEFAULT_HTTP_HOST = "0.0.0.0"
DEFAULT_HTTP_PORT = 8080

# K线周期枚举（8 项）；1m/1w/1M 与复权(fq)/批量(codes) 走 pybao SDK 路径，其余走 HTTP
KLINE_FREQUENCIES = ("1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M")
_SDK_KLINE_FREQUENCIES = frozenset({"1m", "1w", "1M"})
_KLINE_CODES_MAX = 50  # 批量 codes 上限

# 重型工具串行化锁：query_board_open_effect_history 内部会嵌套调用
# query_fullmarket_daily_snapshot（两者都会开线程池拉全市场），用 RLock 避免同线程二次获取死锁。
_HEAVY_LOCK = threading.RLock()

# === 统一错误码契约（本批全局） ===
# server.py isError content 统一为 {"error": str, "code": str}（PYBAO_UNAVAILABLE 附加
# "hint"）；pybao_tools outcome 携带的 code/hint 直接透传，缺省按文案推断。
ERROR_PARAM_INVALID = "PARAM_INVALID"
ERROR_PYBAO_UNAVAILABLE = "PYBAO_UNAVAILABLE"
ERROR_UNKNOWN_TOOL = "UNKNOWN_TOOL"
ERROR_INTERNAL = "INTERNAL"
ERROR_DATA_NOT_FOUND = "DATA_NOT_FOUND"

_PYBAO_HINT = (
    "容器镜像自动携带（/opt/stockdb/pybao）；本机开发请把 macOS 版 pybao 放 "
    "/tmp/pybao_mac 或设 PYBAO_DIR"
)
# 推断 INTERNAL 的文案标记（与 pybao_tools 的中文错误后缀一致）
_INTERNAL_ERROR_MARKERS = ("查询失败", "计算失败", "加工失败", "读取失败")


class _TTLCache:
    """线程安全 TTL 缓存（threading.Lock；惰性过期，TTL=300s）。

    get(key) 命中且未过期返回缓存值，否则返回 None（过期项惰性删除）；
    set(key, value) 写入当前时间戳。应用于 query_stock_list / query_adjust_factors /
    _latest_trade_date，返回形状不变，仅减少对 stockdb HTTP 的重复查询。
    """

    def __init__(self, ttl: float = 300.0):
        self._ttl = ttl
        self._lock = threading.Lock()
        self._entries: dict[object, tuple[float, object]] = {}

    def get(self, key: object) -> object | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            created, value = entry
            if time.monotonic() - created > self._ttl:
                del self._entries[key]
                return None
            return value

    def set(self, key: object, value: object) -> None:
        with self._lock:
            self._entries[key] = (time.monotonic(), value)


_TTL = _TTLCache(ttl=300.0)  # 全局 TTL 缓存：stock_list / adj / latest_date


def _notify_progress(stage: str, detail: str | None = None) -> None:
    """通过 pybao_tools.notify_progress 上报进度阶段（快照开始/完成等）。

    pybao_tools 缺失或未提供 notify_progress 时静默跳过；回调异常不影响查询。
    进度钩子由调用方（app SSE 推送等）在当前线程 set_progress_hook 设置。
    """
    if pybao_tools is None:
        return
    if not hasattr(pybao_tools, "notify_progress"):
        return
    try:
        pybao_tools.notify_progress(stage, detail)
    except Exception:  # noqa: BLE001 - 进度通知失败不影响查询
        pass


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


def _as_limit(value: object) -> int:
    """limit 参数：缺省/0 = 不截断；非法输入抛中文 ValueError。"""
    if value in (None, ""):
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError("get_kline: limit 必须是整数") from None
    return max(0, number)


def _pybao_sdk_client() -> object | None:
    """pybao SDK client（可为 None）。pybao_tools 缺失时同样返回 None。"""
    if pybao_tools is None:
        return None
    return pybao_tools.get_sdk_client()


def _query_kline_via_sdk(
    *,
    code: str,
    codes: list[str],
    frequency: str,
    fq: str,
    limit: int,
    fields: str | None,
    start: str,
    end: str,
) -> dict:
    """pybao SDK 路径：复权(fq)/1m/1w/1M 周期/批量 codes。client 不可用抛中文 ValueError。

    对接 stock_sdk 真实接口 StockDBClient.get_data(code|codes, start, end,
    frequency, fields=None, fq, limit=None)：单码返回行列表，批量返回
    {code: rows}；复权/周月K 聚合由 SDK 内存完成；8 位日期在分钟频率下
    由 SDK 自动补全为 14 位时间戳区间。返回 {"source", "code", "codes",
    "frequency", "data", "total", "truncated"}；limit>0 时每码保留最新 limit 行。
    """
    client = _pybao_sdk_client()
    if client is None:
        raise ValueError(
            "pybao 不可用：复权/1m/1w/1M/批量 K 线查询需要 pybao SDK"
            "（容器 /opt/stockdb/pybao 或 PYBAO_DIR）"
        )
    target_codes = codes or ([code] if code else [])
    sdk_fq = None if fq in ("", "none") else fq
    try:
        raw = client.get_data(
            target_codes[0] if len(target_codes) == 1 else target_codes,
            start=start or None,
            end=end or None,
            frequency=frequency,
            fields=None,
            fq=sdk_fq,
            limit=None,
        )
    except Exception as exc:  # noqa: BLE001 - 查询失败转为中文 ValueError
        raise ValueError(f"get_kline: pybao 查询失败: {type(exc).__name__}: {exc}") from exc

    # 归一化：批量 → {code: rows}，单码 → 行列表
    if isinstance(raw, dict):
        per_code = {item: _normalize_rows(rows) for item, rows in raw.items()}
    else:
        per_code = {target_codes[0]: _normalize_rows(raw)}
    # 防御：单码查询时若 SDK 返回空 dict {}（而非空 list）导致 per_code 缺键，
    # 补齐空行列表，避免下方 per_code[target_codes[0]]/total_by_code 抛 KeyError
    # 落入 tools/call 的 INTERNAL 兜底（真实 SDK 单码返回 list 不触发该分支）。
    if len(target_codes) == 1 and target_codes[0] not in per_code:
        per_code[target_codes[0]] = []
    # fields 投影（SDK 的 fields 参数返回二维值列表，这里统一客户端投影为 dict 行）
    if fields:
        keys = [k.strip() for k in fields.split(",")]
        per_code = {
            item: [{k: row.get(k) for k in keys} for row in rows]
            for item, rows in per_code.items()
        }
    total_by_code = {item: len(rows) for item, rows in per_code.items()}
    truncated = limit > 0 and any(n > limit for n in total_by_code.values())
    if limit > 0:
        per_code = {
            item: rows[-limit:] if len(rows) > limit else rows
            for item, rows in per_code.items()
        }
    result = {
        "source": "pybao",
        "code": code or target_codes[0],
        "codes": codes,
        "frequency": frequency,
        "data": per_code[target_codes[0]] if len(target_codes) == 1 else per_code,
        "total": (
            total_by_code[target_codes[0]]
            if len(target_codes) == 1
            else total_by_code
        ),
        "truncated": truncated,
    }
    # 非交易日提示：data 为空 且 start 为 8 位日期 且非交易日时附加 hint（与 HTTP 路径一致）
    if all(len(rows) == 0 for rows in per_code.values()):
        hint = _non_trading_day_hint(start)
        if hint is not None:
            result["hint"] = hint
    return result


def _non_trading_day_hint(start: str) -> str | None:
    """start 为 8 位日期且为非交易日时返回提示文案，否则 None（非法日期同样返回 None）。

    供 get_kline 空结果时附加 hint（如 "20260101 非交易日；最近交易日 20251231"）。
    """
    if len(start) != 8 or not start.isdigit():
        return None
    try:
        if calendar_xshg.is_trading_day(start):
            return None
    except ValueError:
        return None
    nearest = calendar_xshg.nearest_trading_day(start)
    if nearest is None:
        return f"{start} 非交易日"
    return f"{start} 非交易日；最近交易日 {nearest}"


def query_kline(args: dict) -> dict:
    """K线查询（HTTP 降级路径 + pybao SDK 增强路径）。

    - HTTP 路径：1d/5m/15m/30m/60m，走 free-stockdb HTTP API（无需 pybao）
    - pybao SDK 路径：fq 复权、1m/1w/1M 周期、批量 codes（需 pybao，不可用时报明确降级错误）
    参数非法时抛 ValueError（中文文案），由 _call_tool 转 isError。
    返回 {"source", "code", "frequency", "data", "total", "truncated"}。
    """
    frequency = str(args.get("frequency", "1d") or "1d")
    fq = str(args.get("fq", "none") or "none").lower()
    limit = _as_limit(args.get("limit"))
    fields = str(args.get("fields", "") or "") or None
    code = str(args.get("code", "") or "")
    raw_codes = args.get("codes")
    if raw_codes is not None and not isinstance(raw_codes, list):
        raise ValueError("get_kline: codes 必须为数组")
    codes = [str(item).strip() for item in (raw_codes or []) if str(item).strip()]
    if not code and not codes:
        raise ValueError("get_kline: code 与 codes 至少提供一个")
    if codes and len(codes) > _KLINE_CODES_MAX:
        raise ValueError(f"get_kline: codes 最多 {_KLINE_CODES_MAX} 个")
    if frequency not in KLINE_FREQUENCIES:
        raise ValueError(f"get_kline: 不支持的周期: {frequency}")
    if fq not in ("", "none", "qfq", "hfq"):
        raise ValueError(f"get_kline: 不支持的复权方式: {fq}")

    start = str(args.get("start", "") or "")
    end = str(args.get("end", "") or "")
    needs_sdk = fq not in ("", "none") or frequency in _SDK_KLINE_FREQUENCIES or bool(codes)
    if needs_sdk:
        return _query_kline_via_sdk(
            code=code, codes=codes, frequency=frequency, fq=fq,
            limit=limit, fields=fields, start=start, end=end,
        )

    if frequency == "1d":
        data = query_daily_kline(code, start, end, fields)
    else:  # 5m/15m/30m/60m 分钟K
        data = query_minute_kline(code, start)
    if isinstance(data, dict) and "error" in data:
        raise ValueError(f"get_kline: {data['error']}")
    rows = [row for row in _normalize_rows(data) if isinstance(row, dict)]
    total = len(rows)
    truncated = limit > 0 and total > limit
    kept = rows[-limit:] if truncated else rows  # 保留最新 limit 行
    result = {
        "source": "http",
        "code": code,
        "frequency": frequency,
        "data": kept,
        "total": total,
        "truncated": truncated,
    }
    if not kept:
        hint = _non_trading_day_hint(start)
        if hint is not None:
            result["hint"] = hint
    return result


def query_screen(args: dict) -> dict:
    """全市场条件选股（pybao）：解析筛选条件 → 决定股票池 → screen_stocks。

    股票池解析顺序：codes（调试限定）> board（板块成分股）> 全市场 A 股；
    每个来源都带 universe.source / universe.count 审计字段。codes 传入即
    is_partial=True（调试语义，同 get_board_open_effect_history 先例），并标记
    partial_reasons=["EXPLICIT_CODES_DEBUG"]，结果不得当作市场结论。
    参数非法抛中文 ValueError，由 _call_tool 转 isError。
    成功返回 screen_stocks 的 result 并并入 universe/is_partial/partial_reasons。
    """
    # === 参数校验（先于任何网络/pybao 访问，离线即可报错） ===
    codes = args.get("codes")
    board = args.get("board")
    if codes is not None:
        if not isinstance(codes, list):
            raise ValueError("screen_stocks: codes 必须为数组")
        if not 1 <= len(codes) <= 200:
            raise ValueError(f"screen_stocks: codes 数量必须为 1-200，当前 {len(codes)} 个")
        normalized_codes: list[str] = []
        for item in codes:
            code = str(item).strip()
            if len(code) != 6 or not code.isdigit():
                raise ValueError(f"screen_stocks: 股票代码 {item!r} 必须是 6 位数字")
            normalized_codes.append(code)
        codes = normalized_codes
    if board is not None:
        if not isinstance(board, dict):
            raise ValueError("screen_stocks: board 必须为对象")
        board_name = str(board.get("name") or "").strip()
        if not board_name:
            raise ValueError("screen_stocks: board.name 不能为空")

    # pybao 依赖检查：board 分支与最终计算都需要
    # （_call_tool 已先行拦截，此处为直接调用本函数时的防御降级）
    if pybao_tools is None:
        raise ValueError(
            "pybao 不可用：全市场条件选股需要 pybao（容器 /opt/stockdb/pybao 或 PYBAO_DIR）"
        )

    # === 决定股票池 ===
    if codes is not None:
        universe = codes
        universe_source = "codes"
    elif board is not None:
        outcome = pybao_tools.query_boards({
            "query": board_name,
            "category": board.get("category"),
            "include_symbols": True,
        })
        if not outcome.get("ok"):
            raise _ToolError(
                outcome.get("error", "未知错误"),
                outcome.get("code"),
                outcome.get("hint"),
            )
        symbols = (outcome.get("result") or {}).get("symbols") or []
        if not symbols:
            raise ValueError("board 无成分股")
        universe = [str(symbol) for symbol in symbols]
        universe_source = f"board:{board_name}"
    else:
        stock_list = query_stock_list()
        universe = [
            str(code) for code in (stock_list.get("codes") or [])
            if is_supported_a_share_code(str(code))
        ]
        universe_source = "full_market"

    # === 执行筛选，并在 result 中并入股票池审计字段 ===
    out = pybao_tools.screen_stocks(args, universe, universe_source=universe_source)
    if not out.get("ok"):
        raise _ToolError(
            out.get("error", "未知错误"),
            out.get("code"),
            out.get("hint"),
        )
    result = out.get("result")
    result["universe"] = {"source": universe_source, "count": len(universe)}
    result["is_partial"] = codes is not None
    if codes is not None:
        result["partial_reasons"] = ["EXPLICIT_CODES_DEBUG"]
    return result


def query_stock_list() -> object:
    """全市场股票代码。返回 {total, codes}。结果 TTL 缓存 300s（key "stock_list"）。"""
    cached = _TTL.get("stock_list")
    if cached is not None:
        return cached
    data = _http_get("get", "股票代码")
    codes: list[str] = []
    if isinstance(data, dict):
        for group in data.values():
            if isinstance(group, list):
                codes.extend(str(c) for c in group)
    codes = sorted(set(codes))
    result = {"total": len(codes), "codes": codes}
    _TTL.set("stock_list", result)
    return result


def query_adjust_factors(code: str, date_pattern: str) -> object:
    """复权因子。date_pattern 支持通配，如 2026* 或 *。结果 TTL 缓存 300s。"""
    cache_key = ("adj", code, date_pattern)
    cached = _TTL.get(cache_key)
    if cached is not None:
        return cached
    table = f"复权:{code}:{date_pattern or '*'}"
    data = _http_get("get", table)
    rows: list[dict] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, list) and len(item) == 2:
                key, factors = item[0], item[1]
                date = str(key).split(":")[-1]
                rows.append({"date": date, **factors})
    _TTL.set(cache_key, rows)
    return rows


def _latest_trade_date() -> str | None:
    """探针最新交易日：当前月与前 2 个月的 YYYYMM 前缀逐个 _http_get("get", "日k:000001:<YYYYMM>*")，
    取全部行最大 date（int→str 8 位）；全空返回 None。结果 TTL 缓存 300s（key "latest_date"）。

    失败的前缀静默跳过（单个前缀不影响整体）；网络全失败时返回 None，由调用方降级。
    """
    cached = _TTL.get("latest_date")
    if cached is not None:
        return cached
    now = datetime.now()
    year, month = now.year, now.month
    prefixes: list[str] = []
    for _ in range(3):
        prefixes.append(f"{year:04d}{month:02d}")
        month -= 1
        if month < 1:
            month, year = 12, year - 1
    max_date: int | None = None
    for prefix in prefixes:
        try:
            data = _http_get("get", f"日k:000001:{prefix}*")
        except Exception:  # noqa: BLE001 - 探针失败继续下一前缀
            continue
        # cmd=get 通配返回 [键, 行] 对列表；兼容 dict/行列表两种形态
        if isinstance(data, dict):
            rows: list[object] = list(data.values())
        else:
            rows = [
                item[1]
                if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict)
                else item
                for item in (data or [])
            ]
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                day = int(row.get("date"))
            except (TypeError, ValueError):
                continue
            if max_date is None or day > max_date:
                max_date = day
    latest = None if max_date is None else str(max_date)
    _TTL.set("latest_date", latest)
    return latest


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
        _notify_progress("snapshot_start")
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
        _notify_progress("snapshot_done")
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


def get_trading_days(args: dict) -> dict:
    """A股交易日历查询（无 pybao 依赖，纯 calendar_xshg 休市表）。

    start 必填（8 位）；end 缺省 = start 后 90 个自然日；limit 为返回上限
    （硬上限 400，超出截断并标记 truncated）。参数非法抛中文 ValueError，
    由 _call_tool 转 isError（code=PARAM_INVALID）。
    """
    start = str(args.get("start") or "")
    if len(start) != 8 or not start.isdigit():
        raise ValueError("get_trading_days: start 必须是 8 位日期 YYYYMMDD")
    end = str(args.get("end") or "")
    if end:
        if len(end) != 8 or not end.isdigit():
            raise ValueError("get_trading_days: end 必须是 8 位日期 YYYYMMDD")
    else:
        end = (datetime.strptime(start, "%Y%m%d") + timedelta(days=90)).strftime("%Y%m%d")
    try:
        limit = int(args.get("limit", 60) or 60)
    except (TypeError, ValueError):
        raise ValueError("get_trading_days: limit 必须是整数") from None
    if limit < 1:
        raise ValueError("get_trading_days: limit 必须是正整数")
    limit = min(limit, 400)  # 硬上限 400，超出截断
    days = calendar_xshg.trading_days_between(start, end)
    truncated = len(days) > limit
    kept = days[:limit] if truncated else days
    return {
        "trading_days": kept,
        "count": len(kept),
        "truncated": truncated,
        "calendar_through": calendar_xshg.XSHG_HOLIDAYS_THROUGH,
        "note": "休市表数据截至 2026 年；2027 官方安排公布后更新",
    }


def get_data_status() -> dict:
    """数据基座状态（无 pybao 依赖）：最新交易日与滞后天数、pybao 可用性、版本与日历覆盖。"""
    latest = _latest_trade_date()
    lag_days = None
    if latest is not None:
        try:
            lag_days = (datetime.now() - datetime.strptime(latest, "%Y%m%d")).days
        except ValueError:
            lag_days = None
    pybao_available = (
        pybao_tools is not None and pybao_tools.get_pybao() is not None
    )
    return {
        "latest_trade_date": latest,
        "lag_days": lag_days,
        "pybao_available": pybao_available,
        "server_version": SERVER_VERSION,
        "tool_count": len(TOOLS),
        "calendar_through": calendar_xshg.XSHG_HOLIDAYS_THROUGH,
    }


# === MCP 工具定义 ===


TOOLS: list[dict] = [
    {
        "name": "get_kline",
        "description": (
            "获取A股K线。frequency: 1d日K、5m/15m/30m/60m分钟K(HTTP)、"
            "1m/1w/1M(需pybao)。日K传8位start(可加end构成区间)；分钟K传8位日期或14位时间戳。"
            "fq复权(qfq/hfq)、批量codes需pybao(容器镜像自动携带)，不可用时返回明确降级错误。"
            "fields为逗号分隔投影字段。limit>0时只保留最新limit行并标记truncated。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "6 位股票代码，如 600633（与 codes 二选一）"},
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "批量股票代码列表，最多 50 个（与 code 二选一；需 pybao SDK）",
                },
                "frequency": {
                    "type": "string",
                    "enum": ["1m", "5m", "15m", "30m", "60m", "1d", "1w", "1M"],
                    "description": "K 线周期；1d=日K，5m/15m/30m/60m=分钟K，1m/1w/1M 需 pybao SDK",
                    "default": "1d",
                },
                "fq": {
                    "type": "string",
                    "enum": ["none", "qfq", "hfq"],
                    "description": "复权方式；none=不复权(默认)，qfq=前复权，hfq=后复权（需 pybao SDK）",
                    "default": "none",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "最多返回行数；0=不截断（默认）",
                    "default": 0,
                },
                "start": {
                    "type": "string",
                    "description": "日K：8位起始日期(如 20260620)；分钟K：14位时间戳(如 20260625145200)",
                },
                "end": {"type": "string", "description": "日K 结束日期(可选，与 start 构成开区间 start<end)"},
                "fields": {"type": "string", "description": "投影字段，逗号分隔（可选）"},
            },
            "required": [],
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
                "date": {
                    "type": "string",
                    "description": "交易日，8 位日期，如 20260625；缺省=最新交易日（自动探测）",
                },
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "股票代码列表，如 ['600633', '000001']",
                },
            },
            "required": ["codes"],
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
    {
        "name": "get_indicators",
        "description": (
            "批量计算A股技术指标（39项，含自建指数zhishu）。支持多股票×多指标×多参数一次计算、"
            "金叉/死叉信号(cross)、前/后复权(fq)。依赖pybao指标库（容器镜像自动携带，缺省报错并给指引）。"
            "常用默认参数：macd=12,26,9；kdj=9,3,3；rsi=24；boll=20,2。"
            "基础指标(ma/ema/sma/wma/dma/std/sum/hhv/llv/ref)可用fields指定计算字段。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "indicators": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "指标名列表，1-8个，如 ['macd','kdj']。支持：ma,ema,sma,wma,dma,std,sum,hhv,llv,ref,macd,kdj,rsi,wr,bias,boll,psy,cci,atr,bbi,dmi,taq,ktn,trix,vr,cr,emv,dpo,brar,dfma,mtm,mass,roc,expma,obv,mfi,asi,xsii,zhishu",
                },
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "6位股票代码列表，1-50个，如 ['600633','000001']",
                },
                "params": {
                    "type": "array",
                    "items": {"type": ["string", "integer", "null"]},
                    "description": "与indicators逐项对齐的参数，如 ['5,10,20', null]；单指标可直接传标量",
                },
                "frequency": {
                    "type": "string",
                    "enum": ["1d", "1m", "5m", "15m", "30m", "60m", "1w", "1M"],
                    "description": "指标计算周期",
                    "default": "1d",
                },
                "start": {
                    "type": "string",
                    "description": "8位起始日期(如20260601)。缺省=最近120个自然日（保证MACD类指标收敛）",
                },
                "end": {
                    "type": "string",
                    "description": "8位结束日期；\"N\"=最新。缺省=N",
                },
                "cross": {
                    "type": ["boolean", "string"],
                    "description": "false=原始值；true=仅金叉死叉信号；\"with_value\"=信号+原始值",
                    "default": False,
                },
                "fq": {
                    "type": "string",
                    "enum": ["qfq", "hfq", "none"],
                    "description": "复权方式，默认qfq",
                    "default": "qfq",
                },
                "fields": {
                    "type": "string",
                    "description": "仅基础指标组：计算字段(open/high/low/close/volume/amount)，逗号分隔",
                },
                "method": {
                    "type": "integer",
                    "enum": [1, 2, 3, 4, 5],
                    "description": "zhishu专用加权方法：1平权/2流通市值/3成交额/4成交量/5总市值",
                    "default": 1,
                },
                "base": {
                    "type": "number",
                    "description": "zhishu指数基点",
                    "default": 1000,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "每只股票最多返回行数（硬上限1000），保留最新行；截断返回truncated标记",
                    "default": 500,
                },
                "compact": {
                    "type": "boolean",
                    "description": "True=列式(dates+指标数组，省token)，False=行列表",
                    "default": True,
                },
            },
            "required": ["indicators", "codes"],
        },
    },
    {
        "name": "get_board_members",
        "description": (
            "板块↔股票双向映射（概念/申万一级/申万二级/申万三级）。传6位股票代码→查所属板块；"
            "传板块名称(支持模糊)或板块代码(如801760.SL)→查成分股。依赖pybao（容器镜像自动携带，"
            "缺省报错并给指引）。include_symbols=True时返回合并去重的成分股列表（上限500，超出截断）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "6位股票代码 / 板块代码 / 板块名称（模糊匹配）"},
                "category": {
                    "type": ["string", "integer"],
                    "enum": ["概念", "申万一级", "申万二级", "申万三级", 0, 1, 2, 3],
                    "description": "板块分类；整数0-3亦可用；不传=全部",
                },
                "fields": {
                    "type": "string",
                    "description": "投影字段，逗号分隔；symbols成分股需include_symbols=true",
                    "default": "code,name,type,group,category",
                },
                "include_symbols": {
                    "type": "boolean",
                    "description": "是否返回成分股代码列表（上限500，超出截断）",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "成分股最多返回条数（硬上限500）",
                    "default": 500,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "screen_stocks",
        "description": (
            "全市场条件选股：单次调用完成 板块过滤 + 指标金叉/死叉"
            "（39种指标除zhishu）+ 流通市值区间 + 剔除ST，返回候选列表。"
            "耗时参考：板块范围约 15-20 秒，全市场约 1-2 分钟（pybao 批量计算）。"
            "不传 board 时扫全市场；"
            "codes 仅用于调试（传入即 is_partial=true，结果不得当作市场结论）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "board": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "板块名称（支持模糊），如 算力",
                        },
                        "category": {
                            "type": ["string", "integer"],
                            "description": "概念/申万一级/申万二级/申万三级 或 0-3",
                        },
                    },
                    "description": "可选：限定板块成分股",
                },
                "indicator_cross": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "指标名（39种中除 zhishu），如 macd/ma/kdj",
                        },
                        "golden": {
                            "type": "boolean",
                            "default": True,
                            "description": "true=金叉；false=死叉",
                        },
                        "within_days": {
                            "type": "integer",
                            "default": 5,
                            "description": "最近 N 个交易日内出现（1-60）",
                        },
                    },
                    "description": "可选：指标交叉信号条件",
                },
                "float_mv_min": {
                    "type": "number",
                    "description": "流通市值下限（与日K float_mv 同单位，元；1亿元=1e8）",
                },
                "float_mv_max": {"type": "number", "description": "流通市值上限"},
                "exclude_st": {
                    "type": "boolean",
                    "default": True,
                    "description": "剔除 ST（默认开启）",
                },
                "date": {
                    "type": "string",
                    "description": "截面日期 8 位（默认最新交易日）",
                },
                "limit": {
                    "type": "integer",
                    "default": 50,
                    "description": "候选返回上限（1-200）",
                },
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "调试用：限定股票池（1-200；传入即 is_partial=true）",
                },
            },
            "required": [],
        },
    },
    {
        "name": "get_mydb_data",
        "description": (
            "读取 mydb 私有库（港股日K表 hk日k、AI 写入的自定义表）。只读；"
            "依赖 pybao（容器自动携带）。key 形如 00700:20260813 或前缀通配"
            "（如 00700:*）；不传 key 列出表内全部键值（上限500，超出截断）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "string",
                    "description": "表名，如 hk日k 或自定义表（禁止上游保留表名）",
                },
                "key": {
                    "type": "string",
                    "description": "键或前缀通配（可选；缺省列出全表）",
                },
                "limit": {
                    "type": "integer",
                    "default": 100,
                    "description": "key 缺省时全表键值上限（硬上限500）",
                },
            },
            "required": ["table"],
        },
    },
    {
        "name": "get_trading_days",
        "description": (
            "A股交易日历（休市表覆盖 2024-2026，来源 exchange_calendars XSHG）。"
            "返回区间内交易日列表；用于判断某日是否开盘、计算 N 个交易日区间。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "8位起始日期（含）"},
                "end": {
                    "type": "string",
                    "description": "8位结束日期（含）；缺省=start 后 90 个自然日",
                },
                "limit": {
                    "type": "integer",
                    "default": 60,
                    "description": "返回上限（硬上限400，超出截断）",
                },
            },
            "required": ["start"],
        },
    },
    {
        "name": "get_data_status",
        "description": (
            "数据基座状态：全市场行情最新交易日与滞后天数、pybao 指标库可用性、"
            "MCP 版本与工具数、交易日历覆盖范围。用于判断数据新鲜度与能力边界。"
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


# === prompts 能力（screen-workflow / limit-up-review） ===


PROMPTS: list[dict] = [
    {
        "name": "screen-workflow",
        "description": "条件选股工作流",
        "content": (
            "条件选股工作流（screen_stocks）：\n"
            "1. 缩小股票池优先：能传 board（板块名+可选 category）就传，板块范围约 15-20 秒；"
            "不传 board 即扫全市场，约 1-2 分钟，先评估必要性。\n"
            "2. indicator_cross 可传数组：多个指标交叉条件需同时满足（AND 语义）；"
            "within_days 的语义是「最近 N 个交易日」内出现金叉/死叉信号（1-60）。\n"
            "3. float_mv_min / float_mv_max 单位与日K float_mv 字段一致为「元」："
            "1亿元 = 1e8，不要按亿传。\n"
            "4. codes 仅用于调试（限定股票池）：传入即 is_partial=true，结果不得当作市场结论，"
            "汇报时必须注明是调试样本。\n"
            "5. 结果解读：candidates 为最终候选（含 code/cross_date/signal/float_mv/is_st）；"
            "dropped 为剔除计数（missing_bar/st/mv）；methodology 说明交叉窗口与单位；"
            "truncated=true 表示候选被截断（超 limit）；universe 给出股票池来源与数量。\n"
            "6. 汇报时说明 date（截面日期）与 universe 来源，区分「板块结论」与「全市场结论」。"
        ),
    },
    {
        "name": "limit-up-review",
        "description": "涨停复盘工作流",
        "content": (
            "涨停复盘工作流：\n"
            "1. 用 get_board_open_effect_history 取「T-1 收盘涨停（非一字板）股在 T 日开盘溢价」时序："
            "start/end 为 8 位日期区间，返回 days 数组（每交易日样本数、溢价均值/分位数）。\n"
            "2. 注意 is_partial 语义：传 codes/limit 调试时 is_partial=true，该结果不得当作全市场结论。\n"
            "3. 对样本股用 get_indicators 计算情绪指标（如 macd/kdj/rsi，可选 cross 信号），"
            "观察涨停后的量价关系与指标状态，形成个股→板块→市场的复盘链条。\n"
            "4. 用 get_trading_days 核对交易日：开盘溢价按交易日对齐，跨周末/节假日时确认 start/end "
            "落在交易日上，避免把非交易日当作 T 日。\n"
            "5. 复盘输出应区分「全市场时序结论」与「样本股指标明细」，注明数据来源、日期区间"
            "与 is_partial 状态。"
        ),
    },
]


# === 统一错误码：isError content = {"error": str, "code": str}(, "hint") ===


def _error_result(message: str, code: str, *, hint: str | None = None) -> dict:
    """构造 MCP 工具 isError 结果：content 文本为 {"error": str, "code": str}（契约统一错误码）。"""
    payload: dict[str, object] = {"error": message, "code": code}
    if hint:
        payload["hint"] = hint
    return {
        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
        "isError": True,
    }


class _ToolError(ValueError):
    """带错误码/提示的工具错误：由 query_screen 等业务函数抛出，_call_tool 透传 code/hint。"""

    def __init__(self, message: str, code: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _resolve_error(message: str, code: object, hint: object) -> tuple[str, str | None]:
    """错误码解析：显式 code 优先；缺省按文案推断（pybao 不可用→PYBAO_UNAVAILABLE 等）。"""
    if isinstance(code, str) and code:
        return code, (hint if isinstance(hint, str) and hint else None)
    if "pybao 不可用" in message:
        return ERROR_PYBAO_UNAVAILABLE, (
            hint if isinstance(hint, str) and hint else _PYBAO_HINT
        )
    if any(marker in message for marker in _INTERNAL_ERROR_MARKERS):
        return ERROR_INTERNAL, (hint if isinstance(hint, str) and hint else None)
    return ERROR_PARAM_INVALID, (hint if isinstance(hint, str) and hint else None)


def _pybao_outcome_error(outcome: dict) -> dict:
    """pybao_tools outcome（ok=False）→ isError：code/hint 透传，缺省按规则推断。"""
    message = str(outcome.get("error") or "未知错误")
    code, hint = _resolve_error(message, outcome.get("code"), outcome.get("hint"))
    return _error_result(message, code, hint=hint)


def _value_error_result(exc: ValueError) -> dict:
    """ValueError（含 _ToolError）→ isError：携带 code/hint 时透传，否则按文案推断。"""
    message = str(exc)
    code, hint = _resolve_error(
        message, getattr(exc, "code", None), getattr(exc, "hint", None)
    )
    return _error_result(message, code, hint=hint)


def _pybao_tool_missing_error(tool_name: str) -> dict:
    """pybao_tools 模块整体缺失时的明确降级错误（code=PYBAO_UNAVAILABLE + hint）。"""
    return _error_result(
        f"{tool_name}: pybao 工具模块不可用", ERROR_PYBAO_UNAVAILABLE, hint=_PYBAO_HINT
    )


def _call_tool(name: str, args: dict) -> dict:
    """Dispatch a tools/call request to a handler. Returns MCP result dict.

    所有 isError 分支按统一错误码契约返回 content={"error", "code"(, "hint")}：
    参数类 PARAM_INVALID；pybao 相关 PYBAO_UNAVAILABLE（带 hint）；未知工具
    UNKNOWN_TOOL；其余 INTERNAL；pybao_tools outcome 的 code/hint 直接透传。
    """
    if name == "get_kline":
        try:
            result = query_kline(args)
        except ValueError as exc:
            return _value_error_result(exc)
    elif name == "get_stock_list":
        result = query_stock_list()
    elif name == "get_adjust_factors":
        result = query_adjust_factors(str(args.get("code", "")), str(args.get("date_pattern", "*")))
    elif name == "get_market_snapshot":
        codes = args.get("codes") or []
        if not isinstance(codes, list):
            return _error_result(
                "get_market_snapshot: codes 必须为数组", ERROR_PARAM_INVALID
            )
        date = str(args.get("date") or "")
        if not date:
            # date 缺省 = 最新交易日探针；探针失败（无法确定）时报 DATA_NOT_FOUND
            date = _latest_trade_date() or ""
            if not date:
                return _error_result(
                    "无法确定最新交易日，请显式传 date", ERROR_DATA_NOT_FOUND
                )
        result = query_market_snapshot(date, [str(c) for c in codes])
    elif name == "get_board_open_effect_history":
        codes = args.get("codes")
        if codes is not None and not isinstance(codes, list):
            return _error_result(
                "get_board_open_effect_history: codes 必须为数组", ERROR_PARAM_INVALID
            )
        try:
            result = query_board_open_effect_history(
                str(args.get("start", "")),
                str(args.get("end", "")),
                codes=None if codes is None else [str(code) for code in codes],
                limit=int(args.get("limit", 0) or 0),
                workers=int(args.get("workers", 16) or 16),
                include_distribution=bool(args.get("include_distribution", False)),
            )
        except ValueError as exc:
            return _value_error_result(exc)
    elif name == "get_indicators":
        if pybao_tools is None:
            return _pybao_tool_missing_error("get_indicators")
        outcome = pybao_tools.compute_indicators(args)
        if not outcome.get("ok"):
            return _pybao_outcome_error(outcome)
        result = outcome.get("result")
    elif name == "get_board_members":
        if pybao_tools is None:
            return _pybao_tool_missing_error("get_board_members")
        outcome = pybao_tools.query_boards(args)
        if not outcome.get("ok"):
            return _pybao_outcome_error(outcome)
        result = outcome.get("result")
    elif name == "screen_stocks":
        if pybao_tools is None:
            return _pybao_tool_missing_error("screen_stocks")
        try:
            result = query_screen(args)
        except ValueError as exc:
            return _value_error_result(exc)
    elif name == "get_mydb_data":
        if pybao_tools is None:
            return _pybao_tool_missing_error("get_mydb_data")
        outcome = pybao_tools.query_mydb(args)
        if not outcome.get("ok"):
            return _pybao_outcome_error(outcome)
        result = outcome.get("result")
    elif name == "get_trading_days":
        try:
            result = get_trading_days(args)
        except ValueError as exc:
            return _value_error_result(exc)
    elif name == "get_data_status":
        result = get_data_status()
    else:
        return _error_result(f"未知工具: {name}", ERROR_UNKNOWN_TOOL)
    return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}]}


def _log_tool_call(tool: str, result: dict, elapsed_ms: int) -> None:
    """tools/call 调用日志（stderr 一行 JSON，flush）：事件/工具/成败/耗时/返回字节数。

    仅用于可观测性，不改变任何返回行为；ok = 无 isError。bytes = 序列化 result 的字节数。
    """
    is_error = bool(result.get("isError"))
    log_line = json.dumps({
        "event": "mcp_tool_call",
        "tool": tool,
        "ok": not is_error,
        "is_error": is_error,
        "elapsed_ms": int(elapsed_ms),
        "bytes": len(json.dumps(result, default=str)),
    }, ensure_ascii=False, default=str)
    sys.stderr.write(log_line + "\n")
    sys.stderr.flush()


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
                "capabilities": {"tools": {}, "prompts": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    elif method == "ping":
        return {"jsonrpc": "2.0", "id": request_id, "result": {}}
    elif method == "tools/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}}
    elif method == "prompts/list":
        return {"jsonrpc": "2.0", "id": request_id, "result": {"prompts": PROMPTS}}
    elif method == "prompts/get":
        prompt_name = params.get("name") if isinstance(params, dict) else None
        prompt = next((p for p in PROMPTS if p.get("name") == prompt_name), None)
        if prompt is None:
            return {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32602, "message": f"Invalid params: unknown prompt: {prompt_name}"},
            }
        return {
            "jsonrpc": "2.0", "id": request_id,
            "result": {
                "description": prompt["description"],
                "messages": [{
                    "role": "user",
                    "content": {"type": "text", "text": prompt["content"]},
                }],
            },
        }
    elif method == "tools/call":
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32602, "message": "Invalid params: arguments must be an object"},
            }
        started = time.monotonic()
        try:
            result = _call_tool(str(tool_name), arguments)
        except Exception as exc:  # noqa: BLE001 - 工具异常转为 MCP 错误响应
            result = _error_result(str(exc), ERROR_INTERNAL)
        _log_tool_call(str(tool_name), result, int((time.monotonic() - started) * 1000))
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
