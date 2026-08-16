#!/usr/bin/env python3
"""free-stockdb webui — 同步管理 + 运维面板（纯 Python 标准库，零第三方依赖）

功能：
  - 同步管理：网页一键完成「停 stockdb 进程 → 运行数据更新（增量同步）→ 重启」
  - 行情查询：代理 stockdb 7899 HTTP API（日K/分钟K/复权/股票代码）
  - 私有存储：mydb 写入（pybao 客户端）+ 港股日K 拉取（东财/腾讯）
  - 日志查看：同步过程实时写入 /data/sync.log，页面轮询展示

0.5.0 单镜像架构：stockdb 与 webui 同容器，进程级控制（pidfile + SIGTERM），
不再依赖 docker socket 挂载。

安全边界：
  - 进程操控仅限固定的 stockdb 服务（pidfile 停止/启动），不开放任意命令；
    写操作（同步/定时）面向内网信任环境，无令牌鉴权。
  - 只读查询不鉴权；局域网部署即可，如需公网暴露请自行加反向代理鉴权。
  - 同步串行化（互斥锁），防止并发点击
"""

from __future__ import annotations

import collections
import http.client
import json
import math
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# 只读 MCP（stockdb-native）dispatch：HTTP POST /mcp 复用（纯标准库，随 webui 同目录 mcp/ 分发）。
# 缺失/加载失败时 webui 其余功能不受影响，/mcp 路由返回 500。
try:
    from mcp.stockdb_mcp_server import dispatch as mcp_dispatch
except Exception:  # noqa: BLE001 - MCP 模块缺失时优雅降级
    mcp_dispatch = None

# /mcp SSE 流式进度推送依赖 pybao_tools 的线程级 progress hook
# （set_progress_hook/clear_progress_hook，见 pybao_tools 模块）；
# 注意：必须用顶层 `import pybao_tools`（而非 `from mcp import pybao_tools`），
# 与 mcp.stockdb_mcp_server 内的 `import pybao_tools` 共用同一模块实例——
# 否则 threading.local 进度钩子分属两个模块对象，SSE 进度帧永不触发
# （mcp_dispatch 已在上方导入，server 已将 mcp/ 插入 sys.path，身份一致）。
# 缺失/加载失败时 webui 其余功能不受影响，SSE 流式请求退化为现有 JSON 响应。
try:
    import pybao_tools
except ImportError:  # noqa: BLE001 - pybao_tools 缺失时优雅降级
    pybao_tools = None
    print("webui: 未加载 pybao_tools，/mcp SSE 流式退化为 JSON 响应", file=sys.stderr)

# ---- 0.9.1 四层架构：配置单一入口（config.py）----
# 运行配置全部收敛于 config 模块（引擎地址/端口/数据目录/调度触发点/版本号/并发闸门），
# 本文件不再直接读环境变量定义这些配置（0.9.2 各层迁移后从 config 引用）。
from config import (  # noqa: E402 - 配置为纯 stdlib，无循环依赖
    AUCTION_CLOSE_TIME,
    AUCTION_COLLECT_TIME,
    DATA_DIR,
    LISTEN_PORT,
    STOCKDB_HOST,
    STOCKDB_LOG_FILE,
    STOCKDB_MAX_CONCURRENCY,
    STOCKDB_PAUSE,
    STOCKDB_PIDFILE,
    STOCKDB_PORT,
    WEBUI_VERSION,
)

# ---- 0.9.2 批次 3：mydb/引擎访问迁 storage/providers（本文件保留同名引用） ----
from storage.providers.free_stockdb import (  # noqa: E402
    _breaker as _stockdb_breaker,
    _breaker_open as _stockdb_breaker_open,
    _gate as _stockdb_gate,
    fetch as stockdb_fetch,
)
from storage.providers.mydb_store import (  # noqa: E402
    _mydb_rd,
    _mydb_rd_reset,
    _rd_lock,
    _rd_to_py,
    auction_series_read as _auction_series_read,
    auction_series_write as _auction_series_write,
    mydb_read,
    mydb_tables,
    mydb_write,
    validate_custom_table,
)

# ---- 0.9.2 批次 4：打板用例迁 services/auction_tasks.py（组合根装配） ----
# app 保留同名暴露（HTTP/调度引用不变）；注入点绑定见模块末尾（_wire_auction_tasks）。
import services.auction_tasks as _auction_tasks  # noqa: E402
from services.auction_tasks import (  # noqa: E402
    AUCTION_IMPORT_ERROR,
    AUCTION_METRICS,
    AUCTION_MODULES_AVAILABLE,
    _auction_apply_reference,
    _auction_backfill_state,
    _auction_fired,
    _auction_lag_close,
    _auction_load_codes,
    _auction_load_series,
    _auction_prev_trade_date,
    _auction_points_for_codes,
    auction_run_backfill,
    auction_run_backfill_async,
    auction_run_close,
    auction_run_collect,
    auction_scheduler_loop,
)

# ---- 0.9.2 批次 6：HTTP 路由表外置（web/routes.py） ----
from web.routes import (  # noqa: E402
    GET_ROUTES as _WEB_GET_ROUTES,
    POST_ROUTES as _WEB_POST_ROUTES,
)

SYNC_LOG = DATA_DIR / "sync.log"

# 同步线程状态
_sync_lock = threading.Lock()
_sync_state = {"running": False, "exit_code": None, "last_start": None, "last_end": None,
               "phase": "idle"}  # phase: idle/stopping/syncing/verifying/restarting/done
_last_sync_stdout: str = ""          # 最近一次同步的 stdout（供解析下载/删除数）
_last_verify_result: str | None = None  # 最近一次完整性验证结果（pass/fail/跳过）
_scheduler_alive = False             # 定时线程心跳（每次循环更新时间戳）
_scheduler_heartbeat = 0.0           # 定时线程最近一次心跳时间戳（unix）
_webui_started = time.time()         # webui 进程启动时间戳

HISTORY_FILE = DATA_DIR / "sync_history.json"
SCHEDULE_FILE = DATA_DIR / "sync_schedule.json"
HISTORY_MAX = 30


# ==================== 同步历史 / 定时配置（落 /data 卷，重建容器不丢） ====================
def load_history() -> list[dict]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def append_history(entry: dict) -> None:
    history = load_history()
    history.append(entry)
    history = history[-HISTORY_MAX:]
    try:
        HISTORY_FILE.write_text(
            json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as exc:
        log(f"  ⚠️ 同步历史写入失败: {exc}")


def _default_schedule() -> dict:
    return {"enabled": False, "times": ["15:30"], "trading_only": True,
            "fired": {}, "retried": {}, "retry_pending": None,
            "last_trigger": None, "next_trigger": None}


# ==================== A 股交易日历（休市日表，数据截至 2026 年） ====================
# 来源：exchange_calendars 的 XSHG 日历（https://github.com/gerrymanoim/exchange_calendars）
# 取值规则：每个年份「周一~周五但非交易日」的日期（官方调休安排：春节/国庆/元旦/清明/五一/端午/中秋，
# 以及部分周六周日调休补班的 0 个或 1 个非交易日，均已折算进工作日的缺失）。
# 提取脚本：docker/webui/scripts/extract_xshg_holidays.py（仅维护期使用，不随 webui 运行）。
# 注意：XSHG 日历发布滞后（2027 官方安排通常 2026 年底公布），未收录年份按"工作日=交易日"处理，
# 数据截至年份后请在日志提示更新。
XSHG_HOLIDAYS: dict[str, set[str]] = {
    "2024": {"01-01", "02-09", "02-12", "02-13", "02-14", "02-15", "02-16",
             "04-04", "04-05", "05-01", "05-02", "05-03", "06-10", "09-16", "09-17",
             "10-01", "10-02", "10-03", "10-04", "10-07"},
    "2025": {"01-01", "01-28", "01-29", "01-30", "01-31", "02-03", "02-04",
             "04-04", "05-01", "05-02", "05-05", "06-02",
             "10-01", "10-02", "10-03", "10-06", "10-07", "10-08"},
    "2026": {"01-01", "01-02", "02-16", "02-17", "02-18", "02-19", "02-20", "02-23",
             "04-06", "05-01", "05-04", "05-05", "06-19", "09-25",
             "10-01", "10-02", "10-05", "10-06", "10-07"},
}
XSHG_HOLIDAYS_THROUGH = "2026-12-31"  # 休市表覆盖到的最后日期（用于到期提示）


_calendar_warned: set[int] = set()  # 休市表未收录年份的日志限频（每年只警告一次）


def is_trading_day(d=None) -> bool:
    """A 股交易日判定：工作日 且 非休市表内日期。

    未收录年份（休市表覆盖后）按"工作日=交易日"处理，并在日志提示更新（每年限频一次，
    避免 4s 轮询触发日志风暴）。供定时同步跳过周末/法定节假日触发用。
    """
    from datetime import datetime as _dt
    d = d or _dt.now().date()
    if d.weekday() >= 5:  # 周六/周日
        return False
    holidays = XSHG_HOLIDAYS.get(str(d.year))
    if holidays is None:
        if d.year not in _calendar_warned:
            _calendar_warned.add(d.year)
            log(f"⚠️ A股休市表未收录 {d.year} 年（数据截至 {XSHG_HOLIDAYS_THROUGH}），请更新 XSHG_HOLIDAYS")
        return True  # 未知年份：工作日即视为交易日
    return d.strftime("%m-%d") not in holidays


def _normalize_times(times) -> list[str]:
    """校验并规范化时间点列表（HH:MM，去重、排序、只留合法值）。"""
    result = []
    seen = set()
    for t in times or []:
        t = str(t).strip()
        try:
            datetime.strptime(t, "%H:%M")
        except ValueError:
            continue
        if t not in seen:
            seen.add(t)
            result.append(t)
    return sorted(result)


_schedule_lock = threading.Lock()  # sync_schedule.json 读改写互斥（调度线程/同步回填/Web 请求并发）


def _write_schedule(cfg: dict) -> None:
    """原子写定时配置：临时文件 + os.replace（避免读者看到写一半的内容）。"""
    tmp = SCHEDULE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, SCHEDULE_FILE)


def load_schedule() -> dict:
    """读定时配置；兼容旧格式 {enabled, time} → 迁移为 {enabled, times:[time], trading_only}。

    fired: {日期: [已触发时间点...]}——当天每个时间点只触发一次（多时间点防循环重复触发）。
    retried: {日期: [已安排过自动重试的时间点...]}；retry_pending: 计划执行重试的时间（字符串）。
    纯读不加锁（_write_schedule 原子替换保证读到完整文件）。
    """
    if not SCHEDULE_FILE.exists():
        return _default_schedule()
    try:
        data = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return _default_schedule()
    times = data.get("times")
    if not isinstance(times, list):  # 旧格式迁移：单 time 字段
        times = [str(data.get("time") or "15:30")]
    last = data.get("last_trigger")
    if not isinstance(last, dict):
        last = None
    fired = data.get("fired")
    if not isinstance(fired, dict):
        fired = {}
    retried = data.get("retried")
    if not isinstance(retried, dict):
        retried = {}
    rp = data.get("retry_pending")
    norm_times = _normalize_times(times)
    return {
        "enabled": bool(data.get("enabled")),
        "times": norm_times,
        "trading_only": bool(data.get("trading_only", True)),
        "fired": fired,
        "retried": retried,
        "retry_pending": rp if isinstance(rp, str) else None,
        "last_trigger": last,
        "next_trigger": compute_next_trigger(norm_times, trading_only=bool(data.get("trading_only", True))),
    }


def save_schedule(enabled: bool, times, trading_only: bool = True) -> dict:
    """保存定时配置（保留 last_trigger / fired / retried / retry_pending，不因改配置清空）。

    读改写整体持锁，防止与调度线程/同步回填并发写丢状态。
    """
    with _schedule_lock:
        cfg = {"enabled": bool(enabled), "times": _normalize_times(times),
               "trading_only": bool(trading_only)}
        if not cfg["times"]:
            raise RuntimeError("至少需要一个合法时间点（HH:MM）")
        try:
            old = {}
            if SCHEDULE_FILE.exists():
                try:
                    old = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    old = {}
            # 触发/重试记录：保留有效值，缺失时用空默认（配置结构保持完整）
            cfg["fired"] = old.get("fired") if isinstance(old.get("fired"), dict) else {}
            cfg["retried"] = old.get("retried") if isinstance(old.get("retried"), dict) else {}
            cfg["retry_pending"] = old.get("retry_pending") if isinstance(old.get("retry_pending"), str) else None
            cfg["last_trigger"] = old.get("last_trigger") if isinstance(old.get("last_trigger"), dict) else None
            cfg["next_trigger"] = compute_next_trigger(cfg["times"], trading_only=trading_only)
            _write_schedule(cfg)
        except Exception as exc:
            raise RuntimeError(f"定时配置写入失败: {exc}")
        return cfg


