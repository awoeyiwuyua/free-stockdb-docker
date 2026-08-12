#!/usr/bin/env python3
"""free-stockdb webui — 行情查询 + 同步管理（纯 Python 标准库，零第三方依赖）

功能：
  - 行情查询：代理 stockdb 7899 HTTP API（日K/分钟K/复权/股票代码）
  - 同步管理：挂载 docker socket + /data 卷，网页一键完成
    「停 stockdb 容器 → 容器内运行数据更新（增量同步）→ 重启 stockdb」
  - 日志查看：同步过程实时写入 /data/sync.log，页面轮询展示

安全边界：
  - docker 操控仅限固定的 stockdb 容器（stop/start/inspect 白名单），
    不开放任意命令；写操作（同步/定时/自选）面向内网信任环境，无令牌鉴权。
  - 只读查询不鉴权；局域网部署即可，如需公网暴露请自行加反向代理鉴权。
  - 同步串行化（互斥锁），防止并发点击
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

STOCKDB_HOST = os.environ.get("STOCKDB_HOST", "127.0.0.1")
STOCKDB_PORT = int(os.environ.get("STOCKDB_PORT", "7899"))
STOCKDB_CONTAINER = os.environ.get("STOCKDB_CONTAINER", "stockdb")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
RESEARCH_DB_PATH = Path(os.environ.get("RESEARCH_DB_PATH", str(DATA_DIR / "market_research.sqlite3")))
SYNC_LOG = DATA_DIR / "sync.log"
LISTEN_PORT = int(os.environ.get("WEBUI_PORT", "8080"))

# 同步线程状态
_sync_lock = threading.Lock()
_sync_state = {"running": False, "exit_code": None, "last_start": None, "last_end": None,
               "phase": "idle"}  # phase: idle/stopping/syncing/verifying/restarting/done
_last_sync_stdout: str = ""          # 最近一次同步的 stdout（供解析下载/删除数）
_last_verify_result: str | None = None  # 最近一次完整性验证结果（pass/fail/跳过）
_scheduler_alive = False             # 定时线程心跳（每次循环更新时间戳）
_scheduler_heartbeat = 0.0           # 定时线程最近一次心跳时间戳（unix）
_webui_started = time.time()         # webui 进程启动时间戳
WEBUI_VERSION = "0.3.0"

HISTORY_FILE = DATA_DIR / "sync_history.json"
SCHEDULE_FILE = DATA_DIR / "sync_schedule.json"
WATCHLIST_FILE = DATA_DIR / "watchlist.json"
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


def data_latest_date() -> str | None:
    """全市场行情最新交易日（8位）：取平安银行 000001 日K 最大日期。

    000001 每交易日都有行情，是可靠的"数据同步到哪天"探针；数据源本身
    不含指数表，这里不代表上证指数点位（概览页大盘指数另行处理）。
    近 3 月前缀通配，跨年安全。
    """
    import urllib.request, urllib.parse
    from datetime import datetime as dt, timedelta
    today = dt.now()
    start = (today - timedelta(days=95)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    dates = []
    for prefix in _month_prefixes(start, end):
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'日k:000001:{prefix}*')}"
            with urllib.request.urlopen(url, timeout=10) as resp:
                rows = json.loads(resp.read().decode("utf-8", "replace"))
            for row in rows if isinstance(rows, list) else []:
                if isinstance(row, dict) and row.get("date"):
                    dates.append(str(row["date"]))
        except Exception:
            continue
    return max(dates) if dates else None


def _classify_code(code: str) -> str:
    """按代码段归类：etf / stock / other。

    ETF：沪市 51x/52x/56x/58x（510-518 宽基、520-529 新宽基、560-563 行业、588/589 科创），
          深市 159 开头。
    股票：0/3/6 开头（深主板 00x、创业板 300/301、沪主板 60x、科创板 688/689），
          4/8 开头与 92x（北交所 43x/83x/87x/88x/920x）。
    其他：LOF(16x/50x)、REITs(18x)、B股(200/900) 等场内非股票非 ETF 品种。
    """
    c = str(code)
    if c[:2] in ("51", "52", "56", "58") or c[:3] == "159":
        return "etf"
    if c[:2] == "92" or c[:3] == "430" or c[0] in ("0", "3", "4", "6", "8"):
        return "stock"
    return "other"


# ==================== 市场研究：基础因子（纯计算，不复权日K） ====================
# 因子定义（与 UI 说明一致）：
#   1. 20 日动量   mom20 = close[-1] / close[-21] - 1          （需 ≥21 根）
#   2. 20 日波动率 vol20 = std(最近 20 个日收益率) * sqrt(250) （需 ≥21 根）
#   3. 5/20 日量比 vr520 = mean(vol[-5:]) / mean(vol[-20:])   （需 ≥20 根，均量 > 0）
#   4. 60 日回撤   dd60  = close[-1] / max(high[-60:]) - 1     （需 ≥60 根）
# 使用不复权日K；数据不足时因子为 None（不参与排名）。


def _factor_mom20(bars: list[dict]) -> float | None:
    if len(bars) < 21:
        return None
    try:
        c0 = float(bars[-1]["close"])
        c21 = float(bars[-21]["close"])
        if c21 <= 0:
            return None
        return c0 / c21 - 1.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _factor_vol20(bars: list[dict]) -> float | None:
    if len(bars) < 21:
        return None
    try:
        closes = [float(b["close"]) for b in bars[-21:]]
        rets = []
        for i in range(1, len(closes)):
            if closes[i - 1] <= 0:
                return None
            rets.append(closes[i] / closes[i - 1] - 1.0)
        if len(rets) < 20:
            return None
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        return (var ** 0.5) * (250.0 ** 0.5)
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _factor_vr520(bars: list[dict]) -> float | None:
    if len(bars) < 20:
        return None
    try:
        vols = [float(b.get("volume") or 0) for b in bars[-20:]]
        v5 = sum(vols[-5:]) / 5.0
        v20 = sum(vols) / 20.0
        if v20 <= 0:
            return None  # 零成交量防御
        return v5 / v20
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def _factor_dd60(bars: list[dict]) -> float | None:
    if len(bars) < 60:
        return None
    try:
        c0 = float(bars[-1]["close"])
        hi = max(float(b["high"]) for b in bars[-60:])
        if hi <= 0:
            return None
        return c0 / hi - 1.0
    except (KeyError, TypeError, ValueError, ZeroDivisionError):
        return None


def compute_factors(bars: list[dict]) -> dict:
    """单只标的全套基础因子（不复权日K）。返回 {mom20, vol20, vr520, dd60}。"""
    return {"mom20": _factor_mom20(bars), "vol20": _factor_vol20(bars),
            "vr520": _factor_vr520(bars), "dd60": _factor_dd60(bars)}


def percentile_rank(value: float | None, ordered_values: list[float], higher_is_better: bool = True) -> float | None:
    """value 在 ordered_values 中的百分位排名（0~100，含两端）。

    按值排序后取位次 / 总数 * 100；higher_is_better=False（如波动率/回撤）
    时按低者高排名（位次取反）。value=None 或列表为空 → None。
    """
    if value is None or not ordered_values:
        return None
    try:
        vals = [float(v) for v in ordered_values if v is not None]
        if not vals:
            return None
        n = len(vals)
        if higher_is_better:
            rank = sum(1 for v in vals if v <= value)
        else:
            rank = sum(1 for v in vals if v >= value)
        return round(rank / n * 100.0, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def add_percentiles(stock: dict, field: str, ordered: list[float], higher_better: bool) -> None:
    """给单股结果附加 {field}_pct 百分位。"""
    stock[f"{field}_pct"] = percentile_rank(stock.get(field), ordered, higher_better)


def ma(values: list[float], n: int) -> float | None:
    if len(values) < n:
        return None
    s = values[-n:]
    if any(v is None for v in s):
        return None
    try:
        return sum(s) / n
    except (TypeError, ValueError, ZeroDivisionError):
        return None


# ==================== 市场研究：市场状态聚合 ====================
def _bar_date(b: dict) -> int:
    try:
        return int(b.get("date") or 0)
    except (TypeError, ValueError):
        return 0


def _safe_float(value) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _relative_to_average(current: float | None, history: list[float], n: int) -> float | None:
    """当前值相对前 n 个有效观测均值的变化百分比：(current / mean(prev_n) - 1) × 100。"""
    if current is None:
        return None
    base = _mean([float(v) for v in history[-n:] if v is not None])
    if not base:
        return None
    return round((float(current) / base - 1.0) * 100, 1)


def market_return_distribution(stock_results: list[dict], target_date: str | None = None) -> dict:
    """当日个股涨跌幅分布；阈值使用实际 pct_chg 百分数，不把 ≥9.5% 猜成涨停。"""
    values: list[float] = []
    for row in stock_results:
        if target_date is not None and str(row.get("date") or "") != str(target_date):
            continue
        pct = _safe_float(row.get("pct_chg"))
        if pct is not None:
            values.append(pct)
    buckets = [
        ("ge5", "≥ +5%", lambda x: x >= 5),
        ("up2_5", "+2% ~ +5%", lambda x: 2 <= x < 5),
        ("up0_2", "0 ~ +2%", lambda x: 0 < x < 2),
        ("flat", "平盘", lambda x: x == 0),
        ("down0_2", "0 ~ -2%", lambda x: -2 < x < 0),
        ("down2_5", "-2% ~ -5%", lambda x: -5 < x <= -2),
        ("le_neg5", "≤ -5%", lambda x: x <= -5),
    ]
    total = len(values)
    rows = []
    for key, label, match in buckets:
        count = sum(1 for value in values if match(value))
        rows.append({"key": key, "label": label, "count": count,
                     "ratio": round(count / total, 4) if total else 0.0})
    return {
        "total": total,
        "buckets": rows,
        "large_up": sum(1 for x in values if x >= 9.5),
        "large_down": sum(1 for x in values if x <= -9.5),
    }


def sector_strength(stock_results: list[dict], target_date: str | None = None) -> dict:
    """按申万一级行业聚合等权涨跌中位数、上涨比例与成交额变化。"""
    grouped: dict[str, list[dict]] = {}
    unmapped = 0
    for row in stock_results:
        if target_date is not None and str(row.get("date") or "") != str(target_date):
            continue
        if _safe_float(row.get("pct_chg")) is None:
            continue
        industry = str(row.get("industry") or "").strip()
        if not industry:
            unmapped += 1
            continue
        grouped.setdefault(industry, []).append(row)
    sectors = []
    for name, rows in grouped.items():
        pcts = [_safe_float(r.get("pct_chg")) for r in rows]
        pcts = [x for x in pcts if x is not None]
        amounts = [_safe_float(r.get("amount")) or 0.0 for r in rows]
        prev_amounts = [_safe_float(r.get("prev_amount")) or 0.0 for r in rows]
        up = sum(1 for x in pcts if x > 0)
        total_amount = sum(amounts)
        prev_amount = sum(prev_amounts)
        sectors.append({
            "name": name,
            "count": len(pcts),
            "median_pct": _median(pcts),
            "up_ratio": round(up / len(pcts), 4) if pcts else None,
            "amount": round(total_amount, 1),
            "amount_change": round((total_amount / prev_amount - 1) * 100, 2) if prev_amount else None,
        })
    eligible = [s for s in sectors if s["count"] >= 3 and s["median_pct"] is not None]
    eligible.sort(key=lambda s: s["median_pct"], reverse=True)
    return {
        "available": bool(eligible),
        "mapped": sum(s["count"] for s in sectors),
        "unmapped": unmapped,
        "rows": eligible,
        "top": eligible[:5],
        "bottom": list(reversed(eligible[-5:])),
    }


def market_summary_from_stocks(stock_results: list[dict], target_date: str | None = None) -> dict:
    """从全市场扫描结果（每只股票含 bars 的衍生字段）聚合「市场状态」。

    stock_results 元素：{code, type, date, pct_chg, close, ma20, amount, prev_amount,
                         is_high20/is_low20, daily}；无当日数据的股票 date=None。
    target_date：目标交易日（8位）。只统计 date==target_date 的记录计入当日涨跌/MA20/新高新低，
    其余（无数据/停牌/旧日期）全部计入 na——避免停牌股用历史最后一天的行情冒充当日涨跌。
    温度 = 上涨家数占比40% + 站上MA20占比40% + (20日新高占比-新低占比)20%，各分项归一化 0~100。
    """
    from datetime import datetime as _dt
    up = down = flat = na = 0
    above_ma20 = 0
    high20c = low20c = 0
    pcts: list[float] = []
    total_amount = 0.0
    prev_total = 0.0
    data_date = None
    daily: dict[int, dict] = {}
    for r in stock_results:
        # 历史宽度贡献：所有有 daily 的股票都累加（近 20 日每日占比）
        for d, c in (r.get("daily") or {}).items():
            daily.setdefault(int(d), {"up": 0, "down": 0, "flat": 0, "above": 0,
                                      "amount": 0.0, "high20": 0, "low20": 0, "n": 0})
            agg = daily[int(d)]
            for k in ("up", "down", "flat", "above", "high20", "low20"):
                agg[k] += int(c.get(k) or 0)
            agg["n"] += 1
            try:
                agg["amount"] += float(c.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        # 当日统计：仅 date==target_date 计入涨跌/MA20/新高新低；其余为 na
        date = r.get("date")
        if not date:
            na += 1
            continue
        if target_date is not None and str(date) != str(target_date):
            na += 1
            continue
        if data_date is None or date > data_date:
            data_date = date
        pct = r.get("pct_chg")
        if pct is None:
            na += 1
        elif pct > 0:
            up += 1
        elif pct < 0:
            down += 1
        else:
            flat += 1
        if pct is not None:
            try:
                pcts.append(float(pct))
            except (TypeError, ValueError):
                pass
        close = r.get("close")
        ma20 = r.get("ma20")
        if close is not None and ma20 is not None:
            try:
                if float(close) > float(ma20):
                    above_ma20 += 1
            except (TypeError, ValueError):
                pass
        if r.get("is_high20"):
            high20c += 1
        if r.get("is_low20"):
            low20c += 1
        try:
            total_amount += float(r.get("amount") or 0)
            prev_total += float(r.get("prev_amount") or 0)
        except (TypeError, ValueError):
            pass
    total_data = up + down + flat
    up_ratio = up / total_data if total_data else 0.0
    ma20_ratio = above_ma20 / total_data if total_data else 0.0
    highlow_raw = (high20c - low20c) / total_data if total_data else 0.0
    highlow_score = max(-1.0, min(1.0, highlow_raw))
    temperature = round(up_ratio * 40 + ma20_ratio * 40 + ((highlow_score + 1) / 2) * 20, 1)
    # 21 日宽度历史用于计算“当日相对前 20 日”；对外图表仍只返回最近 20 日。
    width_hist = []
    for d in sorted(daily):
        agg = daily[d]
        n = agg["n"]
        width_hist.append({
            "date": str(d),
            "up_ratio": round(agg["up"] / n, 4) if n else None,
            "ma20_ratio": round(agg["above"] / n, 4) if n else None,
            "high20": agg["high20"],
            "low20": agg["low20"],
            "amount": round(agg["amount"], 1),
        })
    current_hist = width_hist[-1] if width_hist else None
    previous_hist = width_hist[:-1]
    current_amount = current_hist.get("amount") if current_hist else total_amount
    derived = {
        "advance_decline_ratio": round(up / down, 3) if down else None,
        "breadth_gap_pp": round((up_ratio - ma20_ratio) * 100, 1),
        "up_change_1d_pp": round((up_ratio - previous_hist[-1]["up_ratio"]) * 100, 1)
            if previous_hist and previous_hist[-1].get("up_ratio") is not None else None,
        "up_change_5d_pp": round((up_ratio - previous_hist[-5]["up_ratio"]) * 100, 1)
            if len(previous_hist) >= 5 and previous_hist[-5].get("up_ratio") is not None else None,
        "ma20_change_1d_pp": round((ma20_ratio - previous_hist[-1]["ma20_ratio"]) * 100, 1)
            if previous_hist and previous_hist[-1].get("ma20_ratio") is not None else None,
        "amount_vs_prev5_pct": _relative_to_average(current_amount, [x["amount"] for x in previous_hist], 5),
        "amount_vs_prev20_pct": _relative_to_average(current_amount, [x["amount"] for x in previous_hist], 20),
        "net_high20": high20c - low20c,
    }
    return {
        "schema_version": RESEARCH_SCHEMA,
        "date": str(data_date) if data_date else None,
        "up": up, "down": down, "flat": flat, "na": na, "total": total_data,
        "up_ratio": round(up_ratio, 4),
        "median_pct": _median(pcts),
        "total_amount": round(total_amount, 1),
        "amount_change": round((total_amount / prev_total - 1.0) * 100, 2) if prev_total > 0 else None,
        "ma20_above": above_ma20,
        "ma20_ratio": round(ma20_ratio, 4),
        "high20": high20c, "low20": low20c,
        "temperature": temperature,
        "temp_components": {
            "up_ratio": round(up_ratio * 40, 1),      # 上涨家数占比 40%
            "ma20_ratio": round(ma20_ratio * 40, 1),  # 站上 MA20 占比 40%
            "highlow": round(((highlow_score + 1) / 2) * 20, 1),  # 新高-新低 20%
        },
        "derived": derived,
        "distribution": market_return_distribution(stock_results, target_date),
        "methodology": MARKET_METHODOLOGY,
        "width_hist": width_hist[-20:],  # 最近 20 个交易日
        "generated_at": _dt.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def _median(vals: list[float]) -> float | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    return s[n // 2] if n % 2 else round((s[n // 2 - 1] + s[n // 2]) / 2, 4)


def stock_research_row(code: str, name: str, kind: str, bars: list[dict],
                       daily: dict | None = None) -> dict:
    """从单股日K生成研究行（含因子、最新行情、20日宽度贡献、MA20/新高新低）。

    bars 需按日期升序（kline 扫描保证）。daily 为可选近20日贡献（供市场聚合）。
    """
    factors = compute_factors(bars)
    close = float(bars[-1].get("close")) if bars and bars[-1].get("close") is not None else None
    pct = float(bars[-1].get("pct_chg")) if bars and bars[-1].get("pct_chg") is not None else None
    date = _bar_date(bars[-1]) if bars else None
    amount = float(bars[-1].get("amount") or 0) if bars else 0.0
    prev_amount = float(bars[-2].get("amount") or 0) if len(bars) > 1 else 0.0
    closes = [float(b.get("close")) for b in bars if b.get("close") is not None]
    ma20v = ma(closes, 20)
    highs = [float(b.get("high")) for b in bars if b.get("high") is not None]
    lows = [float(b.get("low")) for b in bars if b.get("low") is not None]
    # 20 日新高/新低口径：当日 high 创前 19 日最高 → 新高；当日 low 创前 19 日最低 → 新低
    hi_t = float(bars[-1]["high"]) if bars and bars[-1].get("high") is not None else None
    lo_t = float(bars[-1]["low"]) if bars and bars[-1].get("low") is not None else None
    prev_highs = highs[-20:-1] if len(highs) >= 20 else highs[:-1]
    prev_lows = lows[-20:-1] if len(lows) >= 20 else lows[:-1]
    is_high20 = bool(prev_highs and hi_t is not None and hi_t >= max(prev_highs))
    is_low20 = bool(prev_lows and lo_t is not None and lo_t <= min(prev_lows))
    return {
        "code": code, "name": name, "type": kind,
        "date": date, "close": close, "pct_chg": pct,
        "amount": round(amount, 1), "prev_amount": round(prev_amount, 1),
        "ma20": ma20v, "is_high20": is_high20, "is_low20": is_low20,
        "daily": daily or {},
        **factors,
    }


# ==================== 市场研究：SQLite + 因子缓存 + 后台构建 ====================
# 市场复盘由 SQLite 持久化；现有因子排行榜仍保留 JSON 快照。全市场计算在后台线程执行。
RESEARCH_SCHEMA = 4  # v4：市场复盘改由 WebUI SQLite 研究库统一持久化
RESEARCH_CACHE_DIR = DATA_DIR / "webui_cache"
FACTOR_SNAP = "factor_snapshot_{date}.json"
BUILD_STATUS_FILE = RESEARCH_CACHE_DIR / "build_status.json"
FACTOR_META = {  # 因子 → {标题, 方向(高者好?), 公式说明}
    "mom20": {"label": "20 日动量", "higher": True, "formula": "close[-1] / close[-21] - 1（需≥21根）"},
    "vol20": {"label": "20 日波动率", "higher": False, "formula": "std(最近20个日收益率) × √250（需≥21根）"},
    "vr520": {"label": "5/20 日量比", "higher": True, "formula": "近5日均量 / 近20日均量（需≥20根，均量>0）"},
    # dd60 取值 (-1, 0]，越接近 0 回撤越小越"好" → 值越大分位越高（higher=True）
    "dd60": {"label": "60 日回撤", "higher": True, "formula": "close[-1] / max(high[-60:]) - 1（需≥60根，越接近0回撤越小）"},
}
MARKET_METHODOLOGY = {
    "advance_decline_ratio": "上涨家数 / 下跌家数",
    "breadth_gap_pp": "(上涨家数占比 - 站上MA20占比) × 100，单位：百分点",
    "up_change_nd_pp": "当日上涨占比 - N个交易日前上涨占比，单位：百分点",
    "amount_vs_prev_n_pct": "(当日成交额 / 此前N个交易日平均成交额 - 1) × 100；基准不含当日",
    "return_distribution": "按个股实际pct_chg分为≥5%、2~5%、0~2%、平盘、0~-2%、-2~-5%、≤-5%",
    "sector_strength": "申万一级行业；涨跌取有效成分股等权中位数，上涨比例=上涨数/有效数，量能变化=当日行业成交额/前日行业成交额-1",
}

_research_build_lock = threading.Lock()  # 防重入：同一时间只允许一个构建任务
_research_build = {  # 内存构建状态（与 build_status.json 同步落盘）
    "state": "idle", "date": None, "total": 0, "processed": 0, "current_code": None,
    "started_at": None, "finished_at": None, "last_duration_sec": None,
    "last_date": None, "error": None,
}


def _atomic_write_json(path: Path, obj) -> None:
    """原子写 JSON：临时文件 + os.replace，避免读者读到半写文件。"""
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _open_research_db(path: Path | None = None):
    import sqlite3
    db_path = Path(path or RESEARCH_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_research_db(path: Path | None = None) -> None:
    """初始化 WebUI 研究库；CREATE IF NOT EXISTS 允许多线程/重启安全调用。"""
    conn = _open_research_db(path)
    try:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS market_daily (
            date TEXT PRIMARY KEY,
            schema_version INTEGER NOT NULL,
            generated_at TEXT,
            temperature REAL,
            up_count INTEGER, down_count INTEGER, flat_count INTEGER, na_count INTEGER,
            up_ratio REAL, median_pct REAL, total_amount REAL, amount_change REAL,
            ma20_above INTEGER, ma20_ratio REAL, high20 INTEGER, low20 INTEGER,
            advance_decline_ratio REAL, breadth_gap_pp REAL,
            up_change_1d_pp REAL, up_change_5d_pp REAL, ma20_change_1d_pp REAL,
            amount_vs_prev5_pct REAL, amount_vs_prev20_pct REAL, net_high20 INTEGER,
            snapshot_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS breadth_daily (
            date TEXT PRIMARY KEY,
            up_ratio REAL, ma20_ratio REAL, amount REAL, high20 INTEGER, low20 INTEGER
        );
        CREATE TABLE IF NOT EXISTS return_distribution_daily (
            date TEXT NOT NULL,
            bucket TEXT NOT NULL,
            label TEXT NOT NULL,
            count INTEGER NOT NULL,
            ratio REAL NOT NULL,
            PRIMARY KEY (date, bucket),
            FOREIGN KEY (date) REFERENCES market_daily(date) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS sector_daily (
            date TEXT NOT NULL,
            industry TEXT NOT NULL,
            stock_count INTEGER NOT NULL,
            median_pct REAL,
            up_ratio REAL,
            amount REAL,
            amount_change REAL,
            PRIMARY KEY (date, industry),
            FOREIGN KEY (date) REFERENCES market_daily(date) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_sector_industry_date ON sector_daily(industry, date DESC);
        CREATE TABLE IF NOT EXISTS methodology (
            schema_version INTEGER NOT NULL,
            metric TEXT NOT NULL,
            formula TEXT NOT NULL,
            PRIMARY KEY (schema_version, metric)
        );
        PRAGMA user_version=1;
        """)
        conn.commit()
    finally:
        conn.close()


