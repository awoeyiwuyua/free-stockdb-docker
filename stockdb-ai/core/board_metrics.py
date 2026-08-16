"""打板开盘溢价计算的最小纯标准库子集。

vendor 自个人量化研究项目的情绪指标模块（quant/emotion/metrics.py，仅保留
MCP 所需最小集），保持算法原样未改（打板溢价口径、涨停价取整、20cm 判定、过滤规则一行不改）。

保留符号：
- DailyBar dataclass
- A_SHARE_PREFIXES、is_supported_a_share_code
- _is_20cm、_is_north_exchange、_price_equal、_rounded_limit_price、_nearest_rank
- summarize_board_open_effect_values、compute_board_open_effect_details
- BOARD_OPEN_COUNTER_FIELDS

剔除符号（MCP 用不到，不搬）：
- 四维短线情绪指标：UP_LINE_PCT、TOP_N、compute_short_term_earn_index_v2、
  compute_daily_metrics、_ret10、classify_earn_index_v2_eligibility 等
- 打板候选/反馈成员：BoardOpenFeedbackMember、build_prior_session_candidates、
  join_candidates_to_target_open、summarize_board_open_feedback_members 等
- load_market_snapshot（依赖 sqlite_core）、compute_board_effect_v2
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


BOARD_OPEN_COUNTER_FIELDS = (
    "prior_limit_up_count",
    "excluded_one_word_count",
    "eligible_count",
    "missing_open_count",
    "excluded_st_limit_up_count",
    "excluded_north_limit_up_count",
)
A_SHARE_PREFIXES = (
    "000", "001", "002", "003",  # 深市主板
    "300", "301",                  # 创业板
    "600", "601", "603", "605",  # 沪市主板
    "688", "689",                  # 科创板 / CDR
)


@dataclass
class DailyBar:
    code: str
    close: float
    high: float
    low: float
    amount: float
    prev_close: float | None = None
    open: float | None = None
    is_st: bool = False


def _is_20cm(code: str) -> bool:
    """创业板（sz 3 开头）或科创 688 → 20cm；其余 10cm。"""
    return code.startswith(("3", "688", "689"))


def is_supported_a_share_code(code: str) -> bool:
    """仅接受沪深 A 股代码，排除 ETF/基金/债券/B股/北交所。"""
    return len(code) == 6 and code.startswith(A_SHARE_PREFIXES)


def _is_north_exchange(code: str) -> bool:
    """Return whether a code belongs to Beijing Stock Exchange."""
    return code.startswith(("4", "8", "92"))


def _price_equal(left: float | None, right: float | None) -> bool:
    """Compare two A-share prices at the one-cent tick boundary."""
    if left is None or right is None:
        return False
    return abs(left - right) < 0.0050001


def _rounded_limit_price(prev_close: float, rate: Decimal) -> float:
    """Calculate an exchange-style one-cent limit price using half-up rounding."""
    return float(
        (Decimal(str(prev_close)) * (Decimal("1") + rate)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    )


def rebuild_limit_reference_price(pre_close: float, cum_at_date: float, cum_latest: float) -> float:
    """引擎历史 K 线 pre_close 被未来除权除息回溯污染 → 重建当日法定涨跌停参考价。

    污染机制（2026-08-16 命理档案异源核验 + 引擎数据实测确认）：引擎按最新复权
    因子统一重算全部历史行的 pre_close——
        pre_close_engine(D) = 真实法定参考价(D) × cum_D / cum_latest
    反推：
        法定参考价(D) = pre_close_engine(D) × cum_latest / cum_D
    - 未除权股票（无因子事件）：cum_D == cum_latest → 原样返回（未污染）
    - 普通日：反推 = 上一实际成交日未复权收盘
    - 除权除息日：反推 = 交易所法定除权参考价
    禁止直接使用引擎 pre_close 或机械使用 lag(close) 参与历史涨停判定。
    """
    try:
        c_d = float(cum_at_date)
        c_l = float(cum_latest)
    except (TypeError, ValueError):
        return float(pre_close)
    if c_d <= 0 or c_l <= 0:
        return float(pre_close)
    if abs(c_d - c_l) < 1e-12:
        return float(pre_close)  # 无因子事件：未污染
    return float(pre_close) * c_l / c_d


def _nearest_rank(values: list[float], fraction: float) -> float | None:
    """Match the percentile convention used by the official board-open collector."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[round((len(ordered) - 1) * fraction)]