def _today_key() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _prune_fired(cfg: dict) -> dict:
    """清理 fired/retried 里早于今天的日期（只保留最近记录，防累积）。"""
    today = _today_key()
    for k in ("fired", "retried"):
        d = cfg.get(k) or {}
        stale = [day for day in d if day < today]
        for day in stale:
            d.pop(day, None)
        cfg[k] = d
    return cfg


def _mark_fired(t: str) -> None:
    """记录某时间点今天已触发（防同一天重复触发），落盘。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            today = _today_key()
            fired = dict(cfg.get("fired") or {})
            fired.setdefault(today, [])
            if t not in fired[today]:
                fired[today].append(t)
            cfg["fired"] = fired
            cfg["next_trigger"] = compute_next_trigger(cfg["times"], trading_only=cfg["trading_only"])
            cfg = _prune_fired(cfg)
            _write_schedule(cfg)
        except Exception as exc:
            log(f"⏰ 定时触发标记失败: {exc}")


def _mark_last_trigger(key: str, t: str | None = None, retry: bool = False) -> None:
    """记录最近一次定时触发（key=日期 时间点；t=该时间点 HH:MM），供界面展示与重试判定。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            cfg["last_trigger"] = {"key": key, "t": t, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                   "exit": None, "retry": bool(retry)}
            cfg["next_trigger"] = compute_next_trigger(cfg["times"], trading_only=cfg["trading_only"])
            _write_schedule(cfg)
        except Exception as exc:
            log(f"⏰ 定时触发标记失败: {exc}")


