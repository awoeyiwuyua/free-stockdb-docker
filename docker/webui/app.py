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

import http.client
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

# 只读 MCP（stockdb-native）dispatch：HTTP POST /mcp 复用（纯标准库，随 webui 同目录 mcp/ 分发）。
# 缺失/加载失败时 webui 其余功能不受影响，/mcp 路由返回 500。
try:
    from mcp.stockdb_mcp_server import dispatch as mcp_dispatch
except Exception:  # noqa: BLE001 - MCP 模块缺失时优雅降级
    mcp_dispatch = None

STOCKDB_HOST = os.environ.get("STOCKDB_HOST", "127.0.0.1")
STOCKDB_PORT = int(os.environ.get("STOCKDB_PORT", "7899"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SYNC_LOG = DATA_DIR / "sync.log"
LISTEN_PORT = int(os.environ.get("WEBUI_PORT", "8080"))

# 0.5.0 单镜像：stockdb 与 webui 同容器，进程级控制（不再依赖 docker socket）。
# entrypoint 后台监督 stockdb 进程存活（读 pidfile，暂停标记存在时不拉起）；
# webui 通过 pidfile + SIGTERM 停、删除暂停标记让监督器拉起。
STOCKDB_PIDFILE = Path(os.environ.get("STOCKDB_PIDFILE", "/data/stockdb.pid"))
STOCKDB_PAUSE = Path(os.environ.get("STOCKDB_PAUSE_FLAG", "/data/.stockdb-paused"))
STOCKDB_LOG_FILE = Path(os.environ.get("STOCKDB_LOG_FILE", "/data/log.txt"))

# 同步线程状态
_sync_lock = threading.Lock()
_sync_state = {"running": False, "exit_code": None, "last_start": None, "last_end": None,
               "phase": "idle"}  # phase: idle/stopping/syncing/verifying/restarting/done
_last_sync_stdout: str = ""          # 最近一次同步的 stdout（供解析下载/删除数）
_last_verify_result: str | None = None  # 最近一次完整性验证结果（pass/fail/跳过）
_scheduler_alive = False             # 定时线程心跳（每次循环更新时间戳）
_scheduler_heartbeat = 0.0           # 定时线程最近一次心跳时间戳（unix）
_webui_started = time.time()         # webui 进程启动时间戳
WEBUI_VERSION = "0.5.0"

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
    if not force and _latest_date_cache["val"] is not None and now - _latest_date_cache["at"] < 8:
        return _latest_date_cache["val"]
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
    result = max(dates) if dates else None
    _latest_date_cache.update(at=now, val=result)
    return result


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


# ==================== mydb 私有存储写入（pybao 客户端） ====================
# 上游 stockdb 内置私有存储 ./mydb：HTTP 层只读，写入须用 pybao 客户端
# （stockdb.abi3.so + stock_sdk.py，随发行包分发，容器内 PYTHONPATH 注入）。
# 本机开发若未装 pybao，相关接口优雅降级（A 股功能不受影响）。

# 保留表前缀：禁止覆盖上游同步数据，防止与 A 股行情冲突
_RESERVED_TABLES = ("日k", "分钟k", "复权", "股票代码", "周k", "月k", "板块", "行业", "概念")
_HK_TABLE = "hk日k"  # 港股日K自定义表（与上游命名空间隔离）


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


def _mydb_import():
    """惰性导入 pybao 客户端。未安装/加载失败时抛 ImportError（调用方降级）。

    候选路径：容器内 /opt/stockdb/pybao，本地开发 /tmp/pybao_mac 等。
    """
    import importlib
    candidates = ["/opt/stockdb/pybao", "/tmp/pybao_mac"]
    for p in candidates:
        try:
            sys.path.insert(0, p)
            return importlib.import_module("stockdb")
        except ImportError:
            continue
    raise ImportError("pybao 写库不可用（PYTHONPATH 未注入或平台不兼容）")


def _mydb_rd():
    """获取连接 NAS stockdb 的 pybao 客户端（惰性、缓存）。"""
    if getattr(_mydb_rd, "_rd", None) is None:
        mod = _mydb_import()
        _mydb_rd._rd = mod.init(STOCKDB_HOST, int(STOCKDB_PORT), socket_timeout=5)
    return _mydb_rd._rd


def validate_custom_table(table: str) -> str:
    """校验自定义表名：禁止覆盖上游保留表，禁止危险字符。返回规范化表名。"""
    t = str(table or "").strip().strip(":")
    if not t:
        raise ValueError("表名不能为空")
    if not all(ch.isalnum() or ch in "_:-" for ch in t):
        raise ValueError("表名只能含字母数字与 _:-")
    for r in _RESERVED_TABLES:
        if t == r or t.startswith(r + ":"):
            raise ValueError(f"表名 {t!r} 与上游保留表 {r!r} 冲突，请用自定义命名空间（如 hk日k: / 自定义:）")
    return t


def mydb_write(table: str, items: list[tuple], batch: bool = False) -> dict:
    """写入 mydb 私有存储。items=[(key, value), ...]。

    注意：pybao 的 rd.set 返回 QueryResult，必须调用 .do() 才真正发送写入
    （否则只是客户端排队，读不到）。batch 参数保留兼容，统一逐条 .do()。
    """
    table = validate_custom_table(table)
    if not items:
        raise ValueError("没有可写入的数据")
    rd = _mydb_rd()
    result = []
    for key, value in items:
        result.append(rd.set(table, key, value).do())
    # 回读校验（QueryResult 转原生）
    readback = []
    for key, _ in items:
        try:
            v = rd.get(table, key)
            if hasattr(v, "keys") and hasattr(v, "all"):
                v = dict(v)
            readback.append(v)
        except Exception:
            readback.append(None)
    return {"table": table, "written": len(items), "readback": readback, "result": result}


def mydb_read(table: str, key: str = "") -> dict:
    """读取 mydb 自定义表。key 为空时列出表内全部键值。"""
    table = validate_custom_table(table)
    rd = _mydb_rd()

    def _to_py(v):
        """pybao 返回值可能是 QueryResult，转原生 Python 对象。"""
        if v is None:
            return None
        if hasattr(v, "keys") and hasattr(v, "all"):
            try:
                return dict(v)
            except Exception:
                pass
        return v

    if key:
        val = _to_py(rd.get(table, key))
        return {"table": table, "key": key, "value": val}
    keys = rd.keys(table, "*") or []
    values = {}
    for k in keys:
        date_str = str(k).split(":")[-1]
        try:
            values[str(k)] = _to_py(rd.get(table, date_str))
        except Exception:
            values[str(k)] = None
    return {"table": table, "keys": keys, "values": values}


def mydb_tables() -> list[str]:
    """列出自定义表名（含保留表前缀过滤）。"""
    rd = _mydb_rd()
    keys = rd.keys("*")
    tables = set()
    for k in keys:
        table = str(k).split(":")[0] if ":" in str(k) else str(k)
        if table and not any(table.startswith(r) for r in _RESERVED_TABLES):
            tables.add(table)
    return sorted(tables)


# ==================== 港股数据（东财 + 腾讯，写入 hk日k: 表） ====================
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
            rd = _mydb_rd()
            for key, value in items:
                rd.set(_HK_TABLE, code, key, value).do()  # .do() 真正发送写入
            results[code] = {"ok": True, "bars": len(items),
                             "latest": max(r["date"] for r in rows)}
        except Exception as exc:
            results[code] = {"ok": False, "error": str(exc)[:200]}
    return results


def hk_klines(code: str) -> list[dict]:
    """读取 mydb hk日k: 表（升序）。value 内嵌 date，用 vals 全量读取。"""
    code = _normalize_hk_code(code)
    rd = _mydb_rd()
    vals = rd.vals(_HK_TABLE, code, "*") or []
    rows = []
    for v in vals:
        if hasattr(v, "keys") and hasattr(v, "all"):
            try:
                v = dict(v)
            except Exception:
                v = None
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
        _code_stats_cache.update(at=now, val=stats)
        return stats
    except Exception:
        return {"stock": None, "etf": None, "other": None, "latency_ms": None}


_coverage_cache: dict = {"at": 0.0, "data": None}  # 15 分钟缓存，避免 4s 轮询重复全历史扫描
_latest_date_cache: dict = {"at": 0.0, "val": None}  # 8 秒缓存：/api/status 4s 心跳不重复打 stockdb
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

/* 顶部导航：吸顶 + tab 均匀排列 */
.topbar{position:sticky;top:0;z-index:50;background:rgba(11,17,32,.94);backdrop-filter:blur(8px);
border-bottom:1px solid var(--line)}
.topbar-inner{max-width:1120px;margin:0 auto;padding:10px 20px}
.tabs{display:flex;gap:2px;width:100%}
.tab-btn{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);
font-size:15px;padding:8px 0;cursor:pointer;white-space:nowrap;flex:1;text-align:center}
.tab-btn.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:700}
.tab-panel{display:none;padding-top:16px}.tab-panel.active{display:block}

