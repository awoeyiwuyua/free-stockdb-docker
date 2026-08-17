#!/usr/bin/env python3
"""auction_metrics — 打板业务指标 + 滚动 60 交易日分位 + mydb 序列维护（0.7.0 任务C 契约桩）

纯标准库纯逻辑；mydb 读写通过注入的 read_fn/write_fn（便于测试与复用 pybao 通道）。
语义对齐上游 emotion-v1：窗口 60、rank∈[0,1] 越小越弱。

本文件为契约桩：只定义接口与常量，函数体由任务C实现（不允许改签名与常量）。
"""

from __future__ import annotations

import json  # 序列载荷在 mydb 里可能是 JSON 字符串，也可能是已解析的 dict，两种都要吃下

WINDOW = 60                            # 滚动窗口（交易日）
METRICS = ("premium_mean", "success_rate")  # v1 指标集：溢价均值 / 成功率
LIST_NAME = "limitup_non_yizi"         # 清单名（非一字板涨停）
METRIC_CONTRACT = "auction-metric-v1"  # 指标契约版本


# === mydb 键契约（命名空间保留，文档约定 AI 勿写） ===
def snapshot_key(trade_date: str, code: str) -> str:
    """竞价快照:<YYYYMMDD>:<code>"""
    return f"竞价快照:{trade_date}:{code}"


def metrics_key(trade_date: str) -> str:
    """打板指标:<YYYYMMDD>"""
    return f"打板指标:{trade_date}"


def series_key(metric: str) -> str:
    """打板序列:<metric>"""
    return f"打板序列:{metric}"


def list_key(trade_date: str) -> str:
    """清单:<YYYYMMDD>:limitup_non_yizi"""
    return f"清单:{trade_date}:{LIST_NAME}"


def _premiums_from_snapshots(snapshots: list[dict]) -> list[tuple[float, str]]:
    """快照列表 → 有效样本 [(溢价小数, code), ...]（compute_metrics/build_daily_row 共用）。

    剔除规则与 compute_metrics 完全一致：open_price/prev_close 任一缺失、非数值、
    prev_close<=0 的样本跳过；脏数据防御不炸整批。
    """
    out: list[tuple[float, str]] = []
    for s in snapshots or []:
        open_price = s.get("open_price")
        prev_close = s.get("prev_close")
        # 停牌/无竞价 → open_price=None，按设计文档口径剔除（并计入 errors 属采集层职责）
        if open_price is None or prev_close is None:
            continue
        try:
            o, p = float(open_price), float(prev_close)
        except (TypeError, ValueError):
            continue  # 脏数据防御：非数值样本跳过，而不是让整批指标崩溃
        if p <= 0:
            continue  # 昨收必须为正，否则除零/负昨收会让溢价失真
        out.append((o / p - 1.0, str(s.get("code") or "")))  # 溢价 = open/prev - 1
    return out


def compute_metrics(snapshots: list[dict]) -> dict:
    """快照列表 → 当日业务指标。

    snapshots 每项 {code, open_price, prev_close}（open_price/prev_close 为 None 的剔除）。
    溢价 = open_price/prev_close - 1。
    返回 {"premium_mean": float|None, "success_rate": float|None, "n_samples": int}
    （n_samples=0 → 两指标均为 None）
    """
    premiums = [p for p, _ in _premiums_from_snapshots(snapshots)]
    n_samples = len(premiums)
    if n_samples == 0:
        # 无有效样本：指标置 None，让上层分位/展示明确知道"今日算不出"
        return {"premium_mean": None, "success_rate": None, "n_samples": 0}
    premium_mean = sum(premiums) / n_samples                      # 溢价算术均值
    success_rate = sum(1.0 for p in premiums if p > 0.0) / n_samples  # 溢价>0 占比
    return {
        "premium_mean": premium_mean,
        "success_rate": success_rate,
        "n_samples": n_samples,
    }