def _mark_retried(t: str) -> None:
    """记录某时间点今天已安排过自动重试，并登记 10 分钟后的执行计划（retry_pending）。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            today = _today_key()
            retried = dict(cfg.get("retried") or {})
            retried.setdefault(today, [])
            if t not in retried[today]:
                retried[today].append(t)
            cfg["retried"] = retried
            cfg["retry_pending"] = (datetime.now() + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
            cfg = _prune_fired(cfg)
            _write_schedule(cfg)
        except Exception as exc:
            log(f"↻ 重试登记失败: {exc}")


def _clear_retry_pending() -> None:
    with _schedule_lock:
        try:
            cfg = load_schedule()
            cfg["retry_pending"] = None
            _write_schedule(cfg)
        except Exception:
            pass


def _update_schedule_trigger_exit(exit_code, retry: bool = False) -> None:
    """run_sync 结束后回填最近一次定时触发的 exit 码（retry 记录随 last_trigger 保留）。"""
    with _schedule_lock:
        try:
            cfg = load_schedule()
            if cfg["last_trigger"] and cfg["last_trigger"].get("exit") is None:
                cfg["last_trigger"]["exit"] = exit_code
                if retry:
                    cfg["last_trigger"]["retry"] = True
                _write_schedule(cfg)
        except Exception:
            pass


def compute_next_trigger(times: list[str], now=None, trading_only: bool = True) -> str | None:
    """最近的下一次触发时间，返回如 '今天 16:05' / '明天 15:30' / '08-13 15:30'。

    与调度器使用同一套交易日判定（trading_only 时跳过周末/A股法定休市），
    避免界面显示"明天"而调度器实际跳过（周五/节假日前）的不一致。
    8 天内无交易日时退回不跳过的兜底计算（极端长假边界）。
    """
    if not times:
        return None
    now = now or datetime.now()
    today = now.date()
    for offset in range(8):
        d = today + timedelta(days=offset)
        if trading_only and not is_trading_day(d):
            continue
        hm = now.strftime("%H:%M") if offset == 0 else "00:00"
        for t in times:
            if t > hm:
                label = "今天" if offset == 0 else ("明天" if offset == 1 else d.strftime("%m-%d"))
                return f"{label} {t}"
    # 兜底：8 天内无交易日，退回不跳过的原始逻辑
    today_hm = now.strftime("%H:%M")
    for t in times:
        if t > today_hm:
            return f"今天 {t}"
    return f"明天 {times[0]}"


def parse_sync_counts(stdout: str) -> dict:
    """从数据更新输出中提取下载/删除数量。"""
    d, r = None, None
    for line in (stdout or "").splitlines():
        if "待下载资源数" in line:
            try:
                d = int(line.split("待下载资源数")[1].strip().split(":")[1].split("个")[0].strip())
            except Exception:
                pass
        if "待删除资源数" in line:
            try:
                r = int(line.split("待删除资源数")[1].strip().split(":")[1].split("个")[0].strip())
            except Exception:
                pass
    return {"downloads": d, "deletes": r}


def _sync_failure_reason(stdout: str) -> str | None:
    """从同步器输出识别数据源失败（0.8.17）。

    同步器（数据更新）对认证/连接失败也返回退出码 0——若不识别，会把
    "auth failed" 当成成功进入验证，并因未打印下载数量触发 None>0 崩溃
    （2026-08-16 事故：认证失败被掩盖成"同步异常：'>' not supported..."）。
    返回失败原因文本；无失败迹象 → None。
    """
    text = stdout or ""
    if "auth failed" in text:
        return "认证失败（auth failed），请检查数据源授权"
    if "状态:连接失败" in text or "连接失败" in text:
        return "数据源连接失败，请检查网络/数据源可用性"
    return None


def _sync_effective(before_date, after_date, counts) -> bool:
    """判定一次同步是否真正生效（纯函数，可单测）。

    同步器（数据更新）退出码 0 不代表有增量：镜像清单协议升级/清单损坏等
    情况下，更新器可能 0 下载且不改数据（2026-08-12 事故：manifest 解析失败
    零下载却退出码 0）。以「待下载数>0」或「本地数据日期确实前进」为准——
    两者都无则视为未生效，避免 webui 误报同步成功。
    """
    if counts and counts.get("downloads") not in (None, 0):
        return True
    return bool(before_date) and bool(after_date) and after_date > before_date


def data_latest_date(force: bool = False) -> str | None:
    """全市场行情最新交易日（8位）：取平安银行 000001 日K 最大日期。

    000001 每交易日都有行情，是可靠的"数据同步到哪天"探针；数据源本身
    不含指数表，这里不代表上证指数点位（概览页大盘指数另行处理）。
    近 3 月前缀通配，跨年安全。

    4s 心跳轮询会频繁调用：8 秒 TTL 缓存，避免每 4s 打 3 次 stockdb HTTP。
    同步验证/重启检测等需要实时的路径传 force=True 绕过缓存。
    """
    now = time.time()
    if not force and now - _latest_date_cache["at"] < 8:
        return _latest_date_cache["val"]  # 失败结果（None）同样缓存：stockdb 忙/挂时不重复打探
    if _stockdb_breaker_open():
        return _latest_date_cache["val"]  # 熔断中快速降级（冷却期后自动恢复探测）
    if not _latest_date_probe_lock.acquire(blocking=False):
        # 已有并发探测在跑（单飞）：立即返回缓存旧值（可能为 None），
        # 下一轮轮询自然拿到新值——避免多标签切换时 N 路同时打 4 个 10s 慢请求
        return _latest_date_cache["val"]
    try:
        import urllib.parse
        from datetime import datetime as dt, timedelta
        today = dt.now()
        start = (today - timedelta(days=95)).strftime("%Y%m%d")
        end = today.strftime("%Y%m%d")
        dates = []
        for prefix in _month_prefixes(start, end):
            try:
                path = f"/?cmd=vals&t={urllib.parse.quote(f'日k:000001:{prefix}*')}"
                rows = json.loads(stockdb_fetch(path, timeout=10, breaker=True))
                for row in rows if isinstance(rows, list) else []:
                    if isinstance(row, dict) and row.get("date"):
                        dates.append(str(row["date"]))
            except Exception:
                continue
        result = max(dates) if dates else None
        _latest_date_cache.update(at=now, val=result)
        return result
    finally:
        _latest_date_probe_lock.release()


def _classify_code(code: str) -> str:
    """按代码段归类：hk / etf / stock / other。

    港股：5 位数字或带 hk 前缀（如 00700 / hk00700）。
    ETF：沪市 51x/52x/56x/58x（510-518 宽基、520-529 新宽基、560-563 行业、588/589 科创），
          深市 159 开头。
    股票：0/3/6 开头（深主板 00x、创业板 300/301、沪主板 60x、科创板 688/689），
          4/8 开头与 92x（北交所 43x/83x/87x/88x/920x）。
    其他：LOF(16x/50x)、REITs(18x)、B股(200/900) 等场内非股票非 ETF 品种。
    """
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    if c.isdigit() and len(c) == 5:
        return "hk"
    if c[:2] in ("51", "52", "56", "58") or c[:3] == "159":
        return "etf"
    if c[:2] == "92" or c[:3] == "430" or c[0] in ("0", "3", "4", "6", "8"):
        return "stock"
    return "other"




# ==================== 港股数据（东财 + 腾讯，写入 hk日k: 表） ====================
_HK_TABLE = "hk日k"  # 港股日K自定义表（与上游命名空间隔离）
_HK_EM = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_HK_QT = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def _hk_fetch_daily_em(code: str) -> list[dict]:
    """东财港股日K（含成交额）。返回升序 [{date,open,high,low,close,volume,amount}]。"""
    import urllib.request, urllib.parse, re
    secid = "116." + code  # 116 = 港股市场
    url = (_HK_EM + "?" + urllib.parse.urlencode({
        "secid": secid, "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "klt": "101", "fqt": "1", "beg": "0", "end": "20500101"}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    klines = ((data.get("data") or {}).get("klines") or [])
    rows = []
    for line in klines:
        parts = str(line).split(",")
        if len(parts) < 6:
            continue
        date = int(parts[0].replace("-", ""))
        rows.append({
            "date": date,
            "open": float(parts[1]), "close": float(parts[2]),
            "high": float(parts[3]), "low": float(parts[4]),
            "volume": float(parts[5]),
            "amount": float(parts[6]) if len(parts) > 6 else None,
        })
    return rows


def _is_hk_code(code: str) -> bool:
    """港股代码识别：5 位数字，或带 hk 前缀（如 00700 / hk00700）。"""
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    return c.isdigit() and len(c) == 5


def _normalize_hk_code(code: str) -> str:
    """规范化为 5 位港股代码（00700）。"""
    c = str(code).strip().lower()
    if c.startswith("hk"):
        c = c[2:]
    return c.zfill(5)


def _hk_fetch_daily_qt(code: str) -> list[dict]:
    """腾讯港股日K（降级源）。格式 [date, open, close, high, low, volume]。"""
    import urllib.request, urllib.parse
    url = (_HK_QT + "?" + urllib.parse.urlencode({
        "param": f"hk{code},day,,,320,qfq"}))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0",
                                               "Referer": "https://gu.qq.com/"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    days = ((data.get("data") or {}).get(f"hk{code}", {}) or {}).get("day") or []
    rows = []
    for d in days:
        if len(d) < 6:
            continue
        rows.append({
            "date": int(d[0].replace("-", "")),
            "open": float(d[1]), "close": float(d[2]),
            "high": float(d[3]), "low": float(d[4]),
            "volume": float(d[5]), "amount": None,
        })
    return rows


def _hk_fetch_daily(code: str) -> list[dict]:
    """拉取港股日K（东财优先，腾讯降级）。"""
    code = _normalize_hk_code(code)
    try:
        rows = _hk_fetch_daily_em(code)
        if rows:
            return rows
    except Exception:
        pass
    return _hk_fetch_daily_qt(code)


def hk_sync(codes: list[str], years: int = 2) -> dict:
    """港股同步：拉取日K写入 mydb hk日k:{code}:{date}（三层 key，代码隔离）。

    years：保留最近 N 年日K（默认 2，约 520 根），避免全量写爆 mydb。
    value 内嵌 date（读取用 vals 无需 key 解析）；keys 通配查询有缓存问题，
    统一用 vals/get 读写（实测可靠）。
    """
    years = max(1, min(int(years or 2), 10))
    cutoff = int((datetime.now().replace(year=datetime.now().year - years)).strftime("%Y%m%d"))
    results = {}
    for raw in codes:
        code = _normalize_hk_code(raw)
        try:
            rows = _hk_fetch_daily(code)
            if not rows:
                results[code] = {"ok": False, "error": "无数据"}
                continue
            items = []
            for r in rows:
                val = {k: v for k, v in r.items() if k != "date"}
                val["date"] = r["date"]  # 内嵌 date，读取无需解析 key
                if r["date"] >= cutoff:
                    items.append((str(r["date"]), val))
            items = items[-520 * years:]  # 兜底截断
            with _rd_lock:  # 0.8.10：rd 单连接串行化 + 失败自愈
                try:
                    rd = _mydb_rd()
                    for key, value in items:
                        rd.set(_HK_TABLE, code, key, value).do()  # .do() 真正发送写入
                except Exception:
                    _mydb_rd_reset()
                    raise
            results[code] = {"ok": True, "bars": len(items),
                             "latest": max(r["date"] for r in rows)}
        except Exception as exc:
            results[code] = {"ok": False, "error": str(exc)[:200]}
    return results


def hk_klines(code: str) -> list[dict]:
    """读取 mydb hk日k: 表（升序）。value 内嵌 date，用 vals 全量读取。
    0.8.10：rd 读取持锁 + 失败自愈。"""
    code = _normalize_hk_code(code)
    with _rd_lock:
        try:
            rd = _mydb_rd()
            vals = rd.vals(_HK_TABLE, code, "*") or []
        except Exception:
            _mydb_rd_reset()
            raise
    rows = []
    for v in vals:
        v = _rd_to_py(v)
        if isinstance(v, dict) and v.get("date"):
            rows.append(v)
    rows.sort(key=lambda r: int(r["date"]))
    return rows


def code_stats() -> dict:
    """全市场标的数量，股票 / ETF 分开统计（其余归 other），并返回查询延迟 ms。

    延迟 = 拉取全市场代码列表耗时，供系统页「行情服务」健康卡显示。
    全市场代码列表 GET 较贵且 4s 心跳反复调用：15 秒缓存（仅缓存成功结果）。
    """
    now = time.time()
    if _code_stats_cache["val"] is not None and now - _code_stats_cache["at"] < 15:
        return _code_stats_cache["val"]
    if _stockdb_breaker_open():
        return {"stock": None, "etf": None, "other": None, "latency_ms": None}  # 熔断快速降级
    import urllib.parse
    t0 = time.time()
    try:
        path = f"/?cmd=get&t={urllib.parse.quote('股票代码')}"
        data = json.loads(stockdb_fetch(path, timeout=15, breaker=True))
        latency_ms = round((time.time() - t0) * 1000)
        codes: list[str] = []
        if isinstance(data, dict):
            for group in data.values():
                if isinstance(group, list):
                    codes.extend(str(c) for c in group)
        stats = {"stock": 0, "etf": 0, "other": 0}
        for c in set(codes):
            stats[_classify_code(c)] += 1
        stats["latency_ms"] = latency_ms
        _code_stats_cache.update(at=now, val=stats)
        return stats
    except Exception:
        return {"stock": None, "etf": None, "other": None, "latency_ms": None}


_coverage_cache: dict = {"at": 0.0, "data": None}  # 15 分钟缓存，避免 4s 轮询重复全历史扫描
_latest_date_cache: dict = {"at": 0.0, "val": None}  # 8 秒缓存：/api/status 4s 心跳不重复打 stockdb
_latest_date_probe_lock = threading.Lock()  # 单飞锁：并发缓存失效时只跑一路探测（其余立即取缓存）
_code_stats_cache: dict = {"at": 0.0, "val": None}   # 15 秒缓存：全市场代码列表 GET 较贵
_container_state_cache: dict = {"at": 0.0, "val": None}  # 5 秒缓存：stockdb 进程探测



def data_coverage() -> dict | None:
    """行情数据覆盖范围（最早 ~ 最新交易日），基于 000001 逐年前缀扫描。

    每 4s 轮询会反复请求 /api/status，全历史扫描较重，故缓存 15 分钟。
    返回 {"earliest": int, "latest": int} 或 None（无数据）。
    """
    now = time.time()
    if _coverage_cache["data"] is not None and now - _coverage_cache["at"] < 900:
        return _coverage_cache["data"]
    if _stockdb_breaker_open():
        return _coverage_cache["data"]  # 熔断快速降级（可能为 None）
    import urllib.parse
    from datetime import datetime as _dt
    this_year = _dt.now().year
    earliest = latest = None
    for y in range(1990, this_year + 1):
        try:
            path = f"/?cmd=vals&t={urllib.parse.quote(f'日k:000001:{y}*')}"
            rows = json.loads(stockdb_fetch(path, timeout=8, breaker=True))
            ds = [int(r["date"]) for r in rows if isinstance(r, dict) and r.get("date")]
            if ds:
                earliest = min(ds) if earliest is None else earliest
                latest = max(ds)
        except Exception:
            continue
    data = {"earliest": earliest, "latest": latest} if earliest is not None else None
    _coverage_cache.update(at=now, data=data)
    return data


_mirror_cache: dict = {"at": 0.0, "val": None}  # 镜像日期抓取缓存（10 分钟），避免 4s 轮询重复访问外网
_mirror_refresh_lock = threading.Lock()  # 防重入：镜像刷新后台线程同一时间只跑一个


def mirror_latest_date() -> str | None:
    """镜像源（a.123128.xyz 网页）标注的最新数据日期（10 分钟缓存）。

    镜像源是 LevelDB 文件镜像（HTTP + manifest），无行情 API，但它首页明文标注
    「数据更新至:YYYY-MM-DD」。抓该日期可判断「本地落后是同步未跑 vs 镜像未发布」。
    可配置 MIRROR_PAGE_URL 覆盖（内网映射/镜像源变更时）。

    镜像抓取是公网请求且可能很慢（最多 12s）：/api/status 4s 心跳里不能同步等它。
    缓存过期时在后台线程刷新，本请求立即返回旧值（无则 None），下次轮询即有新值。
    """
    if _mirror_cache["val"] is not None and time.time() - _mirror_cache["at"] < 600:
        return _mirror_cache["val"]

    def _refresh():
        try:
            import urllib.request, re
            url = os.environ.get("MIRROR_PAGE_URL", "https://a.123128.xyz/")
            result = None
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=12) as resp:
                    html = resp.read().decode("utf-8", "replace")
                m = re.search(r"数据更新至:(\d{4}-\d{2}-\d{2})", html)
                result = m.group(1) if m else None
            except Exception:
                result = None
            _mirror_cache.update(at=time.time(), val=result)
        finally:
            _mirror_refresh_lock.release()

    if _mirror_refresh_lock.acquire(blocking=False):
        threading.Thread(target=_refresh, daemon=True).start()
    # 过期瞬间：立即返回旧值（无则 None），避免阻塞心跳
    return _mirror_cache["val"]


def _workday_lag(today, latest_dt) -> int:
    """latest 距 today 之间的工作日数（跳过周六日）。法定节假日按近似处理。"""
    lag = 0
    d = latest_dt + timedelta(days=1)
    while d <= today:
        if d.weekday() < 5:
            lag += 1
        d += timedelta(days=1)
    return lag


def health_status() -> dict:
    """数据健康度：用工作日计数判定落后（跨周末不误报），盘前宽容。

    联动镜像源日期：mirror = 镜像网页标注的最新数据日期。
    - 本地落后但镜像已更新 → 提示"可同步"
    - 本地落后且镜像未更新 → 提示"镜像尚未发布"，避免误判同步坏了
    """
    latest = data_latest_date()
    mirror = mirror_latest_date()
    if not latest:
        return {"latest": None, "lag_days": None, "mirror": mirror,
                "status": "unknown", "note": "无法获取数据最新日期"}
    try:
        from datetime import datetime as dt
        latest_dt = dt.strptime(latest, "%Y%m%d").date()
        today = dt.now().date()
        lag = _workday_lag(today, latest_dt)
        if lag == 0:
            return {"latest": latest, "lag_days": 0, "mirror": mirror, "status": "ok",
                    "note": f"数据最新 {latest}（已是最新交易日）"}
        if lag == 1:
            # 交易日盘中/盘前：今日数据要等收盘后同步，属正常
            if today.weekday() < 5 and dt.now().hour < 16:
                return {"latest": latest, "lag_days": 0, "mirror": mirror, "status": "ok",
                        "note": f"数据至 {latest}（今日待收盘后同步）"}
        # 落后 1+ 交易日：看镜像是否已更新
        if mirror:
            mirror_norm = mirror.replace("-", "")
            if mirror_norm > latest:
                note = f"本地 {latest}，镜像已至 {mirror}——可同步"
            else:
                note = f"本地 {latest}，镜像尚未发布新数据（{mirror}）"
        else:
            note = f"数据落后 {lag} 个交易日（{latest}），建议立即同步"
        return {"latest": latest, "lag_days": lag, "mirror": mirror,
                "status": "stale", "note": note}
    except Exception as exc:
        return {"latest": latest, "lag_days": None, "mirror": mirror,
                "status": "unknown", "note": f"健康度计算失败: {exc}"}


# ==================== stockdb 进程级控制（0.5.0 单镜像，不再依赖 docker socket） ====================
# entrypoint 后台监督 stockdb 进程存活（读 pidfile，/data/.stockdb-paused 存在时不拉起）；
# webui 通过 pidfile + SIGTERM 停进程、删除暂停标记让监督器重新拉起。
# 本地开发模式（无 pidfile/进程）优雅降级为 unknown。


def _stockdb_pid() -> int | None:
    try:
        text = STOCKDB_PIDFILE.read_text(encoding="utf-8", errors="replace").strip()
        return int(text) if text.isdigit() else None
    except Exception:
        return None


def stockdb_proc_alive() -> bool:
    """pidfile 中的进程是否存活（pid 存在且可 SIG 0 探测）。"""
    pid = _stockdb_pid()
    if not pid:
        return False
    try:
        os.kill(pid, 0)  # 探测存在性，不发信号
        return True
    except OSError:
        return False


def _send_term(pid: int, timeout: float = 30.0) -> None:
    """SIGTERM 并等待退出；超时升级 SIGKILL。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return  # 已退出
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not stockdb_proc_alive():
            return
        time.sleep(0.3)
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def container_state(force: bool = False) -> dict:
    """返回 stockdb 运行状态详情（进程级）。

    {ok, status, note}：ok=False 表示本机无法控制 stockdb（本地开发/无 pidfile）。
    started 为进程启动时间（epoch 秒，供运行时长展示）；查询失败时保持 None。
    进程探测在 4s 心跳中频繁触发：5 秒缓存；同步流程里停/启后的状态校验传
    force=True 绕过缓存（否则可能读到停服前的 running 漏掉补启）。
    """
    now = time.time()
    if not force and _container_state_cache["val"] is not None and now - _container_state_cache["at"] < 5:
        return _container_state_cache["val"]
    alive = stockdb_proc_alive()
    started = None
    if alive:
        try:
            started = int(Path(f"/proc/{_stockdb_pid()}/stat").stat().st_ctime)
        except Exception:
            started = None
    result = {"ok": alive, "status": "running" if alive else "stopped",
              "note": "" if alive else "stockdb 进程未运行",
              "started": started}
    _container_state_cache.update(at=now, val=result)
    return result


def container_start() -> None:
    """删除暂停标记，由 entrypoint 监督器拉起 stockdb（进程不存在时立即拉起）。"""
    STOCKDB_PAUSE.unlink(missing_ok=True)


def container_stop() -> None:
    """写暂停标记（监督器不再拉起）+ SIGTERM 停进程（同 PID 双保险）。"""
    STOCKDB_PAUSE.touch(exist_ok=True)
    pid = _stockdb_pid()
    if pid:
        _send_term(pid)


def container_restart() -> None:
    """重启 stockdb 进程（热更新失败止损/加载新快照用）。"""
    container_stop()
    time.sleep(1)
    container_start()