/* 同步页子页签 */
.sync-tabs{display:flex;gap:8px;border-bottom:1px solid var(--line);padding-bottom:10px;margin-bottom:16px}
.sync-tab{background:transparent;border:1px solid var(--line);border-radius:8px;color:var(--muted);
font-size:14px;padding:7px 18px;cursor:pointer}
.sync-tab.active{background:rgba(56,189,248,.08);border-color:rgba(56,189,248,.4);color:var(--brand);font-weight:700}
.sync-panel{display:none}.sync-panel.active{display:block}

/* 卡片 */
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:18px;margin:14px 0}
.card-title{font-size:14px;font-weight:600;margin-bottom:10px}
.k{color:var(--muted);font-size:12px;margin-bottom:2px}
.v{font-size:16px;font-weight:600}
.row{display:flex;gap:16px;flex-wrap:wrap}
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

pre{background:#0A0F1C;border:1px solid var(--line);border-radius:10px;padding:12px;
max-height:340px;overflow:auto;font:12px/1.5 ui-monospace,monospace;color:#A5D8F8}
.actions{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
.hint{color:var(--muted);font-size:12px}
input[type=text],input[type=time],select{background:#0A0F1C;border:1px solid var(--line);
color:var(--text);padding:8px 10px;border-radius:8px;width:220px}
.setting-row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:10px 0;border-bottom:1px solid var(--line)}
.setting-row:last-child{border-bottom:0}
.setting-row .lbl{font-size:13px;color:var(--text)}
.setting-row .dsc{font-size:12px;color:var(--muted)}
.times-pill{display:inline-block;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:2px 10px;font-size:13px;margin:2px 4px 2px 0}
</style></head><body>
<div class="topbar"><div class="topbar-inner">
  <nav class="tabs">
    <button class="tab-btn active" data-tab="sync" onclick="showTab('sync',this)">数据同步</button>
    <button class="tab-btn" data-tab="system" onclick="showTab('system',this)">系统</button>
  </nav>
</div></div>
<div class="wrap">
<div id="toast"></div>

<!-- 数据同步：数据源同步 + 私有存储同步（两个子页签） -->
<div id="tab-sync" class="tab-panel">
  <div class="sync-tabs">
    <button class="sync-tab active" data-sync-tab="source" onclick="showSyncTab('source',this)">数据源同步</button>
    <button class="sync-tab" data-sync-tab="mydb" onclick="showSyncTab('mydb',this)">私有存储同步（手动）</button>
  </div>

  <!-- 子页签1：数据源同步（A股行情增量同步） -->
  <div id="sync-source" class="sync-panel active">
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

  <!-- 子页签2：私有存储同步（手动）——港股拉取 + AI 写入接口 -->
  <div id="sync-mydb" class="sync-panel">
    <div class="card"><div class="card-title">港股同步 <span class="hint" style="font-weight:normal">（拉取日K写入 hk日k 表 · 手动）</span></div>
      <div class="hint" style="margin-bottom:8px">输入港股代码（如 00700 腾讯控股）拉取指定时间范围的日K写入私有存储，供个股/ETF研究页查询。手动点击触发。</div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input type="text" id="hkCodes" placeholder="港股代码，逗号分隔，如 00700,00941" style="width:280px">
        <select id="hkYears">
          <option value="1">近1年</option>
          <option value="2" selected>近2年</option>
          <option value="3">近3年</option>
          <option value="5">近5年</option>
          <option value="10">近10年</option>
        </select>
        <button class="btn-ghost" onclick="hkSync()">拉取港股</button>
        <span class="hint" id="hkMsg"></span>
      </div>
    </div>
    <div class="card"><div class="card-title">私有数据写入 <span class="hint" style="font-weight:normal">（AI 接口 · 供扩展）</span></div>
      <div class="hint" style="margin-bottom:8px">写入自定义表（如 <code>hk日k</code>、<code>自定义指标</code>），表名不可与上游保留表（日k/复权/分钟k 等）冲突。该接口预留扩展为 AI agent 介入的写入通道，支持单条或 JSON 批量。</div>
      <div style="display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap">
        <div style="display:flex;flex-direction:column;gap:6px;flex:1;min-width:260px">
          <input type="text" id="dwTable" placeholder="表名，如 自定义指标" style="width:220px">
          <textarea id="dwPayload" rows="4" placeholder='单条：{"key":"600633","value":{"score":85}}\n批量：{"items":[["000001",{"score":70}],["000967",{"score":80}]]}' style="width:100%;font-family:monospace;font-size:12px"></textarea>
          <div style="display:flex;gap:8px;align-items:center">
            <button class="btn-ghost" onclick="dataWrite()">写入</button>
            <button class="btn-ghost" onclick="dataTables()">查看表</button>
            <span class="hint" id="dwMsg"></span>
          </div>
        </div>
        <div id="dwTables" class="hint" style="max-height:140px;overflow:auto;flex:1;min-width:200px"></div>
      </div>
    </div>
    <div class="card"><div class="card-title">说明</div>
      <div class="hint" style="line-height:1.8">
        · <b>数据源同步</b>：从镜像源增量同步 A 股行情（日K/分钟K/复权），写入 ./data，自动定时或手动触发。<br>
        · <b>私有存储同步（手动）</b>：港股日K与自定义数据写入私有存储 ./mydb（与上游 ./data 物理隔离，互不冲突）。<br>
        · 港股数据源：东财/腾讯公网接口；表名以 <code>hk日k</code> / <code>自定义:</code> 等命名空间隔离，不覆盖上游保留表。<br>
        · <b>AI 接口</b>：私有数据写入 API（<code>POST /api/data/write</code>）预留为 AI agent 介入的扩展点，后续可对接自动数据接入。
      </div>
    </div>
  </div>
</div>

<!-- 系统：健康检查面板 -->
<div id="tab-system" class="tab-panel">
  <div class="card"><div class="card-title">系统健康检查</div>
    <div id="sysOK" style="display:flex;align-items:center;gap:8px;font-weight:700;font-size:16px;margin-bottom:4px">…</div>
    <div class="hc-cards">
      <div class="hc"><div class="lbl">行情服务</div><div class="st"><i id="hcSvcDot"></i><span id="hcSvc">…</span></div><div class="sub" id="hcSvcSub">…</div></div>
      <div class="hc"><div class="lbl">stockdb</div><div class="st"><i id="hcDkrDot"></i><span id="hcDkr">…</span></div><div class="sub" id="hcDkrSub">…</div></div>
      <div class="hc"><div class="lbl">自动任务</div><div class="st"><i id="hcSchedDot"></i><span id="hcSched">…</span></div><div class="sub" id="hcSchedSub">…</div></div>
      <div class="hc"><div class="lbl">同步能力</div><div class="st"><i id="hcCapDot"></i><span id="hcCap">…</span></div><div class="sub" id="hcCapSub">…</div></div>
    </div>
    <div id="dkrWarn" class="warn-card" style="display:none">
      <div class="t">stockdb 进程不可控</div>
      <div class="d">未检测到 stockdb 进程（pidfile 不存在）。行情查询仍可使用，但无法重启 stockdb 或执行停服同步。</div>
    </div>
  </div>
  <div class="card"><div class="card-title">存储空间</div>
    <div id="cDisk" style="font-size:15px;font-weight:600">…</div>
    <div class="storage-bar"><i id="diskBar"></i></div>
    <div class="hint" style="margin-top:6px">挂载点 /data（数据卷）</div>
  </div>
  <div class="card"><div class="card-title">运行信息</div>
    <div class="info-grid">
      <div class="it"><span class="lk">数据卷</span><span id="cImage">—</span></div>
      <div class="it"><span class="lk">stockdb 进程</span><span id="cState">—</span></div>
      <div class="it"><span class="lk">进程时长</span><span id="cUptime">—</span></div>
      <div class="it"><span class="lk">同步节点</span><span id="cSource">—</span></div>
      <div class="it"><span class="lk">WebUI 版本</span><span id="cVer">—</span></div>
      <div class="it"><span class="lk">WebUI 启动</span><span id="cStart">—</span></div>
      <div class="it"><span class="lk">调度心跳</span><span id="cHeartbeat">—</span></div>
      <div class="it"><span class="lk">数据目录</span><span id="cDataDir">—</span></div>
      <div class="it"><span class="lk">交易日历</span><span id="cCalendar">—</span></div>
      <div class="it"><span class="lk">最近同步</span><span id="cLastSyncInfo">—</span></div>
    </div>
  </div>
  <div class="card"><div class="card-title">运维工具</div>
    <div class="actions">
      <button class="btn-ghost" id="btnLogs" disabled onclick="toggleContainerLogs()">查看 stockdb 日志</button>
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
  refresh(true); // 切页立即按当前页刷新
}
function showSyncTab(name,btn){
  document.querySelectorAll('.sync-tab').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.sync-panel').forEach(p=>p.classList.remove('active'));
  (btn||document.querySelector('[data-sync-tab="'+name+'"]')).classList.add('active');
  $('sync-'+name).classList.add('active');
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
    const active=document.querySelector('.tab-panel.active')?.id||'tab-sync';
    if(active==='tab-sync'){
      // 同步页：进度/日志需精确 → 每帧全量 status+health（后端 TTL 缓存后开销大降）
      const s=await j('/api/status');
      const h=await j('/api/health');
      if(h)_healthCache=h;
      renderHero(s,h);
      renderSchView(s.schedule||{});
      renderDataOverview(s);
      const sched=s.schedule||{};
      $('schToday').textContent=(sched.enabled&&sched.trading_only)?(s.trading_today?'':'今日非交易日，定时跳过'):'';
      loadHistory();
      const lg=await j('/api/log?n=80');
      $('log').textContent=lg.log;   // 日志常展开（HTML 无 display:none，无需折叠逻辑）
    }else if(active==='tab-system'){
      // 系统页：健康卡需实时 → 每帧全量 status+health（后端缓存后已可接受）
      const s=await j('/api/status');
      const h=await j('/api/health');
      if(h)_healthCache=h;
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
  // stockdb 进程（单镜像同容器，进程级控制）
  $('hcDkrDot').style.background=cs.ok?'var(--ok)':'var(--err)';
  $('hcDkr').textContent=cs.ok?'运行中':'已停止';
  $('hcDkrSub').textContent=cs.ok?('进程 '+cs.status):'stockdb 进程未运行';
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
  // stockdb 不可控警告卡 + 运维按钮禁用
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
  $('cImage').textContent=s.data_dir||'—';
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

async function doQuery(){
  const q=$('qtype').value;
  const r=await fetch('/api/query?t='+encodeURIComponent(q));
  $('qres').textContent=await r.text();
}

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
  if(!confirm('确定重启 stockdb 进程？重启期间行情服务会短暂中断。')){$('containerMsg').textContent='已取消';return}
  try{const r=await j('/api/container/restart',{method:'POST'});$('containerMsg').textContent=r.msg||'已执行';toast(r.msg||'已执行')}
  catch(e){$('containerMsg').textContent='重启失败: '+e}
}
// ---- 数据写入（mydb 私有存储，手动触发） ----
function parsePayload(text){
  // 支持单条 {key,value} 或批量 {items:[[k,v],...]}
  const t=(text||'').trim();
  if(!t)throw new Error('请输入 JSON');
  const obj=JSON.parse(t);
  if(obj.items)return {items:obj.items};
  if(obj.key!==undefined)return {key:String(obj.key),value:obj.value};
  throw new Error('格式需为 {"key":"...","value":...} 或 {"items":[["k",v],...]}');
}
async function dataWrite(){
  const table=$('dwTable').value.trim();
  if(!table){$('dwMsg').textContent='请输入表名';return}
  let payload;
  try{payload=parsePayload($('dwPayload').value)}catch(e){$('dwMsg').textContent=e.message;return}
  $('dwMsg').textContent='写入中…';
  try{
    const r=await j('/api/data/write',{method:'POST',body:JSON.stringify({table,...payload})});
    $('dwMsg').textContent=r.msg||('写入 '+r.written+' 条');
    toast(r.msg||'写入完成');
    dataTables();
  }catch(e){$('dwMsg').textContent='写入失败: '+e}
}
async function dataTables(){
  try{
    const r=await j('/api/data/tables');
    const ts=r.tables||[];
    const el=$('dwTables');
    if(r.error){el.textContent=r.error;return}
    el.innerHTML=ts.length?('自定义表：<br>'+ts.map(t=>'<div style="margin:2px 0">• '+esc(t)+'</div>').join('')):'（无自定义表）';
  }catch(e){$('dwTables').textContent='读取失败: '+e}
}
async function hkSync(){
  const codes=$('hkCodes').value.trim();
  if(!codes){$('hkMsg').textContent='请输入港股代码';return}
  const list=codes.split(/[,，\s]+/).filter(Boolean);
  const years=parseInt($('hkYears').value||'2',10);
  $('hkMsg').textContent='拉取中…';
  try{
    const r=await j('/api/hk/sync',{method:'POST',body:JSON.stringify({codes:list,years})});
    const parts=Object.entries(r).map(([c,v])=>c+' '+(v.ok?('✓ '+v.bars+'根'):('✗ '+v.error))).join('；');
    $('hkMsg').textContent=parts;
    toast('港股同步完成');
  }catch(e){$('hkMsg').textContent='拉取失败: '+e}
}
async function toggleContainerLogs(){
  const pre=$('containerLog');
  if(pre.style.display!=='none'){pre.style.display='none';return}
  pre.style.display='block';pre.textContent='（加载中…）';
  try{
    const r=await j('/api/container/logs?tail=150');
    pre.textContent=r.log||'（stockdb 日志为空）';
    if(r.error)pre.textContent+='\n\n'+r.error;
    $('containerMsg').textContent='';
  }catch(e){pre.textContent='读取失败: '+e}
}
setInterval(()=>{if($('syncProgress')&&$('syncProgress').style.display==='block'&&window._syncStarted){$('progElapsed').textContent='已运行 '+fmtDur(Date.now()/1000-window._syncStarted)}},1000);
// 输入框 Enter 快捷触发（页面无 <form>，原生 Enter 无动作）：港股同步输入框回车即拉取
document.addEventListener('keydown',e=>{
  if(e.key==='Enter'&&e.target&&e.target.id==='hkCodes'){e.preventDefault();hkSync()}
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
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._status()
            elif path == "/api/history":
                self._history()
            elif path == "/api/schedule":
                self._schedule()
            elif path == "/api/health":
                self._health()
            elif path == "/api/log":
                self._log()
            elif path == "/api/query":
                self._query()
            elif path == "/api/container/logs":
                self._container_logs()
            elif path == "/api/data/tables":
                self._data_tables()
            elif path == "/api/data/read":
                self._data_read()
            elif path == "/api/hk/sync":
                self._hk_sync()
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}))

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/sync":
                self._sync()
            elif path == "/api/container/restart":
                self._container_restart()
            elif path == "/api/data/write":
                self._data_write()
            elif path == "/api/hk/sync":
                self._hk_sync()
            elif path == "/mcp":
                self._mcp()
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

    def _mcp(self):
        """只读 MCP JSON-RPC 端点：复用 stockdb_mcp_server.dispatch（与 stdio 同协议）。

        单请求单响应：请求 dict → JSON-RPC 响应体；通知（无 id）返回 202 空体。
        """
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
        try:
            response = mcp_dispatch(msg)
        except Exception as exc:  # noqa: BLE001 - 分发异常转为 JSON-RPC internal error
            self._send(200, json.dumps({
                "jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }))
            return
        if response is None:
            # 通知：无 JSON-RPC 响应，202 空体
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self._send(200, json.dumps(response, ensure_ascii=False))

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


def main():
    print(f"webui listening on 0.0.0.0:{LISTEN_PORT}", file=sys.stderr)
    print(f"stockdb: {STOCKDB_HOST}:{STOCKDB_PORT}（同容器进程）| data: {DATA_DIR}", file=sys.stderr)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
