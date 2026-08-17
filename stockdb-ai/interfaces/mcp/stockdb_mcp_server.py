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
        uv run python interfaces/mcp/stockdb_mcp_server.py   # stdio（MCP 客户端自动拉起）
    uv run python interfaces/mcp/stockdb_mcp_server.py --self-check  # 连通性自检
    uv run python interfaces/mcp/stockdb_mcp_server.py --http \
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
    get_point_snapshot  指定交易日全市场/指定股票池的单日时点快照（TRADED/SUSPENDED/… 分类，
                        纯 HTTP 无 pybao 依赖）

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
from decimal import Decimal  # 当日段涨停价取整与 board_metrics 同口径（0.7.0 双源合并）
import http.server
import json
import math  # 0.9.11：响应 NaN/Inf 清洗
import os
from pathlib import Path
import sys
import threading
import time
import urllib.parse
import urllib.request

# 保证同目录（interfaces/mcp/）与仓库根（stockdb-ai/）在 sys.path：
# 1) 直接 `python interfaces/mcp/stockdb_mcp_server.py`（sys.path[0] 已是脚本目录，
#    但 core/ 领域层在仓库根，需补 _BASE_DIR）
# 2) 作为包导入 `from interfaces.mcp import stockdb_mcp_server`（sys.path 是仓库根）
_MCP_DIR = Path(__file__).resolve().parent
_BASE_DIR = _MCP_DIR.parent.parent  # stockdb-ai/（core/ 领域层所在）
for _p in (_MCP_DIR, _BASE_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core.board_metrics import (  # noqa: E402 - 领域层（单一真身；0.9.8 去 mcp shim）
    DailyBar,
    is_supported_a_share_code,
    compute_board_open_effect_details,
    summarize_board_open_effect_values,
    _is_20cm,            # 创业板/科创板 20cm 判定（当日段重组需与日K口径逐条对齐）
    _is_north_exchange,  # 北交所排除（当日段重组审计计数用）
    _price_equal,        # 分位价差比较（涨停价判定）
    _rounded_limit_price,
    rebuild_limit_reference_price,  # 0.8.14：pre_close 污染重建（除权因子反推）
    BOARD_OPEN_COUNTER_FIELDS,  # 当日段重组审计计数键集合（与日K行同构）
)

from core import calendar_xshg  # noqa: E402 - A 股交易日历（领域层，休市表与 app.py 一致）

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
# 与 WEBUI_VERSION 同步（stockdb-ai/config.py；0.9.10 起手工对齐）
SERVER_VERSION = "0.9.13"

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

# === 统一错误码契约（本批全局：8 码体系） ===
# server.py isError content 统一为 {"error": str, "code": str}（DEPENDENCY_UNAVAILABLE 附加
# "hint"）；pybao_tools outcome 携带的 code/hint 直接透传，缺省按文案推断。
# 以下 8 个常量与 pybao_tools.py 中的同名常量值完全一致（Task D 同步）。
ERROR_INVALID_ARGUMENT = "INVALID_ARGUMENT"  # 参数非法（含未知工具，替换原 PARAM_INVALID/UNKNOWN_TOOL）
ERROR_NO_DATA = "NO_DATA"  # 合法查询但无数据（替换原 DATA_NOT_FOUND）
ERROR_NOT_PUBLISHED = "NOT_PUBLISHED"  # 该时点数据尚未入库/尚未发布
ERROR_INVALID_SYMBOL = "INVALID_SYMBOL"  # 代码不在股票池
ERROR_DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"  # pybao 缺失（替换原 PYBAO_UNAVAILABLE，hint 文案不变）
ERROR_PARTIAL_RESULT = "PARTIAL_RESULT"  # 预留：仅作数据面 is_partial 标志的语义说明，不作为 isError code 返回
ERROR_RATE_LIMITED = "RATE_LIMITED"  # 预留常量，当前无配额实现，不返回
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"  # 内部异常（替换原 INTERNAL）

_PYBAO_HINT = (
    "容器镜像自动携带（/opt/stockdb/pybao）；本机开发请把 macOS 版 pybao 放 "
    "/tmp/pybao_mac 或设 PYBAO_DIR"
)
# 推断 INTERNAL_ERROR 的文案标记（与 pybao_tools 的中文错误后缀一致）
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
        rows = [r for r in rows if s <= _row_date_key(r) <= e]
        rows.sort(key=_row_date_key)
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


_DAILY_KLINE_FREQUENCIES = frozenset({"1d", "1w", "1M"})  # 日K级周期（1w/1M 由日线聚合）


def _bump_end(end: str, frequency: str) -> str:
    """SDK 区间为开区间（start<end 不含 end），将 end 顺延一个粒度后交给 SDK，
    客户端再过滤 rows 至 <= 原 end，实现闭区间 [start, end] 语义。

    日K（1d/1w/1M）end 8 位 → end+1 自然日（datetime 计算）；分钟K end 8 位 →
    end+1 日的 "000000"；分钟K end 14 位 → int+100（+1 分钟）；其余形态原样返回。
    """
    if len(end) == 8 and end.isdigit():
        try:
            next_day = (
                datetime.strptime(end, "%Y%m%d") + timedelta(days=1)
            ).strftime("%Y%m%d")
        except ValueError:
            return end
        if frequency in _DAILY_KLINE_FREQUENCIES:
            return next_day
        return next_day + "000000"
    if len(end) == 14 and end.isdigit():
        return str(int(end) + 100)
    return end


def _row_date_within_end(date_value: object, end: str) -> bool:
    """行 date 是否 <= 原 end（闭区间客户端过滤，字符串比较）。

    8 位 end 与 14 位分钟行日期比较时取行日期前 8 位（当日任意分钟 bar 均属当日）。
    """
    if date_value is None:
        return False
    s = str(date_value)
    if len(end) == 8:
        return s[:8] <= end
    return s <= end


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
    由 SDK 自动补全为 14 位时间戳区间。SDK 区间为开区间（start<end 不含 end），
    本函数用 _bump_end 顺延 end 后查询，再客户端过滤 rows <= 原 end，
    对外统一为闭区间 [start, end] 语义。
    返回 {"source", "code", "codes", "frequency", "fq", "price_unit",
    "volume_unit", "amount_unit", "mode", "start", "end", "data", "total",
    "truncated"}；limit>0 时每码保留最新 limit 行。
    """
    client = _pybao_sdk_client()
    if client is None:
        raise ValueError(
            "pybao 不可用：复权/1m/1w/1M/批量 K 线查询需要 pybao SDK"
            "（容器 /opt/stockdb/pybao 或 PYBAO_DIR）"
        )
    target_codes = codes or ([code] if code else [])
    sdk_fq = None if fq in ("", "none") else fq
    sdk_end = _bump_end(end, frequency) if end else None
    try:
        raw = client.get_data(
            target_codes[0] if len(target_codes) == 1 else target_codes,
            start=start or None,
            end=sdk_end,
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
    # 落入 tools/call 的 INTERNAL_ERROR 兜底（真实 SDK 单码返回 list 不触发该分支）。
    if len(target_codes) == 1 and target_codes[0] not in per_code:
        per_code[target_codes[0]] = []
    # 闭区间语义：SDK 开区间已用 bumped end 查询，客户端按原 end 过滤（先于 fields
    # 投影，保证 fields 不含 date 时仍能过滤）。
    if end:
        per_code = {
            item: [
                row for row in rows
                if isinstance(row, dict) and _row_date_within_end(row.get("date"), end)
            ]
            for item, rows in per_code.items()
        }
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
        "fq": sdk_fq or "none",
        "price_unit": "元",
        "volume_unit": "股",
        "amount_unit": "元",
        "mode": "range" if end else "point",
        "start": start,
        "end": end,
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
    返回 {"source", "code", "frequency", "fq", "price_unit", "volume_unit",
    "amount_unit", "mode", "start", "end", "data", "total", "truncated"}。
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
        "fq": "none",
        "price_unit": "元",
        "volume_unit": "股",
        "amount_unit": "元",
        "mode": "range" if end else "point",
        "start": start,
        "end": end,
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
    """指定交易日多只股票的单日行情。HTTP 层不支持 code 通配，逐只查询。

    errors 元素为契约形态 {"code", "symbol", "message"}（code ∈ NO_DATA /
    INTERNAL_ERROR）；结果键保持 {"results", ...} 并附加 "date"（供 envelope
    known_at 派生）。
    """
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
                errors.append({
                    "code": ERROR_NO_DATA, "symbol": code, "message": "无数据",
                })
        except Exception as exc:  # noqa: BLE001 - 单只失败不影响整体
            errors.append({
                "code": ERROR_INTERNAL_ERROR, "symbol": code, "message": str(exc),
            })
    return {"results": results, "errors": errors, "date": date}


def _parse_yyyymmdd(value: str, *, field: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y%m%d")
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 8 位日期 YYYYMMDD") from exc


def _row_date_key(row: dict) -> int:
    """行 date 排序/区间键（0.9.11 容错）：非法形态 → -1（该行被过滤/排最前，
    不中断整批约 5000 只的全市场快照——此前 int() 位于逐行 try 之外，单条脏
    date 记录会让整批失败返回错误而非带诊断的部分结果）。"""
    try:
        return int(row.get("date") or 0)
    except (TypeError, ValueError):
        return -1


def _json_clean(value):
    """递归替换 NaN/Inf 浮点为 None（0.9.11：响应必须输出合法 JSON——裸 NaN/
    Infinity 令牌会让严格解析器（JS JSON.parse）整包失败；读/响应侧此前无护栏）。"""
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, dict):
        return {key: _json_clean(val) for key, val in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_clean(val) for val in value]
    return value


def _json_dumps(obj, **kwargs):
    """响应序列化（NaN/Inf → null 后 dumps）。"""
    return json.dumps(_json_clean(obj), ensure_ascii=False, **kwargs)


def _classify_empty_codes(empty_codes: list[str], end_compact: str) -> dict:
    """空代码（区间内无任何日K）分类：停牌 / 退市未上市 / 未发布 / 无法分类。

    复用 query_point_snapshot 的单日时点判定（universe 归属 + bar 有无）：
      - ERROR_NO_DATA（无 bar 但在股票池）→ "suspended"（停牌/长期停牌，不否决）
      - ERROR_INVALID_SYMBOL（不在股票池）→ "delisted_or_not_listed"（不否决）
      - ERROR_NOT_PUBLISHED（时点未发布）→ "not_published"（否决：候选识别不完整）
      - TRADED（当日有 bar 但区间无 → 矛盾）/ 请求失败 → "unclassified"（否决：
        可能是真实采集失败，保守按影响处理）
    探测日 = 区间最后交易日（nearest_trading_day 兜底，避免 end 为非交易日报错）。
    分批 200（query_point_snapshot 上限）；任一异常 → 全部 unclassified（保守）。
    """
    breakdown: dict[str, list[str]] = {
        "suspended": [], "delisted_or_not_listed": [],
        "not_published": [], "unclassified": [],
    }
    probe = calendar_xshg.nearest_trading_day(end_compact) or end_compact
    try:
        for i in range(0, len(empty_codes), _POINT_SNAPSHOT_CODES_MAX):
            chunk = empty_codes[i:i + _POINT_SNAPSHOT_CODES_MAX]
            res = query_point_snapshot({"date": probe, "codes": chunk, "limit": 0})
            traded_codes = {str(p.get("code")) for p in (res.get("points") or [])}
            seen: set[str] = set()
            for err in (res.get("errors") or []):
                code = str(err.get("symbol") or "")
                if not code or code in seen:
                    continue
                seen.add(code)
                kind = err.get("code")
                if kind == ERROR_INVALID_SYMBOL:
                    breakdown["delisted_or_not_listed"].append(code)
                elif kind == ERROR_NOT_PUBLISHED:
                    breakdown["not_published"].append(code)
                elif kind == ERROR_NO_DATA:
                    breakdown["suspended"].append(code)
                else:
                    breakdown["unclassified"].append(code)
            for code in chunk:
                if code in traded_codes and code not in seen:
                    breakdown["unclassified"].append(code)
    except Exception:  # noqa: BLE001 - 分类失败：全部保守按未分类（不改变"影响"语义）
        breakdown = {"suspended": [], "delisted_or_not_listed": [],
                     "not_published": [], "unclassified": list(empty_codes)}
    return breakdown


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

        # 连接卫生（0.8.5）：该路径同样全市场并发拉取，默认 16 线程无节流会耗尽
        # NAS 临时端口（与 query_point_snapshot 0.8.4 同因）。8 并发 + 每请求 50ms。
        workers = max(1, min(int(workers or 8), 8))
        pacing = 0.05

        def fetch_one(code: str) -> tuple[str, list, str | None]:
            last_error: Exception | None = None
            time.sleep(pacing)  # 每请求节流：峰值 ≈160 req/s，TIME_WAIT 存量安全
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
            for row in sorted(rows, key=_row_date_key):
                try:
                    day = str(row.get("date"))
                    if len(day) != 8:
                        continue
                    close = float(row.get("close"))
                    raw_prev_close = row.get("pre_close")
                    # 0.8.15 法定涨跌停参考价（验收修正：污染不均匀，禁止统一反推）：
                    #   - 普通日：上一实际成交日未复权收盘（history_close 逐日追踪）
                    #   - 除权日（因子表当日有事件）：当日 pre_close = 法定除权参考价（可信）
                    #   - 无历史（区间首行）/停牌跨日：pre_close 兜底
                    is_fq_event = bool(pybao_tools) and pybao_tools.is_fq_event_date(code, day)
                    if is_fq_event and raw_prev_close not in (None, "", 0, "0"):
                        prev_close = float(raw_prev_close)  # 除权日法定参考价
                    elif history_close is not None:
                        prev_close = history_close  # 普通日：上一实际成交日未复权收盘
                    elif raw_prev_close not in (None, "", 0, "0"):
                        prev_close = float(raw_prev_close)  # 兜底：区间首行等
                    else:
                        prev_close = history_close
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
        # 0.9.10：空代码分类（停牌/退市未上市不否决；真实失败/未发布/无法分类才否决）
        empty_breakdown: dict[str, list[str]] | None = None
        fatal_empty_codes: list[str] = []
        if empty_codes:
            empty_breakdown = _classify_empty_codes(empty_codes, end)
            fatal_empty_codes = list(empty_breakdown["unclassified"]) + list(
                empty_breakdown["not_published"])
            if empty_breakdown["suspended"]:
                partial_reasons.append("EMPTY_SUSPENDED")
            if empty_breakdown["delisted_or_not_listed"]:
                partial_reasons.append("EMPTY_DELISTED_OR_NOT_LISTED")
            if fatal_empty_codes:
                partial_reasons.append("EMPTY_CODE_UNCLASSIFIED")
        if point_in_time_state_unknown_codes:
            partial_reasons.append("POINT_IN_TIME_STATE_UNKNOWN")
        # 双覆盖率拆分（P2）：候选识别覆盖率（failed + 致命空代码）决定正式可用性；
        # 样本覆盖率（缺价）不否决，由 days 行 missing_open_count / 信封 sample_coverage 呈现
        candidate_coverage_complete = not failed and not fatal_empty_codes
        formal_usable = (
            not scope_is_partial
            and candidate_coverage_complete
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
            "candidate_coverage": {
                "complete": candidate_coverage_complete,
                "failed_count": len(failed),
                "empty_count": len(empty_codes),
            },
            "empty_code_breakdown": empty_breakdown or {},
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


# === 打板开盘溢价：研究成果预计算 + 竞价快照双源合并（0.7.0 任务E；0.9.10 键契约修正） ===
# 键契约（docs/design/auction-collector.md §2，命名空间保留前缀，本文件只读）：
#   写端（services/auction_tasks + storage.research_store）：
#     竞价快照:<YYYYMMDD>（表） + <code>（键）     → 快照 JSON
#     打板指标:<YYYYMMDD>（表） + metrics（键）    → 指标载荷（含 daily 完整日级行）
#     打板序列:<metric>（表）   + series（键）      → 滚动序列
#   读端双通道（顺序）：
#     ① research_store（0.9.5 抽象，写端同源：默认 SqliteResearchStore / mydb 回滚适配）
#     ② 引擎 mydb rd 直读（兼容 0.8.x 遗留数据；新契约 打板指标:<date>/metrics，
#        旧契约 打板指标/<date> 双键形回退）
# 缺失/不可达时静默降级为纯日K 口径，不影响既有行为。
_AUCTION_SNAPSHOT_TABLE = "竞价快照"  # mydb 表名（保留前缀，文档约定 AI 勿写）
_AUCTION_METRICS_TABLE = "打板指标"   # mydb 表名（当日业务指标 + 60 日分位）
_AUCTION_KNOWN_AT_DEFAULT_TIME = "09:26"  # 采集任务定时点（快照 timestamps 缺失时兜底）

_store_cache: object | None = None  # research_store 惰性缓存（写端同源读取通道）


def _get_research_store() -> object | None:
    """惰性获取写端同源的 ResearchStore（0.9.5 抽象）；不可用 → None（不抛异常）。

    stockdb_mcp_server 与 app.py 同进程部署时（容器 /mcp 路由），与采集器共享
    同一仓储实现；独立进程（stdio/--http）部署时通过防御性导入读取同一
    DATA_DIR/research.db（SQLite WAL 支持多进程读）。
    """
    global _store_cache
    if _store_cache is not None:
        return _store_cache
    try:
        from storage.research_factory import get_research_store
        _store_cache = get_research_store()
    except Exception:  # noqa: BLE001 - 仓储不可用：回退引擎 mydb 通道
        _store_cache = None
    return _store_cache


def _auction_value_to_dict(value: object) -> dict | None:
    """快照/指标值归一化：dict 原样；JSON 字符串解析；pybao QueryResult 转 dict。

    mydb 里存的是 JSON 对象（设计文档键契约），但 pybao 返回形态可能已是
    原生 dict 或带 .keys/.all 的 QueryResult，统一转 dict 供后续字段读取。
    """
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    if hasattr(value, "keys") and hasattr(value, "all"):
        try:  # pybao QueryResult：dict(value) 即原生数据
            return dict(value)
        except Exception:  # noqa: BLE001 - 转换失败按缺失处理
            return None
    return None


def _auction_float(value: object) -> float | None:
    """快照数值字段宽松转 float：None/空/非法字符串 → None（后续按缺失剔除）。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number


def _rd_get_locked(table: str, key: str) -> object | None:
    """加锁 rd.get（0.9.11）：经 pybao_tools.rd_get 全进程串行化（pybao rd 单连接
    非线程安全，2026-08-16 协议帧交错事故）；pybao_tools 缺失时防御直调。"""
    if pybao_tools is not None and hasattr(pybao_tools, "rd_get"):
        return pybao_tools.rd_get(table, key)
    rd = pybao_tools.get_mydb_rd() if pybao_tools is not None else None
    if rd is None:
        return None
    return rd.get(table, key)


def _read_auction_snapshots(
    rd: object, start_iso: str, end_iso: str, store: object | None = None
) -> tuple[dict[str, dict[str, dict]], bool]:
    """读竞价快照 → ({date_iso: {code: snapshot}}, read_ok)。

    双通道（0.9.10 键契约修正）：
    ① research_store（写端同源：SqliteResearchStore 逐日读 / MydbResearchStore 适配）；
    ② 引擎 mydb rd（keys 前缀枚举 + 精确键形 get，兼容 0.8.x 遗留数据）。
    单键解析失败跳过（部分股票失败不影响整体）。read_ok=False 表示两通道均不可读
    （连接/权限），调用方据此不得判定"未采集"（避免误报）。
    """
    snapshots_by_date: dict[str, dict[str, dict]] = {}
    read_ok = False

    # ① research_store 通道（写端同源；日历枚举区间内交易日逐日读）
    if store is not None:
        try:
            store_days = calendar_xshg.trading_days_between(
                start_iso.replace("-", ""), end_iso.replace("-", ""))
            for d8 in store_days:
                rows = store.read_snapshots(d8) or {}
                if not rows:
                    continue
                date_iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
                snapshots_by_date[date_iso] = {
                    str(code): snap for code, snap in rows.items()
                    if isinstance(snap, dict)
                }
            read_ok = True  # store 通道成功（可能无数据 → 正常"未采集"结论）
        except Exception:  # noqa: BLE001 - store 通道失败：回退引擎通道
            pass

    # ② 引擎 mydb 通道（0.8.x 遗留数据兼容；0.9.11 起经 pybao_tools 加锁辅助
    #    访问——pybao rd 单连接非线程安全，全进程串行化，防协议帧交错冻结）
    if rd is not None:
        if pybao_tools is not None and hasattr(pybao_tools, "rd_keys"):
            try:
                raw_keys = pybao_tools.rd_keys(_AUCTION_SNAPSHOT_TABLE, "*")
            except Exception:  # noqa: BLE001 - mydb 不可达
                raw_keys = []
        else:  # 防御：pybao_tools 缺失（理论不可达，rd 非 None 则模块必在）
            try:
                raw_keys = list(rd.keys(_AUCTION_SNAPSHOT_TABLE, "*") or [])
            except Exception:  # noqa: BLE001 - mydb 不可达
                raw_keys = []
        if raw_keys:
            read_ok = True
        for raw_key in raw_keys:
            sub = str(raw_key)
            # 兼容两种 keys 形态：含表名前缀（"竞价快照:20260817:600001"）或纯键
            if sub.startswith(_AUCTION_SNAPSHOT_TABLE + ":"):
                sub = sub[len(_AUCTION_SNAPSHOT_TABLE) + 1:]
            parts = sub.split(":")
            # 日期段 = 8 位数字段；缺失或非 "<日期>:<代码>" 形态的键跳过
            date_seg = next((p for p in parts if len(p) == 8 and p.isdigit()), None)
            if date_seg is None or len(parts) < 2:
                continue
            date_iso = f"{date_seg[:4]}-{date_seg[4:6]}-{date_seg[6:]}"
            if not start_iso <= date_iso <= end_iso:
                continue  # 只合并查询区间内的快照日期，days 信封区间不变量不变
            code_seg = next((p for p in parts if p != date_seg), None)
            if code_seg is None:
                continue
            value = None
            try:  # 新契约精确键形：表=竞价快照:<date>，键=code
                value = _rd_get_locked(f"{_AUCTION_SNAPSHOT_TABLE}:{date_seg}", code_seg)
            except Exception:  # noqa: BLE001 - 单键读取失败不影响其余
                value = None
            if value is None:
                try:  # 旧契约回退：表=竞价快照，键=<date>:<code>
                    value = _rd_get_locked(_AUCTION_SNAPSHOT_TABLE, sub)
                except Exception:  # noqa: BLE001 - 单键读取失败不影响其余
                    continue
            snap = _auction_value_to_dict(value)
            if snap is None:
                continue
            # 0.9.11：引擎通道仅补缺（setdefault 而非直接覆盖）——store 通道
            # 已读到的日期以 store 为准（写端同源、权威），迁移期遗留数据不覆盖
            snapshots_by_date.setdefault(date_iso, {}).setdefault(code_seg, snap)
    return snapshots_by_date, read_ok


def _merge_auction_day_row(
    snapshot: dict[str, list[DailyBar]], trade_date_iso: str, snaps: dict[str, dict]
) -> dict | None:
    """按"T-1 日K 涨停判定 + 当日竞价快照溢价"重组单个交易日行。

    判定规则逐条对齐 board_metrics.compute_board_open_effect_details（涨停价取整、
    20cm/北交所/ST/一字板过滤/审计计数），仅"当日开盘溢价"的数据来源改为快照
    open_price/prev_close（任一缺失或 <=0 → missing_open_count，统计剔除）。
    快照仅覆盖清单股，故合格样本集合由 T-1 日K 判定给出（设计文档 §3：涨停判定用
    T-1 kline，溢价用采集表）。T-1 无日K（区间首日）时无法判定 → 返回 None。
    """
    # 前一交易日 = kline 快照中小于 D 的最大日期（与 board_metrics 的序列语义一致：
    # 停牌复牌等缺口不会被误判为"昨日涨停今日溢价"）
    prev_dates = sorted(d for d in snapshot if d < trade_date_iso)
    if not prev_dates:
        return None
    prev_date = prev_dates[-1]
    values: list[float] = []
    sample_codes: list[str] = []
    counts = {field: 0 for field in BOARD_OPEN_COUNTER_FIELDS}
    for bar in snapshot.get(prev_date, []):  # T-1 日K 全市场逐只判定（与日K口径一致）
        if bar.prev_close is None or bar.prev_close <= 0:
            continue
        if _is_north_exchange(bar.code):  # 北交所排除（涨停价 30% 仅用于审计计数）
            north_limit = _rounded_limit_price(bar.prev_close, Decimal("0.30"))
            if _price_equal(bar.close, north_limit):
                counts["excluded_north_limit_up_count"] += 1
            continue
        if not is_supported_a_share_code(bar.code):
            continue
        if bar.is_st:  # ST 股排除（涨停价 5% 仅用于审计计数）
            st_limit = _rounded_limit_price(bar.prev_close, Decimal("0.05"))
            if _price_equal(bar.close, st_limit):
                counts["excluded_st_limit_up_count"] += 1
            continue
        rate = Decimal("0.20") if _is_20cm(bar.code) else Decimal("0.10")
        limit_price = _rounded_limit_price(bar.prev_close, rate)
        if not _price_equal(bar.close, limit_price):
            continue  # 非涨停：不构成样本
        counts["prior_limit_up_count"] += 1
        one_word = all(  # 一字板严格定义：昨开/高/低/收全等于涨停价（T 字板保留）
            _price_equal(price, limit_price)
            for price in (bar.open, bar.high, bar.low, bar.close)
        )
        if one_word:
            counts["excluded_one_word_count"] += 1
            continue
        counts["eligible_count"] += 1
        # 溢价 = 快照 open_price / T-1 收盘价 - 1（0.8.13：分母统一用 T-1 日收盘
        # bar.close——快照 prev_close 为采集时交易所调整昨收，除权除息日会混入分红失真；
        # 与历史段 board_metrics 口径一致）
        rec = snaps.get(bar.code)
        open_price = _auction_float(rec.get("open_price")) if rec else None
        prev_close = _auction_float(bar.close)  # T-1 收盘（bar 即 T-1 日 K 线）
        if open_price is None or prev_close is None or open_price <= 0 or prev_close <= 0:
            counts["missing_open_count"] += 1  # 无可用开盘价：剔除并计数（同日K口径）
            continue
        values.append((open_price / prev_close - 1) * 100)
        sample_codes.append(bar.code)
    # 复用 board_metrics 的统计汇总：行结构与日K 行完全同构（分位数/分布/计数）
    return summarize_board_open_effect_values(trade_date_iso, values, counts, sample_codes)


def _read_metrics_payload(rd: object, date_iso: str, store: object | None = None) -> dict | None:
    """读 打板指标:<日期> 指标载荷（含 daily 子载荷；0.9.10 键契约修正）。

    双通道：① research_store（写端同源）；② 引擎 mydb rd 双键形——新契约
    （表=打板指标:<date>，键=metrics）优先，旧契约（表=打板指标，键=<date>）回退。
    缺失/损坏 → None（调用方按"无预计算"处理，不抛错）。
    """
    date_compact = date_iso.replace("-", "")
    if store is not None:
        try:
            payload = store.read_metrics(date_compact)
            if isinstance(payload, dict) and isinstance(payload.get("metrics"), dict):
                return payload
        except Exception:  # noqa: BLE001 - store 通道失败：回退引擎
            pass
    # 引擎通道：经 pybao_tools 加锁辅助（0.9.11；rd 参数仅为通道可用性兼容，
    # 实际访问由 _rd_get_locked 内部获取连接并串行化）
    if pybao_tools is None:
        return None
    for table, key in (
        (f"{_AUCTION_METRICS_TABLE}:{date_compact}", "metrics"),  # 新契约（写端键形）
        (_AUCTION_METRICS_TABLE, date_compact),                   # 旧契约（0.8.x 遗留）
    ):
        try:
            payload = _auction_value_to_dict(_rd_get_locked(table, key))
        except Exception:  # noqa: BLE001 - 单键读取失败不影响其余
            continue
        if payload and isinstance(payload.get("metrics"), dict):
            return payload
    return None


def _attach_auction_metrics(row: dict, rd: object, date_iso: str,
                            store: object | None = None) -> None:
    """打板指标:<日期> 存在时，当日行附 metrics 字段（指标载荷平铺，原样透传）。

    载荷键契约（auction_metrics.build_metrics_payload）：metrics{premium_mean,
    success_rate, n_samples} + rank_60d{premium_mean, success_rate} +
    strength_60d{premium_mean, success_rate} + window + value_source；
    n_samples 兼容"顶层也冗余一份"的两种写库形态。
    """
    payload = _read_metrics_payload(rd, date_iso, store)
    if not payload:
        return
    metrics = payload["metrics"]
    row["metrics"] = {
        "premium_mean": metrics.get("premium_mean"),
        "success_rate": metrics.get("success_rate"),
        "n_samples": metrics.get("n_samples", payload.get("n_samples")),
        "rank_60d": payload.get("rank_60d"),
        "strength_60d": payload.get("strength_60d"),
        "value_source": payload.get("value_source"),
        "window": payload.get("window"),
    }


def _precomputed_row(rd: object, date_iso: str, store: object | None = None) -> dict | None:
    """读 打板指标:<date> 的 daily 子载荷（完整日级行，0.9.10 起随指标持久化）。

    无指标载荷或载荷无 daily → None（该日无预计算，须走日K 判定）。
    """
    payload = _read_metrics_payload(rd, date_iso, store)
    if not payload:
        return None
    daily = payload.get("daily")
    return daily if isinstance(daily, dict) else None


def _row_from_daily(daily: dict, date_iso: str, include_distribution: bool) -> dict:
    """预计算 daily 行 → 与日K 行同构的 days 行。

    审计计数（BOARD_OPEN_COUNTER_FIELDS）在采集器侧不可得（清单口径），置 None
    占位而非伪造 0；统计/分布/指标字段原样透传。
    """
    row = {field: None for field in BOARD_OPEN_COUNTER_FIELDS}
    row.update(daily)
    row["trade_date"] = row.get("trade_date") or date_iso
    if not include_distribution:
        row.pop("distribution", None)
    return row


def _precomputed_days(rd: object, start_iso: str, end_iso: str,
                      store: object | None = None) -> dict[str, dict]:
    """区间内全部有预计算 daily 行的交易日 → {date_iso: row}（日历枚举，与采集器同源）。"""
    rows: dict[str, dict] = {}
    for d8 in calendar_xshg.trading_days_between(
        start_iso.replace("-", ""), end_iso.replace("-", "")
    ):
        date_iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        row = _precomputed_row(rd, date_iso, store)
        if row is not None:
            rows[date_iso] = row
    return rows


def _auction_known_at(snapshots_by_date: dict[str, dict[str, dict]], date_iso: str) -> str | None:
    """当日信封 known_at 标注：如 "20260817 09:26 竞价采集(source=tencent)"。

    时间取快照 fetched_at（其次 known_at）的 HH:MM——采集证据时点；两者均缺失时用
    采集定时点 09:26 兜底。来源取当日出现次数最多的 source（并列按字典序取小，
    保证同数据确定性输出）。
    """
    snaps = list((snapshots_by_date.get(date_iso) or {}).values())
    if not snaps:
        return None
    hhmm = None
    for snap in snaps:  # 首个带时间的快照即定"采集证据时点"，无需遍历全部
        for field in ("fetched_at", "known_at"):
            raw = snap.get(field)
            if isinstance(raw, str) and len(raw) >= 16:
                hhmm = raw[11:16]  # "2026-08-17T09:26:03" → "09:26"
                break
        if hhmm:
            break
    hhmm = hhmm or _AUCTION_KNOWN_AT_DEFAULT_TIME
    source_count: dict[str, int] = {}
    for snap in snaps:
        source = snap.get("source")
        if source:
            source_count[str(source)] = source_count.get(str(source), 0) + 1
    source = "unknown"
    if source_count:
        source = sorted(source_count.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return f"{date_iso.replace('-', '')} {hhmm} 竞价采集(source={source})"


def _auction_missing_note(
    start_iso: str, end_iso: str, snapshots_by_date: dict[str, dict[str, dict]], read_ok: bool
) -> dict | None:
    """当日（<= 今天的最近交易日）落在查询区间但无竞价快照 → 降级提示条目。

    契约形态 {"code","symbol","message"}（envelope 会按 8 键 errors 保留）；
    不改变 ok 语义。mydb 读取失败（read_ok=False）不等于"未采集"，不误报。
    """
    if not read_ok:
        return None
    today_compact = datetime.now().strftime("%Y%m%d")
    latest_td = calendar_xshg.nearest_trading_day(today_compact)  # 采集器运行的"当日"
    if latest_td is None:
        return None
    td_iso = f"{latest_td[:4]}-{latest_td[4:6]}-{latest_td[6:]}"
    if not start_iso <= td_iso <= end_iso:
        return None  # 查询区间不含当日：历史查询不产生提示
    if td_iso in snapshots_by_date:
        return None  # 当日已采集：正常双源路径
    return {"code": ERROR_NO_DATA, "symbol": latest_td, "message": "当日竞价快照未采集"}


def _overlay_day(days: list[dict], row: dict) -> None:
    """合并行覆盖 days 中同交易日条目；不存在则追加（随后整体按 trade_date 排序）。

    同一日期日K 与快照并存时以快照口径为准（设计 §3：采集表有数据的日期溢价用采集表）。
    """
    trade_date = row.get("trade_date")
    for index, item in enumerate(days):
        if item.get("trade_date") == trade_date:
            days[index] = row
            return
    days.append(row)


def _precomputed_known_at(daily: dict, date_iso: str) -> str:
    """预计算日信封标注：如 "20260817 09:25 预计算(auction)"。

    时间取 daily.known_at（其次 computed_at）的 HH:MM；缺失用采集定时点兜底。
    """
    raw = daily.get("known_at") or daily.get("computed_at")
    if isinstance(raw, str) and len(raw) >= 16:
        hhmm = raw[11:16]  # "2026-08-17T09:25:00" → "09:25"
    else:
        hhmm = _AUCTION_KNOWN_AT_DEFAULT_TIME
    source = daily.get("value_source") or "unknown"
    return f"{date_iso.replace('-', '')} {hhmm} 预计算({source})"


def _merge_auction_days(
    snapshot: dict[str, list[DailyBar]],
    days: list[dict],
    start_iso: str,
    end_iso: str,
    include_distribution: bool,
) -> tuple[str | None, dict | None, dict]:
    """研究成果预计算 + 竞价快照双源合并主流程：就地改写 days。

    返回 (known_at, errors_note, stats)：
    - 有预计算 daily 行的日期：直接用完整日级行覆盖（写端同源，0.9.10）；
    - 其余有快照的日期：以"T-1 日K 判定 + 快照溢价"重组该日行（现逻辑）；
    - 打板指标:<日期> 存在 → 该日行附 metrics 字段（无 daily 时）；
    - known_at：最近一个合并日期的来源标注；无合并 → None；
    - errors_note：当日未采集时给降级提示；两通道均不可读时不给（无法判定）；
    - stats：{"merged_dates": [...], "precomputed_dates": [...]}（信封覆盖统计）。
    """
    rd = pybao_tools.get_mydb_rd() if pybao_tools is not None else None
    store = _get_research_store()
    if rd is None and store is None:  # 两通道皆无：纯日K 口径，行为与现状完全一致
        return None, None, {"merged_dates": [], "precomputed_dates": []}
    snapshots_by_date, read_ok = _read_auction_snapshots(rd, start_iso, end_iso, store)
    merged_dates: list[str] = []
    precomputed_dates: list[str] = []
    known_at_by_date: dict[str, str] = {}
    for date_iso in sorted(snapshots_by_date):
        daily = _precomputed_row(rd, date_iso, store)
        if daily is not None:  # 预计算完整日级行优先（写端同源，无需 T-1 日K 判定）
            row = _row_from_daily(daily, date_iso, include_distribution)
            known_at_by_date[date_iso] = _precomputed_known_at(daily, date_iso)
            precomputed_dates.append(date_iso)
        else:
            row = _merge_auction_day_row(snapshot, date_iso, snapshots_by_date[date_iso])
            if row is None:
                continue  # T-1 无日K（区间首日等）：无法判定，跳过该快照日期
            if not include_distribution:
                row.pop("distribution", None)
            _attach_auction_metrics(row, rd, date_iso, store)
            known_at_by_date[date_iso] = _auction_known_at(snapshots_by_date, date_iso)
        _overlay_day(days, row)
        merged_dates.append(date_iso)
    stats = {"merged_dates": merged_dates, "precomputed_dates": precomputed_dates}
    if merged_dates:  # 合并存在 → 整体重排，保持 days 按交易日升序（信封不变量）
        days.sort(key=lambda item: item.get("trade_date", ""))
        return known_at_by_date.get(merged_dates[-1]), None, stats
    return None, _auction_missing_note(start_iso, end_iso, snapshots_by_date, read_ok), stats


def _fast_path_result(start_iso: str, end_iso: str, include_distribution: bool) -> dict | None:
    """预计算全覆盖快速通道：区间内每个交易日都有 daily 行 → 直读返回。

    - 不进入 _HEAVY_LOCK、不拉全市场日K（正常查询目标 <1s）；
    - 任一交易日无预计算 → None（调用方走全市场重算慢路径）；
    - 仅全市场默认请求适用（调用方保证 codes is None 且 limit == 0）。
    """
    rd = pybao_tools.get_mydb_rd() if pybao_tools is not None else None
    store = _get_research_store()
    if rd is None and store is None:
        return None  # 两通道皆无：无预计算可读
    trade_days = calendar_xshg.trading_days_between(
        start_iso.replace("-", ""), end_iso.replace("-", ""))
    if not trade_days:
        return None  # 区间无交易日：走慢路径语义（快照判定产生空结果）
    # 0.9.11：休市表覆盖护栏——未收录年份"工作日=交易日"会把休市日误判为交易日，
    # 快速通道不得把休市日的全 None 指标行当真实预计算返回（回退慢路径，基于
    # 真实快照数据仍正确）
    if trade_days[-1] > calendar_xshg.XSHG_HOLIDAYS_THROUGH.replace("-", ""):
        return None
    pre_rows: dict[str, dict] = {}
    for d8 in trade_days:
        date_iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        row = _precomputed_row(rd, date_iso, store)
        if row is None:
            return None  # 任一交易日缺失 → 不全覆盖，走慢路径
        pre_rows[date_iso] = row
    days = []
    latest_coverage: dict | None = None
    latest_known_at: str | None = None
    for d8 in trade_days:
        date_iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
        daily = pre_rows[date_iso]
        row = _row_from_daily(daily, date_iso, include_distribution)
        days.append(row)
        if isinstance(daily.get("coverage"), dict):
            latest_coverage = daily["coverage"]
        latest_known_at = _precomputed_known_at(daily, date_iso)
    coverage = (latest_coverage or {}).get
    sample_coverage = {
        "codes_requested": coverage("codes_requested"),
        "fetched": coverage("fetched"),
        "fetch_errors": coverage("fetch_errors"),
        "missing_open": coverage("missing_open"),
        "n_samples": days[-1].get("matched_count"),
        "complete": coverage("missing_open") == 0,
    }
    result = {
        "source_name": "free-stockdb",
        "source_contract_version": "board-open-effect-stockdb-v4.2",
        "start": start_iso,
        "end": end_iso,
        "load_path": "mydb",
        "cache_hit": True,
        "precomputed_days": len(days),
        "fallback_reason": None,
        "sample_coverage": sample_coverage,
        "methodology": {
            "sample": "T-1沪深非ST、非一字板收盘涨停股（预计算清单口径）",
            "one_word": "T-1 open=high=low=close=涨停价",
            "limit_price": "前收×(1+10%/20%)，0.01元 ROUND_HALF_UP",
            "value": "(T日开盘价/T-1收盘价-1)×100%",
        },
        "known_limitations": [
            "预计算直读路径：样本集合 = 采集清单（T-1 非一字板涨停），"
            "审计计数（涨停/排除/候选）在采集器侧不可得，置 null",
        ],
        "days": days,
    }
    if latest_known_at is not None:
        result["known_at"] = latest_known_at  # 信封标注预计算来源（_derive_known_at 读取）
    return result


def query_board_open_effect_history(
    start: str,
    end: str,
    *,
    codes: list[str] | None = None,
    limit: int = 0,
    workers: int = 16,
    include_distribution: bool = False,
) -> dict:
    """基于预计算结果 / 全市场日 K 返回可审计的打板开盘溢价时序（0.7.0 双源合并版）。

    读取优先级（0.9.10 快速通道）：
    - 区间内所有交易日都有预计算 daily 行（打板指标:<日期> 载荷，写端同源）→
      直读返回（load_path=mydb, cache_hit=true，正常查询 <1s，不扫全市场）；
    - 否则全市场日K 重算（load_path=kline，cache_hit=false），当日段仍用预计算/
      快照覆盖（mydb+overlay），信封 fallback_reason 注明降级原因。
    双源合并（契约 docs/design/auction-collector.md §3）：
    - 历史日期（无竞价快照）：涨停判定 + 溢价全部从日K 计算（现逻辑，行为不变）；
    - 当日段（存在 竞价快照:<日期>:*）：涨停判定仍用 T-1 日K，开盘溢价改用
      快照 open_price/prev_close（任一为 None/<=0 的样本剔除，计入 missing_open_count）；
    - 打板指标:<日期> 存在时，当日行附 metrics 字段（premium_mean/success_rate/
      n_samples/rank_60d/strength_60d/value_source/window，取自指标库载荷；
      strength_60d 为强弱标签 strong/weak/neutral，口径=此前 60 有效观测中
      严格低于当日值天数/60，不足 60 观测为 null）；
    - 信封 known_at：预计算直读 → "<YYYYMMDD> <HH:MM> 预计算(<source>)"；
      当日段合并成功 → "<YYYYMMDD> <HH:MM> 竞价采集(source=<src>)"；
      否则保持现语义（None）；
    - 降级：当日（<= 今天的最近交易日，且落在查询区间内）无快照 → 结果与历史口径
      一致，errors 附一条 "当日竞价快照未采集"（两通道均不可读时不下该结论）。
    """
    start_iso = _parse_yyyymmdd(start, field="start").date().isoformat()
    end_iso = _parse_yyyymmdd(end, field="end").date().isoformat()
    # 快速通道（_HEAVY_LOCK 之外）：预计算全覆盖 → 直读 mydb/SQLite
    if codes is None and limit <= 0:
        fast = _fast_path_result(start_iso, end_iso, include_distribution)
        if fast is not None:
            return fast
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
        days = []
        for trade_date, row in sorted(details.items()):
            if not start_iso <= trade_date <= end_iso:
                continue
            item = dict(row)
            if not include_distribution:
                item.pop("distribution", None)
            days.append(item)
        # 0.7.0：研究成果预计算 / 竞价快照双源合并（当日段用预计算或采集表，历史段仍日K）
        auction_known_at, auction_error, merge_stats = _merge_auction_days(
            snapshot, days, start_iso, end_iso, include_distribution
        )
        precomputed_dates = merge_stats.get("precomputed_dates") or []
        result = {
            "source_name": "free-stockdb",
            "source_contract_version": "board-open-effect-stockdb-v4.2",
            "start": start_iso,
            "end": end_iso,
            **metadata,
            "load_path": "mydb+overlay" if precomputed_dates else "kline",
            "cache_hit": False,
            "precomputed_days": len(precomputed_dates),
            "fallback_reason": (
                None
                if precomputed_dates
                else "区间交易日无预计算结果，已全市场重算"
            ),
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
        if auction_known_at is not None:
            # 当日段合并成功：信封 known_at 标注当日来源（_derive_known_at 读取）
            result["known_at"] = auction_known_at
        if auction_error is not None:
            result["errors"] = [auction_error]  # 降级提示，不改变 ok 语义
        return result


def get_trading_days(args: dict) -> dict:
    """A股交易日历查询（无 pybao 依赖，纯 calendar_xshg 休市表）。

    start 必填（8 位）；end 缺省 = start 后 90 个自然日；limit 为返回上限
    （硬上限 400，超出截断并标记 truncated）。参数非法抛中文 ValueError，
    由 _call_tool 转 isError（code=INVALID_ARGUMENT）。
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


# === get_point_snapshot（单日时点快照，纯 HTTP，无 pybao 依赖） ===

_POINT_SNAPSHOT_CODES_MAX = 200  # codes 显式数量上限
_POINT_SNAPSHOT_LIMIT_MAX = 200  # points 返回硬上限
_POINT_SNAPSHOT_DEFAULT_LIMIT = 50

_POINT_SNAPSHOT_KNOWN_LIMITATIONS = [
    "日线级时点事实：开盘价为当日收盘同步后的值，无盘中 09:25 实时数据",
    "单日 bar 缺失无法区分停牌与当日未上市/已退市（均标 SUSPENDED）",
]


def _a_share_universe_set() -> set[str]:
    """全市场 A 股代码集合（query_stock_list 结果 TTL 缓存 300s）。"""
    stock_list = query_stock_list()
    return {
        str(code) for code in (stock_list.get("codes") or [])
        if is_supported_a_share_code(str(code))
    }


def _bar_from_get(data: object) -> dict | None:
    """cmd=get 精确日K 响应 → 单日 bar dict（兼容单行 dict / {key: row} / [键, 行] 对），
    无数据返回 None。"""
    if isinstance(data, dict):
        if "date" in data:
            return data
        for value in data.values():
            if isinstance(value, dict):
                return value
        return None
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
            if isinstance(item, list) and len(item) == 2 and isinstance(item[1], dict):
                return item[1]
        return None
    return None


def _point_snapshot_item(code: str, bar: dict) -> dict:
    """TRADED 点的业务行：{code, name, status, open, prev_close, close, high, low,
    volume, amount, is_st}。"""
    return {
        "code": code,
        "name": bar.get("name") or "",
        "status": "TRADED",
        "open": bar.get("open"),
        "prev_close": bar.get("pre_close"),
        "close": bar.get("close"),
        "high": bar.get("high"),
        "low": bar.get("low"),
        "volume": bar.get("volume"),
        "amount": bar.get("amount"),
        "is_st": bar.get("is_st"),
    }


def _fullmarket_sdk_outcomes(codes: list[str], date: str):
    """全市场单日 bars 批量快路径（0.8.7）：SDK pipeline，一次往返取整批。

    替代 5200 次逐只 HTTP（回填 45min → 秒级）。分块 1000 只/批防单响应过大；
    缺失代码补一轮重试（一次往返）；SDK 不可用/失败 → 返回 None 由调用方回退 HTTP。
    """
    if pybao_tools is None:
        return None
    try:
        sdk = pybao_tools.get_sdk_client()
    except Exception:  # noqa: BLE001 - SDK 不可用回退
        return None
    if sdk is None or not hasattr(sdk, "get_data"):
        return None

    bars: dict[str, dict] = {}

    def _pull(chunk: list[str]) -> None:
        # 0.9.11：SDK 区间为开区间（start<end 不含 end，见 _bump_end 契约）——
        # start==end 是空区间，整批返回空会被静默判为全市场无 bar（SUSPENDED）
        # 且 formal_usable 仍为 True。end 顺延一日，客户端过滤回原日期。
        data = sdk.get_data(chunk, start=date, end=_bump_end(date, "1d"), fq=None) or {}
        for c, recs in data.items():
            recs = [r for r in (recs or []) if isinstance(r, dict)
                    and str(r.get("date") or "")[:8] == date]
            if recs:
                bars[str(c)] = recs[-1]  # 单日区间，取最后一条

    try:
        # 分块 50：实测 pybao pipeline 单次响应上限 50 条（0.8.8 修复——
        # 块 1000 时每天只返回 50 只，涨停清单大范围漏检）。
        for i in range(0, len(codes), 50):
            _pull(codes[i:i + 50])
        missing = [c for c in codes if str(c) not in bars]
        if missing:  # 补一轮重试（一次往返），瞬态缺失自愈
            for i in range(0, len(missing), 50):
                _pull(missing[i:i + 50])
        return [(c, bars.get(str(c)), None) for c in codes]
    except Exception:  # noqa: BLE001 - 批路径失败整体回退 HTTP
        return None


def query_point_snapshot(args: dict) -> dict:
    """指定交易日单日时点快照（纯 HTTP，无 pybao 依赖）。

    date 缺省 = 最新交易日探针（无法确定 → NO_DATA）；非交易日 → INVALID_ARGUMENT
    + hint 最近交易日。codes 显式（1-200，逐只顺序拉取）为调试语义；缺省 = 全市场
    A 股（ThreadPoolExecutor(16) 并发）。每 code 取 日k:{code}:{date}：有 bar →
    TRADED 进 points；无 bar 按 universe 归属 / 时点是否已发布 分类为
    INVALID_SYMBOL / NOT_PUBLISHED / SUSPENDED（分类与失败进 errors，上限 100 条）。
    返回 {"source", "date", "points", "truncated", "coverage", "errors",
    "known_limitations"}；envelope known_at = date。
    """
    date = str(args.get("date") or "")
    if not date:
        date = _latest_trade_date() or ""
        if not date:
            raise _ToolError(
                "get_point_snapshot: 无法确定最新交易日，请显式传 date", ERROR_NO_DATA
            )
    if len(date) != 8 or not date.isdigit():
        raise ValueError("get_point_snapshot: date 必须是 8 位日期 YYYYMMDD")
    try:
        trading = calendar_xshg.is_trading_day(date)
    except ValueError:  # 防御：日历模块对异常日期抛错
        trading = False
    if not trading:
        nearest = calendar_xshg.nearest_trading_day(date)
        raise _ToolError(
            f"get_point_snapshot: {date} 非交易日",
            ERROR_INVALID_ARGUMENT,
            hint=(f"最近交易日 {nearest}" if nearest else None),
        )

    raw_codes = args.get("codes")
    explicit = raw_codes is not None
    if explicit:
        if not isinstance(raw_codes, list):
            raise ValueError("get_point_snapshot: codes 必须为数组")
        if not 1 <= len(raw_codes) <= _POINT_SNAPSHOT_CODES_MAX:
            raise ValueError(
                f"get_point_snapshot: codes 数量必须为 1-200，当前 {len(raw_codes)} 个"
            )
        requested: list[str] = []
        for item in raw_codes:
            code = str(item).strip()
            if len(code) != 6 or not code.isdigit():
                raise ValueError(
                    f"get_point_snapshot: 股票代码 {item!r} 必须是 6 位数字"
                )
            requested.append(code)
    else:
        requested = sorted(_a_share_universe_set())

    try:
        raw_limit = args.get("limit", _POINT_SNAPSHOT_DEFAULT_LIMIT)
        # 注意：不能用 `raw or 默认值`——显式传 0（内部全量语义）会被 or 吞成默认 50，
        # 0.8.3 的全量修复因此从未生效（0.8.8 修复）。
        limit = int(raw_limit) if raw_limit not in (None, "") else _POINT_SNAPSHOT_DEFAULT_LIMIT
    except (TypeError, ValueError):
        raise ValueError("get_point_snapshot: limit 必须是整数") from None
    if limit < 0:
        raise ValueError("get_point_snapshot: limit 必须是正整数")
    if limit == 0:
        limit = None  # 内部全量语义（打板任务用）：不截断；工具 schema 仍限 1..200
    elif limit > _POINT_SNAPSHOT_LIMIT_MAX:
        limit = _POINT_SNAPSHOT_LIMIT_MAX

    universe_set = _a_share_universe_set()
    latest = _latest_trade_date()

    def fetch_one(code: str) -> tuple[str, dict | None, str | None]:
        try:
            return code, _bar_from_get(_http_get("get", f"日k:{code}:{date}")), None
        except Exception as exc:  # noqa: BLE001 - 单只失败保留诊断，不影响整体
            return code, None, str(exc)

    if explicit:
        outcomes = [fetch_one(code) for code in requested]
    else:
        # 全市场批量快路径（0.8.7）：SDK pipeline 一次往返取全市场单日 bars，
        # 替代 5200 次逐只 HTTP（回填 45min → 秒级）。SDK 不可用 → 回退逐只 HTTP。
        outcomes = _fullmarket_sdk_outcomes(requested, date)
        if outcomes is None:
            # 回退路径连接卫生（0.8.4）：8 并发 + 每请求 50ms 节流 ≈ 160 req/s，
            # 防 5200 条新连接耗尽 NAS 临时端口（Errno 99 事故）。
            def fetch_one_paced(code: str):
                time.sleep(0.05)
                return fetch_one(code)

            outcomes = []
            with ThreadPoolExecutor(max_workers=8) as pool:
                futures = {pool.submit(fetch_one_paced, code): code for code in requested}
                for future in as_completed(futures):
                    outcomes.append(future.result())

    points: list[dict] = []
    errors: list[dict] = []
    traded = suspended = invalid_symbol = not_published = failed = 0
    for code, bar, error in outcomes:
        if error is not None:
            failed += 1
            errors.append({
                "code": ERROR_INTERNAL_ERROR, "symbol": code, "message": error,
            })
            continue
        if bar is not None:
            traded += 1
            points.append(_point_snapshot_item(code, bar))
            continue
        # 无 bar 分类：不在股票池 → INVALID_SYMBOL；时点晚于最新交易日 → NOT_PUBLISHED；
        # 其余 → SUSPENDED（单日 bar 无法进一步区分）
        if code not in universe_set:
            invalid_symbol += 1
            errors.append({
                "code": ERROR_INVALID_SYMBOL, "symbol": code,
                "message": "代码不在股票池",
            })
        elif latest is not None and date > latest:
            not_published += 1
            errors.append({
                "code": ERROR_NOT_PUBLISHED, "symbol": code,
                "message": "该时点数据尚未入库/尚未发布",
            })
        else:
            suspended += 1
            errors.append({
                "code": ERROR_NO_DATA, "symbol": code,
                "message": "交易日无 bar（停牌/未上市/退市，单日 bar 无法进一步区分）",
            })

    truncated = (limit is not None) and (len(points) > limit)
    kept = points[:limit] if truncated else points

    partial_reasons: list[str] = []
    if explicit:
        partial_reasons.append("EXPLICIT_CODES_DEBUG")
    if truncated:
        partial_reasons.append("LIMIT_APPLIED")
    if failed:
        partial_reasons.append("SOURCE_REQUEST_FAILED")
    full_scope = set(requested) == universe_set and len(requested) == len(universe_set)
    formal_usable = full_scope and failed == 0 and not truncated

    coverage = {
        "universe": len(universe_set),
        "requested": len(requested),
        "traded": traded,
        "suspended": suspended,
        "invalid_symbol": invalid_symbol,
        "not_published": not_published,
        "failed": failed,
        "formal_usable": formal_usable,
        "partial_reasons": partial_reasons,
    }
    return {
        "source": "http",
        "date": date,
        "points": kept,
        "truncated": truncated,
        "is_partial": bool(partial_reasons),
        "coverage": coverage,
        "errors": errors[:100],
        "known_limitations": list(_POINT_SNAPSHOT_KNOWN_LIMITATIONS),
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
                "end": {"type": "string", "description": "日K 结束日期(可选；与 start 构成闭区间 [start, end]，含端点)"},
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
            "预计算直读（0.9.10）：区间内交易日均有采集/收口预计算 → 直接返回 mydb/SQLite"
            "结果（cache_hit=true，摘要<1s）；缺失时才全市场日K重算（cache_hit=false，"
            "fallback_reason 注明）。返回样本数、匹配数、成功率、均值和分位数。"
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
    {
        "name": "get_point_snapshot",
        "description": (
            "获取指定交易日全市场/指定股票池的单日时点快照：每只股票返回 开盘/前收/收盘/"
            "最高/最低/量额/is_st 及状态分类（TRADED/SUSPENDED/INVALID_SYMBOL/NOT_PUBLISHED）"
            "与覆盖率 coverage。纯 HTTP，无 pybao 依赖。date 缺省=最新交易日（无法确定或非交易日"
            "报错）；codes 缺省=全市场 A 股（16 线程并发），显式传入（1-200，顺序逐只）为调试语义"
            "（is_partial=true）；limit 为 points 返回上限（默认 50，硬上限 200，超出截断）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "时点日期 8 位 YYYYMMDD（缺省=最新交易日自动探测）",
                },
                "codes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "股票代码列表 1-200 个（每项 6 位数字）；缺省=全市场 A 股",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "points 返回上限（默认 50，硬上限 200，超出截断并标记 truncated）",
                    "default": 50,
                },
            },
            "required": [],
        },
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

# === 0.9.0 M4：上游 SDK 41 工具（契约外壳，见 docs/design/sdk-mcp-bridge.md） ===
# sdk_bridge 懒加载上游，本模块任何环境下均可导入；无上游 → tool_specs() 为空列表，
# SDK 工具不注册（AI 侧不可见），调用路径返回 DEPENDENCY_UNAVAILABLE（见 _call_tool）。
try:
    from sdk_bridge import (  # noqa: E402 - 同目录模块（_MCP_DIR 已插入 sys.path）
        KNOWN_SDK_TOOL_NAMES as _sdk_known_names,
        call_tool as _sdk_call_tool,
        import_error as _sdk_import_error,
        tool_specs as _sdk_tool_specs,
    )
except ImportError:  # 防御：sdk_bridge 缺失时 MCP 服务器仍可用（SDK 工具整体不注册）
    _sdk_known_names = frozenset()
    _sdk_call_tool = None
    _sdk_import_error = lambda: "sdk_bridge 缺失"  # noqa: E731 - 防御降级
    _sdk_tool_specs = lambda: []  # noqa: E731 - 防御降级

_sdk_tool_specs_ext = _sdk_tool_specs()  # 41 个上游工具规格（无上游 → []）
if _sdk_tool_specs_ext:
    TOOLS = TOOLS + _sdk_tool_specs_ext

# === 0.9.3：MCP 工具分组（Gateway）——按业务域分组注册，缓解 LLM 上下文占用 ===
# 每个工具带 group 元数据；客户端以 /mcp?group=<组名> 接入即只注册该组工具。
# 不传 group = 全量注册（向后兼容）。
BASE_TOOL_GROUPS: dict[str, str] = {
    "get_kline": "market_data", "get_stock_list": "market_data",
    "get_adjust_factors": "market_data", "get_market_snapshot": "market_data",
    "get_point_snapshot": "market_data", "get_trading_days": "market_data",
    "get_board_open_effect_history": "research", "get_mydb_data": "research",
    "get_indicators": "factor_analysis", "get_board_members": "factor_analysis",
    "screen_stocks": "factor_analysis", "get_data_status": "system_health",
}

TOOL_GROUPS: dict[str, str] = {
    "market_data": "行情数据：K线/竞价/tick/资金流/日历",
    "fundamental": "基本面：财务/估值/解禁/龙虎榜",
    "factor_analysis": "因子与指标：alpha/因子看板/技术指标/选股",
    "market_structure": "市场结构：板块/指数/期货",
    "research": "研究成果：mydb 私有存储/打板情绪指标",
    "system_health": "系统：数据状态/表查询",
}

for _tool_spec in TOOLS:
    _tool_spec.setdefault("group", BASE_TOOL_GROUPS.get(_tool_spec["name"], "system_health"))


# === 统一错误码：isError content = {"error": str, "code": str}(, "hint") ===


def _error_result(message: str, code: str, *, hint: str | None = None) -> dict:
    """构造 MCP 工具 isError 结果：content 文本为 {"error": str, "code": str}（契约统一错误码）。"""
    payload: dict[str, object] = {"error": message, "code": code}
    if hint:
        payload["hint"] = hint
    return {
        "content": [{"type": "text", "text": _json_dumps(payload)}],
        "isError": True,
    }


class _ToolError(ValueError):
    """带错误码/提示的工具错误：由 query_screen 等业务函数抛出，_call_tool 透传 code/hint。"""

    def __init__(self, message: str, code: str | None = None, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.hint = hint


def _resolve_error(message: str, code: object, hint: object) -> tuple[str, str | None]:
    """错误码解析：显式 code 优先；缺省按文案推断（pybao 不可用→DEPENDENCY_UNAVAILABLE 等）。"""
    if isinstance(code, str) and code:
        return code, (hint if isinstance(hint, str) and hint else None)
    if "pybao 不可用" in message:
        return ERROR_DEPENDENCY_UNAVAILABLE, (
            hint if isinstance(hint, str) and hint else _PYBAO_HINT
        )
    if any(marker in message for marker in _INTERNAL_ERROR_MARKERS):
        return ERROR_INTERNAL_ERROR, (hint if isinstance(hint, str) and hint else None)
    return ERROR_INVALID_ARGUMENT, (hint if isinstance(hint, str) and hint else None)


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
    """pybao_tools 模块整体缺失时的明确降级错误（code=DEPENDENCY_UNAVAILABLE + hint）。"""
    return _error_result(
        f"{tool_name}: pybao 工具模块不可用",
        ERROR_DEPENDENCY_UNAVAILABLE,
        hint=_PYBAO_HINT,
    )


# === 响应 envelope（数据契约规范化：8 键恒在，全部工具成功结果必须包含） ===
# source/source_contract_version 按工具族注入；known_at 为数据内容覆盖的最后时点
# （日线 8 位、分钟 14 位；静态数据 null）；is_partial/truncated/total/errors/
# known_limitations 已有则保留，否则按规则补默认。

_CONTRACT_BY_TOOL: dict[str, tuple[str | None, str]] = {
    "get_stock_list": ("http", "stock-list-v1"),
    "get_adjust_factors": ("http", "adjust-factors-v1"),
    "get_kline": (None, "kline-v2"),  # source 取 result（http/pybao 双路径）
    "get_market_snapshot": ("http", "market-snapshot-v1"),
    "get_board_open_effect_history": ("http", "board-open-effect-v1"),
    "get_indicators": ("pybao", "indicators-v1"),
    "get_board_members": ("pybao", "boards-v1"),
    "screen_stocks": ("pybao", "screen-v1"),
    "get_mydb_data": ("pybao", "mydb-v1"),
    "get_trading_days": ("static", "calendar-v1"),
    "get_data_status": ("http", "status-v1"),
    "get_point_snapshot": ("http", "snapshot-v1"),
}

# 0.9.0 M4：SDK 41 工具族统一契约（source="sdk"，上游通道）
for _sdk_contract_name in _sdk_known_names:
    _CONTRACT_BY_TOOL.setdefault(_sdk_contract_name, ("sdk", "sdk-bridge-v1"))


def _known_at_str(value: object) -> str | None:
    """known_at 归一化：int/str 日期 → str，None/空 → None。"""
    if value is None:
        return None
    if isinstance(value, str):
        return value if value else None
    if isinstance(value, int):
        return str(value)
    return None


def _max_date_in_data(data: object) -> str | None:
    """data（行列表 / {code: 行} 批量 / 列式 {"dates": [...]}）中最大 date，int→str，
    空 → None。"""
    candidates: list[object] = []
    if isinstance(data, list):
        candidates = list(data)
    elif isinstance(data, dict):
        dates_col = data.get("dates")
        if isinstance(dates_col, list):
            candidates.extend({"date": d} for d in dates_col)
        for value in data.values():
            if isinstance(value, list):
                candidates.extend(item for item in value if isinstance(item, dict))
    max_date: int | None = None
    for row in candidates:
        if not isinstance(row, dict):
            continue
        try:
            # 兼容 8 位（20260812）与 ISO（2026-08-12）两种日期形态（SDK 工具为 ISO）
            day = int(str(row.get("date")).replace("-", ""))
        except (TypeError, ValueError):
            continue
        if max_date is None or day > max_date:
            max_date = day
    return None if max_date is None else str(max_date)


def _derive_known_at(tool_name: str, result: dict) -> str | None:
    """known_at 派生规则：get_kline/get_indicators → data 最大 date；screen_stocks /
    get_market_snapshot / get_point_snapshot → date；get_data_status →
    latest_trade_date；get_board_open_effect_history → 当日段合并标注
    （如 "20260817 09:26 竞价采集(source=tencent)"，未合并时 None，历史口径不变）；
    mydb 与其余静态工具 → null。"""
    if tool_name in ("get_kline", "get_indicators"):
        return _max_date_in_data(result.get("data"))
    if tool_name in _sdk_known_names:
        # 0.9.0 M4：SDK 工具 known_at = data 最大日期（无日期字段 → null）
        return _max_date_in_data(result.get("data"))
    if tool_name in ("screen_stocks", "get_market_snapshot", "get_point_snapshot"):
        return _known_at_str(result.get("date"))
    if tool_name == "get_data_status":
        return _known_at_str(result.get("latest_trade_date"))
    if tool_name == "get_board_open_effect_history":
        # 0.7.0 双源合并：当日段成功 → 快照采集证据标注；否则保持现语义（None）
        return _known_at_str(result.get("known_at"))
    return None


def _row_count(value: object) -> int | None:
    """数据单元行数：list → len；列式 dict → "dates" 列长度；其余 → None。"""
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        dates = value.get("dates")
        if isinstance(dates, list):
            return len(dates)
        return len(value)
    return None


def _derive_total(result: dict) -> object:
    """total 兜底：按 data 行数（list → len；dict 批量 → {"code": n}；无 data → null）。"""
    data = result.get("data")
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        return {str(key): _row_count(value) for key, value in data.items()}
    return None


def _is_contract_errors(errors: object) -> bool:
    """errors 元素均为 {"code","symbol","message"} 形态才保留，否则 envelope 置 []。"""
    if not isinstance(errors, list):
        return False
    for item in errors:
        if not isinstance(item, dict):
            return False
        if not all(key in item for key in ("code", "symbol", "message")):
            return False
    return True


def _apply_contract(tool_name: str, result: object) -> dict:
    """成功结果注入统一 envelope（8 键恒在）：list 结果（如 get_adjust_factors）先包
    {"data": result} 再注入；时间/覆盖率/部分性/失败原因成为一等字段。"""
    if isinstance(result, list):
        result = {"data": result}
    if not isinstance(result, dict):
        result = {"data": result}
    family_source, contract_version = _CONTRACT_BY_TOOL.get(tool_name, (None, None))
    source = result.get("source") or family_source or "http"
    out = {
        "source": str(source),
        "source_contract_version": contract_version or "unknown-v0",
        "known_at": _derive_known_at(tool_name, result),
        "is_partial": bool(result.get("is_partial", False)),
        "truncated": bool(result.get("truncated", False)),
        "total": result.get("total", _derive_total(result)),
        "errors": (
            result.get("errors") if _is_contract_errors(result.get("errors")) else []
        ),
        "known_limitations": list(result.get("known_limitations") or []),
    }
    for key, value in result.items():
        if key not in out:
            out[key] = value
    return out


def _call_tool(name: str, args: dict) -> dict:
    """Dispatch a tools/call request to a handler. Returns MCP result dict.

    所有 isError 分支按统一错误码契约返回 content={"error", "code"(, "hint")}：
    参数类与未知工具 INVALID_ARGUMENT；pybao 相关 DEPENDENCY_UNAVAILABLE（带 hint）；
    其余 INTERNAL_ERROR；pybao_tools outcome 的 code/hint 直接透传。
    成功分支统一经 _apply_contract 注入 8 键 envelope。
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
                "get_market_snapshot: codes 必须为数组", ERROR_INVALID_ARGUMENT
            )
        date = str(args.get("date") or "")
        if not date:
            # date 缺省 = 最新交易日探针；探针失败（无法确定）时报 NO_DATA
            date = _latest_trade_date() or ""
            if not date:
                return _error_result(
                    "无法确定最新交易日，请显式传 date", ERROR_NO_DATA
                )
        result = query_market_snapshot(date, [str(c) for c in codes])
    elif name == "get_board_open_effect_history":
        codes = args.get("codes")
        if codes is not None and not isinstance(codes, list):
            return _error_result(
                "get_board_open_effect_history: codes 必须为数组", ERROR_INVALID_ARGUMENT
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
    elif name == "get_point_snapshot":
        try:
            result = query_point_snapshot(args)
        except ValueError as exc:
            return _value_error_result(exc)
    elif name in _sdk_known_names:
        # 0.9.0 M4：上游 SDK 41 工具（sdk_bridge 契约外壳）
        if _sdk_call_tool is None:
            return _error_result(f"{name}: sdk_bridge 未加载", ERROR_DEPENDENCY_UNAVAILABLE)
        sdk_reason = _sdk_import_error()
        if sdk_reason:
            # 上游 stockdb_full_mcp 未加载（无 pybao / 缺文件）：工具已知但不可用
            return _error_result(
                f"{name}: SDK 工具不可用（{sdk_reason}）",
                ERROR_DEPENDENCY_UNAVAILABLE,
                hint="本机需 PYBAO_DIR 指向原生 pybao 目录；容器镜像自动携带",
            )
        try:
            result = _sdk_call_tool(name, args)
        except ValueError as exc:
            return _value_error_result(exc)
        except RuntimeError as exc:
            return _error_result(str(exc), ERROR_INTERNAL_ERROR)
    else:
        # 未知工具同样按 INVALID_ARGUMENT（契约：未知工具 = 参数非法）
        return _error_result(f"未知工具: {name}", ERROR_INVALID_ARGUMENT)
    return {
        "content": [{
            "type": "text",
            "text": _json_dumps(_apply_contract(name, result)),
        }],
    }


def _log_tool_call(tool: str, result: dict, elapsed_ms: int,
                   trace_id: str | None = None) -> None:
    """tools/call 调用日志（stderr 一行 JSON，flush）：事件/工具/成败/耗时/返回字节数。

    仅用于可观测性，不改变任何返回行为；ok = 无 isError。bytes = 序列化 result 的字节数。
    trace_id（0.9.3）：请求级追踪，与响应/日检记录关联。
    """
    is_error = bool(result.get("isError"))
    log_line = json.dumps({
        "event": "mcp_tool_call",
        "tool": tool,
        "ok": not is_error,
        "is_error": is_error,
        "elapsed_ms": int(elapsed_ms),
        "bytes": len(json.dumps(result, default=str)),
        "trace_id": trace_id,
    }, ensure_ascii=False, default=str)
    sys.stderr.write(log_line + "\n")
    sys.stderr.flush()


# === MCP 协议（换行分隔 JSON-RPC，stdio / HTTP 共用同一份 dispatch） ===


def _write_message(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _new_trace_id() -> str:
    """请求级 Trace ID（0.9.3）：uuid4 前 12 位，贯穿日志/响应/日检诊断。"""
    import uuid
    return uuid.uuid4().hex[:12]


def _tools_for_group(group: str | None) -> list[dict]:
    """按工具分组过滤 TOOLS（0.9.3 Gateway）；group 为 None → 全量（向后兼容）。"""
    if not group:
        return TOOLS
    return [t for t in TOOLS if t.get("group") == group]


def _handle_request(msg: dict, group: str | None = None) -> dict:
    """核心分发：把一条带 id 的 JSON-RPC 请求映射为响应 dict（id/result 或 id/error）。

    group（0.9.3）：tools/list 按业务域过滤（/mcp?group= 接入）；None = 全量。
    trace_id（0.9.3）：tools/call 生成并贯穿 result/日志，供日检按 ID 诊断。
    """
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
        return {"jsonrpc": "2.0", "id": request_id,
                "result": {"tools": _tools_for_group(group)}}
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
        trace_id = _new_trace_id()  # 0.9.3：请求级 Trace ID
        started = time.monotonic()
        try:
            result = _call_tool(str(tool_name), arguments)
        except Exception as exc:  # noqa: BLE001 - 工具异常转为 MCP 错误响应
            result = _error_result(str(exc), ERROR_INTERNAL_ERROR)
        result.setdefault("trace_id", trace_id)  # JSON-RPC result 顶层附加键（不破坏信封）
        _log_tool_call(str(tool_name), result, int((time.monotonic() - started) * 1000),
                       trace_id=trace_id)
        return {"jsonrpc": "2.0", "id": request_id, "result": result}
    else:
        # Speculative/unknown request methods get a JSON-RPC method-not-found error.
        return {
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


def dispatch(msg: dict, group: str | None = None) -> dict | None:
    """JSON-RPC 分发纯函数：通知（无 id 或已知通知方法）返回 None，否则返回响应 dict。

    stdio 与 HTTP 共用：请求返回 dict 写回；notification 返回 None 不回写。
    group（0.9.3）：tools/list 业务域过滤（HTTP 端 /mcp?group= 传入；stdio 无分组）。
    """
    if not isinstance(msg, dict):
        return None
    method = msg.get("method")
    if method in ("notifications/initialized", "notifications/cancelled", "notifications/progress"):
        return None
    if "id" not in msg:
        # 无 id 的未知消息按 JSON-RPC 通知处理（不期待响应）
        return None
    return _handle_request(msg, group)


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
        body = _json_dumps(response).encode("utf-8")
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