def wait_stockdb_ready(timeout: float = 60.0) -> bool:
    """轮询等待 stockdb HTTP 服务就绪（重启后验证/查询前调用）。

    进程 restart 返回不代表服务已可查询，直接连可能连接拒绝导致误判；
    轮询 7899 的 /?cmd=get&t=股票代码 直到响应。超时返回 False。
    """
    import urllib.request, urllib.parse
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote('股票代码')}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def reload_stockdb() -> list[str]:
    """向运行中的 stockdb 发送 reload 命令，热重载 dataN 快照（零中断）。

    上游同步器设计为同步后向本地 127.0.0.1:7899 发 GET /?cmd=reload&t=<remote>
    让运行中的服务重载 LevelDB。单镜像下 STOCKDB_HOST=127.0.0.1 即自身。
    返回成功重载的 remote 列表（如 ["0","1"]）。
    """
    import urllib.request, urllib.parse
    ok: list[str] = []
    for remote in ("0", "1"):
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=reload&t={remote}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            if '"ok":true' in body or '"ok": true' in body:
                ok.append(remote)
        except Exception:
            continue
    return ok


def container_logs(tail: int = 150) -> str:
    """读取 stockdb 日志尾部（conf 的 logger.output=/data/log.txt）。"""
    if not STOCKDB_LOG_FILE.exists():
        return ""
    try:
        lines = STOCKDB_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])
    except Exception as exc:
        raise RuntimeError(f"读取 stockdb 日志失败: {exc}") from exc


# ==================== 同步任务（后台线程） ====================
def _verify_data(expected_date: str | None = None) -> list[str]:
    """同步后完整性验证。

    expected_date：同步前抓取的全市场最新交易日（8位）。同步后抽样股票的
    最新 bar 日期须 ≥ expected_date 才算同步生效——镜像停在旧日期时不会误报，
    也不会随时间推移退化成"验证某一天有没有数据"。
    返回异常列表（空=通过）。
    """
    problems = []
    try:
        import urllib.request, urllib.parse
        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now()
        months = _month_prefixes(
            (today - _td(days=95)).strftime("%Y%m%d"), today.strftime("%Y%m%d")
        )
        def q(table: str) -> str:
            path = f"/?cmd=get&t={urllib.parse.quote(table)}"
            return stockdb_fetch(path, timeout=15)  # 控制路径：只过信号量，不受熔断牵连
        def vals(table: str) -> list:
            path = f"/?cmd=vals&t={urllib.parse.quote(table)}"
            return json.loads(stockdb_fetch(path, timeout=15))
        codes_raw = q("股票代码")
        try:
            codes = json.loads(codes_raw)
            total = len(codes.get("0", codes)) if isinstance(codes, dict) else len(codes)
            if total == 0:
                problems.append(f"股票代码为空（total=0）")
        except Exception:
            problems.append("股票代码解析失败")
        # 抽样 3 只不同板块：沪市 600633 / 深市 000001 / 创业板 300750
        exp = int(expected_date or 0)
        for code in ("600633", "000001", "300750"):
            try:
                dates = []
                for prefix in months:
                    for row in vals(f"日k:{code}:{prefix}*"):
                        if isinstance(row, dict) and row.get("date"):
                            dates.append(int(row["date"]))
                if not dates:
                    problems.append(f"{code} 日K 无数据")
                elif exp and max(dates) < exp:
                    problems.append(f"{code} 最新 {max(dates)}，早于同步前基准 {exp}（同步未生效）")
            except Exception as exc:
                problems.append(f"{code} 日K 查询失败: {exc}")
    except Exception as exc:
        problems.append(f"验证接口异常: {exc}")
    return problems


def run_sync(hot: bool = True, trigger: str = "manual", retry: bool = False) -> None:
    """同步数据。串行执行，防并发点击。

    hot=True（默认，热更新）：不停服务直接增量同步——同步器下载到 .part 临时文件、
    SHA256 校验后原子 rename 替换（Unix rename 对读进程无影响），服务端持续可查。
    官方 DATA_SOURCE.md 保守要求"同步期间停止服务"，但实测与机制均支持热更新；
    上游多点数据源架构下增量下载进 data1/LevelDB，运行中的服务进程持旧快照，
    故检测到新数据文件（下载数>0）时同步完成后自动重启 stockdb 加载新快照，
    再自动做完整性验证，失败则再次重启止损。

    hot=False（严格模式）：按官方要求先停服务 → 同步 → 重启，作为兜底。

    trigger=manual|scheduled|scheduled-retry：记录触发来源（手动按钮 / 定时线程 /
    定时失败后的自动重试），写入同步历史，便于回看"某次同步是不是定时自动跑的"。
    """
    if not _sync_lock.acquire(blocking=False):
        return  # 已在同步中
    _sync_state.update(running=True, exit_code=None, last_start=time.time(), last_end=None,
                       trigger=trigger, phase="stopping" if not hot else "syncing",
                       fail_reason=None)
    global _last_sync_stdout, _last_verify_result
    _last_sync_stdout = ""
    _last_verify_result = None
    try:
        log(f"=== 同步开始 {now()}（{'热更新' if hot else '严格模式(停服)'}｜{'定时' if trigger.startswith('scheduled') else '手动'}{'·重试' if retry else ''}）===")

        # 0. 同步前抓全市场最新交易日（作为同步后验证基准；绕过 TTL 缓存）
        before_date = None
        try:
            before_date = data_latest_date(force=True)
            if before_date:
                log(f"→ 同步前数据最新交易日：{before_date}")
        except Exception:
            pass

        # 1.（严格模式）停 stockdb；热更新模式不停
        if not hot:
            _sync_state["phase"] = "stopping"
            log("→ 停止 stockdb 进程 ...")
            try:
                if container_state(force=True).get("status") == "running":
                    container_stop()
                else:
                    log("  （stockdb 已处于停止状态）")
            except Exception as exc:
                log(f"  ⚠️ 停止失败，继续同步（风险：数据卷并发写）：{exc}")
        else:
            st = container_state(force=True)
            if st["status"] == "running":
                log("→ 热更新：stockdb 保持运行，直接增量同步 ...")
            elif st["status"] in ("exited", "not-found"):
                log(f"→ 热更新：stockdb 当前 {st['status']}，同步后尝试启动 ...")
            else:
                log(f"→ 热更新：stockdb 状态 {st['status']}（{st['note']}），仍继续同步 ...")

        # 2. 同步数据（同步器读当前目录 sync_url.txt / stockdb.conf）
        _sync_state["phase"] = "syncing"
        log("→ 运行 数据更新（增量同步，断点续传）...")
        cfg = DATA_DIR / "sync_url.txt"
        if not cfg.exists():
            log("  ⚠️ /data/sync_url.txt 不存在，使用镜像模板")
        else:
            sources = [ln for ln in cfg.read_text(encoding="utf-8").splitlines()
                       if ln.strip() and not ln.strip().startswith("#")]
            log(f"  数据源: {sources[0] if sources else '（空，无法同步）'}")
        proc = subprocess.run(
            ["/opt/stockdb/数据更新"],
            cwd=str(DATA_DIR),
            capture_output=True, text=True, timeout=3600 * 6,
        )
        for line in (proc.stdout or "").splitlines():
            log(f"  {line}")
        for line in (proc.stderr or "").splitlines():
            log(f"  [err] {line}")
        _last_sync_stdout = proc.stdout or ""
        _sync_state["exit_code"] = proc.returncode
        if proc.returncode != 0:
            _sync_state["fail_reason"] = f"同步器异常退出（code {proc.returncode}）"
        log(f"→ 数据更新退出码 {proc.returncode}")

        # 3. 热更新模式：验证数据完整性（此时 stockdb 仍在运行）
        if hot:
            if proc.returncode == 0:
                _sync_state["phase"] = "verifying"
                # 0.8.17：同步器对认证/连接失败也返回 0——先识别失败输出，
                # 避免把"auth failed"当成成功进入验证（并触发 None>0 崩溃）
                failure = _sync_failure_reason(_last_sync_stdout)
                if failure:
                    _sync_state["fail_reason"] = f"数据源失败：{failure}"
                    _last_verify_result = "skipped"
                    log(f"  ⚠️ 数据源失败（{failure}），跳过完整性验证")
                    log("  → 同步未执行，数据保持原状（恢复认证后重试）")
                else:
                    log("→ 热更新完成，验证数据完整性 ...")
                # 上游新架构（多点数据源）下增量下载进 data1/LevelDB，
                # 运行中的 stockdb 进程仍持旧快照。优先用上游 reload 命令热重载
                # （零中断）；reload 不可用（老版本/连接失败）则降级重启。
                # 无新文件（downloads=0）时数据未变，跳过加载。
                counts = parse_sync_counts(_last_sync_stdout)
                downloads = counts.get("downloads")
                # 0.8.17 修复：downloads=None（同步器未打印数量）时禁止比较——
                # 旧写法 counts.get("downloads", 0) > 0 在 key 存在值为 None 时
                # 抛 "'>' not supported between instances of 'NoneType' and 'int'"
                # （2026-08-16 auth failed 事故：认证失败被掩盖成同步异常）
                if failure:
                    pass  # 数据源失败：不重启不验证（fail_reason 已置，_last_verify_result=skipped）
                elif downloads is None or downloads > 0:
                    _sync_state["phase"] = "restarting"
                    reloaded = reload_stockdb()
                    if reloaded:
                        _sync_state["phase"] = "verifying"
                        log(f"  ✅ stockdb 热重载成功（remote {','.join(reloaded)}），零中断")
                    else:
                        log("→ reload 不可用（老版本/命令失败），降级重启 stockdb 加载新快照 ...")
                        try:
                            container_restart()
                            log("  ✅ stockdb 已重启")
                            if not wait_stockdb_ready():
                                log("  ⚠️ 重启后服务未在 60s 内就绪，验证可能失败")
                        except Exception as exc:
                            log(f"  ❌ 重启失败：{exc}")
                        _sync_state["phase"] = "verifying"
                if failure:
                    pass  # 数据源失败：跳过完整性验证
                else:
                    problems = _verify_data(before_date)
                    if problems:
                        _last_verify_result = "fail"
                        _sync_state["fail_reason"] = "数据完整性验证未通过"
                        log(f"  ⚠️ 完整性验证未通过：{problems}")
                        log("  → 数据异常，自动重启 stockdb 止损 ...")
                        try:
                            container_restart()
                            log("  ✅ stockdb 已重启")
                        except Exception as exc:
                            log(f"  ❌ 重启失败：{exc}")
                    else:
                        _last_verify_result = "pass"
                    log("  ✅ 数据完整性验证通过（股票代码 + 抽样日K/复权/分钟K）")
            else:
                _last_verify_result = "skipped"
                log("  ⚠️ 同步退出码非 0，跳过完整性验证")
                # 同步失败时确保服务仍在（可能中途被手动停过）；force 绕过缓存
                if container_state(force=True).get("status") != "running":
                    try:
                        container_start()
                        log("  → 已尝试重新启动 stockdb")
                    except Exception as exc:
                        log(f"  ❌ 启动失败：{exc}")

        # 4.（严格模式）重启服务；热更新若中途发现服务没跑也补启
        if not hot:
            _sync_state["phase"] = "restarting"
            log("→ 启动 stockdb 进程 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")
        elif container_state(force=True).get("status") in ("exited", "not-found"):
            _sync_state["phase"] = "restarting"
            log("→ 热更新收尾：stockdb 未在运行，尝试启动 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")

        log(f"=== 同步结束 {now()} ===")
        _sync_state["last_end"] = time.time()
    except Exception as exc:
        log(f"❌ 同步异常：{exc}")
        _sync_state["exit_code"] = -1
        _sync_state["fail_reason"] = f"同步异常：{exc}"
    finally:
        # 记录同步历史（时间/触发来源/模式/结果/耗时/下载删除数/数据最新日期/失败原因）
        try:
            counts = parse_sync_counts(_last_sync_stdout)
            after_date = data_latest_date(force=True)
            effective = _sync_effective(before_date, after_date, counts)
            warn = None
            if _sync_state.get("exit_code") == 0 and not effective:
                warn = "同步未生效：下载 0 文件且数据未更新（镜像清单可能已变更）"
            append_history({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": _sync_state.get("trigger", "manual"),
                "mode": "hot" if hot else "strict",
                "exit_code": _sync_state.get("exit_code"),
                "reason": _sync_state.get("fail_reason"),
                "downloads": counts.get("downloads"),
                "deletes": counts.get("deletes"),
                "verified": _last_verify_result,
                "duration_sec": round(time.time() - _sync_state["last_start"], 1)
                if _sync_state.get("last_start") else None,
                "data_latest": after_date,
                "warn": warn,
            })
            if _sync_state.get("trigger", "").startswith("scheduled"):
                _update_schedule_trigger_exit(_sync_state.get("exit_code"), retry=retry)
        except Exception:
            pass
        _sync_state["running"] = False
        _sync_state["last_end"] = time.time()
        _sync_state["phase"] = "done"
        _sync_lock.release()


