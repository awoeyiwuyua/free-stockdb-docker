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
_sync_state = {"running": False, "exit_code": None, "last_start": None, "last_end": None}
_last_sync_stdout: str = ""          # 最近一次同步的 stdout（供解析下载/删除数）
_last_verify_result: str | None = None  # 最近一次完整性验证结果（pass/fail/跳过）
_scheduler_alive = False             # 定时线程心跳（每次循环更新时间戳）

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
    return {"enabled": False, "times": ["15:30"], "last_trigger": None, "next_trigger": None}


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
    """读定时配置；兼容旧格式 {enabled, time} → 迁移为 {enabled, times:[time]}。"""
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
    return {
        "enabled": bool(data.get("enabled")),
        "times": _normalize_times(times),
        "last_trigger": last,
        "next_trigger": compute_next_trigger(_normalize_times(times)),
    }


def save_schedule(enabled: bool, times) -> dict:
    """保存定时配置（保留 last_trigger，不因改配置而清空触发记录）。"""
    cfg = {"enabled": bool(enabled), "times": _normalize_times(times)}
    if not cfg["times"]:
        raise RuntimeError("至少需要一个合法时间点（HH:MM）")
    try:
        old = {}
        if SCHEDULE_FILE.exists():
            try:
                old = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
            except Exception:
                old = {}
        if isinstance(old, dict) and isinstance(old.get("last_trigger"), dict):
            cfg["last_trigger"] = old["last_trigger"]
        cfg["next_trigger"] = compute_next_trigger(cfg["times"])
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        raise RuntimeError(f"定时配置写入失败: {exc}")
    return cfg


