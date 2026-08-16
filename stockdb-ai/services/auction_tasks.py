"""services.auction_tasks — 打板用例（应用服务层，0.9.2 批次 4 从 app.py 搬迁）。

用例：auction_run_collect（09:26 采集）/ auction_run_close（16:30 收口对账）/
auction_run_backfill（历史回填）/ auction_scheduler_loop（调度线程）。
编排职责：拉数据（storage/注入快照）→ 算指标（auction_metrics 领域）→ 存（mydb）→
降级与告警（ops）。行为与 app.py 搬迁前完全一致（0.8.x 验收基线）。

依赖纪律：本模块不 import 接口层（web/mcp）——对外部能力的引用一律走"注入点"，
由 app.py（组合根）装配时绑定（见模块底部注释）。
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta

import config  # 模块引用（打板调度触发点等）
from ops.alerts import notify_alert
from ops.logging import log
from storage.records import append as _records_append  # 日检记录（0.9.2 批次 7）

# ---- 依赖注入点（app.py 装配时绑定）----
# 0.9.2：query_snapshot/data_latest/is_fq_event/is_trading_day（接口层/探针/日历）
# 0.9.5（M5）：research_store —— 研究成果仓储（SqliteResearchStore 主线 /
#   MydbResearchStore 回滚，见 storage/research_factory.py）；应用层只依赖接口，
#   不感知存储实现（D3/D8 兑现）。
query_snapshot = None   # mcp.stockdb_mcp_server.query_point_snapshot（全市场单日快照）
data_latest = None      # app.data_latest_date（最新交易日探针）
is_fq_event = None      # mcp.pybao_tools.is_fq_event_date（除权事件日判定）
is_trading_day = None   # app.is_trading_day（交易日历判定）
research_store = None   # storage.research_store.ResearchStore 实现（工厂注入）


def _now_iso() -> str:
    """当前本地时间 ISO（秒级）：2026-08-14T21:52:30。"""
    return datetime.now().isoformat(timespec="seconds")


# 惰性 import：任务A/B/C（quote_sources/auction_list/auction_metrics）与 MCP 快照
# 任一缺失 → AUCTION_MODULES_AVAILABLE=False，采集/收口返回 {ok:False}（不拖垮 webui）。
# D11（0.9.9）：采集执行在数据层 storage/providers/quote_sources（编排在本层，执行归数据层）。
try:
    from storage.providers.quote_sources import fetch_quotes as _auction_fetch_quotes
    from core.auction_list import compute_limitup_list as _auction_compute_limitup_list
    from core.auction_metrics import (
        METRICS as AUCTION_METRICS,
        compute_metrics as _auction_compute_metrics,
        load_series as _auction_load_series,
        append_series as _auction_append_series,
        build_metrics_payload as _auction_build_payload,
        list_key as _auction_list_key,
        metrics_key as _auction_metrics_key,
        percentile_rank as _auction_percentile_rank,
        strength_label as _auction_strength_label,
        series_key as _auction_series_key,
    )
    AUCTION_MODULES_AVAILABLE = True
    AUCTION_IMPORT_ERROR = ""
except Exception as _auction_import_exc:  # noqa: BLE001 - 任一模块缺失时优雅降级
    AUCTION_MODULES_AVAILABLE = False
    AUCTION_IMPORT_ERROR = f"{type(_auction_import_exc).__name__}: {_auction_import_exc}"


_auction_fired: dict = {}  # 日级防重触发守卫：{date: {"collect": bool, "close": bool}}
_auction_backfill_state: dict = {"running": False, "started": None, "finished": None,
                                 "result": None}  # 回填任务状态（0.8.2 异步化 + 单飞防重）


def _auction_apply_reference(points: list[dict], date: str,
                             lag_close_by_code: dict | None) -> list[dict]:
    """涨停判定参考价替换（0.8.15，命理档案验收修正版）。

    0.8.14 曾对全部历史行统一套复权因子反推（ref = pre_close × cum_latest/cum_D），
    验收证实污染不均匀（513/517 条为误删）——废弃。正确口径：
      - 普通日：参考价 = 上一实际成交日未复权收盘（lag close，date 前一交易日快照）
      - 除权日（因子表当日有事件）：参考价 = 当日 pre_close（法定除权参考价，可信）
      - 该股前一交易日无交易（停牌跨日）或 lag 缺失 → 原值兜底
    """
    if not points:
        return points
    lag_close_by_code = lag_close_by_code or {}
    try:
        fixed = []
        for p in points:
            if not isinstance(p, dict):
                fixed.append(p)
                continue
            code = str(p.get("code") or "")
            pc = p.get("prev_close")
            if not code or pc is None:
                fixed.append(p)
                continue
            if is_fq_event is not None and is_fq_event(code, date):
                fixed.append(p)  # 除权日：pre_close = 法定参考价，原样
                continue
            lag = lag_close_by_code.get(code)
            if lag is None:
                fixed.append(p)  # 停牌跨日/无历史：原值兜底
                continue
            fixed.append({**p, "prev_close": float(lag)})
        return fixed
    except Exception:  # noqa: BLE001 - 重建失败按原值降级
        return points


def _auction_lag_close(points: list[dict]) -> dict:
    """前一交易日快照 → {code: 未复权收盘}（仅 TRADED 有效行）。"""
    out: dict = {}
    for p in points or []:
        if not isinstance(p, dict):
            continue
        if p.get("status") != "TRADED":
            continue
        try:
            out[str(p["code"])] = float(p["close"])
        except (TypeError, ValueError, KeyError):
            continue
    return out


def _auction_load_codes(trade_date: str) -> list[str]:
    """读研究成果清单（research store）的 codes；缺失/损坏/为空 → []。

    返回 [] 时调用方走兜底现算路径（query_point_snapshot + compute_limitup_list）。
    0.9.5（M5）：清单存 research store（SqliteResearchStore 主线 / mydb 回滚适配）。
    """
    try:
        if research_store is None:
            return []
        data = research_store.read_list(trade_date)
        if isinstance(data, dict) and isinstance(data.get("codes"), list):
            return [str(c) for c in data["codes"]]
    except Exception:  # noqa: BLE001 - 清单缺失/损坏 → 兜底现算
        return []
    return []


def _research_series_read(key: str):
    """打板序列读取（0.9.5：research store 接口适配；key=打板序列:<metric>）。"""
    if research_store is None:
        return None
    try:
        return research_store.read_series(str(key).split(":")[-1])
    except Exception:  # noqa: BLE001 - 序列缺失/损坏 → 调用方按空序列处理
        return None


def _research_series_write(key: str, value) -> None:
    """打板序列写入（0.9.5：research store 接口适配；覆盖写幂等）。"""
    if research_store is not None:
        research_store.write_series(str(key).split(":")[-1], value)


def _auction_prev_trade_date(d8: str, n: int = 1) -> str:
    """从 d8 往前找第 n 个交易日（YYYYMMDD），用于清单缺失时的兜底现算。

    防御（0.9.2）：is_trading_day 注入点未装配（如单测）时按自然日回退并设上限，
    避免无限循环；正常装配后按交易日回退。
    """
    d = datetime.strptime(d8, "%Y%m%d").date()
    found = 0
    guard = 0
    while found < n:
        d -= timedelta(days=1)
        guard += 1
        if guard > 400:  # 约一年自然日上限：注入点缺失时防御死循环
            raise ValueError(f"_auction_prev_trade_date: 400 日内未找到交易日（{d8}）")
        if is_trading_day is None or is_trading_day(d):
            found += 1
    return d.strftime("%Y%m%d")


def auction_run_collect() -> dict:
    """09:26 打板竞价采集任务（幂等，可手动重跑）。

    步骤：① 读今日清单（缺/空 → query_point_snapshot 前一交易日兜底现算）；
    ② fetch_quotes 批量采集 → 写竞价快照:<今日>:<代码>（state=captured，含
       raw/contract/fetched_at/known_at=当日 09:25）；
    ③ compute_metrics + load_series（读近 59 日序列）→ build_metrics_payload
       （value_source=auction）→ 写打板指标:<今日>。
    单块 try/except 降级：任何异常 → {ok:False} + log + 告警，绝不外抛。
    """
    if not AUCTION_MODULES_AVAILABLE:
        return {"ok": False, "reason": f"打板模块未就绪：{AUCTION_IMPORT_ERROR}",
                "collected": 0, "errors_count": 0, "metrics": None, "rank_60d": None, "strength_60d": None}
    today = datetime.now().strftime("%Y%m%d")
    try:
        # ① 清单：昨日 16:30 已算好落库；缺失/为空 → 前一交易日快照兜底现算
        codes = _auction_load_codes(today)
        if not codes:
            prev = _auction_prev_trade_date(today)
            snaps = query_snapshot({"date": prev, "limit": 0})
            # 0.8.15：判定参考价 = prev 前一交易日未复权收盘；除权日例外
            prev2 = _auction_prev_trade_date(prev)
            snaps2 = query_snapshot({"date": prev2, "limit": 0})
            pts1 = _auction_apply_reference(snaps.get("points") or [], prev,
                                            _auction_lag_close(snaps2.get("points") or []))
            listing = _auction_compute_limitup_list(pts1)
            codes = listing.get("codes") or []
            log(f"📊 打板清单缺失，已兜底现算（{prev}）→ {len(codes)} 只")
        if not codes:
            return {"ok": False, "reason": "清单为空且兜底现算无结果",
                    "collected": 0, "errors_count": 0, "metrics": None, "rank_60d": None, "strength_60d": None}

        # ② 采集竞价价（fetch_quotes 内部主源腾讯/备源东财降级）→ 逐条写快照
        quote = _auction_fetch_quotes(codes)
        ok_items = quote.get("ok") or []
        errors = quote.get("errors") or []
        items = []
        for snap in ok_items:
            row = dict(snap)
            row["state"] = "captured"          # 生命周期中间态：captured → reconciled
            row["contract"] = quote.get("contract")
            row["fetched_at"] = quote.get("fetched_at")
            row["known_at"] = f"{today[:4]}-{today[4:6]}-{today[6:8]}T09:25:00"  # 业务口径：9:25 竞价=开盘
            row["diff_pct"] = None             # 对账后回填
            row["reconciled_at"] = None
            items.append((str(snap["code"]), row))  # 0.8.6：pybao 存原生 dict，不存 JSON 字符串
        if items:                              # 空 items 时仓储写会抛 ValueError
            research_store.write_snapshots(today, dict(items))

        # ③ 当日业务值 + 60 日分位（竞价版；序列由 16:30 K线权威版追加，此处只读近 59 日）
        metrics = _auction_compute_metrics(ok_items)
        series_by = {m: _auction_load_series(_research_series_read, m) for m in AUCTION_METRICS}
        payload = _auction_build_payload(ok_items, series_by,
                                         computed_at=_now_iso(), value_source="auction")
        research_store.write_metrics(today, payload)
        log(f"📊 打板竞价采集（{today}）: {len(ok_items)} 只快照（errors={len(errors)}），"
            f"premium_mean={metrics.get('premium_mean')}, n={metrics.get('n_samples')}")
        _records_append({"date": today, "task": "collect", "ok": True,
                         "collected": len(ok_items), "errors": len(errors),
                         "metrics": metrics, "at": _now_iso()})
        _daily_backup()  # 0.9.5 M5：日检后自动备份研究成果库
        return {"ok": True, "collected": len(ok_items), "errors_count": len(errors),
                "metrics": metrics, "rank_60d": payload.get("rank_60d"), "strength_60d": payload.get("strength_60d")}
    except Exception as exc:  # noqa: BLE001 - 单块降级：采集异常不抛给调度线程/HTTP
        log(f"⚠️ 打板竞价采集失败（{today}）: {exc}")
        try:
            notify_alert("error", "打板采集", f"竞价采集失败（{today}）: {exc}")
        except Exception:  # noqa: BLE001 - 告警通道异常忽略
            pass
        _records_append({"date": today, "task": "collect", "ok": False,
                         "reason": str(exc), "at": _now_iso()})
        return {"ok": False, "reason": str(exc), "collected": 0, "errors_count": 0,
                "metrics": None, "rank_60d": None}


def auction_run_close() -> dict:
    """16:30 打板收口对账任务（幂等，可手动重跑）。

    步骤：① data_latest_date(force=True) >= 今日（未就绪 → {ok:False,"当日数据未同步"}）；
    ② 明日清单：今日 K 线快照 → compute_limitup_list → 写清单:<明日>:limitup_non_yizi；
    ③ 对账：今日竞价快照 open_price vs K线 open → 逐条回写 state=reconciled /
       diff_pct / reconciled_at，|diff|>0.5% → log + 告警（长期监测口径偏差）；
    ④ K线权威指标：清单口径 points（close/open/prev_close）→ compute_metrics →
       append_series（追加并裁剪 60 日）→ build_metrics_payload（value_source=kline）
       → 覆盖写打板指标:<今日>。
    单块 try/except 降级：任何异常 → {ok:False} + log + 告警，绝不外抛。
    """
    if not AUCTION_MODULES_AVAILABLE:
        return {"ok": False, "reason": f"打板模块未就绪：{AUCTION_IMPORT_ERROR}",
                "list_count": 0, "reconciled": 0, "diff_alerts": 0, "metrics": None}
    today = datetime.now().strftime("%Y%m%d")
    try:
        # ① 当日 K 线已同步校验（force 绕过 8s 缓存，收口口径必须实时）
        latest = str(data_latest(force=True) or "").replace("-", "")
        if not latest or latest < today:
            return {"ok": False, "reason": "当日数据未同步",
                    "list_count": 0, "reconciled": 0, "diff_alerts": 0, "metrics": None,
                    "latest_date": latest or None}

        # 全市场今日时点快照（K线权威源，②③④ 共用）
        points = (query_snapshot({"date": today, "limit": 0}) or {}).get("points") or []
        points_by_code = {str(p.get("code")): p for p in points}

        # ② 明日清单：今日非一字板涨停（compute_limitup_list 内部已剔除一字板）
        # 0.8.15：判定参考价 = 昨日未复权收盘（prev_day 快照）；除权日例外
        prev_day = _auction_prev_trade_date(today)
        prev_points = (query_snapshot({"date": prev_day, "limit": 0}) or {}).get("points") or []
        listing = _auction_compute_limitup_list(
            _auction_apply_reference(points, today, _auction_lag_close(prev_points)))
        tomorrow = (datetime.strptime(today, "%Y%m%d").date() + timedelta(days=1)).strftime("%Y%m%d")
        list_payload = {"codes": listing.get("codes") or [],
                        "computed_at": _now_iso(),
                        "contract": "limitup-non-yizi-v1"}
        research_store.write_list(tomorrow, list_payload)

        # ③ 对账：今日 09:26 竞价快照 vs 今日 K线开盘价（口径偏差长期监测）
        # 0.9.5（M5）：快照存 research store（read_snapshots 返回 {code: row}）
        snap_rows = (research_store.read_snapshots(today)
                     if research_store is not None else {})
        reconcile_items: list[tuple] = []
        reconciled, diff_alerts = 0, 0
        for code, row in snap_rows.items():
            if not isinstance(row, dict):
                continue
            pt = points_by_code.get(str(code))
            kline_open = pt.get("open") if pt else None
            auction_open = row.get("open_price")
            diff_pct = None
            try:
                if auction_open is not None and kline_open:
                    diff_pct = (float(auction_open) - float(kline_open)) / float(kline_open)
            except (TypeError, ValueError, ZeroDivisionError):
                diff_pct = None
            row["state"] = "reconciled"            # 生命周期终态：对账结论 + 竞价证据
            row["diff_pct"] = diff_pct
            row["reconciled_at"] = _now_iso()
            reconcile_items.append((str(code), row))  # 原生 dict
            reconciled += 1
            if diff_pct is not None and abs(diff_pct) > 0.005:   # ±0.5% 口径偏差阈值
                diff_alerts += 1
                msg = (f"竞价/开盘口径偏差 {code}: 竞价 {auction_open} vs K线 {kline_open}"
                       f"（{diff_pct:+.2%}）")
                log(f"⚠️ 对账 {msg}")
                try:
                    notify_alert("warning", "打板对账", f"{today} {msg}")
                except Exception:  # noqa: BLE001 - 告警通道异常不阻塞对账
                    pass
        if reconcile_items:
            research_store.write_snapshots(today, dict(reconcile_items))

        # ④ K线权威指标：与 09:26 竞价版同一清单口径（保证两版可比）；
        #    清单缺失 → 当日快照代码 → 全市场（逐级兜底）
        #    0.8.13：溢价分母 = T-1 收盘价（补拉 T-1 快照；T 日 bar 的 pre_close
        #    在除权除息日为调整昨收，混入分红会失真）
        codes = _auction_load_codes(today) or list(snap_rows)
        prev_close_by_code = _auction_lag_close(prev_points)
        missing_open = 0  # 0.9.0 M1 边界 c：清单股取不到 (open, prev_close) 有效对
        if codes:
            code_set = set(codes)
            snapshots = [{"code": p["code"], "open_price": p.get("open"),
                          "prev_close": prev_close_by_code.get(str(p.get("code")))}
                         for p in points if str(p.get("code")) in code_set]
            # 守恒：候选 = n_samples + missing_open_count（无 bar / open 缺失 /
            # T-1 无收盘均计 missing_open；0.9.0 之前静默丢弃）
            missing_open = len(code_set) - sum(
                1 for s in snapshots
                if s["open_price"] is not None and s["prev_close"] is not None)
            snapshots = [s for s in snapshots
                         if s["open_price"] is not None and s["prev_close"] is not None]
        else:
            snapshots = [{"code": p["code"], "open_price": p.get("open"),
                          "prev_close": prev_close_by_code.get(str(p.get("code")))}
                         for p in points]
            # 兜底全市场路径无候选语义：prev_close 缺失防御剔除（0.8.13 语义不变）
            snapshots = [s for s in snapshots if s["prev_close"] is not None]
        metrics = _auction_compute_metrics(snapshots)
        metrics = {**metrics, "missing_open_count": missing_open}
        series_by = {}
        for m in AUCTION_METRICS:
            value = metrics.get(m)
            if value is None:      # n_samples=0 时指标为 None：不入序列
                continue
            series_by[m] = _auction_append_series(
                _research_series_read, _research_series_write, m, value, today)
        payload = _auction_build_payload(snapshots, series_by,
                                         computed_at=_now_iso(), value_source="kline")
        research_store.write_metrics(today, payload)
        log(f"📊 打板收口对账（{today}）: 明日清单 {len(listing.get('codes') or [])} 只，"
            f"对账 {reconciled} 条（偏差告警 {diff_alerts}），"
            f"premium_mean={metrics.get('premium_mean')}")
        _records_append({"date": today, "task": "close", "ok": True,
                         "list_count": len(listing.get("codes") or []),
                         "reconciled": reconciled, "diff_alerts": diff_alerts,
                         "metrics": metrics, "at": _now_iso()})
        _daily_backup()  # 0.9.5 M5：日检后自动备份研究成果库
        return {"ok": True, "list_count": len(listing.get("codes") or []),
                "reconciled": reconciled, "diff_alerts": diff_alerts,
                "metrics": metrics, "rank_60d": payload.get("rank_60d"), "strength_60d": payload.get("strength_60d")}
    except Exception as exc:  # noqa: BLE001 - 单块降级：异常不抛给调度线程/HTTP
        log(f"⚠️ 打板收口对账失败（{today}）: {exc}")
        try:
            notify_alert("error", "打板对账", f"收口任务失败（{today}）: {exc}")
        except Exception:  # noqa: BLE001 - 告警通道异常忽略
            pass
        _records_append({"date": today, "task": "close", "ok": False,
                         "reason": str(exc), "at": _now_iso()})
        return {"ok": False, "reason": str(exc), "list_count": 0, "reconciled": 0,
                "diff_alerts": 0, "metrics": None}


def _auction_points_for_codes(date: str, codes: list[str]) -> list[dict]:
    """按 200/批分块拉取指定代码单日点集并合并（MCP 显式清单硬上限 1-200，0.8.9）。

    打板溢价日清单可达 200+ 只（全市场口径修复后实测 256），显式路径一次传超限
    会 ValueError——分块后逐批拉取、合并去重（by_code dict 天然去重）。
    """
    points: list[dict] = []
    for i in range(0, len(codes), 200):
        chunk = codes[i:i + 200]
        points.extend(
            (query_snapshot({"date": date, "codes": chunk, "limit": 0}) or {})
            .get("points") or [])
    return points


def auction_run_backfill(days: int = 60) -> dict:
    """历史序列回填任务（0.8.1 冷启动修复：首跑前序列为空 → 当日分位无分母）。

    用历史 K 线把过去 `days` 个交易日的业务指标（溢价均值/成功率）逐日重算：
      - 打板序列:<metric>：滚动序列（分位分母），按时间正序裁剪至 60
      - 打板指标:<T>：逐日指标 + 当日可得的滚动分位（只用 T 之前的值，无未来函数），
        value_source="kline"，供研究直接读历史
    幂等：全量重算覆盖写（确定性，重跑结果一致）。

    流程：以最新已同步交易日 L 为起点，逐日回推：T-1 K线算清单 → T K线算溢价指标。
    """
    if not AUCTION_MODULES_AVAILABLE:
        return {"ok": False, "reason": f"打板模块未就绪：{AUCTION_IMPORT_ERROR}",
                "backfilled_days": 0, "series": {}}
    try:
        latest = str(data_latest() or "").replace("-", "")
        if not latest:
            return {"ok": False, "reason": "无法确定最新交易日", "backfilled_days": 0, "series": {}}

        # 第一遍：按时间正序收集 (date, metrics)，从最旧到最新
        rows: list[tuple[str, dict]] = []
        t = latest
        for _ in range(days):
            t1 = _auction_prev_trade_date(t)          # T-1 交易日（涨停判定日）
            pts1 = (query_snapshot({"date": t1, "limit": 0}) or {}).get("points") or []
            # 0.8.15：判定参考价 = 上一实际成交日未复权收盘（拉 t2 快照）；
            # 除权日例外（当日 pre_close = 法定参考价）
            t2 = _auction_prev_trade_date(t1)
            pts2 = (query_snapshot({"date": t2, "limit": 0}) or {}).get("points") or []
            pts1 = _auction_apply_reference(pts1, t1, _auction_lag_close(pts2))
            codes = _auction_compute_limitup_list(pts1).get("codes") or []
            # T-1 收盘价（0.8.13：溢价分母 = "t-1 日收盘价"——T 日 bar 的 pre_close
            # 在除权除息日为交易所调整昨收，会混入分红使溢价失真，不可用作分母）
            prev_close_by_code = {str(p.get("code")): p.get("close") for p in pts1}
            snaps = []
            missing_open = 0  # 0.9.0 M1 边界 c：板日涨停但指标日无有效 (open, prev_close)
            if codes:
                # 溢价日只查清单股（免全扫）；清单可能 >200 只 → 分块拉取（0.8.9）
                pts_t = _auction_points_for_codes(t, codes)
                by_code = {str(p.get("code")): p for p in pts_t}
                for c in codes:
                    p = by_code.get(c)
                    prev_close = prev_close_by_code.get(c)
                    if p and p.get("open") is not None and prev_close is not None:
                        snaps.append({"code": c, "open_price": p.get("open"),
                                      "prev_close": prev_close})
                    else:
                        missing_open += 1  # 守恒：候选 = n_samples + missing_open_count
            m = _auction_compute_metrics(snaps)
            m = {**m, "missing_open_count": missing_open}
            if m["n_samples"] > 0:
                rows.append((t, m))
            t = t1
        rows.reverse()  # 最旧 → 最新

        # 第二遍：写逐日指标 + 用"当日之前"的值算滚动分位（无未来函数）
        # 0.8.11：分位口径改为用户拍板定义——此前 60 个有效观测中严格低于当日值
        # 的天数/60；不足 60 个观测 → None（历史回填日因此无分位，属预期）
        all_vals = {metric: [] for metric in AUCTION_METRICS}
        for (d, m) in rows:
            rank = {}
            for metric in AUCTION_METRICS:
                if m.get(metric) is not None:
                    rank[metric] = _auction_percentile_rank(m[metric], all_vals[metric][-60:])
            strength = {metric: _auction_strength_label(rank.get(metric))
                        for metric in AUCTION_METRICS}
            payload = {"metrics": m,
                       "rank_60d": rank,
                       "strength_60d": strength,
                       "window": 60, "n_samples": m["n_samples"],
                       "computed_at": _now_iso(), "value_source": "kline",
                       "contract": "auction-metric-v1"}
            research_store.write_metrics(d, payload)
            for metric in AUCTION_METRICS:
                if m.get(metric) is not None:
                    all_vals[metric].append(m[metric])

        # 写序列（正序、裁剪 60）：周一 09:26 分位的分母
        series_result = {}
        for metric in AUCTION_METRICS:
            vals = all_vals[metric][-60:]
            seq = {"metric": metric, "values": vals,
                   "window": 60, "contract": "auction-metric-v1",
                   "updated_at": _now_iso(), "source": "backfill-kline"}
            research_store.write_series(metric, seq)
            series_result[metric] = len(vals)
        return {"ok": True, "backfilled_days": len(rows), "series": series_result}
    except Exception as exc:  # noqa: BLE001 - 单块降级
        log(f"⚠️ 打板历史回填失败：{exc}")
        return {"ok": False, "reason": str(exc), "backfilled_days": 0, "series": {}}


def auction_run_backfill_async(days: int = 60) -> dict:
    """异步触发历史回填（0.8.2）：60 天全市场扫描是分钟级重活，不能占请求线程。

    单飞防重：已在运行 → 返回 {ok:False, reason:"回填已在运行中"}，绝不并发第二份；
    否则后台线程执行 auction_run_backfill，状态进 _auction_backfill_state
    （GET /api/auction/status 查询），请求立即返回 {ok:True, async:True}。
    幂等：重跑覆盖写，结果确定性。
    """
    if _auction_backfill_state["running"]:
        return {"ok": False, "async": True, "reason": "回填已在运行中",
                "started": _auction_backfill_state["started"]}

    def _worker():
        _auction_backfill_state.update(running=True, started=_now_iso(),
                                       finished=None, result=None)
        try:
            result = auction_run_backfill(days)
        except Exception as exc:  # noqa: BLE001 - 状态落库，绝不外抛
            result = {"ok": False, "reason": str(exc), "backfilled_days": 0, "series": {}}
        _auction_backfill_state.update(running=False, finished=_now_iso(), result=result)
        log(f"📊 打板历史回填完成：{result}")

    threading.Thread(target=_worker, daemon=True).start()
    return {"ok": True, "async": True, "reason": "回填已启动（后台执行，GET /api/auction/status 查进度）"}


def _daily_backup() -> None:
    """日检后自动备份研究成果库（0.9.5 M5；失败静默，不阻塞日检）。"""
    try:
        if research_store is not None:
            research_store.backup()
    except Exception:  # noqa: BLE001 - 备份失败不影响业务
        pass


def auction_scheduler_loop() -> None:
    """打板竞价调度线程：每 2s 轮询，严格交易日触发采集/收口（独立线程，与现有调度并列）。

    触发语义：now>=AUCTION_COLLECT_TIME（默认 09:26）且当日 collect 未触发 → 线程内
    同步执行 auction_run_collect；now>=AUCTION_CLOSE_TIME（默认 16:30）且当日 close
    未触发 → 同步执行 auction_run_close；任务完成后守卫置位（防重复触发）。
    任务函数内部单块 try/except 降级不抛异常；即便硬异常也经 finally 置位守卫，
    避免 2s 轮询空转重试。进程重启后内存守卫清空，同日可能再触发一次——采集/收口
    均按 key 覆盖写/回写覆盖，天然幂等，不产生重复数据。
    """
    while True:
        try:
            dt_now = datetime.now()
            if is_trading_day is not None and not is_trading_day(dt_now.date()):
                time.sleep(2)
                continue
            today = dt_now.strftime("%Y%m%d")
            now_hm = dt_now.strftime("%H:%M")
            guard = _auction_fired.setdefault(today, {"collect": False, "close": False})
            if now_hm >= config.AUCTION_COLLECT_TIME and not guard["collect"]:
                try:
                    res = auction_run_collect()
                    log(f"📊 打板竞价采集完成（{today}）: ok={res.get('ok')} "
                        f"collected={res.get('collected')} errors={res.get('errors_count')} "
                        f"reason={res.get('reason') or ''}")
                finally:
                    guard["collect"] = True  # 完成后置位：当日不重复触发
            elif now_hm >= config.AUCTION_CLOSE_TIME and not guard["close"]:
                try:
                    res = auction_run_close()
                    log(f"📊 打板收口对账完成（{today}）: ok={res.get('ok')} "
                        f"list={res.get('list_count')} reconciled={res.get('reconciled')} "
                        f"diff_alerts={res.get('diff_alerts')} reason={res.get('reason') or ''}")
                finally:
                    guard["close"] = True
        except Exception as exc:  # noqa: BLE001 - 调度线程异常不退出（与 scheduler_loop 同级容错）
            log(f"📈 打板调度线程异常: {exc}")
        time.sleep(2)