def save_market_snapshot_db(summary: dict, path: Path | None = None) -> None:
    """一个事务写入市场日表、宽度、分布、行业和方法论。"""
    date = str(summary.get("date") or "")
    if not date:
        raise ValueError("市场快照缺少 date")
    ensure_research_db(path)
    derived = summary.get("derived") or {}
    conn = _open_research_db(path)
    try:
        with conn:
            market_values = (
                date, int(summary.get("schema_version") or RESEARCH_SCHEMA), summary.get("generated_at"),
                summary.get("temperature"), summary.get("up"), summary.get("down"), summary.get("flat"),
                summary.get("na"), summary.get("up_ratio"), summary.get("median_pct"),
                summary.get("total_amount"), summary.get("amount_change"), summary.get("ma20_above"),
                summary.get("ma20_ratio"), summary.get("high20"), summary.get("low20"),
                derived.get("advance_decline_ratio"), derived.get("breadth_gap_pp"),
                derived.get("up_change_1d_pp"), derived.get("up_change_5d_pp"),
                derived.get("ma20_change_1d_pp"), derived.get("amount_vs_prev5_pct"),
                derived.get("amount_vs_prev20_pct"), derived.get("net_high20"),
                json.dumps(summary, ensure_ascii=False, separators=(",", ":")),
            )
            conn.execute(f"INSERT OR REPLACE INTO market_daily VALUES ({','.join('?' for _ in market_values)})",
                         market_values)
            for row in summary.get("width_hist") or []:
                conn.execute("INSERT OR REPLACE INTO breadth_daily VALUES (?,?,?,?,?,?)", (
                    str(row.get("date") or ""), row.get("up_ratio"), row.get("ma20_ratio"),
                    row.get("amount"), row.get("high20"), row.get("low20")))
            conn.execute("DELETE FROM return_distribution_daily WHERE date=?", (date,))
            for row in (summary.get("distribution") or {}).get("buckets") or []:
                conn.execute("INSERT INTO return_distribution_daily VALUES (?,?,?,?,?)", (
                    date, row.get("key"), row.get("label"), int(row.get("count") or 0),
                    float(row.get("ratio") or 0)))
            conn.execute("DELETE FROM sector_daily WHERE date=?", (date,))
            sectors = summary.get("sectors") or {}
            sector_rows = sectors.get("rows") or (sectors.get("top") or []) + (sectors.get("bottom") or [])
            seen = set()
            for row in sector_rows:
                name = str(row.get("name") or "").strip()
                if not name or name in seen:
                    continue
                seen.add(name)
                conn.execute("INSERT INTO sector_daily VALUES (?,?,?,?,?,?,?)", (
                    date, name, int(row.get("count") or 0), row.get("median_pct"),
                    row.get("up_ratio"), row.get("amount"), row.get("amount_change")))
            for metric, formula in (summary.get("methodology") or {}).items():
                conn.execute("INSERT OR REPLACE INTO methodology VALUES (?,?,?)",
                             (RESEARCH_SCHEMA, str(metric), str(formula)))
    finally:
        conn.close()


def load_latest_market_snapshot_db(path: Path | None = None) -> dict | None:
    try:
        ensure_research_db(path)
        conn = _open_research_db(path)
        try:
            row = conn.execute("SELECT snapshot_json FROM market_daily ORDER BY date DESC LIMIT 1").fetchone()
        finally:
            conn.close()
        if not row:
            return None
        value = json.loads(row["snapshot_json"])
        return value if isinstance(value, dict) and value.get("schema_version") == RESEARCH_SCHEMA else None
    except Exception:
        return None


def research_db_status(path: Path | None = None) -> dict:
    db_path = Path(path or RESEARCH_DB_PATH)
    result = {"path": str(db_path), "ok": False, "latest_date": None, "size_mb": 0.0}
    try:
        ensure_research_db(db_path)
        conn = _open_research_db(db_path)
        try:
            row = conn.execute("SELECT MAX(date) AS latest FROM market_daily").fetchone()
        finally:
            conn.close()
        result.update(ok=True, latest_date=row["latest"] if row else None,
                      size_mb=round(db_path.stat().st_size / 2 ** 20, 2) if db_path.exists() else 0.0)
    except Exception as exc:
        result["error"] = str(exc)[:200]
    return result


def _write_build_status() -> None:
    try:
        RESEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(BUILD_STATUS_FILE, _research_build)
    except Exception:
        pass


def _load_build_status() -> dict:
    try:
        if BUILD_STATUS_FILE.exists():
            d = json.loads(BUILD_STATUS_FILE.read_text(encoding="utf-8"))
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return dict(_research_build)


def _latest_cache_file(prefix: str) -> Path | None:
    """返回缓存目录下最新日期的缓存文件（按文件名 YYYYMMDD 排序）。"""
    try:
        if not RESEARCH_CACHE_DIR.exists():
            return None
        files = [p for p in RESEARCH_CACHE_DIR.iterdir()
                 if p.is_file() and p.name.startswith(prefix)]
        if not files:
            return None
        return max(files, key=lambda p: p.name)
    except Exception:
        return None


def _load_snapshot(prefix: str) -> dict | None:
    """读取最新一份可用快照（按日期降序，跳过损坏/schema 不符的文件）。

    单个最新文件损坏不会导致整体不可用——回退到上一份有效缓存。
    """
    try:
        if not RESEARCH_CACHE_DIR.exists():
            return None
        files = [p for p in RESEARCH_CACHE_DIR.iterdir()
                 if p.is_file() and p.name.startswith(prefix)]
    except Exception:
        return None
    files.sort(key=lambda p: p.name, reverse=True)  # 最新在前
    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("schema_version") == RESEARCH_SCHEMA:
                return d
        except Exception:
            continue
    return None


def migrate_legacy_market_snapshot() -> bool:
    """SQLite 为空时迁移一份旧 market_snapshot JSON；保留原文件便于人工回滚。"""
    if load_latest_market_snapshot_db() is not None:
        return False
    snap = None
    try:
        files = sorted(
            (p for p in RESEARCH_CACHE_DIR.iterdir()
             if p.is_file() and p.name.startswith("market_snapshot_")),
            key=lambda p: p.name, reverse=True)
    except Exception:
        files = []
    for legacy_path in files:
        try:
            candidate = json.loads(legacy_path.read_text(encoding="utf-8"))
            version = int(candidate.get("schema_version") or 0)
            if isinstance(candidate, dict) and candidate.get("date") and 1 <= version <= RESEARCH_SCHEMA:
                snap = candidate
                break
        except Exception:
            continue
    if not snap:
        return False
    # 旧版可能没有派生指标、行业或方法论；先无损迁入，后台研究任务会按 v4 完整重算。
    snap = dict(snap)
    snap["schema_version"] = RESEARCH_SCHEMA
    snap.setdefault("derived", {})
    snap.setdefault("distribution", {"buckets": []})
    snap.setdefault("sectors", {"available": False, "rows": [], "top": [], "bottom": []})
    snap["methodology"] = {**MARKET_METHODOLOGY, **(snap.get("methodology") or {})}
    save_market_snapshot_db(snap)
    log(f"📊 已迁移旧市场快照 {snap.get('date')} → {RESEARCH_DB_PATH}")
    return True


def _fetch_bars(code: str, months: int = 4) -> list[dict]:
    """拉取单标的近 months 个月不复权日K（升序）。复用 vals 前缀通配。"""
    import urllib.request, urllib.parse
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now()
    start = (today - _td(days=months * 31)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    rows: list[dict] = []
    for prefix in _month_prefixes(start, end):
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'日k:{code}:{prefix}*')}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            rows.extend(r for r in data if isinstance(r, dict))
        except Exception:
            continue
    rows.sort(key=_bar_date)
    return rows


def _stock_name(bars: list[dict], code: str) -> str:
    for b in reversed(bars):
        n = b.get("name")
        if n:
            return str(n)
    return code