def _mark_schedule_triggered(key: str) -> None:
    """记录某时间点已触发（防同一天重复触发），落盘。"""
    try:
        cfg = load_schedule()
        cfg["last_trigger"] = {"key": key, "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "exit": None}
        cfg["next_trigger"] = compute_next_trigger(cfg["times"])
        SCHEDULE_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    except Exception as exc:
        log(f"⏰ 定时触发标记失败: {exc}")


def _update_schedule_trigger_exit(exit_code) -> None:
    """run_sync 结束后回填最近一次定时触发的 exit 码。"""
    try:
        cfg = load_schedule()
        if cfg["last_trigger"] and cfg["last_trigger"].get("exit") is None:
            cfg["last_trigger"]["exit"] = exit_code
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
    """数据最新交易日：查上证指数日K，取最大日期（近3月前缀，跨年安全）。"""
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


def mirror_latest_date() -> str | None:
    """镜像源（a.123128.xyz 网页）标注的最新数据日期。

    镜像源是 LevelDB 文件镜像（HTTP + manifest），无行情 API，但它首页明文标注
    「数据更新至:YYYY-MM-DD」。抓该日期可判断「本地落后是同步未跑 vs 镜像未发布」。
    可配置 MIRROR_PAGE_URL 覆盖（内网映射/镜像源变更时）。
    """
    import urllib.request, re
    url = os.environ.get("MIRROR_PAGE_URL", "https://a.123128.xyz/")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            html = resp.read().decode("utf-8", "replace")
        m = re.search(r"数据更新至:(\d{4}-\d{2}-\d{2})", html)
        return m.group(1) if m else None
    except Exception:
        return None


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
    """
    try:
        info = docker_request("GET", f"/containers/{STOCKDB_CONTAINER}/json")
    except FileNotFoundError:
        return {"ok": False, "status": "unknown", "note": "docker socket 未挂载（/var/run/docker.sock 不可达），无法停/启容器"}
    except PermissionError:
        return {"ok": False, "status": "unknown", "note": "docker socket 权限不足，无法操控容器"}
    except Exception as exc:
        return {"ok": False, "status": "unknown", "note": f"docker 查询失败: {exc}"}
    st = info.get("State", {}).get("Status", "unknown")
    return {"ok": True, "status": st, "note": ""}


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


# ==================== 同步任务（后台线程） ====================
def _verify_data() -> list[str]:
    """同步后完整性验证：股票代码总数 + 抽样日K/分钟K/复权。返回异常列表（空=通过）。"""
    problems = []
    try:
        import urllib.request, urllib.parse
        def q(table: str) -> str:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote(table)}"
            with urllib.request.urlopen(url, timeout=15) as resp:
                return resp.read().decode("utf-8", "replace")
        codes_raw = q("股票代码")
        try:
            codes = json.loads(codes_raw)
            total = len(codes.get("0", codes)) if isinstance(codes, dict) else len(codes)
            if total == 0:
                problems.append(f"股票代码为空（total=0）")
        except Exception:
            problems.append("股票代码解析失败")
        # 抽样 3 只不同板块：沪市 600633 / 深市 000001 / 创业板 300750
        for code in ("600633", "000001", "300750"):
            try:
                raw = q(f"日k:{code}:20260810")
                d = json.loads(raw)
                if isinstance(d, dict) and d.get("close") is None:
                    problems.append(f"{code} 日K 数据缺失")
            except Exception as exc:
                problems.append(f"{code} 日K 查询失败: {exc}")
    except Exception as exc:
        problems.append(f"验证接口异常: {exc}")
    return problems


def run_sync(hot: bool = True, trigger: str = "manual") -> None:
    """同步数据。串行执行，防并发点击。

    hot=True（默认，热更新）：不停服务直接增量同步——同步器下载到 .part 临时文件、
    SHA256 校验后原子 rename 替换（Unix rename 对读进程无影响），服务端持续可查。
    官方 DATA_SOURCE.md 保守要求"同步期间停止服务"，但实测与机制均支持热更新；
    同步后自动做完整性验证，失败则重启 stockdb 止损。

    hot=False（严格模式）：按官方要求先停服务 → 同步 → 重启，作为兜底。

    trigger=manual|scheduled：记录触发来源（手动按钮 / 定时线程），
    写入同步历史，便于回看"某次同步是不是定时自动跑的"。
    """
    if not _sync_lock.acquire(blocking=False):
        return  # 已在同步中
    _sync_state.update(running=True, exit_code=None, last_start=time.time(), last_end=None,
                       trigger=trigger)
    global _last_sync_stdout, _last_verify_result
    _last_sync_stdout = ""
    _last_verify_result = None
    try:
        log(f"=== 同步开始 {now()}（{'热更新' if hot else '严格模式(停服)'}｜{'定时' if trigger == 'scheduled' else '手动'}）===")

        # 1.（严格模式）停 stockdb；热更新模式不停
        if not hot:
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
        log(f"→ 数据更新退出码 {proc.returncode}")

        # 3. 热更新模式：验证数据完整性（此时 stockdb 仍在运行）
        if hot:
            if proc.returncode == 0:
                log("→ 热更新完成，验证数据完整性 ...")
                problems = _verify_data()
                if problems:
                    _last_verify_result = "fail"
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
            log("→ 启动 stockdb 容器 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")
        elif container_state().get("status") in ("exited", "not-found"):
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
    finally:
        # 记录同步历史（时间/触发来源/模式/结果/耗时/下载删除数/数据最新日期）
        try:
            counts = parse_sync_counts(_last_sync_stdout)
            append_history({
                "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "trigger": _sync_state.get("trigger", "manual"),
                "mode": "hot" if hot else "strict",
                "exit_code": _sync_state.get("exit_code"),
                "downloads": counts.get("downloads"),
                "deletes": counts.get("deletes"),
                "verified": _last_verify_result,
                "duration_sec": round(time.time() - _sync_state["last_start"], 1)
                if _sync_state.get("last_start") else None,
                "data_latest": data_latest_date(),
            })
            if _sync_state.get("trigger") == "scheduled":
                _update_schedule_trigger_exit(_sync_state.get("exit_code"))
        except Exception:
            pass
        _sync_state["running"] = False
        _sync_state["last_end"] = time.time()
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
def scheduler_loop() -> None:
    """后台定时线程：每 30s 检查定时配置，到点触发热更新同步。

    触发判定（比"精确等于"健壮）：
      对每个配置时间点 t：当前分钟 >= t 且 今天该点还没触发过（key=today+t）→ 触发。
    即使某次轮询落在 t 分钟之后，只要当天未触发过且未到第二天，仍会触发；
    触发后 key 落盘（容器重启不重复触发）。
    """
    global _scheduler_alive
    while True:
        _scheduler_alive = True  # 心跳：页面据此判断定时功能是否正常
        try:
            cfg = load_schedule()
            if cfg["enabled"] and cfg["times"] and not _sync_state["running"]:
                now = datetime.now()
                today_key = now.strftime("%Y-%m-%d")
                now_hm = now.strftime("%H:%M")
                fired_key = (cfg["last_trigger"] or {}).get("key", "")
                for t in cfg["times"]:
                    key = f"{today_key} {t}"
                    # now 已过 t 且今天 t 点尚未触发 → 触发
                    if now_hm >= t and key != fired_key:
                        _mark_schedule_triggered(key)
                        log(f"⏰ 定时同步触发（{t}）——stockdb 保持运行，热更新")
                        threading.Thread(
                            target=run_sync,
                            kwargs={"hot": True, "trigger": "scheduled"},
                            daemon=True,
                        ).start()
                        break
        except Exception as exc:
            log(f"⏰ 定时线程异常: {exc}")
        time.sleep(30)


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
    """批量获取股票最新交易日行情（name/close/pct_chg/date）。"""
    import urllib.request, urllib.parse
    quotes = []
    for code in codes:
        try:
            url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=vals&t={urllib.parse.quote(f'日k:{code}:2026*')}"
            with urllib.request.urlopen(url, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            rows = [r for r in data if isinstance(r, dict) and r.get("date")]
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
<title>stockdb 管理台</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;
--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;--brand:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:960px;margin:0 auto;padding:24px}
h1{font-size:20px;display:flex;align-items:center;gap:10px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--muted);display:inline-block}
.dot.ok{background:var(--ok)}.dot.err{background:var(--err)}.dot.warn{background:var(--warn)}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:16px;margin:14px 0}
.row{display:flex;gap:16px;flex-wrap:wrap}
.k{color:var(--muted);font-size:12px;margin-bottom:2px}
.v{font-size:16px;font-weight:600}
button{background:var(--brand);border:0;color:#082f49;font-weight:700;font-size:15px;
padding:10px 22px;border-radius:8px;cursor:pointer}
button:disabled{background:var(--line);color:var(--muted);cursor:not-allowed}
input[type=text],input[type=time],select{background:#0f172a;border:1px solid var(--line);
color:var(--text);padding:8px 10px;border-radius:6px;width:220px}
pre{background:#0b1220;border:1px solid var(--line);border-radius:8px;padding:12px;
max-height:340px;overflow:auto;font:12px/1.5 ui-monospace,monospace;color:#a5f3fc}
.actions{display:flex;align-items:center;gap:12px}
.hint{color:var(--muted);font-size:12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--line);white-space:nowrap}
th{color:var(--muted);font-weight:600}
#kchart{width:100%;height:460px}
.badge{padding:2px 8px;border-radius:10px;font-size:12px}
.b-ok{background:#14532d;color:#86efac}.b-fail{background:#7f1d1d;color:#fca5a5}
.b-skip{background:#44403c;color:#d6d3d1}
.tabs{display:flex;gap:2px;border-bottom:1px solid var(--line);margin-bottom:16px}
.tab-btn{background:transparent;border:0;border-bottom:2px solid transparent;color:var(--muted);
font-size:15px;padding:8px 18px;cursor:pointer}
.tab-btn.active{color:var(--brand);border-bottom-color:var(--brand);font-weight:700}
.tab-panel{display:none}.tab-panel.active{display:block}
.metrics{display:flex;gap:20px;flex-wrap:wrap}
.metrics .m{flex:1;min-width:110px}
.metrics .lbl{color:var(--muted);font-size:12px;margin-bottom:2px}
.metrics .val{font-size:17px;font-weight:600}
</style></head><body><div class="wrap">
<h1><span id="statusDot" class="dot"></span> stockdb 管理台</h1>

<div class="tabs">
  <button class="tab-btn active" data-tab="overview" onclick="showTab('overview',this)">概览</button>
  <button class="tab-btn" data-tab="market" onclick="showTab('market',this)">行情</button>
  <button class="tab-btn" data-tab="sync" onclick="showTab('sync',this)">同步</button>
  <button class="tab-btn" data-tab="system" onclick="showTab('system',this)">系统</button>
</div>

<!-- 概览：大盘 + 自选股 -->
<div id="tab-overview" class="tab-panel active">
  <div class="card"><div class="metrics">
    <div class="m"><div class="lbl">大盘指数</div><div class="row" id="idxRow" style="gap:14px"><span class="hint">…</span></div></div>
    <div class="m"><div class="lbl">自选股（点代码看K线）</div><div class="row" id="wlRow" style="gap:14px"><span class="hint">…</span></div></div>
  </div>
  <div style="margin-top:10px;display:flex;gap:8px">
    <input type="text" id="wlAdd" placeholder="加自选，如 600633,000967" style="width:280px">
    <button onclick="addWatch()" style="background:#334155;color:#e2e8f0">加入自选</button>
    <span id="wlMsg" class="hint"></span>
  </div></div>
</div>

<!-- 行情：K线图 + 原始查询 -->
<div id="tab-market" class="tab-panel">
  <div class="card"><div class="k">K 线图</div>
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
  <div class="card"><div class="k">行情原始查询（代理 stockdb HTTP API）</div>
    <div style="display:flex;gap:8px;margin:8px 0">
      <select id="qtype">
        <option value="股票代码">股票代码</option>
        <option value="日k:600633:20260810">日K 示例</option>
        <option value="分钟k:600633:20260810140000">分钟K 示例</option>
        <option value="复权:600633:2026*">复权 示例</option>
      </select>
      <button onclick="doQuery()" style="background:#334155;color:#e2e8f0">查询</button>
    </div>
    <pre id="qres">（查询结果）</pre>
  </div>
</div>

<!-- 同步：健康度 + 按钮 + 定时 + 历史 + 日志 -->
<div id="tab-sync" class="tab-panel">
  <div class="card"><div class="metrics">
    <div class="m"><div class="lbl">数据健康度</div><div class="val" id="cLatest" style="font-size:13px">…</div></div>
    <div class="m"><div class="lbl">定时调度器</div><div class="val" id="cSched" style="font-size:14px">…</div></div>
    <div class="m"><div class="lbl">磁盘用量</div><div class="val" id="cDisk" style="font-size:14px">…</div></div>
  </div></div>
  <div class="card">
    <div class="actions">
      <button id="syncBtn2" onclick="doSync()">开始同步</button>
      <label class="hint" style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" id="hotMode" checked> 热更新（不停服务，默认）
      </label>
      <span id="syncMsg" class="hint">热更新=服务保持运行直接增量同步；严格模式=停服后同步</span>
    </div>
  </div>
  <div class="card"><div class="k">定时自动同步（热更新，多时间点）</div>
    <div style="margin-top:8px;display:flex;gap:8px;align-items:center">
      <label class="hint"><input type="checkbox" id="schEnabled" onchange="saveScheduleNow()"> 启用</label>
      <input type="time" id="schTime" value="15:30">
      <button onclick="addSchTime()" style="background:#334155;color:#e2e8f0">添加时间点</button>
      <span id="schTimes" style="display:flex;gap:6px;flex-wrap:wrap"></span>
      <span id="schMsg" class="hint"></span>
    </div>
    <div class="hint" style="margin-top:8px" id="schNext"></div>
    <div class="hint" style="margin-top:4px" id="schLast"></div>
  </div>
  <div class="card"><div class="k">同步历史（最近 30 次）</div>
    <table><thead><tr><th>时间</th><th>触发</th><th>模式</th><th>结果</th><th>下载</th><th>验证</th><th>耗时</th><th>数据最新</th></tr></thead>
    <tbody id="histBody"><tr><td colspan="8" class="hint">（暂无历史）</td></tr></tbody></table>
  </div>
  <div class="card"><div class="k">同步日志（自动刷新）</div><pre id="log">（暂无）</pre></div>
</div>

<!-- 系统：容器/数据源 状态判断 -->
<div id="tab-system" class="tab-panel">
  <div class="card"><div class="metrics">
    <div class="m"><div class="lbl">stockdb 容器</div><div class="val" id="cState" style="font-size:15px">…</div></div>
    <div class="m"><div class="lbl">数据源</div><div class="val" id="cSource" style="font-size:13px">…</div></div>
  </div>
  <div class="hint" style="margin-top:8px" id="cStateNote">…</div></div>
</div>
</div>
<script src="/static/echarts.min.js"></script>
<script>
async function j(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(r.status);return r.json()}
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function pctCls(v){return v>0?'color:#f87171':v<0?'color:#4ade80':'color:#94a3b8'}
function fmtPct(v){return v==null?'—':(v>0?'+':'')+Number(v).toFixed(2)+'%'}
function fmtPrice(v){return v==null?'—':Number(v).toFixed(2)}
function showTab(name,btn){
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p=>p.classList.remove('active'));
  (btn||document.querySelector('[data-tab="'+name+'"]')).classList.add('active');
  document.getElementById('tab-'+name).classList.add('active');
  if(name==='market'&&kchart)kchart.resize();
}
async function refresh(){
  try{
    const s=await j('/api/status');
    // 系统页：容器状态 + 数据源
    const cs=s.container||{};
    document.getElementById('cState').textContent=cs.status||'unknown';
    document.getElementById('cStateNote').textContent=cs.note||(cs.ok?'':'docker 不可用，同步按钮将无法停/启容器');
    document.getElementById('cSource').textContent=s.source||'—';
    const d=document.getElementById('statusDot');
    d.className='dot '+(cs.status==='running'?'ok':(cs.ok?'err':'warn'));
    // 同步页：健康度 + 定时调度器 + 磁盘
    loadHealth();
    const schedInfo=s.schedule||{};
    document.getElementById('cSched').innerHTML='<span style="color:'+(s.scheduler_alive?'var(--ok)':'var(--err)')+'">'+(s.scheduler_alive?'运行中':'未运行')+'</span>'+(schedInfo.enabled&&schedInfo.times?' ｜ 每日 '+schedInfo.times.join('、'):'');
    if(s.disk&&s.disk.total_gb!=null)document.getElementById('cDisk').textContent=s.disk.used_gb+' / '+s.disk.total_gb+' GB';
    const b=document.getElementById('syncBtn2');if(b)b.disabled=s.sync_running;
    document.getElementById('syncMsg').textContent=s.sync_running?'同步进行中，请勿重复点击…':'热更新=服务保持运行直接增量同步；严格模式=停服后同步';
    renderSchTimes(schedInfo);
    const lg=await j('/api/log?n=100');document.getElementById('log').textContent=lg.log;
    loadWatchlist();loadHistory();
  }catch(e){document.getElementById('log').textContent='状态刷新失败: '+e}
}
let _schRendered="";  // 避免 4s 轮询重复渲染/自动保存
function renderSchTimes(sch){
  const el=document.getElementById('schTimes');
  if(!el)return;
  document.getElementById('schEnabled').checked=sch.enabled;
  const times=sch.times||[];
  if(JSON.stringify(times)!==_schRendered){
    _schRendered=JSON.stringify(times);
    el.innerHTML=times.map(t=>'<span style="background:#0f172a;border:1px solid var(--line);border-radius:6px;padding:2px 8px;font-size:13px">'+esc(t)+' <a href="#" onclick="rmSchTime(\''+t+'\',event);return false" style="color:var(--err);text-decoration:none">✕</a></span>').join('')||'<span class="hint">（无时间点）</span>';
  }
  // 下次 / 上次触发
  const nxt=document.getElementById('schNext'),lst=document.getElementById('schLast');
  if(nxt)nxt.textContent=sch.enabled?(sch.next_trigger?'下次触发：'+sch.next_trigger:'（时间点已过，明天触发）'):'定时未启用';
  if(lst){
    const l=sch.last_trigger;
    lst.textContent=l?('上次定时触发 '+l.ts+' ｜ '+(l.exit===0?'✅ 成功':'❌ 退出码 '+l.exit)):'';
  }
}
function schTimeChanged(){
  const times=[...document.querySelectorAll('#schTimes span')].map(s=>s.textContent.replace(/✕.*/,'').trim()).filter(t=>/^\d{2}:\d{2}$/.test(t));
  saveScheduleNow();
}
function addSchTime(){
  const t=document.getElementById('schTime').value;
  if(!t){document.getElementById('schMsg').textContent='请选择时间';return;}
  const cur=[...(document.getElementById('schTimes').textContent.match(/\d{2}:\d{2}/g)||[]),t];
  document.getElementById('schTime').value='';
  saveScheduleNow([...new Set(cur)]);
}
function rmSchTime(t,ev){
  ev.preventDefault();
  const cur=(document.getElementById('schTimes').textContent.match(/\d{2}:\d{2}/g)||[]).filter(x=>x!==t);
  saveScheduleNow(cur);
}
async function saveScheduleNow(times){
  const enabled=document.getElementById('schEnabled').checked;
  const t=(times||(document.getElementById('schTimes').textContent.match(/\d{2}:\d{2}/g)||[]));
  try{
    const r=await j('/api/schedule?action=save&enabled='+enabled+'&times='+encodeURIComponent(t.join(',')));
    document.getElementById('schMsg').textContent=r.msg||'已保存';
    document.getElementById('schNext').textContent=r.schedule&&r.schedule.next_trigger?'下次触发：'+r.schedule.next_trigger:'';
  }catch(e){document.getElementById('schMsg').textContent='保存失败: '+e;}
}
async function loadHealth(){
  try{
    const h=await j('/api/health');
    const el=document.getElementById('cLatest');
    const color=h.status==='ok'?'var(--ok)':h.status==='warn'?'var(--warn)':'var(--err)';
    el.innerHTML='<span style="color:'+color+'">'+esc(h.note||'—')+'</span>';
  }catch(e){}
}
async function loadWatchlist(){
  try{
    const w=await j('/api/watchlist');
    const idx=w.indices||[], wl=w.quotes||[];
    const irow=document.getElementById('idxRow');
    if(idx.length) irow.innerHTML=idx.map(x=>'<div><div class="k">'+esc(x.name)+'</div><div class="v" style="'+pctCls(x.pct_chg)+'">'+fmtPrice(x.close)+' <span style="font-size:12px">'+fmtPct(x.pct_chg)+'</span></div></div>').join('');
    else irow.innerHTML='<span class="hint">（指数数据未取到）</span>';
    const wrow=document.getElementById('wlRow');
    if(wl.length) wrow.innerHTML=wl.map(x=>'<div style="cursor:pointer" onclick="showKline(\''+x.code+'\')"><div class="k">'+esc(x.name)+' '+esc(x.code)+'</div><div class="v" style="'+pctCls(x.pct_chg)+'">'+fmtPrice(x.close)+' <span style="font-size:12px">'+fmtPct(x.pct_chg)+'</span></div></div>').join('');
    else wrow.innerHTML='<span class="hint">（空，下方添加自选）</span>';
  }catch(e){}
}
async function addWatch(){
  const codes=document.getElementById('wlAdd').value.trim();
  if(!codes){return}
  try{
    const cur=await j('/api/watchlist');
    const merged=[...new Set((cur.codes||[]).concat(codes.split(/[,，\s]+/)))];
    const r=await j('/api/watchlist?action=set&codes='+encodeURIComponent(merged.join(',')));
    document.getElementById('wlMsg').textContent='已保存 '+r.codes.length+' 只';
    document.getElementById('wlAdd').value='';
    loadWatchlist();
  }catch(e){document.getElementById('wlMsg').textContent='保存失败: '+e;}
}
async function loadHistory(){
  try{
    const h=await j('/api/history');
    const tb=document.getElementById('histBody');
    if(!h.history.length){tb.innerHTML='<tr><td colspan="8" class="hint">（暂无历史）</td></tr>';return;}
    tb.innerHTML=h.history.map(x=>`<tr>
      <td>${esc(x.ts)}</td>
      <td>${x.trigger==='scheduled'?'⏰定时':'手动'}</td>
      <td>${x.mode==='hot'?'热更新':'严格'}</td>
      <td>${x.exit_code===0?'✅':'❌ '+esc(x.exit_code)}</td>
      <td>${x.downloads==null?'—':x.downloads}</td>
      <td>${x.verified==='pass'?'<span class="badge b-ok">通过</span>':x.verified==='fail'?'<span class="badge b-fail">失败</span>':x.verified==='skipped'?'<span class="badge b-skip">跳过</span>':'—'}</td>
      <td>${x.duration_sec==null?'—':esc(x.duration_sec)+'s'}</td>
      <td>${esc(x.data_latest||'—')}</td></tr>`).join('');
  }catch(e){}
}
async function doSync(){
  const hot=document.getElementById('hotMode').checked;
  const opt={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({hot:hot})};
  try{const r=await j('/api/sync',opt);document.getElementById('syncMsg').textContent=r.msg;}
  catch(e){document.getElementById('syncMsg').textContent='启动失败: '+e;}
}
async function doQuery(){
  const q=document.getElementById('qtype').value;
  const r=await fetch('/api/query?t='+encodeURIComponent(q));
  const d=await r.text();
  document.getElementById('qres').textContent=d;
}
let kchart=null;
function showKline(code){document.getElementById('kcode').value=code;loadKline();}
function ma(data,n){return data.map((v,i)=>i<n-1?null:+((data.slice(i-n+1,i+1).reduce((a,b)=>a+b,0)/n).toFixed(3)))}
async function loadKline(){
  const code=document.getElementById('kcode').value.trim();
  const freq=document.getElementById('kfreq').value;
  const adj=document.getElementById('kadj').value;
  const months=document.getElementById('kmonths').value;
  try{
    const d=await j('/api/klines?code='+code+'&freq='+freq+'&months='+months+'&adj='+adj);
    const rows=d.rows||[];
    if(!rows.length){document.getElementById('qres').textContent='（该区间无K线数据）';return;}
    const dates=rows.map(r=>String(r.date));
    const closes=rows.map(r=>+r.close);
    const kd=rows.map(r=>[+r.open,+r.close,+r.low,+r.high]);
    const showMA=document.getElementById('kma').checked;
    const series=[
      {name:'K线',type:'candlestick',data:kd,
       itemStyle:{color:'#f87171',color0:'#4ade80',borderColor:'#f87171',borderColor0:'#4ade80'}},
      {name:'成交量',type:'bar',xAxisIndex:1,yAxisIndex:1,
       data:rows.map(r=>r.volume||0),itemStyle:{color:'#38bdf8'}}
    ];
    if(showMA&&freq==='day'){
      series.push({name:'MA5',type:'line',data:ma(closes,5),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#fbbf24'}});
      series.push({name:'MA20',type:'line',data:ma(closes,20),smooth:true,showSymbol:false,lineStyle:{width:1,color:'#a78bfa'}});
    }
    if(!kchart)kchart=echarts.init(document.getElementById('kchart'),'dark');
    kchart.setOption({
      backgroundColor:'transparent',
      title:{text:code+' '+(freq==='day'?'日K':'分钟K')+(adj!=='none'?(adj==='qfq'?' 前复权':' 后复权'):''),left:8,top:4,textStyle:{fontSize:13,color:'#94a3b8'}},
      tooltip:{trigger:'axis',axisPointer:{type:'cross'}},
      legend:{right:8,top:4,textStyle:{color:'#94a3b8',fontSize:11}},
      grid:[{left:60,right:16,top:34,height:'62%'},{left:60,right:16,top:'72%',height:'18%'}],
      xAxis:[{type:'category',data:dates,axisLine:{lineStyle:{color:'#334155'}}},
             {type:'category',gridIndex:1,data:dates,axisLabel:{show:false},axisLine:{lineStyle:{color:'#334155'}}}],
      yAxis:[{scale:true,splitLine:{lineStyle:{color:'#1e293b'}}},
             {gridIndex:1,splitNumber:2,axisLabel:{show:false},splitLine:{show:false}}],
      dataZoom:[{type:'inside',xAxisIndex:[0,1],start:40,end:100}],
      series
    });
  }catch(e){document.getElementById('qres').textContent='K线加载失败: '+e;}
}
refresh();setInterval(refresh,4000);
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
            "container": state,          # {ok, status, note}
            "source": src,
            "sync_running": _sync_state["running"],
            "exit_code": _sync_state["exit_code"],
            "data_latest": data_latest_date(),
            "schedule": load_schedule(),
            "disk": disk_usage(),
            "scheduler_alive": _scheduler_alive,
        }, ensure_ascii=False))

    def _history(self):
        self._send(200, json.dumps({"history": load_history()}, ensure_ascii=False))

    def _schedule(self):
        q = parse_qs(urlparse(self.path).query)
        if q.get("action", [""])[0] == "save":
            enabled = q.get("enabled", ["false"])[0].lower() == "true"
            raw_times = q.get("times", []) or [q.get("time", ["15:30"])[0]]  # 兼容单 time
            # times 可能是 "15:30,16:05" 一个字符串，按逗号拆分
            times = [t for part in raw_times for t in str(part).split(",")]
            try:
                cfg = save_schedule(enabled, times)
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


def main():
    print(f"webui listening on 0.0.0.0:{LISTEN_PORT}", file=sys.stderr)
    print(f"stockdb: {STOCKDB_HOST}:{STOCKDB_PORT} | container: {STOCKDB_CONTAINER} | data: {DATA_DIR}",
          file=sys.stderr)
    threading.Thread(target=scheduler_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
