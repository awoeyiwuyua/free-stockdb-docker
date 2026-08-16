#!/usr/bin/env python3
"""auction_list — T-1 非一字板涨停清单计算（0.7.0 任务B；0.8.12 样本口径与生产路径同源）

纯标准库纯逻辑；输入为 T-1 全市场时点快照点列表（复用 MCP 内部
query_point_snapshot 的输出字段），输出次日采集清单。

样本筛选（2026-08-16 对账拍板：以用户 SQL dwd/board_open_feedback_members.sql 为
基准，实测生产 get_board_open_effect_history 输出与用户仓库逐位一致——08-14
59 候选/12 一字板排除/47 有效/+1.2113%/48.9%——故判定规则直接复用
mcp.board_metrics 的辅助函数，杜绝两套口径漂移）：
  - status==TRADED 且 open/close/prev_close 数值（停牌/缺价不计）
  - 北交所（4/8/92 开头）排除；非沪深 A 股代码排除；ST 排除
  - 涨停判定：收盘价 == round(昨收×(1+10%或20%), 2)（_price_equal 分位容差）——
    严格"封板"语义；涨幅带近似不可用（炸板股带内会被误收）
  - 一字板严格定义：昨开/高/低/收全部等于涨停价（T 字板保留）
"""

from __future__ import annotations

from decimal import Decimal

# 判定辅助函数与生产路径同源（0.8.12）；0.9.2 批次 5：同层引用（core 内）
from core.board_metrics import (  # noqa: E402 - 领域层同层引用
    _is_20cm,              # 创业板 3xx / 科创板 688/689 → 20cm
    _is_north_exchange,    # 北交所排除
    _price_equal,          # 分位价差比较（涨停价判定）
    _rounded_limit_price,  # 涨停价四舍五入到分（half-up）
    is_supported_a_share_code,
)


def limit_pct(code: str, is_st: bool = False) -> float | None:
    """6 位代码 → 名义涨跌幅制度（0.10 / 0.20 / None=无法识别）。

    is_st 参数保留兼容但不再生效：ST 股由 compute_limitup_list 统一排除
    （对齐生产 board_metrics 口径，ST 不设 5% 档）。
    """
    if is_supported_a_share_code(code):
        return 0.20 if _is_20cm(code) else 0.10
    return None  # 北交所/B股/非沪深 A 股 → 无法识别


def compute_limitup_list(points: list[dict]) -> dict:
    """全市场快照点 → 非一字板涨停清单。

    points 每项 {code, name, open, high, low, close, prev_close, is_st, status}
    （缺失字段按 None）。只统计 status=="TRADED" 且 open/close/prev_close 数值。
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
        # —— 样本筛选（对齐生产 board_metrics，0.8.12）——
        if _is_north_exchange(code):
            continue  # 北交所排除（涨停价 30% 不构成样本）
        if not is_supported_a_share_code(code):
            continue  # 非沪深 A 股（ETF/B股等）排除
        if bool(point.get("is_st")):
            continue  # ST 排除（涨停价 5% 不构成样本）
        rate = Decimal("0.20") if _is_20cm(code) else Decimal("0.10")
        limit_price = _rounded_limit_price(prev_close, rate)
        if not _price_equal(close, limit_price):
            continue  # 未封板（含炸板）：不构成样本
        # 一字板严格定义：昨开/高/低/收全部等于涨停价（T 字板保留）
        try:
            high_price = float(point.get("high"))
            low_price = float(point.get("low"))
        except (TypeError, ValueError):
            high_price = low_price = None
        yizi = all(
            price is not None and _price_equal(price, limit_price)
            for price in (open_price, high_price, low_price, close)
        )
        pct = (close - prev_close) / prev_close
        details.append({
            "code": code,
            "name": str(point.get("name") or ""),
            "pct": pct,
            "threshold": float(rate),
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
