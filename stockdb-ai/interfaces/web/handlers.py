"""web.handlers — HTTP Handler（0.9.6 从 app.py 拆分）。

Handler 类与静态服务辅助：对外契约（HTTP 路径/响应结构）与拆分前完全一致。
依赖策略（避免 app ↔ web 循环）：app.py 在模块末尾导入本模块（此时 app 已完整
定义），本模块顶部直接 `from app import ...`——环被打破。

0.9.6 拆分的依赖名（保留在 app.py 的模块级，本模块顶部集中导入）：
  _sync_state/_sync_lock/_scheduler_* 等同步状态、auction_* 用例、data_latest_date、
  health_status、mcp 捕获、mydb 读写、静态服务辅助、SSE 帧、路由表等。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import app  # noqa: E402 - 模块引用（而非 from-import）：DATA_DIR/fetch_upstream_release
# 走 app.* 动态解析，测试 patch.object(app, ...) 才有效（0.9.6 拆分后绑定在
# app 模块上；若 from-import 会在导入期拷贝引用，patch 失效——同 ops.DATA_DIR 教训）。

from app import (  # noqa: E402 - app.py 末尾导入本模块（组合根），此时 app 已完整
    STATIC_DIR,
    WEBUI_UI,
    WEBUI_VERSION,
    XSHG_HOLIDAYS,
    XSHG_HOLIDAYS_THROUGH,
    _CACHEABLE_EXT,
    _MIME,
    _WEB_GET_ROUTES,
    _WEB_POST_ROUTES,
    _auction_backfill_state,
    _auction_fired,
    _get_alerts,
    _mcp_tool_name,
    _scheduler_alive,
    _scheduler_heartbeat,
    _stockdb_breaker,
    _stockdb_breaker_open,
    _sync_lock,
    _sync_state,
    _version_tuple,
    _webui_started,
    auction_run_backfill_async,
    auction_run_close,
    auction_run_collect,
    capture_mcp_call,
    code_stats,
    container_logs,
    container_restart,
    container_state,
    data_coverage,
    data_latest_date,
    disk_usage,
    health_status,
    hk_sync,
    is_trading_day,
    last_sync_summary,
    list_mcp_calls,
    load_history,
    load_schedule,
    mcp_dispatch,
    mcp_stats,
    mirror_latest_date,
    mydb_read,
    mydb_tables,
    mydb_write,
    pybao_tools,
    run_sync,
    save_schedule,
    stockdb_get,
    sync_capability,
    tail_log,
    warehouse_run_async,
    warehouse_status,
)

# 0.9.11：请求体大小上限（防超大 POST 拖垮 NAS；配合 Handler.timeout 防读阻塞挂线程）
_MAX_BODY_BYTES = 16 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    # 0.9.11：每请求 socket 读写超时（此前无超时：客户端声明大 Content-Length 后
    # 停发 → rfile.read 永久阻塞，ThreadingHTTPServer 每连接一线程 → 线程无限累积
    # 拖死 webui）；配合 _read_json 的请求体上限双重防护
    timeout = 30

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
        except (BrokenPipeError, ConnectionResetError):
            return  # 0.9.11：客户端断连短路（同 do_POST）
        except Exception as exc:
            # 0.9.11：500 不回显内部异常细节，日志留痕
            print(f"webui: GET {path} 500: {type(exc).__name__}: {exc}", file=sys.stderr)
            self._send(500, json.dumps({"error": "internal error"}))

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

    def _warehouse_status(self):
        """GET /api/warehouse/status：仓库沉淀状态（0.10.0 W4 运维口，非面板）。"""
        self._send(200, json.dumps(warehouse_status(), ensure_ascii=False))

    def _warehouse_run(self):
        """POST /api/warehouse/run {"days":1-5,"reconcile_sample":n,"backfill":bool}。

        days>1 供小范围测试通道（默认从最新已同步日回看补缺口，幂等：已有分区
        跳过）；backfill=true（0.10.3）为历史回填模式——向已沉淀最早日之前回看
        days 个交易日。异步执行（单飞防重），进度走 GET /api/warehouse/status。
        """
        body = self._read_json()
        days = max(1, min(int(body.get("days") or 1), 5))
        sample = max(1, min(int(body.get("reconcile_sample") or 10), 50))
        backfill = bool(body.get("backfill") or False)
        self._send(200, json.dumps(
            warehouse_run_async(days=days, reconcile_sample=sample, backfill=backfill),
            ensure_ascii=False))

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
        except (BrokenPipeError, ConnectionResetError):
            # 0.9.11：客户端断连直接短路，不再二次写 500（否则二次 _send 再抛，
            # 异常逃逸进 http.server 线程，真实错误与断连无法区分）
            return
        except Exception as exc:
            # 0.9.11：500 响应不回显内部异常细节（路径/实现泄露）；日志留痕
            print(f"webui: POST {path} 500: {type(exc).__name__}: {exc}", file=sys.stderr)
            self._send(500, json.dumps({"error": "internal error"}))

    def _read_json(self) -> dict:
        # 0.9.11：Content-Length 非负且 ≤ 16MB（超大/负值会 rfile.read 阻塞挂线程）；
        # 解析结果必须是 dict（null/[]/123 等合法 JSON 会导致调用方 body.get 崩溃）
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            return {}
        if length > _MAX_BODY_BYTES:
            raise ValueError(f"请求体过大（>{_MAX_BODY_BYTES} 字节）")
        try:
            parsed = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

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
            # 0.9.11：Content-Length 防护（非法/负值/超大——此前负数 read(-1) 挂线程）
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0 or length > _MAX_BODY_BYTES:
                self._send(200, json.dumps({
                    "jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error: 请求体非法"},
                }))
                return
            raw = self.rfile.read(length)
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
        cfg = app.DATA_DIR / "sync_url.txt"
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
            "data_dir": str(app.DATA_DIR),
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
        if not isinstance(codes, list) or not codes:
            self._send(400, json.dumps({"error": "缺少 codes（如 ['00700','00941']）"}))
            return
        # 0.9.11：输入校验——字符串会被按字符迭代成单字符"代码"，dict 迭代键
        if not all(isinstance(c, str) and c.strip() for c in codes):
            self._send(400, json.dumps({"error": "codes 必须是字符串数组"}))
            return
        if not isinstance(years, int) or isinstance(years, bool) or not 1 <= years <= 10:
            self._send(400, json.dumps({"error": "years 必须是 1-10 的整数"}))
            return
        try:
            result = hk_sync([c.strip() for c in codes], years=years)
            self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as exc:
            self._send(500, json.dumps({"error": str(exc)}, ensure_ascii=False))

    def _log(self):
        # 0.9.11：int 防护 + clamp（此前 n=abc → ValueError → 500；负数 tail 语义错乱）
        try:
            n = int(parse_qs(urlparse(self.path).query).get("n", ["100"])[0])
        except (TypeError, ValueError):
            n = 100
        n = max(1, min(n, 5000))
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
        body = self._read_json()
        # 0.9.11：hot 显式布尔解析（此前 bool('false')=True，字符串 false 被误判为
        # 热更新，触发与预期相反的停服/不停服行为）
        raw_hot = body.get("hot", True)
        if isinstance(raw_hot, bool):
            hot = raw_hot
        elif isinstance(raw_hot, str):
            hot = raw_hot.strip().lower() in ("1", "true", "yes", "on")
        else:
            hot = True
        # 0.9.11：探测+启动原子化（锁所有权移交）——此前"探测-释放-再 spawn"存在
        # 竞态窗口：两次并发请求都可探测通过并各起线程，第二个在 run_sync 内
        # acquire 失败静默 return，用户却收到"已启动"。现在探测持锁期间 spawn，
        # 由 worker 线程释放（run_sync 的 acquire(blocking=False) 随即成功）。
        if not _sync_lock.acquire(blocking=False):
            self._send(200, json.dumps({"msg": "同步引擎正在运行中（可能为定时任务），请稍候再试"}))
            return

        def _worker():
            _sync_lock.release()  # 移交：探测占位锁由本线程释放
            run_sync(hot=hot, trigger="manual")

        threading.Thread(target=_worker, daemon=True).start()
        mode = "热更新" if hot else "严格模式(停服)"
        self._send(200, json.dumps({"msg": f"已启动{mode}同步（手动），日志将实时刷新"}))

    def _container_logs(self):
        """stockdb 日志尾部（系统页查看用，读 /data/log.txt）。"""
        # 0.9.11：int 防护 + clamp（此前 tail=abc → ValueError → 500）
        try:
            tail = int(parse_qs(urlparse(self.path).query).get("tail", ["150"])[0])
        except (TypeError, ValueError):
            tail = 150
        tail = max(1, min(tail, 5000))
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
            # 0.9.11：days 非法值回退默认（此前 days="abc" → int() ValueError → 500）
            try:
                days = int(body.get("days") or 60)
            except (TypeError, ValueError):
                days = 60
            self._send(200, json.dumps(auction_run_backfill_async(max(1, min(days, 500))),
                                       ensure_ascii=False))
        else:
            self._send(400, json.dumps(
                {"error": f"非法 task {task!r}；合法值：collect / close / backfill"},
                ensure_ascii=False))

    def _research_migrate(self):
        """POST /api/research/migrate：引擎 mydb 旧研究成果导入 SQLite（0.9.12）。

        0.9.5 存储迁移（引擎 mydb → /data/research.db）后旧数据不自动导入；
        仅补缺失（已存在于 SQLite 的键不覆盖），幂等可重跑。
        """
        try:
            from storage.research_factory import get_research_store as _get_store
            store = _get_store()
            migrate = getattr(store, "migrate_from_engine", None)
            if migrate is None:
                self._send(400, json.dumps(
                    {"error": "当前存储模式不支持迁移（MydbResearchStore 回滚模式无需迁移）"},
                    ensure_ascii=False))
                return
            result = migrate()
            self._send(200, json.dumps(result, ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001 - 迁移失败可观测
            print(f"webui: 研究成果迁移失败: {type(exc).__name__}: {exc}", file=sys.stderr)
            self._send(500, json.dumps({"error": f"迁移失败: {exc}"}, ensure_ascii=False))

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
            upstream = app.fetch_upstream_release()
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
            "data_dir": str(app.DATA_DIR),
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
    upstream = app.fetch_upstream_release()
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