def summarize_board_open_effect_values(
    trade_date: str,
    values: list[float],
    counts: dict[str, int],
    sample_codes: list[str] | None = None,
) -> dict[str, Any]:
    """从个股溢价分布和样本计数生成单日可审计统计。"""
    positive = sum(value > 0 for value in values)
    flat = sum(abs(value) < 1e-12 for value in values)
    negative = sum(value < 0 for value in values)
    return {
        "trade_date": trade_date,
        **{field: int(counts.get(field, 0)) for field in BOARD_OPEN_COUNTER_FIELDS},
        "matched_count": len(values),
        "positive_count": positive,
        "flat_count": flat,
        "negative_count": negative,
        "success_rate": positive / len(values) if values else None,
        "average_open_return_pct": sum(values) / len(values) if values else None,
        "p10_open_return_pct": _nearest_rank(values, 0.10),
        "p25_open_return_pct": _nearest_rank(values, 0.25),
        "median_open_return_pct": _nearest_rank(values, 0.50),
        "p75_open_return_pct": _nearest_rank(values, 0.75),
        "p90_open_return_pct": _nearest_rank(values, 0.90),
        "distribution": {
            "open_return_pct": sorted(round(value, 6) for value in values),
            "sample_codes": sorted(sample_codes or []),
        },
    }


def compute_board_open_effect_details(
    snapshot: dict[str, list[DailyBar]],
) -> dict[str, dict[str, Any]]:
    """按交易日计算“昨日非一字板涨停、今日开盘溢价”的可审计统计。

    样本口径：
    - 仅沪深主板、创业板和科创板；北交所排除。
    - ST 股排除。
    - 昨收盘价必须等于按前收和 10%/20% 计算、分位四舍五入的涨停价。
    - 一字板严格定义为昨日 open/high/low/close 全部等于涨停价；
      开盘涨停但盘中打开的 T 字板保留。
    - 当日溢价 = T 日 open / T-1 日 close - 1。

    输出包含样本数、匹配数、正负分布、均值、分位数和原始分布，
    避免只保留一个均值后无法判断结果是否由小样本或极端值驱动。
    """
    series: dict[str, list[tuple[str, DailyBar]]] = {}
    all_dates = set(snapshot)
    ordered_dates = sorted(all_dates)
    previous_market_date = {
        ordered_dates[index]: ordered_dates[index - 1]
        for index in range(1, len(ordered_dates))
    }
    for date_str, bars in snapshot.items():
        for bar in bars:
            series.setdefault(bar.code, []).append((date_str, bar))

    returns_by_date: dict[str, list[float]] = {date_str: [] for date_str in all_dates}
    sample_codes_by_date: dict[str, list[str]] = {
        date_str: [] for date_str in all_dates
    }
    counters: dict[str, dict[str, int]] = {
        date_str: {field: 0 for field in BOARD_OPEN_COUNTER_FIELDS}
        for date_str in all_dates
    }

    for code, seq in series.items():
        seq.sort(key=lambda item: item[0])
        for index in range(1, len(seq)):
            previous_date, previous = seq[index - 1]
            trade_date, current = seq[index]
            # 停牌若干日后复牌不属于“昨日涨停今日溢价”。
            if previous_market_date.get(trade_date) != previous_date:
                continue
            if previous.prev_close is None or previous.prev_close <= 0:
                continue

            # 记录被市场范围和 ST 规则排除的涨停样本，用于质量审计。
            if _is_north_exchange(code):
                north_limit = _rounded_limit_price(previous.prev_close, Decimal("0.30"))
                if _price_equal(previous.close, north_limit):
                    counters[trade_date]["excluded_north_limit_up_count"] += 1
                continue
            if not is_supported_a_share_code(code):
                continue
            if previous.is_st:
                st_limit = _rounded_limit_price(previous.prev_close, Decimal("0.05"))
                if _price_equal(previous.close, st_limit):
                    counters[trade_date]["excluded_st_limit_up_count"] += 1
                continue

            rate = Decimal("0.20") if _is_20cm(code) else Decimal("0.10")
            limit_price = _rounded_limit_price(previous.prev_close, rate)
            if not _price_equal(previous.close, limit_price):
                continue

            counters[trade_date]["prior_limit_up_count"] += 1
            one_word = all(
                _price_equal(price, limit_price)
                for price in (previous.open, previous.high, previous.low, previous.close)
            )
            if one_word:
                counters[trade_date]["excluded_one_word_count"] += 1
                continue

            counters[trade_date]["eligible_count"] += 1
            if current.open is None or current.open <= 0 or previous.close <= 0:
                counters[trade_date]["missing_open_count"] += 1
                continue
            returns_by_date[trade_date].append(
                (current.open / previous.close - 1) * 100
            )
            sample_codes_by_date[trade_date].append(code)

    results: dict[str, dict[str, Any]] = {}
    for trade_date in sorted(all_dates):
        values = returns_by_date[trade_date]
        counts = counters[trade_date]
        results[trade_date] = summarize_board_open_effect_values(
            trade_date, values, counts, sample_codes_by_date[trade_date]
        )
    return results