def log(line: str) -> None:
    """同步日志（0.9.2 批次 2：实现迁 ops/logging.py，本处仅保留转发兼容）。"""
    _ops_log(line)


def tail_log(n: int = 200) -> str:
    """同步日志尾部（0.9.2 批次 2：实现迁 ops/logging.py）。"""
    return _ops_tail_log(n)


def now() -> str:
    """当前时间戳（0.9.2 批次 2：实现迁 ops/logging.py）。"""
    return _ops_now()


# ==================== 定时自动同步 ====================
def _pending_times(now_hm: str, times: list[str], fired: set) -> list[str]:
    """当前已到且今天尚未触发过的时间点（多时间点防循环触发核心判定）。

    now_hm: HH:MM；times: 配置的时间点（已排序）；fired: 今天已触发时间点集合。
    返回应触发的时间点（通常 0 或 1 个）。
    """
    return [t for t in times if now_hm >= t and t not in fired]


def scheduler_loop() -> None:
    """后台定时线程：每 30s 检查定时配置，到点触发热更新同步。

    触发判定（多时间点防循环）：
      维护 fired={日期:[已触发时间点]}；对每个配置时间点 t，仅当
      now>=t 且 t 不在「今天已触发集合」内才触发，并立即加入集合。
      多时间点不再交替重复触发；容器重启后集合落盘，当天不会重复触发。

    trading_only（默认开）：非交易日不触发（is_trading_day：工作日排除 A 股法定休市）。

    失败自动重试（持久化，重启不丢）：
      最近一次定时触发 exit!=0 且该时间点今天未安排过重试 → 登记 retry_pending=10 分钟后，
      retried={日期:[已安排重试的时间点]} 防重复安排。到点执行 run_sync(trigger=scheduled-retry)；
      执行前若最后一次触发已成功则取消。重试仍失败不再重试。
    """
    global _scheduler_alive, _scheduler_heartbeat
    while True:
        _scheduler_alive = True
        _scheduler_heartbeat = time.time()
        try:
            cfg = load_schedule()
            now = datetime.now()
            today_key = now.strftime("%Y-%m-%d")
            now_hm = now.strftime("%H:%M")
            if cfg["enabled"] and cfg["times"] and not _sync_state["running"]:
                if cfg["trading_only"] and not is_trading_day(now.date()):
                    continue  # 非交易日：不触发，也不安排重试
                # 正常触发优先；每轮只启动一个任务（if/else 隔离），
                # 避免同轮「正常触发 + 到期重试」并发启动两个线程，导致
                # run_sync 锁静默拒绝其中一个而 fired 已标记（计划未执行却视为已执行）。
                # 跨轮由外层 not _sync_state["running"] 守卫，不存在并发。
                fired = set((cfg.get("fired") or {}).get(today_key, []))
                due = _pending_times(now_hm, cfg["times"], fired)
                if due:
                    t = due[0]
                    _mark_fired(t)
                    _mark_last_trigger(f"{today_key} {t}", t)
                    log(f"⏰ 定时同步触发（{t}）——stockdb 保持运行，热更新")
                    threading.Thread(
                        target=run_sync,
                        kwargs={"hot": True, "trigger": "scheduled"},
                        daemon=True,
                    ).start()
                else:
                    # 本轮无正常触发 → 检查失败自动重试（登记或到期执行）
                    rp = cfg.get("retry_pending")
                    if rp and now.strftime("%Y-%m-%d %H:%M:%S") >= rp:
                        _clear_retry_pending()
                        lt = cfg.get("last_trigger") or {}
                        if lt.get("exit") == 0:
                            log("↻ 重试前同步已成功，取消重试")
                        else:
                            log("↻ 定时重试执行（上次失败）——stockdb 保持运行，热更新")
                            threading.Thread(
                                target=run_sync,
                                kwargs={"hot": True, "trigger": "scheduled-retry", "retry": True},
                                daemon=True,
                            ).start()
                    else:
                        lt = cfg.get("last_trigger") or {}
                        retried = set((cfg.get("retried") or {}).get(today_key, []))
                        lt_t = lt.get("t")
                        if (lt.get("exit") not in (None, 0) and lt.get("key", "").startswith(today_key)
                                and lt_t and lt_t not in retried):
                            _mark_retried(lt_t)
                            log(f"↻ 定时同步上次失败（exit={lt.get('exit')}），安排 10 分钟后自动重试 ...")
        except Exception as exc:
            log(f"⏰ 定时线程异常: {exc}")
        time.sleep(30)


_cap_cache: dict = {"at": 0.0, "val": None}  # 同步能力检查缓存（60s），避免 4s 轮询反复探测磁盘