def _fetch_sw1_industry_map() -> dict[str, str]:
    """一次读取板块全集，构建 股票代码 → 申万一级行业；接口不可用时返回空映射。"""
    import urllib.request, urllib.parse
    try:
        query = urllib.parse.urlencode({"cmd": "bk.get", "x": "", "category": "1"})
        with urllib.request.urlopen(f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?{query}", timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    mapping: dict[str, str] = {}
    for raw in data if isinstance(data, list) else []:
        board = raw[1] if isinstance(raw, list) and len(raw) == 2 and isinstance(raw[1], dict) else raw
        if not isinstance(board, dict):
            continue
        category = str(board.get("category") or "")
        board_type = str(board.get("type") or "")
        if category != "申万一级" and board_type != "sw_1":
            continue
        name = str(board.get("name") or "").strip()
        if not name:
            continue
        for symbol in board.get("symbols") or []:
            code = str(symbol).strip().split(".")[0]
            if len(code) == 6 and code.isdigit():
                mapping[code] = name
    return mapping


def _run_research_build(date_target: str) -> None:
    """全市场扫描 + 因子计算 + 聚合，写入缓存。由 _start_research_build 以线程调用。"""
    t0 = time.time()
    _research_build.update(state="building", date=date_target, total=0, processed=0,
                           current_code=None, error=None, started_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    _write_build_status()
    import urllib.request, urllib.parse
    try:
        # 1. 代码列表 + 分类（排除 other：LOF/REITs/B股；含股票与 ETF）
        url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote('股票代码')}"
        with urllib.request.urlopen(url, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        codes: list[str] = []
        if isinstance(data, dict):
            for group in data.values():
                if isinstance(group, list):
                    codes.extend(str(c) for c in group)
        codes = sorted({c for c in codes if _classify_code(c) in ("stock", "etf")})
        _research_build["total"] = len(codes)
        _write_build_status()

        def scan_one(code: str):
            bars = _fetch_bars(code)
            if not bars:
                # 无当日数据/停牌/无历史：返回占位行（date=None → 市场聚合计入 na）
                return {"code": code, "name": code, "type": _classify_code(code), "date": None,
                        "pct_chg": None, "close": None, "amount": 0.0, "prev_amount": 0.0,
                        "ma20": None, "is_high20": False, "is_low20": False, "daily": {},
                        "mom20": None, "vol20": None, "vr520": None, "dd60": None}
            # 近21日宽度贡献：图表展示20日，多取1日用于“当日相对前20日均值”。
            daily = {}
            closes = [float(b["close"]) for b in bars if b.get("close") is not None]
            highs = [float(b["high"]) for b in bars if b.get("high") is not None]
            lows = [float(b["low"]) for b in bars if b.get("low") is not None]
            for i in range(max(0, len(bars) - 21), len(bars)):
                b = bars[i]
                d = _bar_date(b)
                c = float(b.get("close") or 0)
                try:
                    pct = float(b.get("pct_chg")) if b.get("pct_chg") is not None else None
                except (TypeError, ValueError):
                    pct = None
                up = 1 if (pct or 0) > 0 else 0
                down = 1 if (pct or 0) < 0 else 0
                flat = 1 if pct == 0 else 0
                above = 0
                idx = i
                # MA20 含当日（与 stock_research_row 的最新 MA20 口径一致：取当日及其前 19 根）
                if idx >= 19:
                    ma20v = sum(closes[idx - 19:idx + 1]) / 20
                    above = 1 if c > ma20v else 0
                # 20 日新高/新低：当日 high/low 创前 19 日极值（不含当日）
                hi_t = float(b.get("high")) if b.get("high") is not None else None
                lo_t = float(b.get("low")) if b.get("low") is not None else None
                prev_highs = highs[max(0, i - 19):i]
                prev_lows = lows[max(0, i - 19):i]
                is_high = bool(prev_highs and hi_t is not None and hi_t >= max(prev_highs))
                is_low = bool(prev_lows and lo_t is not None and lo_t <= min(prev_lows))
                daily[d] = {"up": up, "down": down, "flat": flat, "above": above,
                            "high20": 1 if is_high else 0,
                            "low20": 1 if is_low else 0,
                            "amount": float(b.get("amount") or 0)}
            row = stock_research_row(code, _stock_name(bars, code),
                                     _classify_code(code), bars, daily)
            return row

        results: list[dict] = []
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {}
            for c in codes:
                futures[ex.submit(scan_one, c)] = c
            done = 0
            for fut in futures:
                try:
                    row = fut.result()
                    if row:
                        results.append(row)
                except Exception:
                    pass
                done += 1
                if done % 50 == 0:
                    _research_build["processed"] = done
                    _write_build_status()
        _research_build["processed"] = len(codes)
        _write_build_status()

        # 1.5 构建结果校验：代码列表非空 + 有效行情比例达标 + 聚合日期有效，
        # 否则判失败并保留旧缓存（不写新文件，前端继续用上一份）。
        if not codes:
            raise RuntimeError("股票代码列表为空（stockdb 不可用或返回异常），保留旧缓存")
        # 有效行情 = 有当日数据（date 非空）；占位行不算有效
        valid = sum(1 for r in results if r.get("date"))
        if valid < max(10, int(len(codes) * 0.5)):
            raise RuntimeError(f"有效行情过少（{valid}/{len(codes)}），中止构建以保留旧缓存")

        # 2. 聚合市场状态（仅普通股票；只统计 date==target_date 的记录）。
        # 板块全集只读取一次，再在内存建立 code→申万一级映射，避免逐股查询。
        stock_rows = [r for r in results if r.get("type") == "stock"]
        industry_map = _fetch_sw1_industry_map()
        for row in stock_rows:
            row["industry"] = industry_map.get(str(row.get("code") or ""))
        summary = market_summary_from_stocks(stock_rows, target_date=date_target)
        if not summary.get("date"):
            raise RuntimeError("市场快照无有效数据日期，中止构建以保留旧缓存")
        summary["sectors"] = sector_strength(stock_rows, target_date=date_target)

        # 3. 全市场因子百分位（股票+ETF 各自独立排名）
        for f in FACTOR_META:
            pool_stock = [r[f] for r in results if r.get("type") == "stock"]
            pool_etf = [r[f] for r in results if r.get("type") == "etf"]
            for r in results:
                pool = pool_stock if r.get("type") == "stock" else pool_etf
                add_percentiles(r, f, pool, FACTOR_META[f]["higher"])

        # 4. 落盘：市场复盘用单个 SQLite 事务；因子 JSON 在数据库提交后再原子发布。
        RESEARCH_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        factor_payload = {
            "schema_version": RESEARCH_SCHEMA, "date": summary["date"],
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "factors": FACTOR_META,
            "codes": [{k: r[k] for k in ("code", "name", "type", "close", "pct_chg",
                                         "mom20", "mom20_pct", "vol20", "vol20_pct",
                                         "vr520", "vr520_pct", "dd60", "dd60_pct")}
                      for r in results if r.get("date")],  # 排除无行情占位行
        }
        factor_path = RESEARCH_CACHE_DIR / FACTOR_SNAP.format(date=summary["date"])
        factor_pending = factor_path.with_suffix(".pending")
        factor_pending.write_text(json.dumps(factor_payload, ensure_ascii=False), encoding="utf-8")
        try:
            save_market_snapshot_db(summary)
            os.replace(factor_pending, factor_path)
        finally:
            if factor_pending.exists():
                try:
                    factor_pending.unlink()
                except Exception:
                    pass
        # 因子排行只保留最新一份；SQLite 市场历史按日期长期保留。
        for p in RESEARCH_CACHE_DIR.iterdir():
            if p.is_file() and p.name.startswith("factor_snapshot_") and summary["date"] not in p.name:
                try:
                    p.unlink()
                except Exception:
                    pass

        _research_build.update(state="done", last_date=summary["date"], error=None,
                               last_duration_sec=round(time.time() - t0, 1),
                               finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               current_code=None)
        _write_build_status()
    except Exception as exc:
        _research_build.update(state="failed", error=str(exc)[:300],
                               finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        _write_build_status()
        log(f"📊 市场因子构建失败: {exc}")


def _start_research_build() -> bool:
    """启动一次后台构建（防重入）。返回是否启动成功。"""
    if not _research_build_lock.acquire(blocking=False):
        return False

    def _wrapper(date_target: str):
        try:
            _run_research_build(date_target)
        finally:
            _research_build_lock.release()

    try:
        date_target = data_latest_date() or ""
        threading.Thread(target=_wrapper, args=(date_target,), daemon=True).start()
        return True
    except Exception:
        _research_build_lock.release()
        return False


def invalidate_research_cache(reason: str = "数据更新") -> None:
    """数据更新后使旧缓存失效并启动重算（同步成功时调用）。"""
    log(f"📊 市场因子缓存失效（{reason}），后台重算启动")
    _start_research_build()


def research_check_loop() -> None:
    """后台守护：缓存缺失 / 落后于本地数据日期 / 无有效 schema（口径升级后旧缓存）时重建。"""
    while True:
        try:
            st = research_status()
            need = (st.get("stale")
                    or (st.get("data_date") and not st.get("cache_generated_at"))
                    or (st.get("data_date") and not st.get("market_date")))
            if need:
                _start_research_build()
        except Exception:
            pass
        time.sleep(60)


def research_status() -> dict:
    """研究功能状态：结合内存状态、因子缓存与 SQLite 市场库。"""
    st = dict(_research_build)
    st["cache_date"] = None
    st["cache_generated_at"] = None
    f = _latest_cache_file("factor_snapshot_")
    if f:
        st["cache_date"] = f.name.replace("factor_snapshot_", "").replace(".json", "")
        try:
            st["cache_generated_at"] = _load_snapshot("factor_snapshot_").get("generated_at")
        except Exception:
            pass
    db = research_db_status()
    st["market_date"] = db.get("latest_date")
    st["research_db"] = db
    st["data_date"] = data_latest_date()
    stored_dates = [d for d in (st.get("cache_date"), st.get("market_date")) if d]
    st["stale"] = bool(st.get("data_date") and stored_dates
                       and min(stored_dates) < st["data_date"])
    # 服务重启后内存态为 idle，但存在有效缓存 → 归一到 done（缓存仍可用）
    if st["state"] == "idle" and st.get("cache_date") and st.get("market_date"):
        st["state"] = "done"
        st["last_date"] = min(st["cache_date"], st["market_date"])
    return st


def research_market() -> dict:
    """市场状态快照：SQLite 为主，旧 JSON 仅作迁移期只读回退。"""
    snap = load_latest_market_snapshot_db()
    if not snap:
        snap = _load_snapshot("market_snapshot_")
    if snap:
        return snap
    return {"schema_version": RESEARCH_SCHEMA, "date": None, "error": "暂无市场快照",
            "building": _research_build["state"] == "building"}


def research_factors(factor: str = "mom20", scope: str = "stock", order: str = "desc",
                     limit: int = 50, q: str = "") -> dict:
    """因子排行榜（读缓存 + 内存过滤排序，不扫全市场）。"""
    if factor not in FACTOR_META:
        return {"error": f"未知因子 {factor}"}
    if scope not in ("stock", "etf"):
        return {"error": f"未知范围 {scope}"}
    if order not in ("desc", "asc"):
        return {"error": f"未知排序 {order}"}
    limit = max(1, min(int(limit), 200))
    snap = _load_snapshot("factor_snapshot_")
    if not snap:
        return {"error": "暂无因子数据（首次构建中或失败）", "date": None}
    rows = [r for r in snap["codes"] if r.get("type") == scope]
    if q:
        q = str(q).strip().lower()
        rows = [r for r in rows if q in r["code"].lower() or q in str(r.get("name") or "").lower()]
    meta = snap.get("factors") or FACTOR_META
    # 空值不参与排序
    rows = [r for r in rows if r.get(factor) is not None]
    rev = order == "desc"
    rows.sort(key=lambda r: r[factor], reverse=rev)
    # 排名：同值并列序号
    for i, r in enumerate(rows[:limit]):
        r = dict(r)
        r["rank"] = i + 1
        rows[i] = r
    return {"date": snap.get("date"), "factor": factor, "scope": scope, "order": order,
            "limit": limit, "total": len(rows), "meta": meta,
            "rows": rows[:limit]}


def code_stats() -> dict:
    """全市场标的数量，股票 / ETF 分开统计（其余归 other），并返回查询延迟 ms。

    延迟 = 拉取全市场代码列表耗时，供系统页「行情服务」健康卡显示。
    """
    import urllib.request, urllib.parse
    t0 = time.time()
    try:
        url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote('股票代码')}"
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
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
        return stats
    except Exception:
        return {"stock": None, "etf": None, "other": None, "latency_ms": None}


_coverage_cache: dict = {"at": 0.0, "data": None}  # 15 分钟缓存，避免 4s 轮询重复全历史扫描


def data_coverage() -> dict | None:
    """行情数据覆盖范围（最早 ~ 最新交易日），基于 000001 逐年前缀扫描。

    每 4s 轮询会反复请求 /api/status，全历史扫描较重，故缓存 15 分钟。
    返回 {"earliest": int, "latest": int} 或 None（无数据）。
    """
    now = time.time()
    if _coverage_cache["data"] is not None and now - _coverage_cache["at"] < 900:
        return _coverage_cache["data"]
    import urllib.request, urllib.parse
    from datetime import datetime as _dt
    this_year = _dt.now().year
    earliest = latest = None
    for y in range(1990, this_year + 1):
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'日k:000001:{y}*')}"
            with urllib.request.urlopen(url, timeout=8) as resp:
                rows = json.loads(resp.read().decode("utf-8", "replace"))
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


def mirror_latest_date() -> str | None:
    """镜像源（a.123128.xyz 网页）标注的最新数据日期（10 分钟缓存）。

    镜像源是 LevelDB 文件镜像（HTTP + manifest），无行情 API，但它首页明文标注
    「数据更新至:YYYY-MM-DD」。抓该日期可判断「本地落后是同步未跑 vs 镜像未发布」。
    可配置 MIRROR_PAGE_URL 覆盖（内网映射/镜像源变更时）。
    """
    if _mirror_cache["val"] is not None and time.time() - _mirror_cache["at"] < 600:
        return _mirror_cache["val"]
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
    return result


def load_watchlist() -> list[str]:
    if not WATCHLIST_FILE.exists():
        return []
    try:
        data = json.loads(WATCHLIST_FILE.read_text(encoding="utf-8"))
        return [str(c) for c in data] if isinstance(data, list) else []
    except Exception:
        return []


def save_watchlist(codes: list[str]) -> list[str]:
    cleaned = []
    for c in codes:
        c = str(c).strip()
        if c and c.isdigit() and len(c) == 6 and c not in cleaned:
            cleaned.append(c)
    WATCHLIST_FILE.write_text(json.dumps(cleaned, ensure_ascii=False, indent=1), encoding="utf-8")
    return cleaned


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


# ==================== 大盘指数快照 ====================
INDEX_CODES = {"000001": "上证指数", "399001": "深证成指", "399006": "创业板指"}


def index_snapshot() -> list[dict]:
    quotes = latest_quotes(list(INDEX_CODES.keys()))
    for q in quotes:
        q["name"] = INDEX_CODES.get(q["code"], q.get("name"))
    return quotes


# ==================== Docker Engine API（unix socket） ====================
class DockerUnixSocket(http.client.HTTPConnection):
    def __init__(self, path: str = "/var/run/docker.sock"):
        super().__init__("localhost")
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._path)
        self.sock = sock


def docker_request(method: str, path: str, timeout: int = 10) -> dict:
    """调用 Docker Engine API，返回 JSON。失败抛异常（由调用方处理）。"""
    conn = DockerUnixSocket(DOCKER_SOCKET)
    try:
        # HTTPConnection.request() 不接受 timeout 关键字参数；
        # 超时在 socket 上设置（Unix socket connect/read 均生效）
        if conn.sock is not None:
            conn.sock.settimeout(timeout)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise RuntimeError(f"docker {method} {path}: HTTP {resp.status} {resp.reason} {body[:200]}")
        return json.loads(body) if body else {}
    finally:
        conn.close()


def container_state() -> dict:
    """返回 stockdb 容器状态详情。

    {ok, status, note}：ok=False 表示 docker socket 不可用（热更新无法停/启容器）。
    image/started 供系统页展示；查询失败时保持 None。
    """
    try:
        info = docker_request("GET", f"/containers/{STOCKDB_CONTAINER}/json")
    except FileNotFoundError:
        return {"ok": False, "status": "unknown", "note": "docker socket 未挂载（/var/run/docker.sock 不可达），无法停/启容器",
                "image": None, "started": None}
    except PermissionError:
        return {"ok": False, "status": "unknown", "note": "docker socket 权限不足，无法操控容器",
                "image": None, "started": None}
    except Exception as exc:
        return {"ok": False, "status": "unknown", "note": f"docker 查询失败: {exc}",
                "image": None, "started": None}
    st = info.get("State", {}).get("Status", "unknown")
    cfg = info.get("Config", {}) or {}
    started = (info.get("State", {}) or {}).get("StartedAt") or None
    return {"ok": True, "status": st, "note": "",
            "image": cfg.get("Image") or None, "started": started}


def container_start() -> None:
    docker_request("POST", f"/containers/{STOCKDB_CONTAINER}/start")


def container_stop() -> None:
    try:
        docker_request("POST", f"/containers/{STOCKDB_CONTAINER}/stop")
    except RuntimeError as exc:
        # 容器已停止时 stop 返回 304，属正常
        if "304" not in str(exc):
            raise


def container_restart() -> None:
    """重启容器（热更新失败止损/加载新快照用）。"""
    docker_request("POST", f"/containers/{STOCKDB_CONTAINER}/restart")


