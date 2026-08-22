"""services.warehouse_tasks — 仓库沉淀编排（0.10.0 W4；D11 同构：编排在服务层、执行在数据层）。

用例：warehouse_run（每日沉淀，默认 16:40 触发）/ warehouse_scheduler_loop（调度线程）。
流程：就绪门（data_latest >= today）→ 全市场快照（TRADED 行 = 当日日K）→
sink 写分区 + codes 刷新 → reconcile 对账（三板斧）→ records 日检 + 告警。
复权快照：周一（或首次）触发，依赖注入的 adjust_provider（引擎键空间无批量端点，
SDK 通道接入前为 None → 跳过，ROADMAP 延后项登记）。

依赖纪律：不 import storage.warehouse（C3，层边界测试强制）——sink/reconcile/
availability 经注入点由 app.py（组合根）绑定；引擎快照/交易日判定同打板注入模式。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime

import config
from ops.alerts import notify_alert
from ops.logging import log
from storage.records import append as _records_append

# ---- 依赖注入点（app.py 装配时绑定；测试直接赋值，见 test_warehouse W4 段） ----
query_snapshot = None   # interfaces.mcp.stockdb_mcp_server.query_point_snapshot
data_latest = None      # app.data_latest_date（最新已同步交易日探针）
is_trading_day = None   # app.is_trading_day
sink = None             # storage.warehouse.sink（模块；.catalog/.layout 为其子模块属性）
reconcile_daily = None  # storage.warehouse.reconcile.reconcile_daily
warehouse_root = None   # () -> Path（storage.warehouse.layout.root_dir）
availability = None     # storage.warehouse.availability
refresh_views = None    # storage.warehouse.engine.get_engine().refresh_views
adjust_provider = None  # () -> list[dict]（复权因子全量行；未接 SDK 通道前为 None → 跳过快照）

# 周度复权快照：周一沉淀日顺带全量刷新（快照小、全量幂等）
_ADJUST_WEEKDAYS = {0}

_wh_fired: dict = {}  # 日级防重守卫：{date: {"fired": bool, "attempts": int, "next_retry": ts}}
_wh_run_state: dict = {"running": False, "started": None, "finished": None, "result": None}
_RETRY_INTERVAL = 600  # 未就绪/失败重试间隔（10 分钟）
_RETRY_UNTIL = "20:00"  # 超过此时刻放弃当日沉淀（告警收口）


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _snapshot_points(date: str) -> list[dict]:
    """全市场单日快照（limit=0 = 不截断，一次往返；快照内部已走 SDK 批量快路径）。"""
    return (query_snapshot({"date": date, "limit": 0}) or {}).get("points") or []


def warehouse_run(days: int = 1, reconcile_sample: int = 10,
                  require_today: bool = False) -> dict:
    """沉淀任务：把已同步交易日沉淀进仓库（幂等；days>1 供小范围测试通道，上限 5）。

    就绪门：require_today=True（调度路径）时要求 data_latest >= 今日（当日 K 线
    已同步——与打板收口同判定），未就绪返回 reason 以"未就绪："开头（调度层重试）；
    手动路径（HTTP 运维口）不设此门，沉淀到最新已同步日为止。
    对账：行数 + 同源字段回读 + （有异源行时）异源开盘/昨收；issues 非空 → 告警 + 日检记录。
    """
    if availability is None or not availability()[0]:
        return {"ok": False, "reason": "warehouse 不可用（duckdb 缺失或 WAREHOUSE_ENABLED=0）"}
    days = max(1, min(5, int(days)))
    root = warehouse_root()
    try:
        latest = str(data_latest(force=True) or "").replace("-", "")
        if not latest:
            return {"ok": False, "reason": "未就绪：无法确定最新已同步交易日"}
        if require_today:
            today = datetime.now().strftime("%Y%m%d")
            if latest < today:
                return {"ok": False,
                        "reason": f"未就绪：数据未同步（最新 {latest} < 今日 {today}）"}
        watermark = sink.catalog.get_watermark(root, "daily") if hasattr(sink, "catalog") else None

        results = []
        # 逐日沉淀（正常调度 days=1；手动小范围测试最多回看 5 日）
        target = latest
        targets = []
        for _ in range(days):
            if watermark and target <= watermark:
                break  # 只补 watermark 之后的缺口，不重复沉淀
            targets.append(target)
            target = _prev_date(target)
        for t in reversed(targets):  # 旧 → 新
            points = [p for p in _snapshot_points(t)
                      if isinstance(p, dict) and p.get("status") == "TRADED"]
            if not points:
                results.append({"date": t, "status": "empty"})
                continue
            w = sink.write_daily(root, t, points)
            sink.write_codes(root, [{"code": p.get("code"), "name": p.get("name")}
                                    for p in points])
            rec = reconcile_daily(root, t, points,
                                  sedimented_rows=w.get("rows", 0),
                                  dropped_nonfinite=w.get("dropped_nonfinite", 0),
                                  sample=reconcile_sample)
            results.append({"date": t, "write": w, "reconcile": rec})
            if not rec["ok"]:
                notify_alert("warn", "warehouse",
                             f"沉淀对账差异 {t}: {rec['issues'][:5]}")
            _records_append({"date": t, "task": "warehouse_sediment",
                             "ok": rec["ok"], "rows": w.get("rows", 0),
                             "traded": rec["traded"], "issues": rec["issues"][:5],
                             "at": _now_iso()})

        # 周度复权快照（周一 or 从未刷新）：复权因子表小，全量幂等
        try:
            if datetime.now().weekday() in _ADJUST_WEEKDAYS or not sink.catalog.get_meta(root, "adjust:snapshot"):
                adjust_rows = _adjust_rows(latest)
                if adjust_rows:
                    sink.write_adjust_snapshot(root, latest, adjust_rows)
        except Exception as exc:  # noqa: BLE001 - 复权快照失败不阻塞日K沉淀
            log(f"⚠️ 仓库复权快照失败（不阻塞日K）：{exc}")

        if refresh_views is not None:
            try:
                refresh_views()
            except Exception:  # noqa: BLE001 - 视图刷新失败下次重建
                pass
        ok = all(r.get("reconcile", {}).get("ok", True) for r in results)
        return {"ok": ok, "days": results, "finished_at": _now_iso()}
    except Exception as exc:  # noqa: BLE001 - 单块降级
        log(f"⚠️ 仓库沉淀失败：{exc}")
        notify_alert("error", "warehouse", f"沉淀任务失败：{exc}")
        return {"ok": False, "reason": str(exc), "finished_at": _now_iso()}


def _prev_date(d: str) -> str:
    from datetime import timedelta
    dt = datetime.strptime(d, "%Y%m%d") - timedelta(days=1)
    return dt.strftime("%Y%m%d")


def _adjust_rows(latest: str) -> list[dict]:
    """复权因子全量行（经注入的 adjust_provider；None → 首版跳过快照，见模块头注）。"""
    if adjust_provider is None:
        return []
    return adjust_provider() or []


def warehouse_run_async(days: int = 1, reconcile_sample: int = 10) -> dict:
    """异步触发沉淀（HTTP 运维口用；单飞防重，状态进 _wh_run_state）。"""
    if _wh_run_state["running"]:
        return {"ok": False, "async": True, "reason": "沉淀任务已在运行中",
                "started": _wh_run_state["started"]}

    def _worker():
        _wh_run_state.update(running=True, started=_now_iso(), finished=None, result=None)
        try:
            result = warehouse_run(days=days, reconcile_sample=reconcile_sample)
        except Exception as exc:  # noqa: BLE001 - 状态落库，绝不外抛
            result = {"ok": False, "reason": str(exc)}
        _wh_run_state.update(running=False, finished=_now_iso(), result=result)
        log(f"📊 仓库沉淀完成：ok={result.get('ok')} {result.get('days') or result.get('reason') or ''}")

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "async": True, "reason": "沉淀已启动（后台执行，GET /api/warehouse/status 查进度）"}


def warehouse_status() -> dict:
    """运维状态（GET /api/warehouse/status）：可用性 + 调度守卫 + 任务状态 + 仓库元数据。"""
    available = availability is not None and availability()[0]
    root = warehouse_root() if warehouse_root is not None else None
    out = {"available": available, "running": _wh_run_state["running"],
           "started": _wh_run_state["started"], "finished": _wh_run_state["finished"],
           "last_result": _wh_run_state["result"],
           "sediment_time": config.WAREHOUSE_SEDIMENT_TIME}
    if available and root is not None and sink is not None:
        try:
            out["watermark_daily"] = sink.catalog.get_watermark(root, "daily")
            out["adjust_snapshot"] = sink.catalog.get_meta(root, "adjust:snapshot")
        except Exception as exc:  # noqa: BLE001 - 状态查询不抛
            out["catalog_error"] = str(exc)
    return out


def warehouse_scheduler_loop() -> None:
    """仓库沉淀调度线程：5s 轮询，交易日 WAREHOUSE_SEDIMENT_TIME 后触发（默认 16:40）。

    就绪门失败（数据未同步）→ 10 分钟重试至 20:00，超时告警收口；成功/彻底失败
    均置位守卫（当日不重复触发）。进程重启守卫清空，重跑幂等（sink 跳过已有分区）。
    """
    while True:
        try:
            dt_now = datetime.now()
            if is_trading_day is not None and not is_trading_day(dt_now.date()):
                time.sleep(5)
                continue
            today = dt_now.strftime("%Y%m%d")
            now_hm = dt_now.strftime("%H:%M")
            guard = _wh_fired.setdefault(today, {"fired": False, "attempts": 0, "next_retry": 0.0})
            if (now_hm >= config.WAREHOUSE_SEDIMENT_TIME and not guard["fired"]
                    and time.time() >= guard["next_retry"]):
                try:
                    if availability is None or not availability()[0]:
                        guard["fired"] = True  # 仓库不可用：当日不再空转
                        log("📊 仓库沉淀跳过：warehouse 不可用（duckdb 缺失或已关闭）")
                        continue
                    res = warehouse_run(days=1, require_today=True)
                    if res.get("ok"):
                        guard["fired"] = True
                        days = res.get("days") or []
                        w = days[-1].get("write", {}) if days else {}
                        log(f"📊 仓库沉淀完成（{today}）: rows={w.get('rows')} "
                            f"markets={w.get('markets')} reconcile={days[-1].get('reconcile', {}).get('ok') if days else None}")
                    elif str(res.get("reason") or "").startswith("未就绪："):
                        # 就绪门未过：数据同步未收口 → 延后重试
                        guard["attempts"] += 1
                        guard["next_retry"] = time.time() + _RETRY_INTERVAL
                        if now_hm >= _RETRY_UNTIL:
                            guard["fired"] = True
                            notify_alert("error", "warehouse",
                                         f"{today} 沉淀就绪门至 {_RETRY_UNTIL} 未过：{res.get('reason')}")
                    else:
                        guard["fired"] = True  # 硬失败：告警已发（warehouse_run 内），当日收口
                        log(f"⚠️ 仓库沉淀失败（当日收口）：{res.get('reason')}")
                except Exception as exc:  # noqa: BLE001 - 调度线程绝不死
                    guard["fired"] = True
                    log(f"⚠️ 仓库调度异常（当日收口）：{exc}")
                    notify_alert("error", "warehouse", f"沉淀调度异常：{exc}")
            time.sleep(5)
        except Exception:  # noqa: BLE001 - 最外层兜底
            time.sleep(30)