def sync_capability() -> dict:
    """同步能力检查：更新程序 / 数据源 / 数据卷可写 / 待重试（warn 级，不参与可用性）。

    比只看 Docker 更能解释"为什么同步失败"。本地开发模式（无 /opt/stockdb/数据更新）
    返回 ok=False，前端展示为不可用。
    待重试任务属于"正在进行中的重试计划"，是 warn/info 而非能力缺失，不参与 ok 计算。
    数据卷探测用唯一临时文件（多浏览器并发不互相删除探针），结果缓存 60s。
    """
    if _cap_cache["val"] is not None and time.time() - _cap_cache["at"] < 60:
        return _cap_cache["val"]
    import tempfile
    checks = {}
    # 1. 更新程序（容器内发行版同步器）
    updater = Path("/opt/stockdb/数据更新")
    if updater.is_file() and os.access(str(updater), os.X_OK):
        checks["updater"] = {"ok": True, "detail": "更新程序存在"}
    elif updater.exists():
        checks["updater"] = {"ok": False, "detail": "更新程序存在但不可执行"}
    else:
        checks["updater"] = {"ok": False, "detail": "未找到更新程序 /opt/stockdb/数据更新"}
    # 2. 数据源配置
    src = ""
    cfg = DATA_DIR / "sync_url.txt"
    if cfg.exists():
        lines = [ln.strip() for ln in cfg.read_text(encoding="utf-8", errors="replace").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        src = lines[0] if lines else ""
    checks["source"] = {"ok": bool(src), "detail": src if src else "sync_url.txt 无有效数据源"}
    # 3. 数据卷可写（唯一临时文件名，避免多浏览器同时删除彼此的探针）
    try:
        probe = Path(tempfile.mktemp(prefix=".write_probe_", dir=str(DATA_DIR)))
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["writable"] = {"ok": True, "detail": "数据卷可写"}
    except Exception as exc:
        checks["writable"] = {"ok": False, "detail": f"数据卷不可写: {exc}"}
    # 4. 待重试任务（warn/info 级，不参与 ok）
    rp = load_schedule().get("retry_pending")
    checks["retry_pending"] = {"warn": bool(rp),
                               "detail": f"等待重试：{rp}" if rp else "无待重试任务"}
    cap_ok = all(c.get("ok") for c in checks.values() if "ok" in c)
    result = {"ok": cap_ok, "warn": bool(rp), "checks": checks}
    _cap_cache.update(at=time.time(), val=result)
    return result


def last_sync_summary() -> dict | None:
    """最近一次同步摘要（历史数组末尾=最新记录）。"""
    h = load_history()
    return h[-1] if h else None


def disk_usage() -> dict:
    """数据卷磁盘用量（/data 挂载点）。"""
    try:
        import shutil
        total, used, free = shutil.disk_usage(str(DATA_DIR))
        return {"total_gb": round(total / 2 ** 30, 1),
                "used_gb": round(used / 2 ** 30, 1),
                "free_gb": round(free / 2 ** 30, 1)}
    except Exception:
        return {"total_gb": None, "used_gb": None, "free_gb": None}


# ==================== K 线区间查询（复刻本地 MCP 的 vals 前缀通配） ====================
def _month_prefixes(start: str, end: str) -> list[str]:
    try:
        y0, m0 = int(start[:4]), int(start[4:6])
        y1, m1 = int(end[:4]), int(end[4:6])
    except ValueError:
        return []
    prefixes, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        prefixes.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return prefixes


def stockdb_get(table: str) -> str:
    import urllib.parse
    return stockdb_fetch(f"/?cmd=get&t={urllib.parse.quote(table)}", timeout=15)


# ==================== 运营支撑（Phase 4.5：告警中心 / MCP 调用捕获 / 上游版本探针） ====================
# 告警中心（Alerts/notify_alert/_get_alerts/常量）0.9.2 批次 2 已迁 ops/alerts.py，
# 此处仅保留 import 暴露（app.Alerts 等名字不变，测试与 HTTP 处理器引用无感）。
from ops.alerts import (  # noqa: E402 - 横切关注点（ops 层）随模块顶部统一装配
    ALERT_LEVELS,
    ALERT_LEVEL_ALIASES,
    MAX_ALERTS,
    Alerts,
    _get_alerts,
    notify_alert,
)
from ops.logging import (  # noqa: E402
    log as _ops_log,
    now as _ops_now,
    tail_log as _ops_tail_log,
)

MCP_CALLS_FILE_MAX_LINES = 2000       # mcp_calls.jsonl 行数上限（超出截断保留尾部）
MCP_CALLS_DEQUE_MAX = 500             # 内存调用 deque 上限（最新 500 条）
MCP_CALLS_LIST_DEFAULT = 100          # list_mcp_calls 默认条数

GITHUB_RELEASE_URL = "https://api.github.com/repos/hello245m/free-stockdb/releases/latest"
RELEASE_TTL_SECONDS = 3600            # 上游版本探针 TTL 缓存（成功与失败均缓存）


def _now_iso() -> str:
    """当前本地时间 ISO（秒级）：2026-08-14T21:52:30。"""
    return datetime.now().isoformat(timespec="seconds")




# ---- 数据新鲜度告警（迁移自 test_ops.py 可执行规格，行为基线一致） ----
FRESHNESS_LAG_THRESHOLD = 2   # 数据新鲜度滞后阈值（交易日滞后 > 2 天告警）


def _parse_date(s) -> date | None:
    """解析日期：支持 YYYYMMDD / YYYY-MM-DD；非法返回 None（不抛）。"""
    s = str(s).strip()
    if len(s) == 8 and s.isdigit():
        try:
            return datetime.strptime(s, "%Y%m%d").date()
        except ValueError:
            return None
    try:
        return date.fromisoformat(s[:10])
    except (ValueError, TypeError):
        return None


def data_freshness_alert(latest_date, is_trading_day, *,
                         threshold: int = FRESHNESS_LAG_THRESHOLD,
                         alerts=None) -> None:
    """行情数据新鲜度告警（两分支，均为 warning 级、来源「数据」、当日去重）。

    分支1：latest_date 为 None（探针失败，或日期格式无法解析）→
          「行情数据不可用（探针失败）」；
    分支2：滞后天数 = (今天 - latest).days > threshold（默认 2）且 is_trading_day
          （今天是交易日，数据本应更新）→ 「行情数据已滞后 N 天（最新 D）」。
          非交易日滞后不告警（休市日数据不更新属正常）；滞后为负（时钟超前）
          不告警。

    参数：
      latest_date:    最新交易日 'YYYYMMDD' / 'YYYY-MM-DD'；None 视为探针失败
      is_trading_day: 今天是否为交易日（由调用方按日历判定后传入）
      threshold:      滞后天数阈值（默认 2，仅 is_trading_day 时生效）
      alerts:         告警中心（缺省用模块单例；测试可注入隔离实例）
    """
    target = alerts if alerts is not None else _get_alerts()
    if latest_date is None:
        target.add("warning", "数据", "行情数据不可用（探针失败）")
        return
    d = _parse_date(latest_date)
    if d is None:  # 日期无法解析 → 视为不可用（保守告警）
        target.add("warning", "数据", "行情数据不可用（探针失败）")
        return
    lag = (date.today() - d).days
    if is_trading_day and lag > threshold:
        target.add("warning", "数据", f"行情数据已滞后 {lag} 天（最新 {latest_date}）")


def ops_watchdog_loop(interval: float = 60.0) -> None:
    """运营支撑看门狗线程：周期投递生产告警（告警中心的生产接线点）。

    每 interval 秒评估一次（启动后预热 30s，等待首次数据探针/日历就绪，避免
    进程启动瞬间误报）：
      数据新鲜度：data_latest_date() 探针失败，或今日（交易日）滞后 > 阈值
      → data_freshness_alert 投递 warning（当日去重，不会刷屏）。
    看门狗自身异常绝不退出线程（stderr 提示后继续，与调度线程同级容错）。
    """
    time.sleep(30)  # 预热：等待首次数据探针/日历就绪，避免进程启动瞬间误报
    while True:
        try:
            data_freshness_alert(data_latest_date(), is_trading_day())
        except Exception:  # noqa: BLE001 - 单次评估异常不退出看门狗
            _warn("数据新鲜度看门狗评估异常（已忽略）")
        time.sleep(interval)


# ==================== MCP 调用捕获 / 列表 / 统计 ====================
_mcp_deque: "collections.deque" = collections.deque(maxlen=MCP_CALLS_DEQUE_MAX)
_mcp_lock = threading.Lock()
_mcp_file_lines = 0     # jsonl 当前行数（截断判断用）
_mcp_loaded = False     # 是否已从文件惰性加载过（进程重启后恢复统计）


def _mcp_ensure_loaded_locked() -> None:
    """惰性加载：进程重启后首次访问，从 jsonl 尾部恢复最近记录到 deque。

    必须在持有 _mcp_lock 时调用。文件缺失/损坏行 → 静默跳过，不影响统计。
    """
    global _mcp_loaded, _mcp_file_lines
    if _mcp_loaded:
        return
    _mcp_loaded = True
    path = str(DATA_DIR / "mcp_calls.jsonl")
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                lines = [ln for ln in f if ln.strip()]
            _mcp_file_lines = len(lines)
            for ln in lines[-MCP_CALLS_DEQUE_MAX:]:
                try:
                    _mcp_deque.append(json.loads(ln))
                except (ValueError, TypeError):
                    continue
    except OSError:
        pass


def _mcp_truncate_file_locked() -> None:
    """jsonl 超上限截断：保留尾部 MCP_CALLS_FILE_MAX_LINES 行（锁内调用）。

    读失败时不反复重试：把行数记到上限，下一轮再触发时重新尝试。
    """
    global _mcp_file_lines
    path = str(DATA_DIR / "mcp_calls.jsonl")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        with open(path, "w", encoding="utf-8") as f:
            f.writelines(lines[-MCP_CALLS_FILE_MAX_LINES:])
        _mcp_file_lines = len(lines[-MCP_CALLS_FILE_MAX_LINES:])
    except OSError:
        _mcp_file_lines = MCP_CALLS_FILE_MAX_LINES


def capture_mcp_call(rec: dict) -> None:
    """捕获一次 MCP 调用。

    rec 只保留 6 键（缺省补默认，冗余键丢弃；值均为 str/int/bool，可安全序列化）：
      ts         调用时间（缺省当前本地时间 ISO）
      tool       工具名（如 get_kline）
      ok         是否成功（缺省 = not is_error）
      is_error   MCP isError 标记
      elapsed_ms 耗时毫秒
      bytes      响应体字节数
    落盘：DATA_DIR/mcp_calls.jsonl 追加一行 JSON；超 2000 行截断保留尾部。
    内存：deque（上限 500）供 list/stats 实时计算；落盘失败不影响内存统计。
    """
    global _mcp_file_lines
    ts = str(rec.get("ts") or _now_iso())
    is_error = bool(rec.get("is_error"))
    ok = rec.get("ok") if rec.get("ok") is not None else (not is_error)
    norm = {
        "ts": ts,
        "tool": str(rec.get("tool") or ""),
        "ok": bool(ok),
        "is_error": is_error,
        "elapsed_ms": int(rec.get("elapsed_ms") or 0),
        "bytes": int(rec.get("bytes") or 0),
    }
    with _mcp_lock:
        _mcp_ensure_loaded_locked()
        try:
            parent = os.path.dirname(str(DATA_DIR / "mcp_calls.jsonl"))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with open(str(DATA_DIR / "mcp_calls.jsonl"), "a", encoding="utf-8") as f:
                f.write(json.dumps(norm, ensure_ascii=False) + "\n")
            _mcp_file_lines += 1
            if _mcp_file_lines >= MCP_CALLS_FILE_MAX_LINES:
                _mcp_truncate_file_locked()
        except OSError:
            pass  # 落盘失败静默降级：内存统计照常
        _mcp_deque.append(norm)


def list_mcp_calls(limit: int = MCP_CALLS_LIST_DEFAULT) -> list:
    """最近 MCP 调用（最新在前，来自内存 deque；进程重启后从 jsonl 惰性恢复）。"""
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = MCP_CALLS_LIST_DEFAULT
    if limit < 1:
        limit = MCP_CALLS_LIST_DEFAULT
    with _mcp_lock:
        _mcp_ensure_loaded_locked()
        items = [dict(r) for r in reversed(_mcp_deque)]
    return items[:limit]


def mcp_stats() -> dict:
    """MCP 调用统计（从内存 deque 计算，最新 500 条窗口）。

    返回 {total, ok_rate, avg_ms, p95_ms, by_tool}：
      ok_rate   成功率 0~1（空窗口为 None）
      avg_ms    平均耗时毫秒（空窗口 None）
      p95_ms    耗时 P95（最近邻排序法；空窗口 None）
      by_tool   [{tool, n, ok, avg_ms}, ...]（按调用次数降序）
    """
    with _mcp_lock:
        _mcp_ensure_loaded_locked()
        recs = list(_mcp_deque)
    total = len(recs)
    if total == 0:
        return {"total": 0, "ok_rate": None, "avg_ms": None,
                "p95_ms": None, "by_tool": []}
    ok = sum(1 for r in recs if r.get("ok"))
    ms = [float(r.get("elapsed_ms") or 0) for r in recs]
    avg_ms = sum(ms) / total
    sorted_ms = sorted(ms)
    p95 = sorted_ms[max(0, math.ceil(0.95 * total) - 1)]
    by_tool: dict[str, dict] = {}
    for r in recs:
        tool = str(r.get("tool") or "?")
        b = by_tool.setdefault(tool, {"tool": tool, "n": 0, "ok": 0, "_sum": 0.0})
        b["n"] += 1
        if r.get("ok"):
            b["ok"] += 1
        b["_sum"] += float(r.get("elapsed_ms") or 0)
    out = []
    for b in by_tool.values():
        out.append({"tool": b["tool"], "n": b["n"], "ok": b["ok"],
                    "avg_ms": round(b["_sum"] / b["n"], 1)})
    out.sort(key=lambda x: (-x["n"], x["tool"]))
    return {"total": total,
            "ok_rate": round(ok / total, 4),
            "avg_ms": round(avg_ms, 1),
            "p95_ms": round(p95, 1),
            "by_tool": out}


def _mcp_tool_name(msg: dict) -> str:
    """从 JSON-RPC 请求提取工具名：tools/call → params.name；其余 → method。"""
    if not isinstance(msg, dict):
        return ""
    method = str(msg.get("method") or "")
    if method == "tools/call":
        params = msg.get("params")
        if isinstance(params, dict) and params.get("name"):
            return str(params["name"])
    return method


# ==================== 上游最新版本探针 ====================
_RELEASE_CACHE = {"at": 0.0, "val": None}   # {at: unix 秒, val: dict|None}


def fetch_upstream_release(*, timeout: float = 10, ttl: float = RELEASE_TTL_SECONDS,
                           force: bool = False) -> dict | None:
    """上游最新版本探针：GET GitHub releases/latest（浏览器形态 UA）。

    成功 → {tag_name, html_url, published_at}；失败/网络异常/解析失败 → None
    （不抛）。结果 TTL 缓存（默认 3600s；成功与失败均缓存，避免失败时反复打
    GitHub）；force=True 绕过缓存（手动刷新用）。
    """
    now = time.time()
    if not force and now - _RELEASE_CACHE["at"] < ttl:
        return _RELEASE_CACHE["val"]
    val = None
    try:
        req = urllib.request.Request(
            GITHUB_RELEASE_URL,
            headers={
                # 浏览器形态 UA：GitHub API 对默认 urllib UA 偶发 403
                "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"),
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and data.get("tag_name") is not None:
            val = {"tag_name": str(data["tag_name"]),
                   "html_url": str(data.get("html_url") or ""),
                   "published_at": data.get("published_at")}
    except Exception:  # noqa: BLE001 - 探针失败返回 None，不抛
        val = None
    _RELEASE_CACHE.update(at=now, val=val)
    return val


def _version_tuple(s) -> tuple | None:
    """从字符串提取首个 X.Y[.Z] 版本三元组（'v0.3.1' / '测试版本0.3.1' → (0,3,1)）。"""
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", str(s))
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3) or 0))


# ==================== HTTP 服务 ====================
# ==================== 前端静态服务（Phase 5 M0：SPA 外壳 + /legacy 逃生通道） ====================
# 前端已重构为 Vue SPA（docker/webui/spa/，构建产物在镜像内 /opt/webui/static/）。
# 旧面板（原 PAGE 字符串）完整保留在 static/legacy/index.html，路由 /legacy 原样渲染，
# 作为逃生通道；WEBUI_UI=legacy 时根路径改用旧面板（默认 spa；SPA 未构建时自动兜底旧面板）。
# 安全：静态文件定位一律 realpath 校验必须落在 STATIC_DIR 内，防路径穿越。
STATIC_DIR = Path(os.environ.get(
    "WEBUI_STATIC_DIR",
    str(Path(__file__).resolve().parent / "static"),
))
WEBUI_UI = os.environ.get("WEBUI_UI", "spa").strip().lower()  # spa | legacy

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".map": "application/json; charset=utf-8",
}
# 可长缓存的静态资源扩展名（Vite 产物文件名带内容哈希，可 immutable 缓存）
_CACHEABLE_EXT = {".js", ".css", ".svg", ".png", ".ico", ".woff", ".woff2", ".map"}


def _static_file(rel: str):
    """在 STATIC_DIR 内安全定位文件：路径穿越 / 不存在一律返回 None。"""
    base = STATIC_DIR.resolve()
    target = (base / rel.lstrip("/")).resolve()
    if target != base and not str(target).startswith(str(base) + os.sep):
        return None
    if not target.is_file():
        return None
    return target


def _spa_index() -> str:
    f = _static_file("index.html")
    if f is None:
        f = _static_file("legacy/index.html")  # SPA 未构建（本地 dev）→ 兜底旧面板
    if f is None:
        return "<!doctype html><html><body><h1>static 目录缺失</h1></body></html>"
    return f.read_text(encoding="utf-8")


def _ui_index() -> str:
    """根路径入口：WEBUI_UI=legacy → 旧面板；否则 SPA index.html。"""
    if WEBUI_UI == "legacy":
        f = _static_file("legacy/index.html")
        return f.read_text(encoding="utf-8") if f else _spa_index()
    return _spa_index()


def version_payload() -> dict:
    """版本信息载荷（_version 与 /api/overview 共用）。"""
    upstream = fetch_upstream_release()
    image_tag = os.environ.get("IMAGE_TAG") or os.environ.get("STOCKDB_VERSION") or None
    stale = False
    msg = ""
    if upstream is not None and upstream.get("tag_name"):
        up_tag = upstream["tag_name"]
        cur_src = image_tag if image_tag else WEBUI_VERSION
        ut = _version_tuple(up_tag)
        ct = _version_tuple(cur_src)
        if ut and ct and ut > ct:
            stale = True
            msg = (f"上游已发布 {up_tag}（当前{'镜像' if image_tag else '面板'} "
                   f"{cur_src}），建议升级")
    return {
        "webui": {"version": WEBUI_VERSION},
        "image": {"tag": image_tag},
        "upstream": upstream,
        "stale": stale,
        "msg": msg,
        "ui_mode": WEBUI_UI,
    }


