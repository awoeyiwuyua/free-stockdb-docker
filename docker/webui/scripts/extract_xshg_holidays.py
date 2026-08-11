#!/usr/bin/env python3
"""提取 exchange_calendars XSHG 休市日表，供维护 XSHG_HOLIDAYS 内嵌数据用。

用途：webui 定时同步的"仅交易日"判定依赖 app.py 内嵌的 A 股休市表。
每年官方放假安排公布后（通常前一年年底），用本脚本重新提取并更新：
  1. 在本机 venv 安装 exchange_calendars：pip install exchange_calendars
  2. 运行：python extract_xshg_holidays.py [年份...]（默认 2024-2026）
  3. 把输出的年月日列表替换进 docker/webui/app.py 的 XSHG_HOLIDAYS
  4. 同步更新 XSHG_HOLIDAYS_THROUGH 截止日期

不随 webui 运行，仅维护期使用。webui 运行时零依赖（纯标准库）。
"""
import sys
from datetime import date, timedelta

def main():
    years = [int(a) for a in sys.argv[1:]] or [2024, 2025, 2026]
    try:
        import exchange_calendars as xcals
    except ImportError:
        print("需要 exchange_calendars：pip install exchange_calendars", file=sys.stderr)
        raise SystemExit(1)
    xshg = xcals.get_calendar("XSHG")
    print("XSHG 日历最后交易日:", xshg.last_session.date())
    for y in years:
        d, e = date(y, 1, 1), date(y, 12, 31)
        non = []
        while d <= e:
            if d.weekday() < 5 and not xshg.is_session(d):
                non.append(d.strftime("%m-%d"))
            d += timedelta(days=1)
        print(f"\n{y}（{len(non)} 个休市工作日）:")
        print("    " + json_dump(non))

def json_dump(items):
    import json
    return json.dumps(items, ensure_ascii=False)

if __name__ == "__main__":
    main()