def percentile_rank(value: float, series: list[float]) -> float | None:
    """value 在滚动 60 日窗口中的排名 ∈[0,1]（用户口径 2026-08-16 拍板）。

    定义：排名 = 此前 60 个有效观测中严格低于当日值的天数 / 60。
      - 严格小于：等值不计入（旧版 count_equal 折半计入已废弃）
      - 分母固定 60：历史不足 60 个有效观测 → None（定义不适用，不硬算近似值）
    语义：越小越弱（与 emotion-v1 一致）。
    """
    if len(series) < WINDOW:
        return None  # 不足 60 个有效观测：口径未定义（首个满分母分位在序列满 60 后出现）
    series_60 = series[-WINDOW:]
    count_less = sum(1 for x in series_60 if x < value)
    return count_less / WINDOW


def strength_label(rank: float | None) -> str | None:
    """排名 → 强弱标签（用户口径 2026-08-16）：固定阈值比例，非固定数值阈值。

    - strong（强）：rank ≥ 0.90，即至少 54 个历史观测低于当日
    - weak（弱）：rank ≤ 0.10，即至多 6 个历史观测低于当日
    - neutral（中性）：0.10 < rank < 0.90
    - rank 为 None（观测不足）→ None
    """
    if rank is None:
        return None
    if rank >= 0.9:
        return "strong"
    if rank <= 0.1:
        return "weak"
    return "neutral"


def load_series(read_fn, metric: str) -> list[float]:
    """读打板序列（近 59 日历史值）。read_fn(key) -> value|None（JSON 解析交给本函数）。
    键缺失/损坏 → 返回 []（首日冷启动）。"""
    try:
        raw = read_fn(series_key(metric))
    except Exception:
        raw = None  # 读取通道异常按"键缺失"处理，保证冷启动不炸
    if raw is None:
        return []
    if isinstance(raw, str):
        # mydb 里可能存的是 JSON 字符串；解析失败视为损坏
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if not isinstance(raw, dict):
        return []  # 结构不对（非对象）→ 损坏
    values = raw.get("values")
    if not isinstance(values, list):
        return []  # 缺 values 字段或非列表 → 损坏
    out = []
    for v in values:
        # bool 是 int 子类，混进来会污染数值序列，先排除；只收数值并规整为 float
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            out.append(float(v))
    return out


def append_series(read_fn, write_fn, metric: str, value: float, trade_date: str) -> list[float]:
    """追加当日值并裁剪至最近 WINDOW 个；返回追加后的完整序列。

    写回结构 {"metric": metric, "values": [...], "dates": [...], "window": WINDOW,
               "contract": METRIC_CONTRACT, "updated_at": trade_date}
    """
    # 读旧载荷：与 load_series 同一套容错解析（损坏按冷启动处理，不覆盖丢历史）
    old_values, old_dates = [], []
    try:
        raw = read_fn(series_key(metric))
    except Exception:
        raw = None
    if raw is not None:
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (ValueError, TypeError):
                raw = None
        if isinstance(raw, dict):
            if isinstance(raw.get("values"), list):
                old_values = [float(v) for v in raw["values"]
                              if isinstance(v, (int, float)) and not isinstance(v, bool)]
            if isinstance(raw.get("dates"), list):
                old_dates = [str(d) for d in raw["dates"]]
    if value is None:
        # 当日无有效值（如 n_samples=0）：不写入，避免 None 污染分位序列
        return old_values
    new_values = old_values + [float(value)]
    new_dates = old_dates + [str(trade_date)]
    # 滚动窗口：只保留最近 WINDOW 个交易日，values 与 dates 同步裁剪保持对齐
    new_values = new_values[-WINDOW:]
    new_dates = new_dates[-WINDOW:]
    payload = {
        "metric": metric,
        "values": new_values,
        "dates": new_dates,
        "window": WINDOW,
        "contract": METRIC_CONTRACT,
        "updated_at": trade_date,
    }
    write_fn(series_key(metric), payload)
    return new_values