def _process_rss_mb() -> int | None:
    """进程常驻内存 MB（0.8.10 遥测：内存类事故可远程观察）。
    Linux 读 /proc/self/statm；其他平台 resource 兜底。"""
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            parts = fh.read().split()
        if len(parts) >= 2:
            page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
            return int(parts[1]) * page_kb // 1024
    except Exception:  # noqa: BLE001 - 遥测失败降级 None
        pass
    try:
        import resource
        # Linux ru_maxrss 单位 KB；macOS 为字节。容器内恒走 /proc，此处仅兜底。
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
    except Exception:  # noqa: BLE001
        return None


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _send(self, code: int, body: str, ctype: str = "application/json; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urlparse(self.path).path
        try:
            if path == "/api" or path.startswith("/api/"):
                self._route_api_get(path)
            elif path == "/legacy" or path.startswith("/legacy/"):
                # 旧面板逃生通道：原 PAGE 字符串已外置 static/legacy/index.html
                f = _static_file("legacy/index.html")
                if f is None:
                    self._send(404, json.dumps({"error": "legacy 页面缺失"}))
                else:
                    self._send_file(f)
            else:
                self._serve_static(path)
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}))

    def _route_api_get(self, path: str):
        """GET /api/* 路由表（0.9.2 批次 6：表外置 web/routes.py，行为不变）。"""
        handler = _WEB_GET_ROUTES.get(path)
        if handler is None:
            self._send(404, json.dumps({"error": "not found"}))
            return
        getattr(self, handler)()

    def _auction_status(self):
        """GET /api/auction/status：回填状态 + 日级守卫（0.9.2 批次 6 从内联提取）。"""
        self._send(200, json.dumps({"backfill": _auction_backfill_state,
                                    "fired": _auction_fired}, ensure_ascii=False))

    def _auction_daily(self):
        """GET /api/auction/daily：打板链路日检记录（0.9.2 批次 7 可观测性）。"""
        from storage.records import recent as _records_recent
        try:
            limit = int(self.path.split("limit=")[-1].split("&")[0]) \
                if "limit=" in self.path else 30
        except (TypeError, ValueError):
            limit = 30
        limit = max(1, min(limit, 100))
        self._send(200, json.dumps({"records": _records_recent(limit),
                                    "count": limit}, ensure_ascii=False))

    def _serve_static(self, path: str):
        """GET 静态服务：精确命中 static 文件 → 直出（带缓存策略）；
        未命中的非 API 路径 → SPA 回退 index.html（前端路由接管深链刷新）。"""
        f = _static_file(path)
        if f is not None:
            self._send_file(f, cache=f.suffix.lower() in _CACHEABLE_EXT)
            return
        self._send(200, _ui_index(), "text/html; charset=utf-8")

    def _send_file(self, f: Path, cache: bool = False):
        try:
            data = f.read_bytes()
        except OSError:
            self._send(404, json.dumps({"error": "not found"}))
            return
        self.send_response(200)
        self.send_header("Content-Type",
                         _MIME.get(f.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control",
                         "public, max-age=31536000, immutable" if cache else "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            handler = _WEB_POST_ROUTES.get(path)
            if handler is None:
                self._send(404, json.dumps({"error": "not found"}))
                return
            getattr(self, handler)()
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}))

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}

    def _mcp(self):
        """只读 MCP JSON-RPC 端点：复用 stockdb_mcp_server.dispatch（与 stdio 同协议）。

        单请求单响应：请求 dict → JSON-RPC 响应体；通知（无 id）返回 202 空体。
        Accept 含 text/event-stream 时切换 SSE 流式响应（向后兼容：非流式客户端
        行为完全不变）：先发进度通知帧（pybao_tools 线程级 hook），再发结果帧；
        通知（无 id）不写结束帧直接返回。
        0.9.3：?group=<组名> 限定 tools/list 返回该业务域工具集（MCP Gateway）；
        不传 = 全量（向后兼容）。
        """
        group = (parse_qs(urlparse(self.path).query).get("group") or [None])[0]
        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept or pybao_tools is None:
            # ===== 非流式（或 pybao_tools 缺失的流式退化）：现有 JSON 返回路径，行为不变 =====
            if "text/event-stream" in accept:
                # pybao_tools 加载失败：流式退化为 JSON 响应并在 stderr 提示
                print("webui: 未加载 mcp.pybao_tools，/mcp SSE 流式退化为 JSON 响应",
                      file=sys.stderr)
            if mcp_dispatch is None:
                self._send(500, json.dumps({"error": "MCP 模块不可用"}))
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length > 0 else b""
            if not raw.strip():
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error: 空请求体"},
                }))
                return
            try:
                msg = json.loads(raw.decode("utf-8"))
                if not isinstance(msg, dict):
                    raise ValueError("请求体必须是 JSON 对象")
            except Exception as exc:  # noqa: BLE001 - 解析失败返回 JSON-RPC parse error
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": f"Parse error: {exc}"},
                }))
                return
            start = time.time()
            try:
                response = mcp_dispatch(msg, group=group)
            except Exception as exc:  # noqa: BLE001 - 分发异常转为 JSON-RPC internal error
                body = json.dumps({
                    "jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                })
                capture_mcp_call({
                    "tool": _mcp_tool_name(msg), "ok": False, "is_error": True,
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "bytes": len(body.encode("utf-8")),
                })
                self._send(200, body)
                return
            if response is None:
                # 通知：无 JSON-RPC 响应，202 空体
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            body = json.dumps(response, ensure_ascii=False)
            is_err = bool(response.get("error")
                          or (response.get("result") or {}).get("isError"))
            capture_mcp_call({
                "tool": _mcp_tool_name(msg), "ok": not is_err, "is_error": is_err,
                "elapsed_ms": int((time.time() - start) * 1000),
                "bytes": len(body.encode("utf-8")),
            })
            self._send(200, body)
            return

        # ===== SSE 流式分支（Accept: text/event-stream 且 pybao_tools 可用）=====
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def _sse_frame(data: dict) -> None:
            """写一个 SSE 帧：event: message + data: <json>（双换行结尾、utf-8、flush）。

            客户端断连等写失败静默忽略（SSE 无重发语义）。
            """
            try:
                frame = ("event: message\ndata: "
                         + json.dumps(data, ensure_ascii=False) + "\n\n")
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
            except Exception:  # noqa: BLE001 - 断连等写失败静默忽略
                pass

        if mcp_dispatch is None:
            _sse_frame({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32603, "message": "MCP 模块不可用"}})
            return
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw.strip():
            _sse_frame({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "Parse error: 空请求体"}})
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
            if not isinstance(msg, dict):
                raise ValueError("请求体必须是 JSON 对象")
        except Exception as exc:  # noqa: BLE001 - 解析失败返回 JSON-RPC parse error
            _sse_frame({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": f"Parse error: {exc}"}})
            return
        # 进度 token：优先 params._meta.progressToken / params.progressToken，缺省用请求 id
        params = msg.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        meta = params.get("_meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        token = (meta.get("progressToken") or params.get("progressToken")
                 or msg.get("id"))
        try:
            pybao_tools.set_progress_hook(
                lambda stage, detail=None: _sse_frame({
                    "jsonrpc": "2.0",
                    "method": "notifications/progress",
                    "params": {
                        "progressToken": token,
                        "message": stage + (": " + detail if detail else ""),
                    },
                })
            )
            start = time.time()
            try:
                response = mcp_dispatch(msg, group=group)
            except Exception as exc:  # noqa: BLE001 - 分发异常转为 JSON-RPC internal error
                err = {
                    "jsonrpc": "2.0", "id": msg.get("id"),
                    "error": {"code": -32603, "message": f"Internal error: {exc}"},
                }
                _sse_frame(err)
                capture_mcp_call({
                    "tool": _mcp_tool_name(msg), "ok": False, "is_error": True,
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "bytes": len(json.dumps(err, ensure_ascii=False).encode("utf-8")),
                })
                return
            if response is not None:
                # 响应 dict → 写事件帧；通知（None）不写结束帧直接返回
                _sse_frame(response)
                is_err = bool(response.get("error")
                              or (response.get("result") or {}).get("isError"))
                capture_mcp_call({
                    "tool": _mcp_tool_name(msg), "ok": not is_err, "is_error": is_err,
                    "elapsed_ms": int((time.time() - start) * 1000),
                    "bytes": len(json.dumps(response, ensure_ascii=False)
                                 .encode("utf-8")),
                })
        finally:
            pybao_tools.clear_progress_hook()

    def _status(self):
        state = container_state()
        src = ""
        cfg = DATA_DIR / "sync_url.txt"
        if cfg.exists():
            lines = [ln for ln in cfg.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
            src = lines[0] if lines else ""
        self._send(200, json.dumps({
            "container": state,          # {ok, status, note, image, started}
            "source": src,
            "sync_running": _sync_state["running"],
            "sync_phase": _sync_state.get("phase", "idle"),
            "sync_started": _sync_state.get("last_start"),
            "exit_code": _sync_state["exit_code"],
            "data_latest": data_latest_date(),
            "code_stats": code_stats(),          # {stock, etf, other, latency_ms}
            "coverage": data_coverage(),         # {earliest, latest} 或 null
            "sync_cap": sync_capability(),       # {ok, checks:{updater,source,writable,retry_pending}}
            "mirror": mirror_latest_date(),
            "webui": {"version": WEBUI_VERSION, "started": _webui_started,
                      "heartbeat": _scheduler_heartbeat},
            "data_dir": str(DATA_DIR),
            "last_sync": last_sync_summary(),
            "schedule": load_schedule(),
            "calendar": {"through": XSHG_HOLIDAYS_THROUGH,
                         "days": sum(len(v) for v in XSHG_HOLIDAYS.values())},
            "disk": disk_usage(),
            "scheduler_alive": _scheduler_alive,
            "trading_today": is_trading_day(),   # 定时是否会在今天触发（严格交易日）
        }, ensure_ascii=False))

    def _history(self):
        self._send(200, json.dumps({"history": load_history()}, ensure_ascii=False))

    def _schedule(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("action", [""])[0] == "save":
            enabled = q.get("enabled", ["false"])[0].lower() == "true"
            trading_only = q.get("trading_only", ["true"])[0].lower() != "false"
            # 兼容单 time；parse_qs 默认丢弃空值（times= 会得到空列表），
            # 此时若不显式 400，会静默兜底到 15:30 —— 前端删光时间点保存会悄悄变回默认值
            raw_times = q.get("times") or q.get("time") or []
            if not raw_times:
                self._send(400, json.dumps({"msg": "至少需要一个执行时间点（HH:MM）"}))
                return
            times = [t for part in raw_times for t in str(part).split(",")]
            try:
                cfg = save_schedule(enabled, times, trading_only)
            except RuntimeError as exc:
                self._send(400, json.dumps({"msg": str(exc)}))
                return
            msg = "已保存：每日 " + "、".join(cfg["times"]) + " 自动热更新" if cfg["enabled"] else "已关闭定时"
            self._send(200, json.dumps({"msg": msg, "schedule": cfg}))
            return
        self._send(200, json.dumps({"schedule": load_schedule()}))

    def _health(self):
        self._send(200, json.dumps(health_status(), ensure_ascii=False))

    def _data_write(self):
        """mydb 私有存储写入：{table, key, value} 单条 或 {table, items:[[k,v],...]} 批量。"""
        body = self._read_json()
        table = body.get("table", "")
        try:
            if "items" in body:
                items = [(str(k), v) for k, v in body["items"]]
                result = mydb_write(table, items, batch=True)
            else:
                key = body.get("key", "")
                if not key:
                    self._send(400, json.dumps({"error": "单条写入需提供 key"}))
                    return
                result = mydb_write(table, [(str(key), body.get("value"))], batch=False)
            self._send(200, json.dumps({"msg": f"已写入 {result['written']} 条", **result},
                                       ensure_ascii=False))
        except ImportError as exc:
            self._send(501, json.dumps({"error": f"pybao 写库不可用：{exc}"}, ensure_ascii=False))
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        except Exception as exc:
            self._send(500, json.dumps({"error": f"写入失败: {exc}"}, ensure_ascii=False))

    def _data_tables(self):
        try:
            self._send(200, json.dumps({"tables": mydb_tables()}, ensure_ascii=False))
        except ImportError as exc:
            self._send(501, json.dumps({"error": f"pybao 写库不可用：{exc}"}, ensure_ascii=False))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))

    def _data_read(self):
        q = parse_qs(urlparse(self.path).query)
        table = q.get("table", [""])[0]
        key = q.get("key", [""])[0]
        try:
            self._send(200, json.dumps(mydb_read(table, key), ensure_ascii=False))
        except ImportError as exc:
            self._send(501, json.dumps({"error": f"pybao 写库不可用：{exc}"}, ensure_ascii=False))
        except ValueError as exc:
            self._send(400, json.dumps({"error": str(exc)}, ensure_ascii=False))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))

    def _hk_sync(self):
        """港股同步：POST {codes:["00700",...], years:2} 拉取日K写入 hk日k: 表。"""
        body = self._read_json()
        codes = body.get("codes") or []
        years = body.get("years", 2)
        if not codes:
            self._send(400, json.dumps({"error": "缺少 codes（如 ['00700','00941']）"}))
            return
        try:
            result = hk_sync(codes, years=years)
            self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))

    def _log(self):
        n = int(parse_qs(urlparse(self.path).query).get("n", ["100"])[0])
        self._send(200, json.dumps({"log": tail_log(n)}, ensure_ascii=False))

    def _query(self):
        t = parse_qs(urlparse(self.path).query).get("t", [""])[0]
        if not t:
            self._send(400, "missing t")
            return
        try:
            self._send(200, stockdb_get(t), "application/json; charset=utf-8")
        except Exception as exc:
            self._send(502, f"stockdb 查询失败: {exc}")

    def _sync(self):
        if _sync_state["running"]:
            self._send(200, json.dumps({"msg": "同步已在运行中，请等待完成"}))
            return
        # 锁探测：running 标志可能与锁短暂不一致，且可区分「定时任务占用」与「空闲」
        if not _sync_lock.acquire(blocking=False):
            self._send(200, json.dumps({"msg": "同步引擎正在运行中（可能为定时任务），请稍候再试"}))
            return
        _sync_lock.release()
        body = self._read_json()
        hot = bool(body.get("hot", True))  # 默认热更新；前端可传 hot=false 走严格模式
        threading.Thread(target=run_sync, kwargs={"hot": hot, "trigger": "manual"}, daemon=True).start()
        mode = "热更新" if hot else "严格模式(停服)"
        self._send(200, json.dumps({"msg": f"已启动{mode}同步（手动），日志将实时刷新"}))

    def _container_logs(self):
        """stockdb 日志尾部（系统页查看用，读 /data/log.txt）。"""
        tail = int(parse_qs(urlparse(self.path).query).get("tail", ["150"])[0])
        try:
            text = container_logs(tail)
            self._send(200, json.dumps({"log": text}, ensure_ascii=False))
        except Exception as exc:
            self._send(200, json.dumps({"log": "", "error": f"读取失败: {exc}"}, ensure_ascii=False))

    def _container_restart(self):
        """重启 stockdb 进程（系统页按钮，前端已二次确认）。"""
        if _sync_state["running"]:
            self._send(200, json.dumps({"msg": "同步进行中，请勿重启 stockdb"}))
            return
        try:
            container_restart()
            self._send(200, json.dumps({"msg": "已发送重启，进程状态将自动刷新"}))
        except Exception as exc:
            self._send(200, json.dumps({"msg": f"重启失败: {exc}"}))

    def _auction_run(self):
        """POST /api/auction/run {"task":"collect"|"close"}：手动触发打板采集/收口。

        同步执行对应任务函数并返回其结果 dict（任务函数内部单块 try/except 降级，
        不会抛异常；模块未就绪 → {ok:False}）；非法 task → 400。手动触发不影响
        日级防重触发守卫（守卫只约束调度线程，手动重跑用于补采/重试）。
        """
        body = self._read_json()
        task = str(body.get("task") or "").strip()
        if task == "collect":
            self._send(200, json.dumps(auction_run_collect(), ensure_ascii=False))
        elif task == "close":
            self._send(200, json.dumps(auction_run_close(), ensure_ascii=False))
        elif task == "backfill":
            days = int(body.get("days") or 60)
            self._send(200, json.dumps(auction_run_backfill_async(max(1, min(days, 500))),
                                       ensure_ascii=False))
        else:
            self._send(400, json.dumps(
                {"error": f"非法 task {task!r}；合法值：collect / close / backfill"},
                ensure_ascii=False))


    # ==================== 运营支撑 API（Phase 4.5：通知中心 / MCP 观测 / 版本检查） ====================
    # 隐私：告警/MCP 记录不含 apikey；版本接口只回显版本号与 release 链接。
    def _alerts(self):
        """GET /api/alerts?limit=N：告警列表（最新在前）。"""
        q = parse_qs(urlparse(self.path).query)
        try:
            limit = int(q.get("limit", ["100"])[0])
        except (TypeError, ValueError):
            limit = 100
        self._send(200, json.dumps({"alerts": _get_alerts().list(limit)},
                                   ensure_ascii=False))

    def _alerts_summary(self):
        """GET /api/alerts/summary：告警条数（顶栏红点徽标数据源）。"""
        self._send(200, json.dumps({"count": _get_alerts().count()},
                                   ensure_ascii=False))

    def _alerts_clear(self):
        """POST /api/alerts/clear：清空全部告警（写入 '[]' 保持文件存在）。"""
        _get_alerts().clear()
        self._send(200, json.dumps({"msg": "已清空全部告警", "count": 0},
                                   ensure_ascii=False))

    def _mcp_stats(self):
        """GET /api/mcp/stats：MCP 调用统计（总调用/成功率/平均耗时/p95/按工具）。"""
        self._send(200, json.dumps(mcp_stats(), ensure_ascii=False))

    def _mcp_calls(self):
        """GET /api/mcp/calls?limit=N：最近 MCP 调用（最新在前）。"""
        q = parse_qs(urlparse(self.path).query)
        try:
            limit = int(q.get("limit", ["50"])[0])
        except (TypeError, ValueError):
            limit = 50
        self._send(200, json.dumps({"calls": list_mcp_calls(limit)},
                                   ensure_ascii=False))

    def _diag(self):
        """GET /api/diag：一键诊断 + 环境信息（只读聚合；单块失败只降级该块，整体 200）。

        五项检查：上游 GitHub / stockdb 服务 / pybao 模块 / 磁盘 / 交易日历。
        每项 {name,label,ok,note}；env 块含 python/架构/镜像 tag/启动时间/数据最新日。
        诊断是"人点一下"的体检入口，允许真实网络探测（上游 TTL 缓存）。
        """
        import importlib.util as _ilu
        import platform as _platform

        upstream = None
        try:
            upstream = fetch_upstream_release()
        except Exception:  # noqa: BLE001 - 上游探针自身已降级，双保险
            upstream = None
        upstream_ok = bool(upstream and upstream.get("tag_name"))

        cs = None
        try:
            cs = container_state(force=True)
        except Exception:  # noqa: BLE001
            cs = None
        stockdb_ok = bool(cs and cs.get("ok"))

        pybao_ok = all(_ilu.find_spec(m) is not None
                       for m in ("stockdb", "zb_core", "zhibiao"))

        disk_ok, disk_note = True, ""
        try:
            disk_note = json.dumps(disk_usage(), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001
            disk_ok, disk_note = False, str(exc)

        checks = [
            {"name": "upstream_github", "label": "上游 GitHub", "ok": upstream_ok,
             "note": (f"最新 release：{upstream['tag_name']}" if upstream_ok
                      else "不可达（网络受限时降级提示，不影响本机数据）")},
            {"name": "stockdb_service", "label": "stockdb 服务", "ok": stockdb_ok,
             "note": ((f"{cs.get('status')}：{cs.get('note', '')}；" if cs else "状态获取失败；")
                      + (f"上游闸口：熔断开（{_stockdb_breaker['fails']} 次失败，降级中）"
                         if _stockdb_breaker_open() else "上游闸口：正常"))},
            {"name": "pybao", "label": "pybao 计算模块", "ok": pybao_ok,
             "note": ("stockdb/zb_core/zhibiao 可导入" if pybao_ok
                      else "存在模块缺失（影响指标/板块/私有存储）")},
            {"name": "disk", "label": "磁盘", "ok": disk_ok, "note": disk_note},
            {"name": "calendar", "label": "交易日历", "ok": True,
             "note": f"覆盖至 {XSHG_HOLIDAYS_THROUGH}；今日{'是' if is_trading_day() else '非'}交易日"},
        ]
        env = {
            "python": sys.version.split()[0],
            "arch": _platform.machine(),
            "webui_version": WEBUI_VERSION,
            "ui_mode": WEBUI_UI,
            "image_tag": os.environ.get("IMAGE_TAG") or os.environ.get("STOCKDB_VERSION"),
            "started": datetime.fromtimestamp(_webui_started).strftime("%Y-%m-%d %H:%M:%S"),
            "uptime_seconds": int(time.time() - _webui_started),
            "data_dir": str(DATA_DIR),
            "data_latest": data_latest_date(),
            "rss_mb": _process_rss_mb(),
        }
        self._send(200, json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "env": env,
            "checks": checks,
            "all_ok": all(c["ok"] for c in checks),
        }, ensure_ascii=False))

    def _version(self):
        """GET /api/version：webui 版本 / 镜像引擎 tag / 上游最新 release / stale 提示 /
        ui_mode（spa|legacy，当前前端壳）。

        镜像 tag 取环境变量 IMAGE_TAG（或 STOCKDB_VERSION，Dockerfile 构建时注入，
        缺省 None → 前端显示 '—'）；stale 用版本三元组比较上游 tag 与当前版本
        （镜像 tag 未知时回退比 webui 版本）。
        """
        self._send(200, json.dumps(version_payload(), ensure_ascii=False))

    def _overview(self):
        """GET /api/overview：总览看板聚合（健康/告警/MCP 统计/版本）。

        全部复用现有只读函数，一次请求替代前端 4 次轮询；单个子块异常只降级该块
        （None/[]），整体始终 200。
        """
        def _safe(fn, default):
            try:
                return fn()
            except Exception:  # noqa: BLE001 - 总览聚合单块降级
                return default

        alerts = _safe(_get_alerts, None)
        self._send(200, json.dumps({
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "health": _safe(health_status, None),
            "alerts": {
                "count": alerts.count() if alerts is not None else 0,
                "recent": alerts.list(8) if alerts is not None else [],
            },
            "mcp": _safe(mcp_stats, None),
            "version": _safe(version_payload, None),
        }, ensure_ascii=False))



