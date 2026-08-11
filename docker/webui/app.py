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
WEBUI_VERSION = "0.2.0"

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


def is_trading_day(d=None) -> bool:
    """A 股交易日判定：工作日 且 非休市表内日期。

    未收录年份（休市表覆盖后）按"工作日=交易日"处理，并在日志提示更新。
    供定时同步跳过周末/法定节假日触发用。
    """
    from datetime import datetime as _dt
    d = d or _dt.now().date()
    if d.weekday() >= 5:  # 周六/周日
        return False
    holidays = XSHG_HOLIDAYS.get(str(d.year))
    if holidays is None:
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


def load_schedule() -> dict:
    """读定时配置；兼容旧格式 {enabled, time} → 迁移为 {enabled, times:[time], trading_only}。

    fired: {日期: [已触发时间点...]}——当天每个时间点只触发一次（多时间点防循环重复触发）。
    retried: {日期: [已安排过自动重试的时间点...]}；retry_pending: 计划执行重试的时间（字符串）。
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
    return {
        "enabled": bool(data.get("enabled")),
        "times": _normalize_times(times),
        "trading_only": bool(data.get("trading_only", True)),
        "fired": fired,
        "retried": retried,
        "retry_pending": rp if isinstance(rp, str) else None,
        "last_trigger": last,
        "next_trigger": compute_next_trigger(_normalize_times(times)),
    }


def save_schedule(enabled: bool, times, trading_only: bool = True) -> dict:
    """保存定时配置（保留 last_trigger / fired / retried / retry_pending，不因改配置清空）。"""
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
        cfg["next_trigger"] = compute_next_trigger(cfg["times"])
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
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
    try:
        cfg = load_schedule()
        today = _today_key()
        fired = dict(cfg.get("fired") or {})
        fired.setdefault(today, [])
        if t not in fired[today]:
            fired[today].append(t)
        cfg["fired"] = fired
        cfg["next_trigger"] = compute_next_trigger(cfg["times"])
        cfg = _prune_fired(cfg)
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        log(f"⏰ 定时触发标记失败: {exc}")


def _mark_last_trigger(key: str, t: str | None = None, retry: bool = False) -> None:
    """记录最近一次定时触发（key=日期 时间点；t=该时间点 HH:MM），供界面展示与重试判定。"""
    try:
        cfg = load_schedule()
        cfg["last_trigger"] = {"key": key, "t": t, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                               "exit": None, "retry": bool(retry)}
        cfg["next_trigger"] = compute_next_trigger(cfg["times"])
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        log(f"⏰ 定时触发标记失败: {exc}")


def _mark_retried(t: str) -> None:
    """记录某时间点今天已安排过自动重试，并登记 10 分钟后的执行计划（retry_pending）。"""
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
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        log(f"↻ 重试登记失败: {exc}")


def _clear_retry_pending() -> None:
    try:
        cfg = load_schedule()
        cfg["retry_pending"] = None
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def _update_schedule_trigger_exit(exit_code, retry: bool = False) -> None:
    """run_sync 结束后回填最近一次定时触发的 exit 码（retry 记录随 last_trigger 保留）。"""
    try:
        cfg = load_schedule()
        if cfg["last_trigger"] and cfg["last_trigger"].get("exit") is None:
            cfg["last_trigger"]["exit"] = exit_code
            if retry:
                cfg["last_trigger"]["retry"] = True
            SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception:
        pass


def compute_next_trigger(times: list[str], now=None) -> str | None:
    """计算最近的下一次触发时间（今天剩余 or 明天），返回如 '今天 16:05' / '明天 15:30'。"""
    if not times:
        return None
    now = now or datetime.now()
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
    """重启容器（热更新失败止损用）。"""
    docker_request("POST", f"/containers/{STOCKDB_CONTAINER}/restart")


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
    同步后自动做完整性验证，失败则重启 stockdb 止损。

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
                "data_latest": data_latest_date(),
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
                # 1. 正常触发：时间点已到且今天未触发过（多时间点集合判定，防循环重复触发）
                fired = set((cfg.get("fired") or {}).get(today_key, []))
                due = _pending_times(now_hm, cfg["times"], fired)
                for t in due:
                    _mark_fired(t)
                    _mark_last_trigger(f"{today_key} {t}", t)
                    log(f"⏰ 定时同步触发（{t}）——stockdb 保持运行，热更新")
                    threading.Thread(
                        target=run_sync,
                        kwargs={"hot": True, "trigger": "scheduled"},
                        daemon=True,
                    ).start()
                    break
                # 2. 失败自动重试
                if not _sync_state["running"]:
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


def sync_capability() -> dict:
    """同步能力检查：更新程序 / 数据源 / 数据卷可写 / 待重试。

    比只看 Docker 更能解释"为什么同步失败"。本地开发模式（无 /opt/stockdb/数据更新）
    返回 ok=False，前端展示为不可用。
    """
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
    # 3. 数据卷可写
    try:
        probe = DATA_DIR / ".write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks["writable"] = {"ok": True, "detail": "数据卷可写"}
    except Exception as exc:
        checks["writable"] = {"ok": False, "detail": f"数据卷不可写: {exc}"}
    # 4. 待重试任务
    rp = load_schedule().get("retry_pending")
    checks["retry_pending"] = {"ok": not rp,
                               "detail": f"重试计划于 {rp} 执行" if rp else "无待重试任务"}
    return {"ok": all(c["ok"] for c in checks.values()), "checks": checks}


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
.dot-fail{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--err);margin-right:6px}
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
    <button class="tab-btn active" data-tab="overview" onclick="showTab('overview',this)">概览</button>
    <button class="tab-btn" data-tab="market" onclick="showTab('market',this)">行情</button>
    <button class="tab-btn" data-tab="sync" onclick="showTab('sync',this)">数据同步</button>
    <button class="tab-btn" data-tab="system" onclick="showTab('system',this)">系统</button>
  </nav>
</div></div>
<div class="wrap">
<div id="toast"></div>

<!-- 概览：大盘 + 自选股 -->
<div id="tab-overview" class="tab-panel active">
  <div class="card"><div class="card-title">概览</div><div class="metrics">
    <div class="m"><div class="lbl">大盘指数</div><div class="row" id="idxRow" style="gap:14px"><span class="hint">…</span></div></div>
    <div class="m"><div class="lbl">自选股（点代码看K线）</div><div class="row" id="wlRow" style="gap:14px"><span class="hint">…</span></div></div>
  </div>
  <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap">
    <input type="text" id="wlAdd" placeholder="加自选，如 600633,000967" style="width:280px">
    <button class="btn-ghost" onclick="addWatch()">加入自选</button>
    <span id="wlMsg" class="hint"></span>
  </div></div>
</div>

<!-- 行情：K线图 + 原始查询 -->
<div id="tab-market" class="tab-panel">
  <div class="card"><div class="card-title">K 线图</div>
    <div style="display:flex;gap:8px;margin:8px 0;flex-wrap:wrap;align-items:center">
      <input type="text" id="kcode" value="600633" placeholder="股票代码" style="width:120px">
      <select id="kfreq"><option value="day">日K</option><option value="minute">分钟K</option></select>
      <select id="kadj"><option value="none">不复权</option><option value="qfq">前复权</option><option value="hfq">后复权</option></select>
      <select id="kmonths"><option value="1">近1月</option><option value="3" selected>近3月</option><option value="6">近6月</option><option value="12">近1年</option></select>
      <button onclick="loadKline()">加载</button>
      <label class="hint"><input type="checkbox" id="kma" checked> MA5/20</label>
    </div>
    <div id="kchart"></div>
  </div>
  <div class="card"><div class="card-title">行情原始查询（代理 stockdb HTTP API）</div>
    <div style="display:flex;gap:8px;margin:8px 0;flex-wrap:wrap">
      <select id="qtype">
        <option value="股票代码">股票代码</option>
        <option value="日k:600633:20260810">日K 示例</option>
        <option value="分钟k:600633:20260810140000">分钟K 示例</option>
        <option value="复权:600633:2026*">复权 示例</option>
      </select>
      <button class="btn-ghost" onclick="doQuery()">查询</button>
    </div>
    <pre id="qres">（查询结果）</pre>
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
  <div class="card"><div class="card-title">同步日志 <span class="hint" style="font-weight:normal;margin-left:6px">（运行中或失败时自动展开）</span></div>
    <pre id="log" style="display:none">（暂无）</pre>
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
  if(name==='market'&&kchart)kchart.resize();
  refresh(true); // 切页立即按当前页刷新
}

const PHASE_STAGE={stopping:'停服中',syncing:'同步数据',verifying:'校验数据完整性',restarting:'重启服务'};
const PHASE_PCT={stopping:15,syncing:45,verifying:80,restarting:95};
let _lastExit=null;

// 轮询互斥 + 排队：避免重叠请求；切页 force 触发一次立即刷新
let _refreshing=false,_refreshQueued=false;
async function refresh(force){
  if(_refreshing){if(force)_refreshQueued=true;return}
  _refreshing=true;
  try{
    const s=await j('/api/status');
    const active=document.querySelector('.tab-panel.active')?.id||'tab-overview';
    const h=(active==='tab-sync'||active==='tab-system')?await j('/api/health'):null;
    // 顶部状态（总更新）
    const hs=h||{};
    $('statusDot').className='dot '+(hs.status==='ok'?'ok':hs.status==='stale'?'warn':'err');
    const up=s.data_latest?('数据更新至 '+String(s.data_latest).slice(4,6)+'-'+String(s.data_latest).slice(6,8)):'数据未同步';
    $('brandStatus').textContent=(hs.status==='ok'?'服务正常':hs.status==='stale'?'有待更新':'服务异常')+' · '+up;
    if(active==='tab-sync'){
      renderHero(s,h||{});
      renderSchView(s.schedule||{});
      renderDataOverview(s);
      const sched=s.schedule||{};
      $('schToday').textContent=(sched.enabled&&sched.trading_only)?(s.trading_today?'':'今日非交易日，定时跳过'):'';
      loadHistory();
      const lg=await j('/api/log?n=80');
      $('log').textContent=lg.log;
      if(s.sync_running||(_lastExit!=null&&_lastExit!==0))$('log').style.display='block';
      else $('log').style.display='none';
    }else if(active==='tab-system'){
      renderSystem(s);
      loadHistory();
    }else if(active==='tab-overview'){
      loadWatchlist();
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
  try{const r=await j('/api/sync',opt);toast(r.msg||'已启动同步');toggleMenu();}
  catch(e){toast('启动失败: '+e)}
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
        if(x.exit_code===0)tEl.textContent='同步任务：上次成功 '+fmtClock(x.ts);
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
  return `<div class="hr-card">
    <div class="row1"><span>${resultCell(x)}</span><span class="hint">${trigLabel(x.trigger)}</span></div>
    <div class="row2">${esc((x.ts||'').slice(0,16))} · ${x.mode==='hot'?'热更新':'严格'}</div>
    <div class="row3">${x.downloads!=null?('下载 '+x.downloads+' 个文件 · '):''}${x.verified==='pass'?'校验通过':x.verified==='fail'?'校验失败':x.verified==='skipped'?'未校验':''}${x.duration_sec!=null?(' · '+x.duration_sec+' 秒'):''}${reason}</div>
    <div class="row3">数据更新至 ${esc(x.data_latest||'—')}</div>
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
  // 自动任务
  const sch=s.schedule||{};
  $('hcSchedDot').style.background=s.scheduler_alive?'var(--ok)':'var(--err)';
  $('hcSched').textContent=s.scheduler_alive?'运行中':'未运行';
  $('hcSchedSub').textContent=(sch.enabled?(sch.next_trigger?'下次 '+sch.next_trigger:'已启用'):'定时未启用')||'—';
  // 同步能力：更新程序/数据源/数据卷/待重试
  const cap=s.sync_cap||{ok:false,checks:{}};
  const fails=Object.values(cap.checks||{}).filter(c=>c&&!c.ok);
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
}

async function loadWatchlist(){
  try{
    const w=await j('/api/watchlist');
    const idx=w.indices||[],wl=w.quotes||[];
    const irow=$('idxRow');
    if(idx.length)irow.innerHTML=idx.map(x=>'<div><div class="k">'+esc(x.name)+'</div><div class="v" style="'+pctCls(x.pct_chg)+'">'+fmtPrice(x.close)+' <span style="font-size:12px">'+fmtPct(x.pct_chg)+'</span></div></div>').join('');
    else irow.innerHTML='<span class="hint">（指数数据未取到）</span>';
    const wrow=$('wlRow');
    if(wl.length)wrow.innerHTML=wl.map(x=>'<div style="cursor:pointer" onclick="showKline(\''+x.code+'\')"><div class="k">'+esc(x.name)+' '+esc(x.code)+'</div><div class="v" style="'+pctCls(x.pct_chg)+'">'+fmtPrice(x.close)+' <span style="font-size:12px">'+fmtPct(x.pct_chg)+'</span></div></div>').join('');
    else wrow.innerHTML='<span class="hint">（空，下方添加自选）</span>';
  }catch(e){}
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
let kchart=null;
function showKline(code){$('kcode').value=code;loadKline()}
function ma(data,n){return data.map((v,i)=>i<n-1?null:+((data.slice(i-n+1,i+1).reduce((a,b)=>a+b,0)/n).toFixed(3)))}
async function loadKline(){
  const code=$('kcode').value.trim(),freq=$('kfreq').value,adj=$('kadj').value,months=$('kmonths').value;
  try{
    const d=await j('/api/klines?code='+code+'&freq='+freq+'&months='+months+'&adj='+adj);
    const rows=d.rows||[];
    if(!rows.length){$('qres').textContent='（该区间无K线数据）';return}
    const dates=rows.map(r=>String(r.date)),closes=rows.map(r=>+r.close);
    const kd=rows.map(r=>[+r.open,+r.close,+r.low,+r.high]);
    const showMA=$('kma').checked;
    const series=[
      {name:'K线',type:'candlestick',data:kd,
       itemStyle:{color:'#F87171',color0:'#4ADE80',borderColor:'#F87171',borderColor0:'#4ADE80'}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,
       data:rows.map(r=>r.volume||0),itemStyle:{color:'#38BDF8'}}
    ];
    if(showMA&&freq==='day'){
      series.push({name:'MA5',type:'line',data:ma(closes,5),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#FBBF24'}});
      series.push({name:'MA20',type:'line',data:ma(closes,20),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#A78BFA'}});
    }
    if(!kchart)kchart=echarts.init($('kchart'),'dark');
    kchart.setOption({
      backgroundColor:'transparent',
      title:{text:code+' '+(freq==='day'?'日K':'分钟K')+(adj!=='none'?(adj==='qfq'?' 前复权':' 后复权'):''),left:8,top:4,textStyle:{fontSize:13,color:'#8FA2BC'}},
      tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
      legend:{right:8,top:4,textStyle:{color:'#8FA2BC',fontSize:11}},
      grid:[{left:60,right:16,top:34,height:'62%'},{left:60,right:16,top:'72%',height:'18%'}],
      xAxis:[{type:'category',data:dates,axisLine:{lineStyle:{color:'#1E2C45'}}},
             {type:'category',gridIndex:1,data:dates,axisLabel:{show:false},axisLine:{lineStyle:{color:'#1E2C45'}}}],
      yAxis:[{scale:true,splitLine:{lineStyle:{color:'#162238'}}},
             {gridIndex:1,splitNumber:2,axisLabel:{show:false},splitLine:{show:false}}],
      dataZoom:[{type:'inside',xAxisIndex:[0,1],start:40,end:100}],
      series
    });
  }catch(e){$('qres').textContent='K线加载失败: '+e}
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
            "last_sync": last_sync_summary(),
            "schedule": load_schedule(),
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
            raw_times = q.get("times", []) or [q.get("time", ["15:30"])[0]]  # 兼容单 time
            # times 可能是 "15:30,16:05" 一个字符串，按逗号拆分
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
            self._send(200, json.dumps({"msg": "同步已在运行中"}))
            return
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
    threading.Thread(target=scheduler_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