def build_metrics_payload(snapshots: list[dict], series_by_metric: dict[str, list[float]],
                          computed_at: str, value_source: str) -> dict:
    """组装「打板指标:<日期>」载荷（竞价版/K线版共用）。

    {metrics:{...}, rank_60d:{metric: rank|None}, window:60, n_samples,
     computed_at, value_source: "auction"|"kline", contract: METRIC_CONTRACT}
    """
    metrics = compute_metrics(snapshots)  # 当日业务值；竞价版与 K 线权威版共用同一口径函数
    rank_60d = {}
    strength_60d = {}
    for m in METRICS:
        day_value = metrics[m]
        if day_value is None:
            rank_60d[m] = None  # 当日无值 → 无分位
        else:
            # 当日值在历史序列中的分位；序列不足 60 个观测时 percentile_rank 返回 None
            rank_60d[m] = percentile_rank(day_value, series_by_metric.get(m) or [])
        strength_60d[m] = strength_label(rank_60d[m])  # strong/weak/neutral/None
    return {
        "metrics": metrics,
        "rank_60d": rank_60d,
        "strength_60d": strength_60d,
        "window": WINDOW,
        "n_samples": metrics["n_samples"],
        "computed_at": computed_at,
        "value_source": value_source,
        "contract": METRIC_CONTRACT,
    }


def _nearest_rank(values: list[float], fraction: float) -> float | None:
    """分位数近似（与 mcp.board_metrics 同构：排序后按最近秩取）。"""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def build_daily_row(snapshots: list[dict], payload: dict, *,
                    coverage: dict | None = None, known_at: str | None = None,
                    trade_date: str | None = None) -> dict:
    """组装「打板指标:<日期>」的 daily 子载荷（完整日级行，供 MCP 快速通道直读）。

    行结构与 mcp.board_metrics.summarize_board_open_effect_values 同构（计数/正平负/
    成功率/均值/分位数/分布），另附指标载荷的 metrics/rank_60d/strength_60d 与
    数据溯源。分布与百分比字段口径 = 日K 行（×100 百分数），与竞价版 metrics
    （小数）并存不冲突。coverage 记录清单请求/采集覆盖（候选数、缺价数）。
    """
    premiums = _premiums_from_snapshots(snapshots)          # [(小数, code), ...]
    values_pct = [round(p * 100, 6) for p, _ in premiums]   # 与日K 行同构（百分数）
    sample_codes = sorted(code for _, code in premiums if code)
    positive = sum(1 for v in values_pct if v > 0)
    flat = sum(1 for v in values_pct if abs(v) < 1e-12)
    negative = sum(1 for v in values_pct if v < 0)
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    row: dict = {
        "matched_count": len(values_pct),
        "positive_count": positive,
        "flat_count": flat,
        "negative_count": negative,
        "success_rate": positive / len(values_pct) if values_pct else None,
        "average_open_return_pct": sum(values_pct) / len(values_pct) if values_pct else None,
        "p10_open_return_pct": _nearest_rank(values_pct, 0.10),
        "p25_open_return_pct": _nearest_rank(values_pct, 0.25),
        "median_open_return_pct": _nearest_rank(values_pct, 0.50),
        "p75_open_return_pct": _nearest_rank(values_pct, 0.75),
        "p90_open_return_pct": _nearest_rank(values_pct, 0.90),
        "distribution": {
            "open_return_pct": sorted(values_pct),
            "sample_codes": sample_codes,
        },
        "metrics": {
            "premium_mean": metrics.get("premium_mean"),
            "success_rate": metrics.get("success_rate"),
            "n_samples": metrics.get("n_samples", payload.get("n_samples")),
        },
        "rank_60d": payload.get("rank_60d"),
        "strength_60d": payload.get("strength_60d"),
        "window": payload.get("window"),
        "n_samples": metrics.get("n_samples", payload.get("n_samples")),
        "computed_at": payload.get("computed_at"),
        "value_source": payload.get("value_source"),
        "contract": METRIC_CONTRACT,
    }
    if coverage is not None:
        row["coverage"] = coverage
    if known_at is not None:
        row["known_at"] = known_at
    if trade_date is not None:
        row["trade_date"] = trade_date
    return row
