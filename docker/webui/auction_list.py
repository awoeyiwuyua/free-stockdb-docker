#!/usr/bin/env python3
"""auction_list — T-1 非一字板涨停清单计算（0.7.0 任务B 契约桩）

纯标准库纯逻辑；输入为 T-1 全市场时点快照点列表（复用 MCP 内部
query_point_snapshot 的输出字段），输出次日采集清单。

涨停阈值三档：ST → 5%；创业板(300/301)/科创板(688/689) → 20%；其余 → 10%。
涨停判定：pct = (close - prev_close)/prev_close >= threshold - TOLERANCE。
一字板判定：open == close（开盘即封死，排除出采集清单）。

本文件为契约桩：只定义接口与常量，函数体由任务B实现（不允许改签名与常量）。
"""

from __future__ import annotations

TOLERANCE = 0.001  # 涨停判定容差（涨跌停价舍入）


def limit_pct(code: str, is_st: bool = False) -> float:
    """6 位代码 → 涨停幅度（5%/10%/20%）。"""
    prefix = str(code)[:3]
    # 创业板(300/301)/科创板(688/689) 无 5% 档：无论是否 ST 一律 20%（交易所规则如此）
    if prefix in ("300", "301", "688", "689"):
        return 0.20
    # 其余板块：is_st 为真 → 5%（涨跌幅限制减半），否则 → 10%
    if is_st:
        return 0.05
    return 0.10


def compute_limitup_list(points: list[dict]) -> dict:
    """全市场快照点 → 非一字板涨停清单。

    points 每项 {code, name, open, close, prev_close, is_st, status}（缺失字段按 None）。
    只统计 status=="TRADED" 且 prev_close/close/open 均为数值的样本。
    返回 {
      "codes":   [...6位代码...],        # 周一 09:26 采集清单
      "details": [{code, name, pct, threshold, yizi}, ...],  # 全量涨停明细（含一字板，供对账）
      "count": int, "yizi_count": int, "traded": int,       # 统计
    }
    """
    codes: list[str] = []      # 非一字板涨停代码（次日 09:26 采集清单）
    details: list[dict] = []   # 全量涨停明细（含一字板，供 15:35 对账）
    yizi_count = 0             # 一字板数量
    traded = 0                 # 有效样本数（status==TRADED 且三价均为数值）

    for point in points or []:
        # 防御：上游结构异常（非 dict 行）直接跳过，不炸整批
        if not isinstance(point, dict):
            continue
        if point.get("status") != "TRADED":
            continue  # 停牌/未上市等非交易状态不计入
        # 三价任一非数值（None/空串/非数字）→ 剔除：无法算涨跌幅
        try:
            open_price = float(point.get("open"))
            close = float(point.get("close"))
            prev_close = float(point.get("prev_close"))
        except (TypeError, ValueError):
            continue
        if prev_close <= 0:
            continue  # 除零防御：prev_close 为 0 时涨跌幅无意义
        traded += 1

        code = str(point.get("code") or "")
        threshold = limit_pct(code, is_st=bool(point.get("is_st")))
        pct = (close - prev_close) / prev_close
        # 涨停判定：pct >= 阈值 - TOLERANCE（涨停价按比例舍入到分，留舍入容差）
        if pct < threshold - TOLERANCE:
            continue  # 未达涨停
        # 一字板判定：开盘价即收盘价（|差|<1e-6），开盘封死 → 剔除出采集清单
        yizi = abs(open_price - close) < 1e-6
        details.append({
            "code": code,
            "name": str(point.get("name") or ""),
            "pct": pct,
            "threshold": threshold,
            "yizi": yizi,
        })
        if yizi:
            yizi_count += 1
        else:
            codes.append(code)

    return {
        "codes": codes,
        "details": details,
        "count": len(details),      # 涨停总数 = 非一字板 + 一字板
        "yizi_count": yizi_count,
        "traded": traded,
    }