def _wire_auction_tasks() -> None:
    """组合根装配（0.9.2 批次 4）：接口层/探针/日历能力绑定到服务层注入点。

    在 main() 调用（模块加载完成后），避免前向引用；测试按需直接设置
    auction_tasks 的注入点（见 test_ops._AuctionBackfillTests）。
    """
    global _auction_query_snapshot
    try:
        from mcp.stockdb_mcp_server import query_point_snapshot as _auction_query_snapshot
    except Exception:  # noqa: BLE001 - MCP 缺失时打板用例整体降级
        _auction_query_snapshot = None
    _auction_tasks.query_snapshot = _auction_query_snapshot
    _auction_tasks.data_latest = data_latest_date
    _auction_tasks.is_fq_event = (pybao_tools.is_fq_event_date
                                  if pybao_tools is not None else None)
    _auction_tasks.is_trading_day = is_trading_day
    # 0.9.5（M5）：研究成果仓储注入（SqliteResearchStore 主线 / mydb 回滚，
    # RESEARCH_STORE 环境变量切换；应用层只依赖 ResearchStore 接口）
    from storage.research_factory import get_research_store as _get_research_store
    _auction_tasks.research_store = _get_research_store()


def main():
    _wire_auction_tasks()  # 组合根：服务层依赖注入（0.9.2 批次 4）
    print(f"webui listening on 0.0.0.0:{LISTEN_PORT}", file=sys.stderr)
    print(f"stockdb: {STOCKDB_HOST}:{STOCKDB_PORT}（同容器进程）| data: {DATA_DIR}", file=sys.stderr)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=ops_watchdog_loop, daemon=True).start()     # 运营支撑看门狗（告警生产接线）
    threading.Thread(target=auction_scheduler_loop, daemon=True).start()  # 打板竞价调度（2s 轮询，独立线程）
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
