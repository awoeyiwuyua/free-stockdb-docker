"""board_metrics — 兼容转发（0.9.2 批次 5：领域模块迁 core/board_metrics.py）。

上游引擎无关的纯规则层：涨停判定/一字板/溢价口径（0.8.x 异源验收签字核心）。
本模块仅转发，不承载逻辑；新代码直接 import core.board_metrics。
"""
from core.board_metrics import (  # noqa: F401,E402 - 兼容转发
    A_SHARE_PREFIXES,
    BOARD_OPEN_COUNTER_FIELDS,
    DailyBar,
    _is_20cm,
    _is_north_exchange,
    _nearest_rank,
    _price_equal,
    _rounded_limit_price,
    compute_board_open_effect_details,
    is_supported_a_share_code,
    rebuild_limit_reference_price,
    summarize_board_open_effect_values,
)
