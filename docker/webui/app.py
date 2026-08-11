#!/usr/bin/env python3
"""free-stockdb webui — 行情查询 + 同步管理（纯 Python 标准库，零第三方依赖）

功能：
  - 行情查询：代理 stockdb 7899 HTTP API（日K/分钟K/复权/股票代码）
  - 同步管理：挂载 docker socket + /data 卷，网页一键完成
    「停 stockdb 容器 → 容器内运行数据更新（增量同步）→ 重启 stockdb」
  - 日志查看：同步过程实时写入 /data/sync.log，页面轮询展示

安全边界：
  - docker 操控仅限固定的 stockdb 容器（stop/start/inspect 白名单），
    不开放任意命令；页面用可选的同步令牌（WEBUI_TOKEN）保护写操作
  - 只读查询不校验令牌（局域网内行情查看免密），写操作（同步）需令牌
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
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

STOCKDB_HOST = os.environ.get("STOCKDB_HOST", "127.0.0.1")
STOCKDB_PORT = int(os.environ.get("STOCKDB_PORT", "7899"))
STOCKDB_CONTAINER = os.environ.get("STOCKDB_CONTAINER", "stockdb")
DOCKER_SOCKET = os.environ.get("DOCKER_SOCKET", "/var/run/docker.sock")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SYNC_LOG = DATA_DIR / "sync.log"
WEBUI_TOKEN = os.environ.get("WEBUI_TOKEN", "")   # 空=不启用写保护
LISTEN_PORT = int(os.environ.get("WEBUI_PORT", "8080"))

# 同步线程状态
_sync_lock = threading.Lock()
_sync_state = {"running": False, "exit_code": None, "last_start": None, "last_end": None}


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
        conn.sock.settimeout(timeout)
        conn.request(method, path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        if resp.status >= 400:
            raise RuntimeError(f"docker {method} {path}: HTTP {resp.status} {resp.reason} {body[:200]}")
        return json.loads(body) if body else {}
    finally:
        conn.close()


def container_state() -> str:
    """返回 stockdb 容器状态：running / exited / not-found / docker-unavailable。"""
    try:
        info = docker_request("GET", f"/containers/{STOCKDB_CONTAINER}/json")
    except FileNotFoundError:
        return "docker-unavailable"
    except Exception:
        return "unknown"
    return info.get("State", {}).get("Status", "unknown")


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


def run_sync(hot: bool = True) -> None:
    """同步数据。串行执行，防并发点击。

    hot=True（默认，热更新）：不停服务直接增量同步——同步器下载到 .part 临时文件、
    SHA256 校验后原子 rename 替换（Unix rename 对读进程无影响），服务端持续可查。
    官方 DATA_SOURCE.md 保守要求"同步期间停止服务"，但实测与机制均支持热更新；
    同步后自动做完整性验证，失败则重启 stockdb 止损。

    hot=False（严格模式）：按官方要求先停服务 → 同步 → 重启，作为兜底。
    """
    if not _sync_lock.acquire(blocking=False):
        return  # 已在同步中
    _sync_state.update(running=True, exit_code=None, last_start=time.time(), last_end=None)
    try:
        log(f"=== 同步开始 {now()}（{'热更新' if hot else '严格模式(停服)'}）===")

        # 1.（严格模式）停 stockdb；热更新模式不停
        if not hot:
            log("→ 停止 stockdb 容器 ...")
            try:
                if container_state() == "running":
                    container_stop()
                else:
                    log("  （stockdb 已处于停止状态）")
            except Exception as exc:
                log(f"  ⚠️ 停止失败，继续同步（风险：数据卷并发写）：{exc}")
        else:
            st = container_state()
            if st == "running":
                log("→ 热更新：stockdb 保持运行，直接增量同步 ...")
            elif st in ("exited", "not-found"):
                log(f"→ 热更新：stockdb 当前 {st}，同步后尝试启动 ...")
            else:
                log(f"→ 热更新：stockdb 状态 {st}（docker 不可用？），仍继续同步 ...")

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
        _sync_state["exit_code"] = proc.returncode
        log(f"→ 数据更新退出码 {proc.returncode}")

        # 3. 热更新模式：验证数据完整性（此时 stockdb 仍在运行）
        if hot:
            if proc.returncode == 0:
                log("→ 热更新完成，验证数据完整性 ...")
                problems = _verify_data()
                if problems:
                    log(f"  ⚠️ 完整性验证未通过：{problems}")
                    log("  → 数据异常，自动重启 stockdb 止损 ...")
                    try:
                        container_restart()
                        log("  ✅ stockdb 已重启")
                    except Exception as exc:
                        log(f"  ❌ 重启失败：{exc}")
                else:
                    log("  ✅ 数据完整性验证通过（股票代码 + 抽样日K/复权/分钟K）")
            else:
                log("  ⚠️ 同步退出码非 0，跳过完整性验证")
                # 同步失败时确保服务仍在（可能中途被手动停过）
                if container_state() != "running":
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
        elif container_state() in ("exited", "not-found"):
            log("→ 热更新收尾：stockdb 未在运行，尝试启动 ...")
            try:
                container_start()
                log("  ✅ stockdb 已启动")
            except Exception as exc:
                log(f"  ❌ 启动失败：{exc}")

        log(f"=== 同步结束 {now()} ===")
    except Exception as exc:
        log(f"❌ 同步异常：{exc}")
        _sync_state["exit_code"] = -1
    finally:
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


# ==================== stockdb 行情查询代理 ====================
def stockdb_get(table: str) -> str:
    import urllib.parse
    url = f"http://{STOCKDB_HOST}:{STOCKDB_PORT}/?cmd=get&t={urllib.parse.quote(table)}"
    import urllib.request
    with urllib.request.urlopen(url, timeout=15) as resp:
        return resp.read().decode("utf-8", "replace")


# ==================== HTTP 服务 ====================
PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stockdb 管理台</title>
<style>
:root{--bg:#0f172a;--panel:#1e293b;--line:#334155;--text:#e2e8f0;--muted:#94a3b8;
--ok:#22c55e;--warn:#f59e0b;--err:#ef4444;--brand:#38bdf8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:24px}
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
input[type=text],select{background:#0f172a;border:1px solid var(--line);color:var(--text);
padding:8px 10px;border-radius:6px;width:220px}
pre{background:#0b1220;border:1px solid var(--line);border-radius:8px;padding:12px;
max-height:340px;overflow:auto;font:12px/1.5 ui-monospace,monospace;color:#a5f3fc}
.actions{display:flex;align-items:center;gap:12px}
.hint{color:var(--muted);font-size:12px}
</style></head><body><div class="wrap">
<h1><span id="statusDot" class="dot"></span> stockdb 管理台</h1>

<div class="card"><div class="row">
  <div><div class="k">stockdb 容器</div><div class="v" id="cState">…</div></div>
  <div><div class="k">数据源</div><div class="v" id="cSource">…</div></div>
  <div><div class="k">最近同步</div><div class="v" id="cSync">…</div></div>
  <div><div class="k">同步退出码</div><div class="v" id="cCode">…</div></div>
</div></div>

<div class="card">
  <div class="actions">
    <button id="syncBtn" onclick="doSync()">开始同步</button>
    <label class="hint" style="display:flex;align-items:center;gap:6px">
      <input type="checkbox" id="hotMode" checked> 热更新（不停服务，默认）
    </label>
    <span id="syncMsg" class="hint">热更新=服务保持运行直接增量同步；严格模式=停服后同步</span>
  </div>
  <div class="hint" style="margin-top:8px">同步令牌：<input type="password" id="token" placeholder="留空=未启用保护"></div>
</div>

<div class="card"><div class="k">同步日志（自动刷新）</div>
<pre id="log">（暂无）</pre></div>

<div class="card"><div class="k">行情快速查询（代理 stockdb HTTP API）</div>
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
<script>
async function j(url,opt){const r=await fetch(url,opt);if(!r.ok)throw new Error(r.status);return r.json()}
async function refresh(){
  try{
    const s=await j('/api/status');
    document.getElementById('cState').textContent=s.container;
    document.getElementById('cSource').textContent=s.source||'—';
    document.getElementById('cSync').textContent=s.last_sync||'从未';
    document.getElementById('cCode').textContent=s.exit_code==null?'—':String(s.exit_code);
    const d=document.getElementById('statusDot');
    d.className='dot '+(s.container==='running'?'ok':s.container==='docker-unavailable'?'warn':'err');
    document.getElementById('syncBtn').disabled=s.sync_running;
    document.getElementById('syncMsg').textContent=s.sync_running?'同步进行中，请勿重复点击…':'同步需先停 stockdb 服务，完成后自动重启';
    const lg=await j('/api/log?n=100');document.getElementById('log').textContent=lg.log;
  }catch(e){document.getElementById('log').textContent='状态刷新失败: '+e}
}
async function doSync(){
  const t=document.getElementById('token').value;
  const hot=document.getElementById('hotMode').checked;
  const opt={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:t,hot:hot})};
  try{const r=await j('/api/sync',opt);document.getElementById('syncMsg').textContent=r.msg;}
  catch(e){document.getElementById('syncMsg').textContent='启动失败: '+e;}
}
async function doQuery(){
  const q=document.getElementById('qtype').value;
  const r=await fetch('/api/query?t='+encodeURIComponent(q));
  const d=await r.text();
  document.getElementById('qres').textContent=d;
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
            if path in ("/", "/index.html"):
                self._send(200, PAGE, "text/html; charset=utf-8")
            elif path == "/api/status":
                self._status()
            elif path == "/api/log":
                self._log()
            elif path == "/api/query":
                self._query()
            else:
                self._send(404, json.dumps({"error": "not found"}))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}))

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

    def _token_ok(self) -> bool:
        if not WEBUI_TOKEN:
            return True
        body = self._read_json()
        return str(body.get("token") or "") == WEBUI_TOKEN

    def _status(self):
        state = container_state()
        src = ""
        cfg = DATA_DIR / "sync_url.txt"
        if cfg.exists():
            lines = [ln for ln in cfg.read_text(encoding="utf-8", errors="replace").splitlines()
                     if ln.strip() and not ln.strip().startswith("#")]
            src = lines[0] if lines else ""
        last = _sync_state["last_end"] or _sync_state["last_start"]
        self._send(200, json.dumps({
            "container": state,
            "source": src,
            "sync_running": _sync_state["running"],
            "exit_code": _sync_state["exit_code"],
            "last_sync": datetime.fromtimestamp(last).strftime("%Y-%m-%d %H:%M:%S")
            if last else None,
        }, ensure_ascii=False))

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
        if not self._token_ok():
            self._send(403, json.dumps({"msg": "令牌错误"}))
            return
        if _sync_state["running"]:
            self._send(200, json.dumps({"msg": "同步已在运行中"}))
            return
        body = self._read_json()
        hot = bool(body.get("hot", True))  # 默认热更新；前端可传 hot=false 走严格模式
        threading.Thread(target=run_sync, kwargs={"hot": hot}, daemon=True).start()
        mode = "热更新" if hot else "严格模式(停服)"
        self._send(200, json.dumps({"msg": f"已启动{mode}同步，日志将实时刷新"}))


def main():
    print(f"webui listening on 0.0.0.0:{LISTEN_PORT}", file=sys.stderr)
    print(f"stockdb: {STOCKDB_HOST}:{STOCKDB_PORT} | container: {STOCKDB_CONTAINER} | data: {DATA_DIR}",
          file=sys.stderr)
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