def wait_stockdb_ready(timeout: float = 60.0) -> bool:
    """轮询等待 stockdb HTTP 服务就绪（重启后验证/查询前调用）。

    容器 restart 返回不代表服务已可查询，直接连可能连接拒绝导致误判；
    轮询 7899 的 /?cmd=get&t=股票代码 直到响应（含健康检查端口映射未生效前的
    短暂窗口）。超时返回 False。
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
    让运行中的服务重载 LevelDB（同步日志 "reload 1 skipped: local program is
    unavailable" 即因容器内 127.0.0.1 指向自身而非 stockdb）。webui 容器内代发
    到 STOCKDB_HOST 即可。返回成功重载的 remote 列表（如 ["0","1"]）。
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
    """读取 stockdb 容器日志尾部。

    Docker Engine logs API 默认返回多路复用帧（8 字节头 + payload），需解帧；
    JSON 日志则返回纯文本行。返回去帧后的日志文本。
    """
    raw = docker_request("GET", f"/containers/{STOCKDB_CONTAINER}/logs?stdout=1&stderr=1&tail={tail}")
    if not isinstance(raw, str):
        raise RuntimeError("docker logs 返回非文本")
    if isinstance(raw, str) and not raw:
        return ""
    data = raw.encode("utf-8", "replace")
    out = bytearray()
    i = 0
    n = len(data)
    try:
        while i + 8 <= n:
            if data[i] > 2:  # 非帧头（0/1/2），按纯文本处理
                out.extend(data[i:])
                break
            size = int.from_bytes(data[i + 4:i + 8], "big")
            i += 8
            out.extend(data[i:i + size])
            i += size
        if i < n and data[i] <= 2 and i + 8 > n:  # 尾部残帧
            out.extend(data[i:])
    except Exception:
        pass
    text = bytes(out).decode("utf-8", "replace")
    return "\n".join(line for line in text.splitlines() if line.strip())


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
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote(table)}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.read().decode("utf-8", "replace")
        def vals(table: str) -> list:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(table)}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8", "replace"))
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

        # 0. 同步前抓全市场最新交易日（作为同步后验证基准）
        before_date = None
        try:
            before_date = data_latest_date()
            if before_date:
                log(f"→ 同步前数据最新交易日：{before_date}")
        except Exception:
            pass

        # 1.（严格模式）停 stockdb；热更新模式不停
        if not hot:
            _sync_state["phase"] = "stopping"
            log("→ 停止 stockdb 容器 ...")
            try:
                if container_state().get("status") == "running":
                    container_stop()
                else:
                    log("  （stockdb 已处于停止状态）")
            except Exception as exc:
                log(f"  ⚠️ 停止失败，继续同步（风险：数据卷并发写）：{exc}")
        else:
            st = container_state()
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
                log("→ 热更新完成，验证数据完整性 ...")
                # 上游新架构（多点数据源）下增量下载进 data1/LevelDB，
                # 运行中的 stockdb 进程仍持旧快照。优先用上游 reload 命令热重载
                # （零中断）；reload 不可用（老版本/连接失败）则降级重启。
                # 无新文件（downloads=0）时数据未变，跳过加载。
                counts = parse_sync_counts(_last_sync_stdout)
                if counts.get("downloads", 0) > 0 or counts.get("downloads") is None:
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
                # 同步失败时确保服务仍在（可能中途被手动停过）
                if container_state().get("status") != "running":
                    try:
                        container_start()
                        log("  → 已尝试重新启动 stockdb")
                    except Exception as exc:
                        log(f"  ❌ 启动失败：{exc}")

        # 4.（严格模式）重启服务；热更新若中途发现服务没跑也补启
        if not hot:
            _sync_state["phase"] = "restarting"
            log("→ 启动 stockdb 容器 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")
        elif container_state().get("status") in ("exited", "not-found"):
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
            after_date = data_latest_date()
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
            # 同步成功后使市场研究缓存失效并后台重算（仅当数据确实更新）
            if _sync_state.get("exit_code") == 0 and effective:
                invalidate_research_cache(reason="同步成功")
        except Exception:
            pass
        _sync_state["running"] = False
        _sync_state["last_end"] = time.time()
        _sync_state["phase"] = "done"
        _sync_lock.release()


def log(line: str) -> None:
    SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
    with SYNC_LOG.open("a", encoding="utf-8") as fh:
        fh.write(f"{now()}  {line}\n")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def tail_log(n: int = 200) -> str:
    if not SYNC_LOG.exists():
        return "（暂无同步日志）"
    lines = SYNC_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-n:])


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


def kline_range(code: str, freq: str, start: str, end: str) -> list[dict]:
    """按区间拉取 K 线。freq: day=日K / minute=分钟K（14位时间戳起止）。"""
    import urllib.request, urllib.parse
    if freq == "day":
        table = "日k"
        s, e = int(start), int(end)
        months = _month_prefixes(start, end)
        rows: list[dict] = []
        for prefix in months:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'{table}:{code}:{prefix}*')}"
            try:
                with urllib.request.urlopen(url, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                for row in data if isinstance(data, list) else []:
                    if isinstance(row, dict):
                        rows.append(row)
            except Exception:
                continue
        rows = [r for r in rows if isinstance(r, dict)
                and s <= int(r.get("date") or 0) <= e]
        rows.sort(key=lambda r: int(r.get("date") or 0))
        return rows
    # 分钟K：区间较大，按日逐日拉取（每交易日 240 根，用 14 位时间戳按 10 分钟步进）
    rows = []
    # 简化：分钟K 只取区间内每天 09:30-15:00 的整点样本（快速可用）
    import urllib.request as _ur, urllib.parse as _up
    from datetime import datetime as _dt, timedelta as _td
    cur = _dt.strptime(start, "%Y%m%d%H%M%S")
    enddt = _dt.strptime(end, "%Y%m%d%H%M%S")
    while cur <= enddt:
        ts = cur.strftime("%Y%m%d%H%M%S")
        url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={_up.quote(f'分钟k:{code}:{ts}')}"
        try:
            with _ur.request.urlopen(url, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(data, dict):
                rows.append(data)
        except Exception:
            pass
        cur += _td(minutes=30)
    rows.sort(key=lambda r: int(r.get("date") or 0))
    return rows


def _adjust_map(code: str) -> dict[int, float]:
    """复权因子表：{日期int: cum}。cum=累计复权因子（后复权方向）。"""
    import urllib.request, urllib.parse
    url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote(f'复权:{code}:*')}"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    result: dict[int, float] = {}
    for item in data if isinstance(data, list) else []:
        if isinstance(item, list) and len(item) == 2:
            key, factors = item[0], item[1]
            try:
                d = int(str(key).split(":")[-1])
                cum = float(factors.get("cum") or 1.0)
                result[d] = cum
            except Exception:
                continue
    return result


def apply_adjust(rows: list[dict], adj_map: dict[int, float], mode: str) -> list[dict]:
    """对日K行应用复权。mode: qfq=前复权（以最新为基准）/ hfq=后复权。"""
    if not rows or not adj_map or mode == "none":
        return rows
    dates = sorted(adj_map.keys())
    latest_cum = adj_map[dates[-1]]
    result = []
    for r in rows:
        row = dict(r)
        d = int(row.get("date") or 0)
        # 找到 ≤ d 的最近复权日的 cum；无则 cum=1（上市初期）
        cum_t = 1.0
        for x in reversed(dates):
            if x <= d:
                cum_t = adj_map[x]
                break
        factor = (latest_cum / cum_t) if mode == "qfq" else cum_t
        for f in ("open", "close", "high", "low", "pre_close"):
            if row.get(f) is not None:
                row[f] = round(float(row[f]) * factor, 3)
        result.append(row)
    return result


def latest_quotes(codes: list[str]) -> list[dict]:
    """批量获取股票最新交易日行情（name/close/pct_chg/date）。

    近 3 月前缀通配（复用 _month_prefixes），跨年/跨月不失效。
    """
    import urllib.request, urllib.parse
    from datetime import datetime as _dt, timedelta as _td
    today = _dt.now()
    start = (today - _td(days=95)).strftime("%Y%m%d")
    end = today.strftime("%Y%m%d")
    prefixes = _month_prefixes(start, end)
    quotes = []
    for code in codes:
        try:
            rows = []
            for prefix in prefixes:
                url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'日k:{code}:{prefix}*')}"
                with urllib.request.urlopen(url, timeout=12) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                rows.extend(r for r in data if isinstance(r, dict) and r.get("date"))
            if not rows:
                continue
            latest = max(rows, key=lambda r: int(r.get("date") or 0))
            quotes.append({
                "code": code,
                "name": latest.get("name") or code,
                "date": latest.get("date"),
                "close": latest.get("close"),
                "pct_chg": latest.get("pct_chg"),
            })
        except Exception:
            continue
    return quotes
def stockdb_get(table: str) -> str:
    import urllib.parse
    url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote(table)}"
    import urllib.request
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


# ==================== HTTP 服务 ====================
PAGE = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stockdb 控制台</title>
<style>
:root{--bg:#0B1120;--panel:#111C2E;--panel2:#162238;--line:#1E2C45;--text:#E5EDF8;--muted:#8FA2BC;
--ok:#22C55E;--warn:#F59E0B;--err:#EF4444;--brand:#38BDF8}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:20px 20px 48px}

/* 顶部导航：吸顶 + 横向滚动页签 */
.topbar{position:sticky;top:0;z-index:50;background:rgba(11,17,32,.94);backdrop-filter:blur(8px);
border-bottom:1px solid var(--line)}
.topbar-inner{max-width:1120px;margin:0 auto;padding:10px 20px;display:flex;align-items:center;gap:18px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px;font-size:16px;font-weight:700;white-space:nowrap}
.dot{width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block}
.dot.ok{background:var(--ok);box-shadow:0 0 0 3px rgba(34,197,94,.14)}
.dot.err{background:var(--err);box-shadow:0 0 0 3px rgba(239,68,68,.14)}
.dot.warn{background:var(--warn);box-shadow:0 0 0 3px rgba(245,158,11,.14)}
.brand-status{font-size:12px;color:var(--muted)}
.tabs{display:flex;gap:2px;overflow-x:auto;flex:1;min-width:0;-webkit-overflow-scrolling:touch;scrollbar-width:none}
.tabs::-webkit-scrollbar{display:none}
.tab-btn{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);
font-size:15px;padding:8px 16px;cursor:pointer;white-space:nowrap}
.tab-btn.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:700}
.tab-panel{display:none;padding-top:16px}.tab-panel.active{display:block}

/* 卡片 */
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}
.card-title{font-size:14px;font-weight:600;margin-bottom:10px}
.k{color:var(--muted);font-size:12px;margin-bottom:2px}
.v{font-size:16px;font-weight:600}
.row{display:flex;gap:16px;flex-wrap:wrap}
#wlRow>div{position:relative;padding:8px 10px;border:1px solid var(--line);border-radius:8px;min-width:120px;transition:border-color .15s}
#wlRow>div:hover{border-color:rgba(56,189,248,.4)}
.wl-del{position:absolute;top:2px;right:6px;color:rgba(143,162,188,.45);font-size:11px;cursor:pointer;line-height:1;transition:color .15s}
.wl-del:hover{color:var(--err)}
.metrics{display:flex;gap:20px;flex-wrap:wrap}
.metrics .m{flex:1;min-width:110px}
.lbl{color:var(--muted);font-size:12px;margin-bottom:2px}
.val{font-size:16px;font-weight:600}

/* 按钮 */
button{background:var(--brand);border:0;color:#082F49;font-weight:700;font-size:15px;
padding:10px 22px;border-radius:8px;cursor:pointer;min-height:40px}
button:disabled{background:var(--line);color:var(--muted);cursor:not-allowed}
.btn-ghost{background:transparent;border:1px solid var(--line);color:var(--text);font-weight:500}
.btn-ghost:hover{border-color:var(--muted)}
.btn-danger{background:transparent;border:1px solid var(--err);color:var(--err);font-weight:600}
.btn-danger:hover{background:rgba(239,68,68,.08)}
.btn-sm{padding:6px 12px;font-size:13px;min-height:32px}

/* 开关 */
.switch{position:relative;display:inline-block;width:40px;height:22px;flex-shrink:0}
.switch input{opacity:0;width:0;height:0}
.switch .sl{position:absolute;inset:0;background:var(--panel2);border:1px solid var(--line);border-radius:20px;transition:.2s;cursor:pointer}
.switch .sl:before{content:"";position:absolute;width:16px;height:16px;left:2px;top:2px;border-radius:50%;background:var(--muted);transition:.2s}
.switch input:checked+.sl{background:rgba(56,189,248,.22);border-color:var(--brand)}
.switch input:checked+.sl:before{transform:translateX(18px);background:var(--brand)}

/* 主状态区 */
.hero{background:linear-gradient(180deg,var(--panel) 0%,#0E1830 100%);border:1px solid var(--line);border-radius:12px;padding:20px;margin:14px 0}
.hero-status{display:flex;align-items:center;gap:10px;font-size:20px;font-weight:700}
.hero-sub{color:var(--muted);font-size:13px;margin-top:6px}
.hero-actions{display:flex;align-items:center;gap:12px;margin-top:14px;flex-wrap:wrap}

/* 两列布局 */
.cols{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:14px 0}
@media(max-width:760px){.cols{grid-template-columns:1fr}}

/* 进度 */
.progress{display:flex;gap:10px;align-items:center;font-size:13px;color:var(--muted);margin-top:12px}
.bar{flex:1;height:6px;border-radius:3px;background:var(--panel2);overflow:hidden}
.bar i{display:block;height:100%;width:0;background:var(--brand);border-radius:3px;transition:width .4s}
.spin{display:inline-block;width:14px;height:14px;border:2px solid var(--brand);border-top-color:transparent;border-radius:50%;animation:spin .8s linear infinite;vertical-align:-2px}
@keyframes spin{to{transform:rotate(360deg)}}

/* Toast */
#toast{position:fixed;left:50%;bottom:28px;transform:translateX(-50%) translateY(20px);background:var(--panel2);
border:1px solid var(--line);color:var(--text);padding:10px 18px;border-radius:10px;font-size:14px;
opacity:0;pointer-events:none;transition:.25s;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,.4)}
#toast.show{opacity:1;transform:translateX(-50%) translateY(0)}

/* 历史 */
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}
tr.hr-row{cursor:pointer}
tr.hr-row:hover{background:rgba(56,189,248,.04)}
.hr-detail{background:var(--panel2);font-size:12px;color:var(--muted)}
.dot-ok{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--ok);margin-right:6px}
.dot-warn{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--warn);margin-right:6px}
.dot-fail{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--err);margin-right:6px}
.warn-text{color:var(--warn);font-size:12px;margin-top:4px}
.hr-cards{display:none}
@media(max-width:760px){table{display:none}.hr-cards{display:block}}
.hr-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:10px 0}
.hr-card .row1{display:flex;justify-content:space-between;align-items:center}
.hr-card .row2{color:var(--muted);font-size:12px;margin-top:4px}
.hr-card .row3{color:var(--muted);font-size:12px;margin-top:2px}

/* 存储条 */
.storage-bar{height:8px;border-radius:4px;background:var(--panel2);overflow:hidden;margin-top:8px}
.storage-bar i{display:block;height:100%;border-radius:4px;background:var(--brand)}
.storage-bar i.high{background:var(--warn)}

/* 系统健康卡 */
.hc-cards{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin:12px 0 4px}
@media(max-width:760px){.hc-cards{grid-template-columns:1fr}}
.hc{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
.hc .lbl{font-size:12px;color:var(--muted);margin-bottom:6px}
.hc .st{font-weight:700;display:flex;align-items:center;gap:8px}
.hc .st i{display:inline-block;width:9px;height:9px;border-radius:50%}
.hc .sub{font-size:12px;color:var(--muted);margin-top:6px}

/* 警告卡 */
.warn-card{background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.35);border-radius:12px;padding:14px 16px;margin:14px 0}
.warn-card .t{color:var(--warn);font-weight:700}
.warn-card .d{color:var(--muted);font-size:13px;margin-top:4px}

.info-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px 24px}
@media(max-width:760px){.info-grid{grid-template-columns:1fr}}
.info-grid .it{display:flex;justify-content:space-between;gap:12px;font-size:13px}
.info-grid .it .lk{color:var(--muted)}

/* 市场研究 */
.badge-st{padding:2px 10px;border-radius:10px;font-size:12px;background:var(--panel2);color:var(--muted);margin-left:8px}
.badge-st.b-building{color:var(--brand)}
.badge-st.b-ok{color:var(--ok)}
.badge-st.b-fail{color:var(--err)}
.research-card{padding:20px}
.research-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:16px}
.research-head .card-title{margin:0;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.research-head .badge-st{margin-left:0}
.ms-review{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:10px}
.ms-review-it{padding:13px 14px;background:linear-gradient(145deg,rgba(56,189,248,.09),rgba(56,189,248,.02));border:1px solid rgba(56,189,248,.16);border-radius:10px}
.ms-review-it .lbl{font-size:11px;margin-bottom:4px}
.ms-review-it .v{font-size:16px;font-weight:750;font-variant-numeric:tabular-nums}
.ms-review-it .sub{color:var(--muted);font-size:11px;margin-top:2px}
.ms-temp-line{display:flex;align-items:baseline;gap:5px}
.ms-temp-val{font-size:19px;font-weight:800;line-height:1.2;font-variant-numeric:tabular-nums}
.ms-temp-unit{font-size:10px;color:var(--muted);font-weight:500}
.ms-temp-track{height:5px;border-radius:99px;background:rgba(143,162,188,.16);overflow:hidden;margin-top:14px}
.ms-temp-track i{display:block;width:0;height:100%;border-radius:inherit;background:var(--brand);transition:width .4s ease,background .4s ease}
.ms-stats{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px}
.ms-st{min-width:0;padding:12px 14px;background:rgba(22,34,56,.58);border:1px solid rgba(143,162,188,.10);border-radius:10px}
.ms-st .lbl{font-size:11px;color:var(--muted);margin-bottom:3px;white-space:nowrap}
.ms-st .v{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ms-st .sub{display:block;color:var(--muted);font-size:11px;font-weight:500;margin-top:1px}
.ms-dist{margin-top:12px;padding:12px 14px;background:rgba(22,34,56,.30);border:1px solid rgba(143,162,188,.10);border-radius:10px}
.ms-section-head{display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:9px;font-size:12px;font-weight:650}
.ms-section-head span{color:var(--muted);font-size:10px;font-weight:400}
.dist-bar{height:10px;display:flex;overflow:hidden;border-radius:99px;background:var(--panel2)}
.dist-bar i{display:block;height:100%;min-width:0}
.dist-bar .ge5{background:#EF4444}.dist-bar .up2_5{background:#F87171}.dist-bar .up0_2{background:#FCA5A5}
.dist-bar .flat{background:#64748B}.dist-bar .down0_2{background:#86EFAC}.dist-bar .down2_5{background:#4ADE80}.dist-bar .le_neg5{background:#16A34A}
.dist-legend{display:grid;grid-template-columns:repeat(7,minmax(0,1fr));gap:8px;margin-top:9px}
.dist-it{min-width:0}.dist-it .k{font-size:9px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.dist-it .n{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}
.ms-note{margin-top:12px;border-top:1px solid rgba(143,162,188,.10);padding-top:10px;color:var(--muted);font-size:11px}
.ms-note summary{cursor:pointer;display:inline-flex;align-items:center;gap:6px;list-style:none;color:var(--muted);user-select:none}
.ms-note summary::-webkit-details-marker{display:none}
.ms-note summary:before{content:'+';display:inline-grid;place-items:center;width:15px;height:15px;border:1px solid var(--line);border-radius:50%;font-size:12px;line-height:1}
.ms-note[open] summary:before{content:'−'}
.ms-note-body{margin-top:7px;line-height:1.75}
.bcharts{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.chart-panel{min-width:0;background:rgba(22,34,56,.38);border:1px solid rgba(143,162,188,.10);border-radius:10px;padding:12px 12px 4px}
.chart-title{display:flex;align-items:center;justify-content:space-between;gap:8px;color:var(--text);font-size:12px;font-weight:600;padding:0 2px}
.chart-title span{color:var(--muted);font-size:10px;font-weight:400}
.bcharts .bchart{width:100%;height:272px}
.sector-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.sector-panel{border:1px solid rgba(143,162,188,.10);background:rgba(22,34,56,.30);border-radius:10px;padding:12px 14px;min-width:0}
.sector-title{display:flex;justify-content:space-between;color:var(--text);font-size:12px;font-weight:650;margin-bottom:5px}
.sector-title span{color:var(--muted);font-size:9px;font-weight:400}
.sector-row{display:grid;grid-template-columns:minmax(70px,1fr) 64px 64px 70px;gap:6px;align-items:center;padding:7px 0;border-top:1px solid rgba(143,162,188,.08);font-size:11px}
.sector-row:first-of-type{border-top:0}.sector-row .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.sector-row .num{text-align:right;font-variant-numeric:tabular-nums}
@media(max-width:900px){.ms-review{grid-template-columns:1fr}.ms-stats{grid-template-columns:repeat(2,minmax(0,1fr))}.bcharts,.sector-grid{grid-template-columns:1fr}.dist-legend{grid-template-columns:repeat(4,minmax(0,1fr))}}
@media(max-width:560px){.research-card{padding:16px}.research-head{align-items:flex-start}.ms-stats{grid-template-columns:1fr 1fr}.ms-st{padding:10px}.ms-st .v{font-size:14px}.bcharts .bchart{height:250px}.sector-row{grid-template-columns:minmax(64px,1fr) 58px 58px 64px}}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
.filters input[type=text]{width:150px}
.ftable-wrap{overflow-x:auto}
.ftable-wrap table td{cursor:pointer}
.ftable-wrap table tr.hr-row:hover{background:rgba(56,189,248,.05)}
.f-cards{display:none}
@media(max-width:760px){.f-cards{display:block}.ftable-wrap table{display:none}}
.f-card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:8px 0;cursor:pointer}
.f-card:hover{border-color:var(--brand)}
.f-card .r1{display:flex;justify-content:space-between;align-items:center}
.f-card .r2{display:flex;gap:10px;color:var(--muted);font-size:12px;flex-wrap:wrap;margin-top:4px}
.rs-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:10px;margin-top:12px}
.rs-it .lbl{font-size:12px;color:var(--muted);margin-bottom:2px}
.rs-it .v{font-size:14px;font-weight:600}
.rs-kline{margin-top:16px}
.rs-trend{margin-top:16px}
#tchart{width:100%;height:200px}
.rs-err{color:var(--warn);font-size:13px}
.rs-kctl{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px}

pre{background:#0A0F1C;border:1px solid var(--line);border-radius:10px;padding:12px;
max-height:340px;overflow:auto;font:12px/1.5 ui-monospace,monospace;color:#A5D8F8}
.actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.hint{color:var(--muted);font-size:12px}
input[type=text],input[type=time],select{background:#0A0F1C;border:1px solid var(--line);
color:var(--text);padding:8px 10px;border-radius:8px;width:220px}
#kchart{width:100%;height:460px}
.setting-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.setting-row:last-child{border-bottom:0}
.setting-row .lbl{font-size:13px;color:var(--text)}
.setting-row .dsc{font-size:12px;color:var(--muted)}
.times-pill{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:2px 10px;font-size:13px;margin:2px 4px 2px 0}
</style></head><body>
<div class="topbar"><div class="topbar-inner">
  <div class="brand"><span id="statusDot" class="dot"></span> stockdb</div>
  <div class="brand-status" id="brandStatus">加载中…</div>
  <nav class="tabs">
    <button class="tab-btn active" data-tab="research" onclick="showTab('research',this)">市场研究</button>
    <button class="tab-btn" data-tab="sync" onclick="showTab('sync',this)">数据同步</button>
    <button class="tab-btn" data-tab="system" onclick="showTab('system',this)">系统</button>
  </nav>
</div></div>
<div class="wrap">
<div id="toast"></div>

<!-- 市场研究：①市场状态 ②宽度趋势 ③因子排行 ④个股研究 + 自选股 -->
<div id="tab-research" class="tab-panel active">
  <div class="card research-card"><div class="research-head"><div class="card-title">今日市场复盘 <span class="hint" id="msAsOf" style="font-weight:normal">本地数据 · 仅普通股票</span></div><span class="badge-st" id="msBuild">…</span></div>
    <div class="ms-review" id="msReview"></div>
    <div class="ms-stats" id="msStats"></div>
    <div class="ms-dist" id="msDistribution"></div>
    <details class="ms-note"><summary>查看温度构成与统计口径</summary><div class="ms-note-body" id="msNote"></div></details>
  </div>

  <div class="card research-card"><div class="research-head"><div class="card-title">市场宽度趋势 <span class="hint" style="font-weight:normal">最近 20 个交易日</span></div></div>
    <div class="bcharts">
      <div class="chart-panel"><div class="chart-title">市场参与度 <span>上涨占比 · 站上 MA20</span></div><div class="bchart" id="wchart1"></div></div>
      <div class="chart-panel"><div class="chart-title">量能与强弱 <span>成交额 · 20 日新高/新低</span></div><div class="bchart" id="wchart2"></div></div>
    </div>
  </div>

  <div class="card research-card" id="sectorCard"><div class="research-head"><div class="card-title">行业强弱 <span class="hint" id="sectorMeta" style="font-weight:normal">申万一级 · 个股等权</span></div></div>
    <div class="sector-grid" id="sectorGrid"></div>
  </div>

  <div class="card"><div class="card-title">因子排行榜 <span class="hint" style="font-weight:normal" id="fMeta"></span></div>
    <div class="filters">
      <select id="fFactor">
        <option value="mom20">20 日动量</option>
        <option value="vol20">20 日波动率</option>
        <option value="vr520">5/20 日量比</option>
        <option value="dd60">60 日回撤</option>
      </select>
      <select id="fScope"><option value="stock">全部股票</option><option value="etf">ETF</option></select>
      <select id="fOrder"><option value="desc">从高到低</option><option value="asc">从低到高</option></select>
      <select id="fLimit"><option value="20">20</option><option value="50" selected>50</option><option value="100">100</option></select>
      <input type="text" id="fSearch" placeholder="代码 / 名称搜索">
      <button class="btn-ghost btn-sm" onclick="loadFactors(true)">查询</button>
      <button class="btn-ghost btn-sm" onclick="rebuildResearch()">重新计算</button>
      <span class="hint" id="fStatus"></span>
    </div>
    <div class="ftable-wrap">
      <table><thead><tr><th>排名</th><th>代码</th><th>名称</th><th id="fThVal">因子值</th><th>分位</th><th>涨跌幅</th><th>收盘</th><th>20日动量</th><th>20日波动率</th></tr></thead>
      <tbody id="fBody"><tr><td colspan="9" class="hint">（加载中…）</td></tr></tbody></table>
      <div class="f-cards" id="fCards"></div>
    </div>
  </div>

  <div class="card"><div class="card-title">个股研究</div>
    <div class="filters" style="margin-bottom:0">
      <input type="text" id="rsCode" placeholder="6 位代码" style="width:120px">
      <button class="btn-ghost btn-sm" onclick="loadStock()">加载</button>
      <span class="hint" id="rsMsg"></span>
    </div>
    <div id="rsEmpty" class="hint" style="margin-top:10px">输入代码或点击排行榜记录加载个股研究。</div>
    <div id="rsDetail" style="display:none">
      <div class="metrics" style="gap:8px">
        <div class="m"><div class="lbl">代码 / 名称</div><div class="v" id="rsName" style="font-size:15px">—</div></div>
        <div class="m"><div class="lbl">最新交易日</div><div class="v" id="rsDate" style="font-size:15px">—</div></div>
        <div class="m"><div class="lbl">收盘 / 涨跌幅</div><div class="v" id="rsPx" style="font-size:15px">—</div></div>
        <div class="m"><div class="lbl">成交量 / 成交额</div><div class="v" id="rsVol" style="font-size:15px">—</div></div>
      </div>
      <div class="rs-grid" id="rsFactors"></div>
      <div class="rs-kline"><div class="lbl">日 K 线（不复权）</div>
        <div class="rs-kctl">
          <select id="rsMonths">
            <option value="1">近1月</option>
            <option value="3" selected>近3月</option>
            <option value="6">近6月</option>
            <option value="12">近12月</option>
          </select>
          <button class="btn-ghost btn-sm" onclick="renderStockKline()">刷新</button>
          <label class="hint"><input type="checkbox" id="rsMA" checked> MA5/20/60</label>
        </div>
        <div id="kchart" style="height:420px"></div>
      </div>
      <div class="rs-trend"><div class="lbl">因子走势（近 60 日：20 日动量 / 20 日波动率）</div>
        <div id="tchart"></div>
      </div>
    </div>
  </div>

  <div class="card"><div class="card-title">自选股 <span class="hint" style="font-weight:normal">（点代码跳转个股研究）</span></div>
    <div class="row" id="wlRow" style="gap:14px"><span class="hint">…</span></div>
    <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">
      <input type="text" id="wlAdd" placeholder="加自选，如 600633,000967" style="width:260px">
      <button class="btn-ghost" onclick="addWatch()">加入自选</button>
      <span id="wlMsg" class="hint"></span>
    </div>
  </div>
</div>

<!-- 数据同步：主状态区 + 两列辅助 + 历史 + 日志 -->
<div id="tab-sync" class="tab-panel">
  <div class="hero">
    <div class="hero-status"><span id="heroSpin" class="spin" style="display:none"></span><span id="heroStatus">…</span></div>
    <div class="hero-sub" id="heroSub">…</div>
    <div class="hero-sub" id="heroTask" style="margin-top:2px"></div>
    <div class="hero-actions">
      <button id="syncBtn2" onclick="startSync(false)">立即热更新</button>
      <span class="hint" id="lastSuccess">…</span>
      <span style="flex:1"></span>
      <button class="btn-ghost btn-sm" onclick="toggleMenu()">更多操作 <span id="menuCaret">▾</span></button>
    </div>
    <div id="moreMenu" style="display:none;margin-top:12px">
      <div class="hint" style="margin-bottom:8px">默认热更新不中断行情服务；以下为故障兜底。</div>
      <button class="btn-danger btn-sm" onclick="startSync(true)">停服同步（备用）</button>
    </div>
    <div id="syncProgress" style="display:none;margin-top:14px">
      <div style="display:flex;gap:8px;align-items:center;font-size:14px">
        <span class="spin"></span><span id="progPhase">正在同步数据</span>
        <span style="flex:1"></span><span class="hint" id="progElapsed">已运行 00:00</span>
      </div>
      <div class="progress"><span id="progStage">准备中</span><div class="bar"><i id="progBar"></i></div><span id="progPct">0%</span></div>
    </div>
  </div>

  <div class="cols">
    <div class="card"><div class="card-title">自动同步</div>
      <div id="schView">
        <div class="setting-row"><span class="lbl">自动同步</span><label class="switch"><input type="checkbox" id="schEnabled" onchange="saveScheduleNow()"><span class="sl"></span></label></div>
        <div class="setting-row"><span class="lbl">仅交易日 <span class="dsc">周末 / 法定休市不执行</span></span><label class="switch"><input type="checkbox" id="schTrading" onchange="saveScheduleNow()"><span class="sl"></span></label></div>
        <div class="setting-row"><span class="lbl">执行时间</span><span class="hint" id="schTimesView">…</span></div>
        <div class="setting-row"><span class="lbl">下次执行</span><span class="hint" id="schNext">…</span></div>
        <div style="margin-top:10px;text-align:right"><button class="btn-ghost btn-sm" onclick="toggleSchEdit(true)">编辑计划</button></div>
      </div>
      <div id="schEdit" style="display:none">
        <div style="display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap">
          <input type="time" id="schTime" value="15:30" style="width:140px">
          <button class="btn-ghost btn-sm" onclick="addSchTime()">添加时间点</button>
        </div>
        <div id="schTimesEdit" style="margin-bottom:10px"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="btn-ghost btn-sm" onclick="toggleSchEdit(false)">取消</button>
          <button class="btn-sm" onclick="saveScheduleNow()">保存计划</button>
        </div>
      </div>
      <div class="hint" style="margin-top:8px" id="schLast"></div>
      <div class="hint" id="schToday"></div>
    </div>
    <div class="card"><div class="card-title">数据概况</div>
      <div class="metrics" style="gap:10px">
        <div class="m"><div class="lbl">股票</div><div class="val" id="cCountStk">…</div></div>
        <div class="m"><div class="lbl">ETF</div><div class="val" id="cCountEtf">…</div></div>
        <div class="m"><div class="lbl">覆盖</div><div class="val" id="cCoverage" style="font-size:14px">…</div></div>
      </div>
      <div class="hint" style="margin-top:8px">本地数据 <span id="cLocalDate">…</span> ｜ 镜像数据 <span id="cMirrorDate">…</span></div>
    </div>
  </div>

  <div class="card"><div class="card-title">最近同步 <span class="hint" style="font-weight:normal;margin-left:6px" id="histStats"></span></div>
    <table><thead><tr><th>时间</th><th>触发</th><th>模式</th><th>结果</th><th>下载</th><th>验证</th><th>耗时</th><th>数据最新</th></tr></thead>
    <tbody id="histBody"><tr><td colspan="8" class="hint">（暂无历史）</td></tr></tbody></table>
    <div class="hr-cards" id="histCards"></div>
  </div>
  <div class="card"><div class="card-title">同步日志</div>
    <pre id="log">（暂无）</pre>
  </div>
</div>

<!-- 系统：健康检查面板 -->
<div id="tab-system" class="tab-panel">
  <div class="card"><div class="card-title">系统健康检查</div>
    <div id="sysOK" style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px;margin-bottom:4px">…</div>
    <div class="hc-cards">
      <div class="hc"><div class="lbl">行情服务</div><div class="st"><i id="hcSvcDot"></i><span id="hcSvc">…</span></div><div class="sub" id="hcSvcSub">…</div></div>
      <div class="hc"><div class="lbl">Docker</div><div class="st"><i id="hcDkrDot"></i><span id="hcDkr">…</span></div><div class="sub" id="hcDkrSub">…</div></div>
      <div class="hc"><div class="lbl">自动任务</div><div class="st"><i id="hcSchedDot"></i><span id="hcSched">…</span></div><div class="sub" id="hcSchedSub">…</div></div>
      <div class="hc"><div class="lbl">同步能力</div><div class="st"><i id="hcCapDot"></i><span id="hcCap">…</span></div><div class="sub" id="hcCapSub">…</div></div>
    </div>
    <div id="dkrWarn" class="warn-card" style="display:none">
      <div class="t">Docker 管理不可用</div>
      <div class="d">未检测到 /var/run/docker.sock。行情查询仍可使用，但无法重启容器或执行停服同步。</div>
    </div>
  </div>
  <div class="card"><div class="card-title">存储空间</div>
    <div id="cDisk" style="font-size:15px;font-weight:600">…</div>
    <div class="storage-bar"><i id="diskBar"></i></div>
    <div class="hint" style="margin-top:6px">挂载点 /data（数据卷）</div>
  </div>
  <div class="card"><div class="card-title">运行信息</div>
    <div class="info-grid">
      <div class="it"><span class="lk">镜像</span><span id="cImage">—</span></div>
      <div class="it"><span class="lk">容器状态</span><span id="cState">—</span></div>
      <div class="it"><span class="lk">运行时长</span><span id="cUptime">—</span></div>
      <div class="it"><span class="lk">同步节点</span><span id="cSource">—</span></div>
      <div class="it"><span class="lk">WebUI 版本</span><span id="cVer">—</span></div>
      <div class="it"><span class="lk">WebUI 启动</span><span id="cStart">—</span></div>
      <div class="it"><span class="lk">调度心跳</span><span id="cHeartbeat">—</span></div>
      <div class="it"><span class="lk">数据目录</span><span id="cDataDir">—</span></div>
      <div class="it"><span class="lk">研究数据库</span><span id="cResearchDB">—</span></div>
      <div class="it"><span class="lk">交易日历</span><span id="cCalendar">—</span></div>
      <div class="it"><span class="lk">最近同步</span><span id="cLastSyncInfo">—</span></div>
    </div>
  </div>
  <div class="card"><div class="card-title">运维工具</div>
    <div class="actions">
      <button class="btn-ghost" id="btnLogs" disabled onclick="toggleContainerLogs()">查看容器日志</button>
      <button class="btn-danger" id="btnRestart" disabled onclick="restartContainer()">重启 stockdb</button>
      <span class="hint" id="containerMsg"></span>
    </div>
    <pre id="containerLog" style="display:none;margin-top:10px">（加载中…）</pre>
  </div>
  <div class="card"><div class="card-title">开发工具 <span class="hint" style="font-weight:normal">（行情原始查询，代理 stockdb HTTP API）</span></div>
    <details>
      <summary class="hint" style="cursor:pointer">展开原始查询</summary>
      <div style="display:flex;gap:8px;margin:10px 0;flex-wrap:wrap">
        <select id="qtype">
          <option value="股票代码">股票代码</option>
          <option value="日k:600633:20260810">日K 示例</option>
          <option value="分钟k:600633:20260810140000">分钟K 示例</option>
          <option value="复权:600633:2026*">复权 示例</option>
        </select>
        <button class="btn-ghost btn-sm" onclick="doQuery()">查询</button>
      </div>
      <pre id="qres">（查询结果）</pre>
    </details>
  </div>
</div>
</div>
<script src="/static/echarts.min.js"></script>
<script>
async function j(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(r.status);return r.json()}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function pctCls(v){return v>0?'color:#F87171':v<0?'color:#4ADE80':'color:#8FA2BC'}
function fmtPct(v){return v==null?'—':(v>0?'+':'')+Number(v).toFixed(2)+'%'}
function fmtPrice(v){return v==null?'—':Number(v).toFixed(2)}
function $(id){return document.getElementById(id)}
function fmtYMD(v){const t=String(v);return t.length===8?t.slice(0,4)+'-'+t.slice(4,6)+'-'+t.slice(6,8):t}
let _toastTimer=null;
function toast(msg){const t=$('toast');t.textContent=msg;t.classList.add('show');clearTimeout(_toastTimer);_toastTimer=setTimeout(()=>t.classList.remove('show'),2600)}
function fmtDur(sec){sec=Math.max(0,Math.floor(sec||0));return String(Math.floor(sec/60)).padStart(2,'0')+':'+String(sec%60).padStart(2,'0')}
function fmtClock(ts){
  if(!ts)return '';
  const d=new Date(ts.replace(' ','T'));if(isNaN(d))return ts;
  const now=new Date(),day0=new Date(now.getFullYear(),now.getMonth(),now.getDate());
  const dd=new Date(d.getFullYear(),d.getMonth(),d.getDate());
  const hm=String(d.getHours()).padStart(2,'0')+':'+String(d.getMinutes()).padStart(2,'0');
  const diff=Math.round((day0-dd)/86400000);
  if(diff===0)return '今天 '+hm;if(diff===1)return '昨天 '+hm;
  return String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')+' '+hm;
}
function showTab(name,btn){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  (btn||document.querySelector('[data-tab="'+name+'"]')).classList.add('active');
  $('tab-'+name).classList.add('active');
  if(name==='research'){
    loadResearchOnce();
    if(kchart)kchart.resize();
  }
  refresh(true); // 切页立即按当前页刷新
}

const PHASE_STAGE={stopping:'停服中',syncing:'同步数据',verifying:'校验数据完整性',restarting:'重启服务'};
const PHASE_PCT={stopping:15,syncing:45,verifying:80,restarting:95};
let _lastExit=null;
let _healthCache=null;   // 最近一次 /api/health 结果缓存：停留概览/行情页时顶部状态用它，避免 h=null 误报"服务异常"

// 轮询互斥 + 排队：避免重叠请求；切页 force 触发一次立即刷新
let _refreshing=false,_refreshQueued=false;
async function refresh(force){
  if(_refreshing){if(force)_refreshQueued=true;return}
  _refreshing=true;
  try{
    const s=await j('/api/status');
    const active=document.querySelector('.tab-panel.active')?.id||'tab-research';
    const h=(active==='tab-sync'||active==='tab-system')?await j('/api/health'):null;
    if(h)_healthCache=h;
    // 顶部状态：优先用本次 health，否则用最近缓存；缓存也没有时以 data_latest 降级判定
    const hs=h||_healthCache||{};
    let topSt,topColor;
    if(hs.status==='ok'){topSt='服务正常';topColor='ok'}
    else if(hs.status==='stale'){topSt='有待更新';topColor='warn'}
    else if(s.data_latest){topSt='服务正常';topColor='ok'}   // 无 health 但数据可达 → 不误报异常
    else {topSt='服务异常';topColor='err'}
    $('statusDot').className='dot '+topColor;
    const up=s.data_latest?('数据更新至 '+String(s.data_latest).slice(4,6)+'-'+String(s.data_latest).slice(6,8)):'数据未同步';
    $('brandStatus').textContent=topSt+' · '+up;
    if(active==='tab-research'){
      loadResearchOnce();   // 首次进入/页面初始加载时加载市场研究（_rsLoaded 防重复）
    }else if(active==='tab-sync'){
      renderHero(s,h||{});
      renderSchView(s.schedule||{});
      renderDataOverview(s);
      const sched=s.schedule||{};
      $('schToday').textContent=(sched.enabled&&sched.trading_only)?(s.trading_today?'':'今日非交易日，定时跳过'):'';
      loadHistory();
      const lg=await j('/api/log?n=80');
      $('log').textContent=lg.log;   // 日志常展开（HTML 无 display:none，无需折叠逻辑）
    }else if(active==='tab-system'){
      renderSystem(s);
      loadHistory();
    }
  }catch(e){
    $('log').textContent='状态刷新失败: '+e;$('log').style.display='block';
  }
  _refreshing=false;
  if(_refreshQueued){_refreshQueued=false;refresh()}
}

function renderHero(s,h){
  const spin=$('heroSpin'),st=$('heroStatus'),sub=$('heroSub');
  $('syncBtn2').disabled=s.sync_running;
  if(s.sync_running){
    window._syncStarted=s.sync_started;
    spin.style.display='inline-block';
    st.textContent='正在同步数据';st.style.color='';
    sub.textContent='当前：'+(PHASE_STAGE[s.sync_phase]||'处理中');
    $('syncProgress').style.display='block';
    const pct=PHASE_PCT[s.sync_phase]||45;
    $('progPhase').textContent='正在同步数据';
    $('progStage').textContent=PHASE_STAGE[s.sync_phase]||'处理中';
    $('progBar').style.width=pct+'%';
    $('progPct').textContent=pct+'%';
    $('progElapsed').textContent='已运行 '+fmtDur((Date.now()/1000)-(s.sync_started||Date.now()/1000));
    $('log').style.display='block';
  }else{
    spin.style.display='none';
    $('syncProgress').style.display='none';
    if(h.status==='ok'){
      st.textContent='数据已是最新';st.style.color='var(--ok)';
      sub.textContent='更新至 '+fmtYMD(h.latest)+' · 服务正常';
    }else if(h.status==='stale'){
      st.textContent='数据有待更新';st.style.color='var(--warn)';
      sub.textContent=h.note||'建议执行一次同步';
    }else{
      st.textContent='数据状态未知';st.style.color='var(--err)';
      sub.textContent=h.note||'无法获取数据最新日期';
    }
  }
}
function renderDataOverview(s){
  const cc=s.code_stats||{};
  $('cCountStk').textContent=cc.stock!=null?cc.stock:'—';
  $('cCountEtf').textContent=cc.etf!=null?cc.etf:'—';
  const cov=s.coverage||{};
  $('cCoverage').textContent=(cov.earliest?String(cov.earliest).slice(0,4)+' ~ '+String(cov.latest).slice(0,4):'—');
  $('cLocalDate').textContent=s.data_latest?fmtYMD(s.data_latest):'—';
  $('cMirrorDate').textContent=s.mirror||'—';
}
function toggleMenu(){const m=$('moreMenu');const show=m.style.display==='none';m.style.display=show?'block':'none';$('menuCaret').textContent=show?'▴':'▾'}
async function startSync(strict){
  const opt={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hot:!strict})};
  try{
    const r=await j('/api/sync',opt);
    toast(r.msg||'已启动同步');
    toggleMenu();
    if(r.msg&&r.msg.includes('运行中')){
      // 同步引擎被占用（如定时任务正在跑）：明确提示，不做无谓动作
    }else{
      // 已请求启动：立即展开日志并强制刷新一次，主状态区切到同步进度
      $('log').style.display='block';
      if(!$('log').textContent.trim()||$('log').textContent==='（暂无）')$('log').textContent='同步启动中，请稍候…';
      refresh(true);
    }
  }catch(e){toast('启动失败: '+e)}
}

// 自动同步：编辑模式下不覆盖时间点草稿（防 4s 轮询吞掉未保存的修改）
function renderSchView(sch){
  const editing=$('schEdit').style.display==='block';
  $('schEnabled').checked=!!sch.enabled;
  $('schTrading').checked=sch.trading_only!==false;
  const times=sch.times||[];
  $('schTimesView').textContent=times.length?times.join('、'):'（未设置）';
  $('schNext').textContent=sch.enabled?(sch.next_trigger?sch.next_trigger:'（时间点已过，明天触发）'):'定时未启用';
  $('schLast').textContent=(sch.last_trigger&&sch.last_trigger.ts)?('上次触发 '+fmtClock(sch.last_trigger.ts)+(sch.last_trigger.exit===0?' ✅':sch.last_trigger.exit==null?' ⏳':' ❌')):'';
  if(!editing)renderEditTimes(times);
}
function renderEditTimes(times){
  $('schTimesEdit').innerHTML=(times||[]).map(t=>'<span class="times-pill">'+esc(t)+' <a href="#" onclick="rmSchTime(\''+t+'\',event);return false" style="color:var(--err);text-decoration:none">✕</a></span>').join('')||'<span class="hint">（无时间点）</span>';
}
function toggleSchEdit(open){$('schView').style.display=open?'none':'block';$('schEdit').style.display=open?'block':'none'}
function addSchTime(){
  const t=$('schTime').value;if(!t){toast('请选择时间');return}
  const cur=[...($('schTimesEdit').textContent.match(/\d{2}:\d{2}/g)||[]),t];
  $('schTime').value='';
  renderEditTimes([...new Set(cur)]);
}
function rmSchTime(t,ev){ev.preventDefault();const cur=($('schTimesEdit').textContent.match(/\d{2}:\d{2}/g)||[]).filter(x=>x!==t);renderEditTimes(cur)}
async function saveScheduleNow(){
  const enabled=$('schEnabled').checked,trading=$('schTrading').checked;
  const times=[...($('schTimesEdit').textContent.match(/\d{2}:\d{2}/g)||[])];
  if(!times.length){toast('至少保留一个执行时间点');return}
  try{
    const r=await j('/api/schedule?action=save&enabled='+enabled+'&trading_only='+trading+'&times='+encodeURIComponent(times.join(',')));
    toast(r.msg||'自动同步计划已保存');
    if($('schEdit').style.display==='block')toggleSchEdit(false);
    renderSchView(r.schedule||{});
  }catch(e){toast('保存失败: '+e)}
}

// 历史：记录按新 → 旧排序（原文件 append，最新在末尾）
let _hist=[];
async function loadHistory(){
  try{
    const h=await j('/api/history');
    _hist=(h.history||[]).slice().reverse();
    const st=$('histStats');
    if(_hist.length){
      const recent=_hist.slice(0,7),ok=recent.filter(x=>x.exit_code===0).length;
      const durs=recent.filter(x=>x.duration_sec!=null).map(x=>x.duration_sec);
      const avg=durs.length?Math.round(durs.reduce((a,b)=>a+b,0)/durs.length):null;
      st.textContent='近 7 次：成功 '+ok+' / '+recent.length+(avg!=null?' · 平均 '+avg+'s':'');
    }else st.textContent='';
    const ls=_hist.find(x=>x.exit_code===0);
    $('lastSuccess').textContent=ls?('上次成功：'+fmtClock(ls.ts)):'';
    _lastExit=_hist.length?_hist[0].exit_code:null;
    $('histBody').innerHTML=_hist.map((x,i)=>historyRow(x,i)).join('')||'<tr><td colspan="8" class="hint">（暂无历史）</td></tr>';
    $('histCards').innerHTML=_hist.map(historyCard).join('')||'<div class="hint">（暂无历史）</div>';
    // 主状态区：同步任务状态（数据状态与同步管道健康分开）
    const tEl=$('heroTask');
    if(tEl){
      if(_hist.length){
        const x=_hist[0];
        if(x.exit_code===0&&x.warn)tEl.innerHTML='同步任务：<span style="color:var(--warn)">上次未生效 '+fmtClock(x.ts)+'</span> · <span style="color:var(--muted)">'+esc(x.warn)+'</span>';
        else if(x.exit_code===0)tEl.textContent='同步任务：上次成功 '+fmtClock(x.ts);
        else if(x.exit_code==null)tEl.textContent='同步任务：进行中';
        else tEl.innerHTML='同步任务：<span style="color:var(--err)">上次失败 '+fmtClock(x.ts)+'</span> · <span style="color:var(--muted)">'+esc(x.reason||('退出码 '+x.exit_code))+'</span>';
      }else tEl.textContent='';
    }
    // 系统页运行信息：最近同步
    const ci=$('cLastSyncInfo');
    if(ci)ci.textContent=_hist.length?(fmtClock(_hist[0].ts)+(_hist[0].exit_code===0?' ✅':_hist[0].exit_code==null?' ⏳':' ❌')+(_hist[0].duration_sec!=null?' · '+_hist[0].duration_sec+'s':'')):'—';
  }catch(e){}
}
function trigLabel(t){return t==='scheduled'?'⏰定时':t==='scheduled-retry'?'↻定时·重试':'手动'}
function resultCell(x){
  if(x.exit_code===0&&x.warn)return '<span class="dot-warn"></span>未生效';
  if(x.exit_code===0)return '<span class="dot-ok"></span>成功';
  if(x.exit_code==null)return '<span class="spin" style="width:8px;height:8px;border-width:2px;border-color:var(--brand);border-top-color:transparent"></span>运行中';
  return '<span class="dot-fail"></span>失败';
}
function historyRow(x,i){
  return `<tr class="hr-row" onclick="toggleHistDetail(${i})">
    <td>${esc(x.ts)}</td><td>${trigLabel(x.trigger)}</td><td>${x.mode==='hot'?'热更新':'严格'}</td>
    <td>${resultCell(x)}</td><td>${x.downloads==null?'—':x.downloads}</td>
    <td>${x.verified==='pass'?'通过':x.verified==='fail'?'失败':x.verified==='skipped'?'跳过':'—'}</td>
    <td>${x.duration_sec==null?'—':esc(x.duration_sec)+'s'}</td><td>${esc(x.data_latest||'—')}</td></tr>`;
}
function historyCard(x){
  const reason=x.reason?(' · '+esc(x.reason)):'';
  const warnHtml=x.warn?('<div class="warn-text">⚠ '+esc(x.warn)+'</div>'):'';
  return `<div class="hr-card">
    <div class="row1"><span>${resultCell(x)}</span><span class="hint">${trigLabel(x.trigger)}</span></div>
    <div class="row2">${esc((x.ts||'').slice(0,16))} · ${x.mode==='hot'?'热更新':'严格'}</div>
    <div class="row3">${x.downloads!=null?('下载 '+x.downloads+' 个文件 · '):''}${x.verified==='pass'?'校验通过':x.verified==='fail'?'校验失败':x.verified==='skipped'?'未校验':''}${x.duration_sec!=null?(' · '+x.duration_sec+' 秒'):''}${reason}</div>
    <div class="row3">数据更新至 ${esc(x.data_latest||'—')}</div>${warnHtml}
  </div>`;
}
function toggleHistDetail(i){
  const x=_hist[i];if(!x)return;
  const rows=document.querySelectorAll('#histBody tr.hr-row');
  if(!rows[i])return;
  let n=rows[i].nextElementSibling;
  if(n&&n.classList.contains('hr-detail')){n.remove();return}
  const det='失败原因：'+(x.reason||'—')+' ｜ 下载 '+(x.downloads==null?'—':x.downloads)+' 个 ｜ 删除 '+(x.deletes==null?'—':x.deletes)+' 个 ｜ 数据最新 '+(x.data_latest||'—');
  rows[i].insertAdjacentHTML('afterend','<tr class="hr-detail"><td colspan="8">'+esc(det)+'</td></tr>');
}

function renderSystem(s){
  const cs=s.container||{};
  const lat=(s.code_stats&&s.code_stats.latency_ms!=null)?s.code_stats.latency_ms:null;
  // 行情服务：以实际请求成功与延迟为准
  const svcOK=lat!=null;
  $('hcSvcDot').style.background=svcOK?'var(--ok)':'var(--err)';
  $('hcSvc').textContent=svcOK?'正常':'不可用';
  $('hcSvcSub').textContent='响应 '+(lat!=null?lat+' ms':'—')+' · 数据至 '+(s.data_latest?String(s.data_latest).slice(4,6)+'-'+String(s.data_latest).slice(6,8):'—');
  // Docker
  $('hcDkrDot').style.background=cs.ok?'var(--ok)':'var(--err)';
  $('hcDkr').textContent=cs.ok?'已连接':'不可用';
  $('hcDkrSub').textContent=cs.ok?('容器 '+cs.status):'未检测到 docker.sock';
  // 自动任务：运行状态 + 待重试提示
  const sch=s.schedule||{};
  $('hcSchedDot').style.background=s.scheduler_alive?'var(--ok)':'var(--err)';
  $('hcSched').textContent=s.scheduler_alive?'运行中':'未运行';
  const rp=sch.retry_pending;
  $('hcSchedSub').innerHTML=(sch.enabled?(sch.next_trigger?'下次 '+esc(sch.next_trigger):'已启用'):'定时未启用')+''
    +(rp?(' · <span style="color:var(--warn)">等待重试：'+esc(rp.slice(11,16))+'</span>'):(sch.enabled?' · 无待重试':''));
  // 同步能力：更新程序/数据源/数据卷（待重试为 warn，不判不可用）
  const cap=s.sync_cap||{ok:false,checks:{}};
  const fails=Object.values(cap.checks||{}).filter(c=>c&&c.ok===false);
  $('hcCapDot').style.background=cap.ok?'var(--ok)':'var(--err)';
  $('hcCap').textContent=cap.ok?'可用':'不可用';
  $('hcCapSub').textContent=fails.length?('受阻：'+fails.map(f=>f.detail).slice(0,2).join('；')):'更新程序 · 数据源 · 数据卷 就绪';
  // 总状态
  const issues=(svcOK?0:1)+(cap.ok?0:1)+(s.scheduler_alive?0:1)+(cs.ok?0:1);
  $('sysOK').innerHTML='<span class="dot '+(issues===0?'ok':'warn')+'"></span> '+(issues===0?'系统运行正常':'存在待处理项');
  // Docker 警告卡 + 运维按钮禁用
  $('dkrWarn').style.display=cs.ok?'none':'block';
  $('btnLogs').disabled=!cs.ok;
  $('btnRestart').disabled=!cs.ok;
  // 存储
  if(s.disk&&s.disk.total_gb!=null){
    const pct=Math.round(s.disk.used_gb/s.disk.total_gb*100);
    $('cDisk').textContent=s.disk.used_gb+' GB / '+s.disk.total_gb+' GB · '+pct+'%'+(s.disk.free_gb!=null?'（'+s.disk.free_gb+' GB 可用）':'');
    const bar=$('diskBar');bar.style.width=pct+'%';bar.className=pct>80?'high':'';
  }
  // 运行信息
  $('cImage').textContent=cs.image||'—';
  $('cState').textContent=cs.status||'—';
  $('cUptime').textContent=cs.started?fmtUptime(cs.started):'—';
  $('cSource').textContent=s.source||'—';
  $('cVer').textContent=(s.webui&&s.webui.version)||'—';
  $('cStart').textContent=(s.webui&&s.webui.started)?new Date(s.webui.started*1000).toLocaleString('zh-CN',{hour12:false}).replace(/\//g,'-'):'—';
  const hb=$('cHeartbeat');
  if(hb){
    if(s.webui&&s.webui.heartbeat){
      const sec=Math.floor(Date.now()/1000-s.webui.heartbeat);
      const t=sec<0?'刚刚':sec<60?sec+' 秒前':sec<3600?Math.floor(sec/60)+' 分钟前':Math.floor(sec/3600)+' 小时前';
      hb.innerHTML=sec>120?('<span style="color:var(--err)">'+t+'</span>'):t;
    }else hb.textContent='—';
  }
  $('cDataDir').textContent=s.data_dir||'—';
  const rdb=s.research_db||{};
  $('cResearchDB').textContent=rdb.ok?((rdb.path||'—')+' · '+(rdb.size_mb||0)+' MB'+(rdb.latest_date?' · '+rdb.latest_date:'')):('异常'+(rdb.error?'：'+rdb.error:''));
  // 交易日历覆盖：临近到期（<90 天）黄色提醒
  const cal=s.calendar||{};
  const cCal=$('cCalendar');
  if(cCal){
    if(cal.through){
      const daysLeft=Math.round((new Date(cal.through.replace(/-/g,'/'))-Date.now())/86400000);
      cCal.innerHTML=cal.through+'（'+cal.days+' 个休市日）'
        +(daysLeft<90?('<span style="color:var(--warn)"> ｜ 即将到期，请更新休市表</span>'):'');
    }else cCal.textContent='—';
  }
}

async function loadWatchlist(){
  try{
    const w=await j('/api/watchlist');
    const wl=w.quotes||[];
    const wrow=$('wlRow');
    if(wl.length)wrow.innerHTML=wl.map(x=>'<div style="cursor:pointer" onclick="loadStockByCode(\''+x.code+'\')"><div class="k">'+esc(x.name)+' '+esc(x.code)+'</div><div class="v" style="'+pctCls(x.pct_chg)+'">'+fmtPrice(x.close)+' <span style="font-size:12px">'+fmtPct(x.pct_chg)+'</span></div><div class="wl-del" onclick="event.stopPropagation();delWatch(\''+x.code+'\')" title="移除">✕</div></div>').join('');
    else wrow.innerHTML='<span class="hint">（空，下方添加自选）</span>';
  }catch(e){}
}
async function delWatch(code){
  try{
    const cur=await j('/api/watchlist');
    const codes=(cur.codes||[]).filter(c=>c!==code);
    const r=await j('/api/watchlist?action=set&codes='+encodeURIComponent(codes.join(',')));
    $('wlMsg').textContent='已移除 '+code;
    loadWatchlist();
  }catch(e){$('wlMsg').textContent='移除失败: '+e}
}
async function addWatch(){
  const codes=$('wlAdd').value.trim();if(!codes)return;
  try{
    const cur=await j('/api/watchlist');
    const merged=[...new Set((cur.codes||[]).concat(codes.split(/[,，\s]+/)))];
    const r=await j('/api/watchlist?action=set&codes='+encodeURIComponent(merged.join(',')));
    $('wlMsg').textContent='已保存 '+r.codes.length+' 只';
    $('wlAdd').value='';
    loadWatchlist();
  }catch(e){$('wlMsg').textContent='保存失败: '+e}
}
async function doQuery(){
  const q=$('qtype').value;
  const r=await fetch('/api/query?t='+encodeURIComponent(q));
  $('qres').textContent=await r.text();
}

// ==================== 市场研究 ====================
let _rsLoaded=false,_rsMarketLoaded=false,_rsFactorLoaded=false;
let _rsStatusTimer=null;
const FACTOR_LABEL={mom20:'20 日动量',vol20:'20 日波动率',vr520:'5/20 日量比',dd60:'60 日回撤'};
let _stockRows=[];   // 当前排行数据（供搜索/联动）

function loadResearchOnce(){
  if(_rsLoaded){return}
  _rsLoaded=true;
  loadWatchlist();
  loadResearchStatus();
  loadResearchMarket();
  loadFactors();
  _rsStatusTimer=setInterval(loadResearchStatus,8000); // 构建中轮询状态
}

async function loadResearchStatus(){
  try{
    const st=await j('/api/research/status');
    const b=$('msBuild');
    const map={
      building:['正在计算 '+st.processed+'/'+(st.total||'?')+(st.current_code?' · '+st.current_code:''),'b-building'],
      done:['可用（'+(st.last_date||st.cache_date||'')+'）'+(st.last_duration_sec!=null?' · 上次计算 '+st.last_duration_sec+'s':''),'b-ok'],
      failed:['计算失败：'+(st.error||'')+' — 点「重新计算」重试','b-fail'],
      idle:['等待数据',''],
    };
    const m=map[st.state]||['…',''];
    if(b){b.textContent=m[0];b.className='badge-st '+m[1]}
    const fs=$('fStatus');
    if(fs){
      if(st.state==='building')fs.textContent='因子数据构建中，排行榜稍后自动刷新';
      else if(st.state==='failed')fs.textContent='上次计算失败，排行榜为空';
    }
    // 构建完成刷新：cache_date 或 generated_at 变化（同日手动重建也触发）→ 重载市场/因子并重渲染图表
    const cacheSig = st.cache_date + (st.cache_generated_at||'');
    if(st.cache_date && cacheSig!==_rsKnownSig){
      _rsKnownSig=cacheSig;
      if(_rsBuildSeen){
        loadResearchMarket();
        loadFactors();
        renderWidthCharts(marketLast||{});
      }
    }
    if(st.state==='building')_rsBuildSeen=true;
  }catch(e){}
}
let _rsKnownSig=null,_rsBuildSeen=false,marketLast=null;

async function loadResearchMarket(){
  try{
    const m=await j('/api/research/market');
    marketLast=m;
    if(m.error){$('msReview').innerHTML='';$('msStats').innerHTML='<span class="hint">'+esc(m.error)+'</span>';$('msDistribution').innerHTML='';$('sectorGrid').innerHTML='<span class="hint">等待市场快照</span>';$('msNote').textContent='';return}
    renderMarket(m);
    if(!_rsMarketLoaded){_rsMarketLoaded=true;renderWidthCharts(m)}
  }catch(e){}
}
function renderMarket(m){
  const temp=m.temperature!=null?Math.max(0,Math.min(100,Number(m.temperature))):null;
  const upc=m.up_ratio!=null?m.up_ratio*100:null;
  const mac=m.ma20_ratio!=null?m.ma20_ratio*100:null;
  const hi20=m.high20||0,lo20=m.low20||0;
  const d=m.derived||{};
  const ad=d.advance_decline_ratio!=null?d.advance_decline_ratio:(m.down?Number(m.up||0)/Number(m.down):null);
  const gap=d.breadth_gap_pp!=null?d.breadth_gap_pp:(upc!=null&&mac!=null?upc-mac:null);
  const netHigh=d.net_high20!=null?d.net_high20:hi20-lo20;
  const hist=m.width_hist||[],lastHist=hist.length?hist[hist.length-1]:null;
  const prev=lastHist&&String(lastHist.date)===String(m.date)?(hist.length>1?hist[hist.length-2]:null):lastHist;
  const upDelta=d.up_change_1d_pp!=null?d.up_change_1d_pp:(prev&&prev.up_ratio!=null&&m.up_ratio!=null?(m.up_ratio-prev.up_ratio)*100:null);
  const signed=(v,d=1)=>v==null?'—':(v>0?'+':'')+Number(v).toFixed(d);
  $('msAsOf').textContent=(m.date?fmtYMD(m.date)+' · ':'')+'本地数据 · 仅普通股票';
  $('msReview').innerHTML=`
    <div class="ms-review-it"><div class="lbl">当日赚钱效应</div><div class="v">${upc!=null?upc.toFixed(1)+'%':'—'} 上涨</div><div class="sub">A/D ${ad!=null?Number(ad).toFixed(2):'—'} · 1日 ${signed(upDelta)}pp · 5日 ${signed(d.up_change_5d_pp)}pp</div></div>
    <div class="ms-review-it"><div class="lbl">短线与中期趋势</div><div class="v">上涨－MA20 ${signed(gap)}pp</div><div class="sub">站上 MA20 ${mac!=null?mac.toFixed(1)+'%':'—'} · 1日 ${signed(d.ma20_change_1d_pp)}pp</div></div>
    <div class="ms-review-it"><div class="lbl">量能确认</div><div class="v">较前5日均额 ${signed(d.amount_vs_prev5_pct)}%</div><div class="sub">成交额 ${fmtAmount(m.total_amount)} · 较前20日均额 ${signed(d.amount_vs_prev20_pct)}%</div></div>`;
  $('msStats').innerHTML=`
    <div class="ms-st"><div class="lbl">综合温度 <span id="tempHelp" style="cursor:help" title="上涨家数占比40% + 站上MA20占比40% + (20日新高占比-新低占比)20%，各分项归一化0~100。仅描述市场状态，不构成买卖建议。">ⓘ</span></div><div class="ms-temp-line"><span class="ms-temp-val">${temp!=null?temp:'—'}</span><span class="ms-temp-unit">/ 100</span></div><div class="ms-temp-track" aria-hidden="true"><i style="width:${temp==null?0:temp}%"></i></div></div>
    <div class="ms-st"><div class="lbl">上涨 / 下跌 / 平盘</div><div class="v">${m.up||0} / ${m.down||0} / ${m.flat||0}<span class="sub">停牌 / 无数据 ${m.na||0}</span></div></div>
    <div class="ms-st"><div class="lbl">涨跌幅中位数</div><div class="v">${fmtPct(m.median_pct)}</div></div>
    <div class="ms-st"><div class="lbl">全市场成交额</div><div class="v">${fmtAmount(m.total_amount)}<span class="sub" style="color:${(m.amount_change||0)>0?'var(--ok)':(m.amount_change||0)<0?'var(--err)':'var(--muted)'}">较前日 ${m.amount_change!=null?fmtPct(m.amount_change):'—'}</span></div></div>
    <div class="ms-st"><div class="lbl">站上 MA20</div><div class="v">${mac!=null?mac.toFixed(1)+'%':'—'}<span class="sub">${m.ma20_above||0} 只股票</span></div></div>
    <div class="ms-st"><div class="lbl">20 日新高 / 新低</div><div class="v">${hi20} / ${lo20}<span class="sub">净新高 ${signed(netHigh,0)}</span></div></div>`;
  renderReturnDistribution(m.distribution||{});
  renderSectorStrength(m.sectors||{});
  const tc=m.temp_components||{};
  $('msNote').innerHTML='A/D＝上涨家数÷下跌家数；上涨－MA20＝上涨占比－站上MA20占比；1日/5日变化均为百分点差。成交额对比使用<b style="color:var(--text)">此前</b>5日/20日均额，不把当日放入基准。行业按申万一级聚合，行业涨跌取成分股等权中位数，上涨比例＝上涨成分股÷有效成分股。<br>涨跌分布按个股实际涨跌幅分桶；≥9.5%仅标记“大涨/大跌”，不等同涨停/跌停。温度＝上涨家数占比 '+(tc.up_ratio!=null?tc.up_ratio:'—')+'（40%）＋站上MA20 '+(tc.ma20_ratio!=null?tc.ma20_ratio:'—')+'（40%）＋新高/新低 '+(tc.highlow!=null?tc.highlow:'—')+'（20%）。指标仅描述市场状态，不构成买卖建议。';
}
function renderReturnDistribution(dist){
  const rows=dist.buckets||[];
  if(!rows.length){$('msDistribution').innerHTML='<div class="hint">涨跌分布将在重新计算后生成</div>';return}
  const bar=rows.map(x=>`<i class="${esc(x.key)}" style="width:${Math.max(0,Number(x.ratio||0)*100)}%" title="${esc(x.label)} ${x.count||0}只"></i>`).join('');
  const legend=rows.map(x=>`<div class="dist-it"><div class="k">${esc(x.label)}</div><div class="n">${x.count||0}</div></div>`).join('');
  $('msDistribution').innerHTML=`<div class="ms-section-head">涨跌幅分布 <span>大涨 ≥9.5%：${dist.large_up||0} · 大跌 ≤−9.5%：${dist.large_down||0}</span></div><div class="dist-bar">${bar}</div><div class="dist-legend">${legend}</div>`;
}
function renderSectorStrength(data){
  const meta=$('sectorMeta');
  if(!data.available){meta.textContent='申万一级 · 板块映射暂不可用';$('sectorGrid').innerHTML='<div class="hint">重新计算时会批量读取 StockDB 板块映射；接口无数据时不生成行业排名。</div>';return}
  meta.textContent='申万一级 · 已映射 '+(data.mapped||0)+' 只 · 未映射 '+(data.unmapped||0)+' 只';
  const panel=(title,rows)=>`<div class="sector-panel"><div class="sector-title">${title}<span>中位涨跌 / 上涨占比 / 量能变化</span></div>${(rows||[]).map(x=>`<div class="sector-row"><span class="name">${esc(x.name)}</span><span class="num" style="${pctCls(x.median_pct||0)}">${fmtPct(x.median_pct)}</span><span class="num">${x.up_ratio!=null?(x.up_ratio*100).toFixed(0)+'%':'—'}</span><span class="num">${fmtPct(x.amount_change)}</span></div>`).join('')}</div>`;
  $('sectorGrid').innerHTML=panel('领涨行业',data.top)+panel('领跌行业',data.bottom);
}
function fmtAmount(v){
  if(v==null)return '—';
  if(v>=1e12)return (v/1e12).toFixed(2)+' 万亿';
  if(v>=1e8)return (v/1e8).toFixed(1)+' 亿';
  if(v>=1e4)return (v/1e4).toFixed(0)+' 万';
  return String(v);
}
function renderWidthCharts(m){
  const hist=m.width_hist||[];
  if(!hist.length){$('wchart1').innerHTML='<span class="hint">（暂无宽度历史）</span>';$('wchart2').innerHTML='';return}
  const dates=hist.map(x=>String(x.date).slice(4));
  const axisColor='#71859F',gridColor='rgba(143,162,188,.10)';
  const c1=echarts.getInstanceByDom($('wchart1'))||echarts.init($('wchart1'),'dark');c1.clear();
  window._wchart1=c1;
  c1.setOption({
    backgroundColor:'transparent',
    aria:{enabled:true,description:'最近20个交易日的上涨股票占比与站上20日均线股票占比'},
    tooltip:{trigger:'axis',backgroundColor:'#0B1120',borderColor:'#263A58',textStyle:{color:'#E5EDF8',fontSize:11},
      valueFormatter:v=>v==null?'—':(v*100).toFixed(1)+'%'},
    legend:{right:4,top:3,itemWidth:16,itemHeight:7,textStyle:{color:'#8FA2BC',fontSize:10}},
    grid:{left:44,right:10,top:38,bottom:30},
    xAxis:{type:'category',boundaryGap:false,data:dates,axisTick:{show:false},axisLine:{lineStyle:{color:gridColor}},axisLabel:{color:axisColor,fontSize:10,formatter:v=>v.slice(0,2)+'/'+v.slice(2)}},
    yAxis:{type:'value',min:0,max:1,interval:.25,axisLabel:{formatter:v=>(v*100)+'%',color:axisColor,fontSize:10},axisLine:{show:false},axisTick:{show:false},splitLine:{lineStyle:{color:gridColor}}},
    series:[
      {name:'上涨占比',type:'line',data:hist.map(x=>x.up_ratio),smooth:.3,showSymbol:false,emphasis:{focus:'series'},lineStyle:{width:2,color:'#F87171'},areaStyle:{color:'rgba(248,113,113,.07)'}},
      {name:'站上 MA20',type:'line',data:hist.map(x=>x.ma20_ratio),smooth:.3,showSymbol:false,emphasis:{focus:'series'},lineStyle:{width:2,color:'#38BDF8'},areaStyle:{color:'rgba(56,189,248,.06)'}}
    ]
  });
  const c2=echarts.getInstanceByDom($('wchart2'))||echarts.init($('wchart2'),'dark');c2.clear();
  window._wchart2=c2;
  c2.setOption({
    backgroundColor:'transparent',
    aria:{enabled:true,description:'最近20个交易日的全市场成交额及创20日新高和新低股票数量'},
    tooltip:{trigger:'axis',backgroundColor:'#0B1120',borderColor:'#263A58',textStyle:{color:'#E5EDF8',fontSize:11}},
    legend:{right:4,top:3,itemWidth:16,itemHeight:7,textStyle:{color:'#8FA2BC',fontSize:10}},
    grid:{left:50,right:48,top:38,bottom:30},
    xAxis:{type:'category',data:dates,axisTick:{show:false},axisLine:{lineStyle:{color:gridColor}},axisLabel:{color:axisColor,fontSize:10,formatter:v=>v.slice(0,2)+'/'+v.slice(2)}},
    yAxis:[
      {type:'value',name:'亿元',nameTextStyle:{color:axisColor,fontSize:9},axisLabel:{color:axisColor,fontSize:10},axisLine:{show:false},axisTick:{show:false},splitLine:{lineStyle:{color:gridColor}}},
      {type:'value',name:'家数',nameTextStyle:{color:axisColor,fontSize:9},axisLabel:{color:axisColor,fontSize:10},axisLine:{show:false},axisTick:{show:false},splitLine:{show:false}}
    ],
    series:[
      {name:'成交额',type:'bar',data:hist.map(x=>x.amount==null?null:+(x.amount/1e8).toFixed(1)),barMaxWidth:20,itemStyle:{color:'#38BDF8',opacity:.48,borderRadius:[3,3,0,0]}},
      {name:'20 日新高',type:'line',yAxisIndex:1,data:hist.map(x=>x.high20),smooth:.25,showSymbol:false,emphasis:{focus:'series'},lineStyle:{width:1.8,color:'#22C55E'}},
      {name:'20 日新低',type:'line',yAxisIndex:1,data:hist.map(x=>x.low20),smooth:.25,showSymbol:false,emphasis:{focus:'series'},lineStyle:{width:1.8,color:'#EF4444'}}
    ]
  });
}

async function loadFactors(){
  _rsFactorLoaded=true;
  const factor=$('fFactor').value,scope=$('fScope').value,order=$('fOrder').value;
  const limit=$('fLimit').value,q=$('fSearch').value.trim();
  try{
    const d=await j('/api/research/factors?factor='+factor+'&scope='+scope+'&order='+order+'&limit='+limit+(q?'&q='+encodeURIComponent(q):''));
    if(d.error){$('fBody').innerHTML='<tr><td colspan="9" class="hint">'+esc(d.error)+'</td></tr>';$('fCards').innerHTML='';return}
    _stockRows=d.rows||[];
    $('fThVal').textContent='因子值（'+(FACTOR_LABEL[factor]||factor)+'）';
    const meta=d.meta&&d.meta[factor];
    $('fMeta').textContent=meta?('公式：'+meta.formula):'';
    $('fStatus').textContent='数据日期 '+d.date+' · 共 '+(d.total||0)+' 只';
    const rows=d.rows||[];
    $('fBody').innerHTML=rows.map((r,i)=>factorRow(r,i)).join('')||'<tr><td colspan="9" class="hint">（无匹配结果）</td></tr>';
    $('fCards').innerHTML=rows.map(factorCard).join('')||'<div class="hint">（无匹配结果）</div>';
  }catch(e){$('fBody').innerHTML='<tr><td colspan="9" class="hint">加载失败: '+esc(e)+'</td></tr>'}
}
function fmtNum(v,n){return v==null?'—':Number(v).toFixed(n!=null?n:2)}
function fmtPctShort(v){return v==null?'—':(v>0?'+':'')+(v*100).toFixed(2)+'%'}
function factorRow(r,i){
  const f=$('fFactor').value;
  const val=f==='vr520'?fmtNum(r[f],2):fmtPctShort(r[f]);
  const pct=r[f+'_pct'];
  return `<tr class="hr-row" onclick="loadStockByCode('${r.code}')">
    <td>${r.rank!=null?r.rank:i+1}</td><td>${esc(r.code)}</td><td>${esc(r.name)}</td>
    <td style="font-weight:700">${val}</td>
    <td>${pct!=null?Math.round(pct)+'%':'—'}</td>
    <td style="color:${(r.pct_chg||0)>0?'var(--ok)':(r.pct_chg||0)<0?'var(--err)':'var(--muted)'}">${fmtPct(r.pct_chg)}</td>
    <td>${fmtNum(r.close)}</td>
    <td>${fmtPctShort(r.mom20)}</td>
    <td>${fmtNum(r.vol20!=null?r.vol20*100:null,1)+'%'}</td></tr>`;
}
function factorCard(r){
  const f=$('fFactor').value;
  const val=f==='vr520'?fmtNum(r[f],2):fmtPctShort(r[f]);
  return `<div class="f-card" onclick="loadStockByCode('${r.code}')">
    <div class="r1"><span style="font-weight:700">${r.rank!=null?r.rank:i+1}. ${esc(r.name)} ${esc(r.code)}</span><span style="font-weight:700;color:var(--brand)">${val}</span></div>
    <div class="r2"><span>分位 ${r[f+'_pct']!=null?Math.round(r[f+'_pct'])+'%':'—'}</span><span style="color:${(r.pct_chg||0)>0?'var(--ok)':(r.pct_chg||0)<0?'var(--err)':'var(--muted)'}">${fmtPct(r.pct_chg)}</span><span>收盘 ${fmtNum(r.close)}</span></div>
  </div>`;
}
function rebuildResearch(){
  if(!confirm('重新计算全市场因子？（后台执行，可能耗时数分钟）'))return;
  try{
    j('/api/research/rebuild',{method:'POST'}).then(r=>{toast(r.msg||'已启动');loadResearchStatus()});
  }catch(e){toast('启动失败: '+e)}
}

// 个股研究
function loadStockByCode(code){
  $('rsCode').value=code;
  $('rsMsg').textContent='';
  const det=$('rsDetail');det.style.display='block';
  $('rsEmpty').style.display='none';
  const el=$('rsDetail').closest('.card');
  if(el)el.scrollIntoView({behavior:'smooth',block:'start'});
  loadStock();
}
async function loadStock(){
  const code=$('rsCode').value.trim();
  if(!code||!/^\d{6}$/.test(code)){$('rsMsg').textContent='请输入 6 位代码';return}
  $('rsMsg').textContent='加载中…';
  // 展开详情区（与 loadStockByCode 一致）：否则 K 线图在 display:none 容器中
  // 初始化会被 ECharts 压成 100px 宽，且后续 setOption 不会自动重排。
  const det=$('rsDetail');det.style.display='block';
  $('rsEmpty').style.display='none';
  try{
    const d=await j('/api/research/stock?code='+code);
    if(d.error){$('rsMsg').textContent=d.error;return}
    $('rsMsg').textContent='';
    renderStock(d);
  }catch(e){$('rsMsg').textContent='加载失败: '+e}
}
function renderStock(d){
  $('rsName').textContent=d.name+' '+d.code;
  $('rsDate').textContent=fmtYMD(d.date);
  $('rsPx').innerHTML=fmtNum(d.close)+' <span style="color:'+pctCls(d.pct_chg)+';font-size:13px">'+fmtPct(d.pct_chg)+'</span>';
  $('rsVol').textContent=(d.volume!=null?fmtAmount(d.volume)+' / ':'—')+(d.amount!=null?fmtAmount(d.amount):'');
  const f=d.factors||{},p=d.percentiles||{};
  const items=[
    ['20 日动量',fmtPctShort(f.mom20),p.mom20_pct],
    ['20 日波动率',f.vol20!=null?(f.vol20*100).toFixed(1)+'%':'—',p.vol20_pct],
    ['5/20 日量比',fmtNum(f.vr520,2),p.vr520_pct],
    ['60 日回撤',fmtPctShort(f.dd60),p.dd60_pct],
  ];
  $('rsFactors').innerHTML=items.map(([l,v,pc])=>`<div class="rs-it"><div class="lbl">${l}</div><div class="v">${v}<span class="hint" style="font-size:11px">${pc!=null?' 分位 '+Math.round(pc)+'%':'（分位计算中）'}</span></div></div>`).join('');
  window._rsStock={code:d.code,trend:d.trend||[],klines:d.klines||[]};
  renderStockKline();
  renderTrend();
}
let kchart=null;
function renderStockKline(){
  if(!window._rsStock)return;
  const rows=window._rsStock.klines||[];
  const months=parseInt($('rsMonths').value||'3');
  const cut=rows.length-months*21;
  const k=rows.slice(Math.max(0,cut));
  if(!k.length){return}
  const dates=k.map(r=>String(r.date)),closes=k.map(r=>+r.close);
  const kd=k.map(r=>[+r.open,+r.close,+r.low,+r.high]);
  const showMA=$('rsMA').checked;
  const series=[
    {name:'K线',type:'candlestick',data:kd,
     itemStyle:{color:'#F87171',color0:'#4ADE80',borderColor:'#F87171',borderColor0:'#4ADE80'}},
    {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,data:k.map(r=>r.volume||0),itemStyle:{color:'#38BDF8'}}
  ];
  if(showMA){
    series.push({name:'MA5',type:'line',data:ma(closes,5),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#FBBF24'}});
    series.push({name:'MA20',type:'line',data:ma(closes,20),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#A78BFA'}});
    series.push({name:'MA60',type:'line',data:ma(closes,60),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#8FA2BC'}});
  }
  if(!kchart)kchart=echarts.init($('kchart'),'dark');
  kchart.setOption({
    backgroundColor:'transparent',
    title:{text:window._rsStock.code+' 日K（不复权）',left:8,top:4,textStyle:{fontSize:13,color:'#8FA2BC'}},
    tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
    legend:{right:8,top:4,textStyle:{color:'#8FA2BC',fontSize:11}},
    grid:[{left:56,right:14,top:34,height:'60%'},{left:56,right:14,top:'74%',height:'18%'}],
    xAxis:[{type:'category',data:dates,axisLine:{lineStyle:{color:'#1E2C45'}}},
           {type:'category',gridIndex:1,data:dates,axisLabel:{show:false},axisLine:{lineStyle:{color:'#1E2C45'}}}],
    yAxis:[{scale:true,splitLine:{lineStyle:{color:'#162238'}}},
           {gridIndex:1,splitNumber:2,axisLabel:{show:false},splitLine:{show:false}}],
    dataZoom:[{type:'inside',xAxisIndex:[0,1],start:30,end:100}],
    series
  });
  // 容器从 display:none 变为可见后必须显式 resize，否则画布停留在初始化时的 100px 宽
  kchart.resize();
}
let _tchart=null;
function renderTrend(){
  if(!window._rsStock)return;
  const t=window._rsStock.trend||[];
  if(!t.length)return;
  const dates=t.map(x=>String(x.date).slice(4));
  if(!_tchart)_tchart=echarts.init($('tchart'),'dark');
  _tchart.setOption({
    backgroundColor:'transparent',
    tooltip:{trigger:'axis'},
    legend:{right:4,top:0,textStyle:{color:'#8FA2BC',fontSize:11}},
    grid:{left:44,right:12,top:26,bottom:22},
    xAxis:{type:'category',data:dates,axisLabel:{color:'#8FA2BC',fontSize:10}},
    yAxis:[{type:'value',scale:true,axisLabel:{color:'#8FA2BC',fontSize:10},splitLine:{lineStyle:{color:'#162238'}}},
           {type:'value',scale:true,axisLabel:{color:'#8FA2BC',fontSize:10},splitLine:{show:false}}],
    series:[
      {name:'20日动量',type:'line',data:t.map(x=>x.mom20),showSymbol:false,lineStyle:{width:1.5,color:'#38BDF8'}},
      {name:'20日波动率',type:'line',yAxisIndex:1,data:t.map(x=>x.vol20),showSymbol:false,lineStyle:{width:1.5,color:'#F59E0B'}}
    ]
  });
  // 容器从 display:none 变为可见后必须显式 resize，否则画布停留在初始化时的 100px 宽
  _tchart.resize();
}
function ma(data,n){return data.map((v,i)=>i<n-1?null:+((data.slice(i-n+1,i+1).reduce((a,b)=>a+b,0)/n).toFixed(3)))}

function fmtUptime(iso){
  try{
    const s=new Date(iso),t=Date.now();
    if(isNaN(s))return iso;
    let sec=Math.floor((t-s.getTime())/1000);if(sec<0)sec=0;
    const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60);
    return (d>0?d+' 天 ':'')+(h>0?h+' 小时 ':'')+m+' 分钟';
  }catch(e){return iso}
}
async function restartContainer(){
  if(!confirm('确定重启 stockdb 容器？重启期间行情服务会短暂中断。')){$('containerMsg').textContent='已取消';return}
  try{const r=await j('/api/container/restart',{method:'POST'});$('containerMsg').textContent=r.msg||'已执行';toast(r.msg||'已执行')}
  catch(e){$('containerMsg').textContent='重启失败: '+e}
}
async function toggleContainerLogs(){
  const pre=$('containerLog');
  if(pre.style.display!=='none'){pre.style.display='none';return}
  pre.style.display='block';pre.textContent='（加载中…）';
  try{
    const r=await j('/api/container/logs?tail=150');
    pre.textContent=r.log||'（容器无日志输出）';
    if(r.error)pre.textContent+='\n\n'+r.error;
    $('containerMsg').textContent='';
  }catch(e){pre.textContent='读取失败: '+e}
}
setInterval(()=>{if($('syncProgress')&&$('syncProgress').style.display==='block'&&window._syncStarted){$('progElapsed').textContent='已运行 '+fmtDur(Date.now()/1000-window._syncStarted)}},1000);
window.addEventListener('resize',()=>{
  if(window._wchart1)window._wchart1.resize();
  if(window._wchart2)window._wchart2.resize();
  if(kchart)kchart.resize();
  if(_tchart)_tchart.resize();
});
refresh();setInterval(()=>refresh(),4000);
</script></body></html>"""


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
            if path.startswith("/static/"):
                self._static(path)
            elif path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._status()
            elif path == "/api/history":
                self._history()
            elif path == "/api/schedule":
                self._schedule()
            elif path == "/api/klines":
                self._klines()
            elif path == "/api/health":
                self._health()
            elif path == "/api/watchlist":
                self._watchlist()
            elif path == "/api/log":
                self._log()
            elif path == "/api/query":
                self._query()
            elif path == "/api/container/logs":
                self._container_logs()
            elif path == "/api/research/status":
                self._research_status()
            elif path == "/api/research/market":
                self._research_market()
            elif path == "/api/research/factors":
                self._research_factors()
            elif path == "/api/research/stock":
                self._research_stock()
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}))

    def _static(self, path: str):
        """服务本地静态文件（如 /static/echarts.min.js），离线可用。"""
        name = path.split("/static/", 1)[-1]
        if "/" in name or ".." in name:
            self._send(403, "forbidden")
            return
        f = Path(__file__).parent / "static" / name
        if not f.is_file():
            self._send(404, "not found")
            return
        ctype = "application/javascript; charset=utf-8" if name.endswith(".js") else "application/octet-stream"
        self._send(200, f.read_bytes().decode("utf-8", "replace"), ctype)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/sync":
                self._sync()
            elif path == "/api/container/restart":
                self._container_restart()
            elif path == "/api/research/rebuild":
                self._research_rebuild()
            else:
                self._send(404, json.dumps({"error": "not found"}))
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
            "research_db": research_db_status(),
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

    def _klines(self):
        q = parse_qs(urlparse(self.path).query)
        code = q.get("code", [""])[0]
        freq = q.get("freq", ["day"])[0]
        months = int(q.get("months", ["3"])[0]) or 3
        adj = q.get("adj", ["none"])[0]  # none/qfq/hfq
        if not code:
            self._send(400, "missing code")
            return
        try:
            if freq == "day":
                end = datetime.now().strftime("%Y%m%d")
                y, m = datetime.now().year, datetime.now().month
                m -= months - 1
                while m <= 0:
                    m += 12
                    y -= 1
                start = f"{y:04d}{m:02d}01"
                rows = kline_range(code, "day", start, end)
                if adj in ("qfq", "hfq"):
                    rows = apply_adjust(rows, _adjust_map(code), adj)
            else:  # minute
                end = datetime.now().strftime("%Y%m%d%H%M%S")
                start = (datetime.now().replace(hour=9, minute=30, second=0) - timedelta(days=1)).strftime("%Y%m%d%H%M%S")
                rows = kline_range(code, "minute", start, end)
            self._send(200, json.dumps({"code": code, "freq": freq, "rows": rows},
                                       ensure_ascii=False))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))

    def _health(self):
        self._send(200, json.dumps(health_status(), ensure_ascii=False))

    # ---- 市场研究 API ----
    def _research_status(self):
        self._send(200, json.dumps(research_status(), ensure_ascii=False))

    def _research_market(self):
        self._send(200, json.dumps(research_market(), ensure_ascii=False))

    def _research_factors(self):
        q = parse_qs(urlparse(self.path).query)
        factor = q.get("factor", ["mom20"])[0]
        scope = q.get("scope", ["stock"])[0]
        order = q.get("order", ["desc"])[0]
        limit = q.get("limit", ["50"])[0]
        search = q.get("q", [""])[0]
        try:
            limit_i = int(limit)
        except (TypeError, ValueError):
            limit_i = 50
        result = research_factors(factor, scope, order, limit_i, search)
        code = 200 if "error" not in result else 400
        self._send(code, json.dumps(result, ensure_ascii=False))

    def _research_stock(self):
        q = parse_qs(urlparse(self.path).query)
        code = q.get("code", [""])[0].strip()
        if not code or not (code.isdigit() and len(code) == 6):
            self._send(400, json.dumps({"error": "缺少或非法 code（需 6 位数字）"}))
            return
        # 个股研究：直连 stockdb 实时查询（单股，快），不依赖全市场缓存
        import urllib.request, urllib.parse
        from datetime import datetime as _dt, timedelta as _td
        today = _dt.now()
        months = _month_prefixes((today - _td(days=190)).strftime("%Y%m%d"),
                                 today.strftime("%Y%m%d"))
        rows = []
        for prefix in months:
            try:
                url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'日k:{code}:{prefix}*')}"
                with urllib.request.urlopen(url, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8", "replace"))
                rows.extend(r for r in data if isinstance(r, dict))
            except Exception:
                continue
        if not rows:
            self._send(404, json.dumps({"error": f"未找到 {code} 的日K数据"}, ensure_ascii=False))
            return
        rows.sort(key=_bar_date)
        bars = rows[-140:]  # 最近约 7 个月，足够 60 日回撤
        factors = compute_factors(bars)
        name = _stock_name(bars, code)
        close = bars[-1].get("close")
        pct = bars[-1].get("pct_chg")
        latest_date = _bar_date(bars[-1])
        # 最新日K明细（K线图表用）
        krows = [{"date": b.get("date"), "open": b.get("open"), "close": b.get("close"),
                  "high": b.get("high"), "low": b.get("low"), "volume": b.get("volume")}
                 for b in bars]
        # 因子走势（近60日 20日动量 / 20日波动率）
        trend = []
        for i in range(max(0, len(bars) - 60), len(bars)):
            seg = bars[:i + 1]
            trend.append({"date": bars[i].get("date"),
                          "mom20": _factor_mom20(seg), "vol20": _factor_vol20(seg)})
        # 全市场分位（读因子缓存；构建中则返回 None）
        snap = _load_snapshot("factor_snapshot_")
        pcts = {}
        if snap:
            for r in snap["codes"]:
                if r.get("code") == code:
                    pcts = {"mom20_pct": r.get("mom20_pct"), "vol20_pct": r.get("vol20_pct"),
                            "vr520_pct": r.get("vr520_pct"), "dd60_pct": r.get("dd60_pct")}
                    break
        self._send(200, json.dumps({
            "code": code, "name": name, "date": str(latest_date), "close": close,
            "pct_chg": pct, "volume": bars[-1].get("volume"),
            "amount": bars[-1].get("amount"),
            "factors": factors, "percentiles": pcts,
            "klines": krows, "trend": trend,
        }, ensure_ascii=False))

    def _research_rebuild(self):
        if _research_build["state"] == "building":
            self._send(200, json.dumps({"msg": "正在计算中，已忽略重复请求"}))
            return
        started = _start_research_build()
        self._send(200, json.dumps({"msg": "已启动重新计算" if started else "计算任务已在运行中"}))

    def _watchlist(self):
        q = parse_qs(urlparse(self.path).query)
        action = q.get("action", [""])[0]
        if action == "set":
            codes_raw = q.get("codes", [""])[0]
            codes = [c for c in codes_raw.split(",") if c.strip()]
            codes = save_watchlist(codes)
            self._send(200, json.dumps({"codes": codes}, ensure_ascii=False))
            return
        codes = load_watchlist()
        quotes = latest_quotes(codes)
        self._send(200, json.dumps({"codes": codes, "quotes": quotes,
                                    "indices": index_snapshot()}, ensure_ascii=False))

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
        """stockdb 容器日志尾部（系统页查看用）。无 docker 时降级提示。"""
        tail = int(parse_qs(urlparse(self.path).query).get("tail", ["150"])[0])
        try:
            text = container_logs(tail)
            self._send(200, json.dumps({"log": text}, ensure_ascii=False))
        except FileNotFoundError:
            self._send(200, json.dumps({"log": "", "error": "docker socket 未挂载，无法读取容器日志" }, ensure_ascii=False))
        except Exception as exc:
            self._send(200, json.dumps({"log": "", "error": f"读取失败: {exc}"}, ensure_ascii=False))

    def _container_restart(self):
        """重启 stockdb 容器（系统页按钮，前端已二次确认）。"""
        if _sync_state["running"]:
            self._send(200, json.dumps({"msg": "同步进行中，请勿重启容器"}))
            return
        try:
            container_restart()
            self._send(200, json.dumps({"msg": "已发送重启命令，容器状态将自动刷新"}))
        except FileNotFoundError:
            self._send(200, json.dumps({"msg": "docker socket 未挂载，无法重启容器（本地开发模式不可用）"}))
        except Exception as exc:
            self._send(200, json.dumps({"msg": f"重启失败: {exc}"}))


def main():
    print(f"webui listening on 0.0.0.0:{LISTEN_PORT}", file=sys.stderr)
    print(f"stockdb: {STOCKDB_HOST}:{STOCKDB_PORT} | container: {STOCKDB_CONTAINER} | data: {DATA_DIR}",
          file=sys.stderr)
    ensure_research_db()
    migrate_legacy_market_snapshot()
    print(f"research db: {RESEARCH_DB_PATH}", file=sys.stderr)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    threading.Thread(target=research_check_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
