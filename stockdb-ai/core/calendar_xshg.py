#!/usr/bin/env python3
"""calendar_xshg — A 股交易日历（休市表覆盖 2024-2026，来源 exchange_calendars XSHG）。

本模块把 stockdb-ai/app.py 第 116-125 行的 XSHG_HOLIDAYS 休市表复制为独立只读
日历，供 stockdb_mcp_server 的 get_trading_days / get_data_status / get_kline
非交易日提示使用。纯标准库（datetime），无第三方依赖。

取值规则与 app.py 一致：每个年份「周一~周五但非交易日」的日期（官方调休安排：
春节/国庆/元旦/清明/五一/端午/中秋，以及部分周六周日调休补班的非交易日）。
XSHG 日历发布滞后（2027 官方安排通常 2026 年底公布），未收录年份按
「工作日=交易日」处理，数据截至 XSHG_HOLIDAYS_THROUGH 后请提示更新。

对外接口：
    is_trading_day(d)            日期或 8 位字符串 → 是否交易日
    trading_days_between(s, e)   [s, e]（含端点）交易日列表（升序 8 位字符串）
    nearest_trading_day(d)       <= d 的最近交易日（8 位字符串；极早期返回 None）
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

# 与 stockdb-ai/app.py 第 116-125 行逐项一致（2024 20 项 / 2025 18 项 / 2026 19 项）。
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


def _as_date(d: object) -> date:
    """入参归一化：date（含 datetime）原样；'YYYYMMDD' 字符串转 date；非法抛 ValueError。"""
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        if len(d) != 8 or not d.isdigit():
            raise ValueError(f"日期必须是 8 位 YYYYMMDD，当前 {d!r}")
        return datetime.strptime(d, "%Y%m%d").date()
    raise ValueError(f"日期必须是 date 或 8 位 YYYYMMDD 字符串，当前 {type(d).__name__}")


def is_trading_day(d: object) -> bool:
    """A 股交易日判定：周六/周日 False；休市表内日期 False；未收录年份工作日视为 True。

    d 为 date（或 datetime）或 8 位 'YYYYMMDD' 字符串。
    """
    d = _as_date(d)
    if d.weekday() >= 5:  # 周六/周日
        return False
    holidays = XSHG_HOLIDAYS.get(str(d.year))
    if holidays is None:
        return True  # 未收录年份：工作日即视为交易日
    return d.strftime("%m-%d") not in holidays


def trading_days_between(start: object, end: object) -> list[str]:
    """返回 [start, end]（含端点）之间的全部交易日，升序 8 位字符串列表。

    start/end 为 8 位 'YYYYMMDD' 字符串（也接受 date）；start 晚于 end 时返回空列表。
    """
    start_d = _as_date(start)
    end_d = _as_date(end)
    if start_d > end_d:
        return []
    days: list[str] = []
    probe = start_d
    while probe <= end_d:
        if is_trading_day(probe):
            days.append(probe.strftime("%Y%m%d"))
        probe += timedelta(days=1)
    return days


def nearest_trading_day(d: object) -> str | None:
    """返回 <= d 的最近交易日（8 位字符串）；找不到（理论上仅极早期）返回 None。

    正常输入在 7 天内必命中一个工作日；2024-2026 休市日不会连续超过 7 天。
    """
    d = _as_date(d)
    probe = d
    for _ in range(40000):  # 防御性上限（约 109 年），正常输入不会走到
        if probe.weekday() < 5:
            holidays = XSHG_HOLIDAYS.get(str(probe.year))
            if holidays is None or probe.strftime("%m-%d") not in holidays:
                return probe.strftime("%Y%m%d")
        probe -= timedelta(days=1)
    return None


if __name__ == "__main__":
    # 离线自检
    print("20260101 is_trading_day:", is_trading_day("20260101"))
    print("20260105 is_trading_day:", is_trading_day("20260105"))
    print("nearest(20260101):", nearest_trading_day("20260101"))
    print("between 20260101-20260131:", trading_days_between("20260101", "20260131"))
