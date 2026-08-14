#!/usr/bin/env python3
"""paper_core — 模拟盘（固定机器策略合同版）纯逻辑核心（任务A）

单进程、单账户、单标的（159915）目标仓位模型的全部纯逻辑：冻结契约常量、
decide_target 四规则、状态机转移、数量计算（delta / sell）、幂等键与对账重放。

冻结契约规范来源：任务简报《冻结契约（全部任务共享）》一节（由任务方提供，
非仓库内文件；仓库内不存在外部冻结 CONTRACTS 文档）。本模块逐字实现该契约，
常量名/取值/规则编号/错误码与任务简报原文一致；同一契约另由任务B（paper_db）
与任务D（mx_client）共享实现，跨文件一致性见任务报告交叉验证部分。

设计约束：
  - 纯函数、无 IO、零第三方依赖（仅标准库），全部行为可单测；
  - 中文注释 / 文案；
  - 不接触 pybao/ 与 mcp 目录现有文件；
  - 本模块不含任何 apikey / 敏感信息（敏感信息只出现在 IO 层，日志仅打掩码）。

自测（贴出输出，见任务报告）：
    python -c "…内联断言…"   # 四规则全分支 / 状态机 / delta / sell / 幂等键 / decision_id / 重放
"""

from __future__ import annotations

import math

# === 冻结契约常量（全部任务共享） ===
STRATEGY_ID = "emotion-trend-159915-v1"  # 策略 ID（幂等键前缀）
STRATEGY_VERSION = "1.0.0"  # 策略版本（幂等键组成部分）
SYMBOL = "159915"  # 标的：创业板 ETF（T+1，不可当日回转）
POSITION_STATES = (0.0, 0.5, 1.0)  # 目标仓位状态全集（0=空仓 / 0.5=半仓 / 1.0=满仓）
LOT_SIZE = 100  # 每手 100 股，数量一律按手向下取整
DECISION_TIME = "09:27"  # 决策时刻（情绪信号 09:25 后投递）
EXEC_WINDOW_START = "14:50:00"  # 执行窗口起点
EXEC_CUTOFF = "14:56:30"  # 执行窗口截止（此后不再撤单追单）
STOP_CHASE = "14:57:00"  # 停止追价时刻
RECONCILE = "15:05:00"  # 收盘对账时刻
MODEL_NAV_DEFAULT = 100000.0  # 模型名义本金默认值（配置可改）

# === 生命周期状态常量（全部任务共享） ===
SIGNAL_READY = "SIGNAL_READY"  # 信号已就绪（尚未形成决策）
DECIDED = "DECIDED"  # 决策已形成（strategy_decisions.status 缺省值）
EXECUTION_PENDING = "EXECUTION_PENDING"  # 意图已生成、待提交（order_intents.status 缺省值）
SUBMITTED = "SUBMITTED"  # 意图已提交到券商，等待回报
PARTIALLY_FILLED = "PARTIALLY_FILLED"  # 部分成交，剩余未成交
FILLED = "FILLED"  # 全部成交
UNFILLED = "UNFILLED"  # 已提交但未成交
UNFILLED_AT_CUTOFF = "UNFILLED_AT_CUTOFF"  # 执行窗口截止仍完全未成交
RECONCILED = "RECONCILED"  # 已对账（收盘后 reconciliation 完成）
DATA_NOT_QUALIFIED = "DATA_NOT_QUALIFIED"  # 情绪数据不合格：目标保持、不生成订单

# === 动作常量（状态机转移结果） ===
BUY_HALF = "BUY_HALF"  # 买入半仓（0→0.5 / 0.5→1）
BUY_FULL = "BUY_FULL"  # 买入满仓（0→1）
SELL_ALL = "SELL_ALL"  # 清仓卖出（0.5→0 / 1→0）
NO_ORDER = "NO_ORDER"  # 目标不变，不下单

# === 决策理由码（decide_target 返回的 reason） ===
REASON_WEAK_STATE_CONFIRMED = "WEAK_STATE_CONFIRMED"  # 规则1：弱势状态确认 → 空仓
REASON_STRONG_TREND_CONFIRMED = "STRONG_TREND_CONFIRMED"  # 规则2：强势趋势确认 → 满仓
REASON_P50_UPCROSS_PROBE = "P50_UPCROSS_PROBE"  # 规则3：P50 上穿试探 → 半仓
REASON_HOLD = "HOLD"  # 规则4：维持当前目标

# === 错误码常量（复用现有 8 码体系） ===
ERROR_INVALID_ARGUMENT = "INVALID_ARGUMENT"  # 参数非法
ERROR_NO_DATA = "NO_DATA"  # 无数据
ERROR_NOT_PUBLISHED = "NOT_PUBLISHED"  # 未发布
ERROR_INVALID_SYMBOL = "INVALID_SYMBOL"  # 标的非法
ERROR_DEPENDENCY_UNAVAILABLE = "DEPENDENCY_UNAVAILABLE"  # 依赖不可用（MX 接口 / apikey 失效等）
ERROR_PARTIAL_RESULT = "PARTIAL_RESULT"  # 部分成功
ERROR_RATE_LIMITED = "RATE_LIMITED"  # 限流
ERROR_INTERNAL_ERROR = "INTERNAL_ERROR"  # 内部错误

# 错误码全集（供 IO 层校验 / 测试断言）
ERROR_CODES = (
    ERROR_INVALID_ARGUMENT,
    ERROR_NO_DATA,
    ERROR_NOT_PUBLISHED,
    ERROR_INVALID_SYMBOL,
    ERROR_DEPENDENCY_UNAVAILABLE,
    ERROR_PARTIAL_RESULT,
    ERROR_RATE_LIMITED,
    ERROR_INTERNAL_ERROR,
)

# MX 模拟盘接口错误 → 错误码映射（冻结契约；供 IO 层使用）
#   401        → DEPENDENCY_UNAVAILABLE（hint：检查 apikey）
#   code=113   → RATE_LIMITED
#   404 未绑定 → INVALID_ARGUMENT（hint：绑定模拟账户）
#   网络异常   → INTERNAL_ERROR（MX_NETWORK_ERROR）
MX_ERROR_MAP = {
    401: (ERROR_DEPENDENCY_UNAVAILABLE, "检查 apikey"),
    113: (ERROR_RATE_LIMITED, None),
    404: (ERROR_INVALID_ARGUMENT, "绑定模拟账户"),
}
MX_NETWORK_ERROR = (ERROR_INTERNAL_ERROR, "网络错误")


# === 目标仓位校验 ===

def is_valid_target(x) -> bool:
    """判断 x 是否为合法目标仓位（POSITION_STATES 成员，0.0 / 0.5 / 1.0）。"""
    return x in POSITION_STATES


def _require_valid_target(x, field: str) -> float:
    """规范化（转 float）并校验目标仓位；非法抛中文 ValueError。"""
    xf = float(x)
    if not is_valid_target(xf):
        raise ValueError(f"{field} 非法目标仓位: {x!r}（仅允许 {POSITION_STATES}）")
    return xf


def _require_trade_date(trade_date) -> str:
    """规范化交易日（YYYYMMDD 8 位数字串）；非法抛 ValueError。"""
    s = str(trade_date).strip()
    if len(s) != 8 or not s.isdigit():
        raise ValueError(f"trade_date 非法: {trade_date!r}（要求 YYYYMMDD）")
    return s


# === 决策：decide_target 四规则（逐字实现，规则编号与冻结契约一致） ===

def decide_target(current_rank, previous_rank, ma5, ma10, ma20, current_target):
    """目标仓位决策 —— 情绪分位 × 均线四规则（冻结契约逐字实现）。

    规则编号（与冻结契约一致）：
      规则1) current_rank < 0.50 且 ma5 < ma10         → (0.0, WEAK_STATE_CONFIRMED)
      规则2) current_rank >= 0.90 且 ma5 > ma10 > ma20 → (1.0, STRONG_TREND_CONFIRMED)
      规则3) current_target == 0.0 且 previous_rank < 0.50 且
             current_rank >= 0.50 且 ma5 > ma10        → (0.5, P50_UPCROSS_PROBE)
      规则4) 否则                                      → (current_target, HOLD)

    规则顺序即优先级：先规则1，再规则2（规则2 优先于规则3），再规则3，最后规则4。

    防御：current_target 必须是合法目标仓位（POSITION_STATES），否则 ValueError——
    规则4 会把 current_target 原样作为 desired 返回，先校验即保证
    "desired 由 decide_target 保证只能产生合法目标 / 合法状态机转移"（契约要求）。

    参数：
      current_rank   当日情绪分位（0~1，越小越弱）
      previous_rank  上一交易日情绪分位（0~1）
      ma5/ma10/ma20  5 / 10 / 20 日均线
      current_target 决策前目标仓位（即上一交易日的 desired_target）
    返回：(desired, reason) —— desired ∈ POSITION_STATES
    """
    current_target = _require_valid_target(current_target, "current_target")
    # 规则1：弱势状态确认 → 空仓
    if current_rank < 0.50 and ma5 < ma10:
        return (0.0, REASON_WEAK_STATE_CONFIRMED)
    # 规则2：强势趋势确认 → 满仓（规则2 优先于规则3）
    if current_rank >= 0.90 and ma5 > ma10 > ma20:
        return (1.0, REASON_STRONG_TREND_CONFIRMED)
    # 规则3：P50 上穿试探 → 半仓
    if current_target == 0.0 and previous_rank < 0.50 and current_rank >= 0.50 and ma5 > ma10:
        return (0.5, REASON_P50_UPCROSS_PROBE)
    # 规则4：维持当前目标
    return (current_target, REASON_HOLD)


# === 状态机：目标仓位转移 → 动作 ===

_TRANSITION_TABLE = {
    (0.0, 0.5): BUY_HALF,  # 空仓 → 半仓：买入半仓
    (0.0, 1.0): BUY_FULL,  # 空仓 → 满仓：买入满仓
    (0.5, 1.0): BUY_HALF,  # 半仓 → 满仓：买入半仓
    (0.5, 0.0): SELL_ALL,  # 半仓 → 空仓：全部卖出
    (1.0, 0.0): SELL_ALL,  # 满仓 → 空仓：全部卖出
}


def state_transition(previous, desired):
    """状态机：根据 上一目标仓位 → 决策目标仓位 返回动作。

    合法转移（冻结契约）：
      (0→0.5)=BUY_HALF、(0→1)=BUY_FULL、(0.5→1)=BUY_HALF、
      (0.5→0)=SELL_ALL、(1→0)=SELL_ALL、不变=NO_ORDER。
    其余组合 raise ValueError（防御：decide_target 保证不可达，测试证明）。

    返回：BUY_HALF / BUY_FULL / SELL_ALL / NO_ORDER
    """
    previous_f = _require_valid_target(previous, "previous")
    desired_f = _require_valid_target(desired, "desired")
    if previous_f == desired_f:
        return NO_ORDER  # 目标不变 → 不下单
    action = _TRANSITION_TABLE.get((previous_f, desired_f))
    if action is None:
        raise ValueError(f"非法的目标仓位转移: {previous_f} -> {desired_f}")
    return action


# === 数量计算 ===

def compute_delta(model_nav, desired, ref_price, actual_qty):
    """按目标仓位计算目标持仓数与调仓股数。

      target_qty = floor(model_nav * desired / ref_price / 100) * 100  （向下取整到整手）
      delta_qty  = target_qty - actual_qty                             （>0 需买 / <0 需卖）

    参数：
      model_nav   模型名义本金（默认 MODEL_NAV_DEFAULT=100000.0，配置可改；必须 > 0）
      desired     决策目标仓位（POSITION_STATES）
      ref_price   参考价（必须 > 0，否则 ValueError）
      actual_qty  当前实际持仓股数（整股）
    返回：(target_qty, delta_qty)，均为 int，且 target_qty 恒为 100 的整数倍。
    """
    desired_f = _require_valid_target(desired, "desired")
    nav = float(model_nav)
    if nav <= 0:
        raise ValueError(f"model_nav 必须为正数: {model_nav!r}")
    price = float(ref_price)
    if price <= 0:
        raise ValueError(f"ref_price 必须为正数: {ref_price!r}")
    actual = int(actual_qty)  # 持仓数量按整股处理
    target_qty = int(math.floor(nav * desired_f / price / LOT_SIZE)) * LOT_SIZE
    delta_qty = target_qty - actual
    return (target_qty, delta_qty)


def sell_quantity(delta_qty, available_to_sell_qty):
    """卖出数量：max(0, min(-delta_qty, available_to_sell_qty))，再向下取整到整手（100）。

    T+1 约束：159915 不可当日回转，卖出上限为 available_to_sell_qty（可卖数量）。
    仅当 delta_qty < 0（需卖出）时有意义；delta_qty >= 0 时恒返回 0。
    不足一手（<100 股）向下取整为 0。

    返回：int（0 或 100 的整数倍）。
    """
    delta = int(delta_qty)
    available = int(available_to_sell_qty)
    raw = max(0, min(-delta, available))
    return (raw // LOT_SIZE) * LOT_SIZE


# === 幂等键 ===

def order_intent_key(trade_date, desired):
    """订单意图幂等键（冻结契约格式）。

      f"{STRATEGY_ID}:{STRATEGY_VERSION}:{trade_date}:{SYMBOL}:{desired_target}"
    例：emotion-trend-159915-v1:1.0.0:20260602:159915:0.5
    同一 (trade_date, desired) 幂等：重复投递不产生新订单意图。
    """
    date_s = _require_trade_date(trade_date)
    desired_f = _require_valid_target(desired, "desired")
    return f"{STRATEGY_ID}:{STRATEGY_VERSION}:{date_s}:{SYMBOL}:{desired_f}"


def decision_id(trade_date):
    """决策幂等键（冻结契约格式，每日唯一、不可覆盖）。

      f"{STRATEGY_ID}:{trade_date}"
    例：emotion-trend-159915-v1:20260602
    """
    date_s = _require_trade_date(trade_date)
    return f"{STRATEGY_ID}:{date_s}"


# === 对账重放 ===

def replay_decision(snapshot):
    """对账重放：从决策快照字段调用 decide_target，重算 (desired, reason)。

    供收盘对账（RECONCILE=15:05:00）校验库内 desired_target 是否与重放结果一致。

    快照字段（与 strategy_decisions / 信号、趋势快照列名对齐）：
      current_rank / previous_rank / ma5 / ma10 / ma20 / current_target
    current_target 为决策前目标（即上一交易日 desired_target）；兼容
    "previous_target" 字段名（strategy_decisions.previous_target）。
    缺少任一必需字段 → ValueError（中文提示）。

    返回：(desired, reason)，与 decide_target 相同。
    """
    required = ("current_rank", "previous_rank", "ma5", "ma10", "ma20")
    missing = [k for k in required if k not in snapshot]
    if missing:
        raise ValueError(f"replay_decision 快照缺少字段: {missing}")
    current_target = snapshot.get("current_target")
    if current_target is None:
        current_target = snapshot.get("previous_target")
    if current_target is None:
        raise ValueError("replay_decision 快照缺少 current_target / previous_target")
    return decide_target(
        current_rank=snapshot["current_rank"],
        previous_rank=snapshot["previous_rank"],
        ma5=snapshot["ma5"],
        ma10=snapshot["ma10"],
        ma20=snapshot["ma20"],
        current_target=current_target,
    )


# === 自测入口（python paper_core.py，等价于任务简报的 python -c 内联断言） ===
# 纯标准库断言，无任何 IO；任何一条失败立即抛 AssertionError（退出码非 0），
# 全部通过打印 [OK] 摘要并以 "ALL_ASSERTIONS_PASSED" 结尾，供验证器判定结论。

def _self_test():
    """模块级自测：四规则全分支（含边界 0.50/0.90）、状态机合法/非法、
    delta 100 取整、sell T+1 约束、幂等键格式、decision_id 格式、对账重放。"""
    # —— 冻结契约常量 ——
    assert STRATEGY_ID == "emotion-trend-159915-v1"
    assert STRATEGY_VERSION == "1.0.0"
    assert SYMBOL == "159915"
    assert POSITION_STATES == (0.0, 0.5, 1.0)
    assert LOT_SIZE == 100
    assert DECISION_TIME == "09:27"
    assert EXEC_WINDOW_START == "14:50:00"
    assert EXEC_CUTOFF == "14:56:30"
    assert STOP_CHASE == "14:57:00"
    assert RECONCILE == "15:05:00"
    assert MODEL_NAV_DEFAULT == 100000.0
    assert (SIGNAL_READY, DECIDED, EXECUTION_PENDING, SUBMITTED, PARTIALLY_FILLED,
            FILLED, UNFILLED, UNFILLED_AT_CUTOFF, RECONCILED, DATA_NOT_QUALIFIED) == (
        "SIGNAL_READY", "DECIDED", "EXECUTION_PENDING", "SUBMITTED", "PARTIALLY_FILLED",
        "FILLED", "UNFILLED", "UNFILLED_AT_CUTOFF", "RECONCILED", "DATA_NOT_QUALIFIED")
    print("[OK] 冻结契约常量（策略/生命周期）逐字一致")
    # —— decide_target 四规则全分支（含边界 rank=0.50/0.90）——
    assert decide_target(0.30, 0.50, 1.0, 1.5, 2.0, 1.0) == (0.0, "WEAK_STATE_CONFIRMED")  # 规则1
    assert decide_target(0.49, 0.50, 1.0, 1.5, 2.0, 0.5) == (0.0, "WEAK_STATE_CONFIRMED")  # 规则1 边界下
    assert decide_target(0.95, 0.50, 3.0, 2.0, 1.0, 0.0) == (1.0, "STRONG_TREND_CONFIRMED")  # 规则2
    assert decide_target(0.90, 0.50, 3.0, 2.0, 1.0, 0.5) == (1.0, "STRONG_TREND_CONFIRMED")  # 规则2 边界=0.90
    assert decide_target(0.95, 0.30, 3.0, 2.0, 1.0, 0.0) == (1.0, "STRONG_TREND_CONFIRMED")  # 规则2 优先于规则3
    assert decide_target(0.55, 0.40, 3.0, 2.0, 2.5, 0.0) == (0.5, "P50_UPCROSS_PROBE")  # 规则3
    assert decide_target(0.50, 0.40, 3.0, 2.0, 2.5, 0.0) == (0.5, "P50_UPCROSS_PROBE")  # 规则3 边界=0.50
    assert decide_target(0.50, 0.60, 2.0, 2.5, 2.5, 0.0) == (0.0, "HOLD")  # 0.50 但 ma5<ma10 → 规则4
    assert decide_target(0.60, 0.40, 2.0, 2.0, 2.5, 0.5) == (0.5, "HOLD")  # 规则4 保持
    assert decide_target(0.80, 0.80, 2.0, 1.9, 2.0, 1.0) == (1.0, "HOLD")  # 规则4 保持
    try:  # 防御：current_target 非法 → ValueError
        decide_target(0.60, 0.40, 2.0, 2.0, 2.5, 0.3)
        raise AssertionError("current_target=0.3 应抛 ValueError")
    except ValueError:
        pass
    print("[OK] decide_target 四规则全分支 + 边界(0.50/0.90) + 优先级 + 防御")
    # —— 状态机：合法转移 + 非法组合 ValueError ——
    assert state_transition(0.0, 0.5) == BUY_HALF
    assert state_transition(0.0, 1.0) == BUY_FULL
    assert state_transition(0.5, 1.0) == BUY_HALF
    assert state_transition(0.5, 0.0) == SELL_ALL
    assert state_transition(1.0, 0.0) == SELL_ALL
    assert state_transition(0.0, 0.0) == NO_ORDER
    assert state_transition(0.5, 0.5) == NO_ORDER
    assert state_transition(1.0, 1.0) == NO_ORDER
    for bad in ((1.0, 0.5), (0.3, 0.5), (0.5, 0.3)):
        try:
            state_transition(*bad)
            raise AssertionError("非法转移应抛 ValueError: %r" % (bad,))
        except ValueError:
            pass
    print("[OK] 状态机合法转移 6 类 + 非法组合 ValueError")
    # —— compute_delta：100 向下取整 ——
    assert compute_delta(100000.0, 0.5, 3.0, 0) == (16600, 16600)
    assert compute_delta(100000.0, 1.0, 1.0, 0) == (100000, 100000)
    assert compute_delta(100000.0, 1.0, 3.0, 500) == (33300, 32800)
    assert compute_delta(100000.0, 0.0, 2.0, 1000) == (0, -1000)
    print("[OK] compute_delta 目标/调仓数量 + 100 向下取整")
    # —— sell_quantity：max(0, min(-delta, available))，整手向下取整 ——
    assert sell_quantity(-500, 300) == 300
    assert sell_quantity(-500, 150) == 100
    assert sell_quantity(-50, 500) == 0
    assert sell_quantity(-100, 99) == 0
    assert sell_quantity(-100, 100) == 100
    assert sell_quantity(0, 500) == 0
    assert sell_quantity(500, 500) == 0
    print("[OK] sell_quantity T+1 可卖约束 + 整手向下取整")
    # —— 幂等键 / decision_id 格式 ——
    assert order_intent_key("20260602", 0.5) == "emotion-trend-159915-v1:1.0.0:20260602:159915:0.5"
    assert order_intent_key("20260602", 1.0) == "emotion-trend-159915-v1:1.0.0:20260602:159915:1.0"
    assert order_intent_key("20260602", 0.0) == "emotion-trend-159915-v1:1.0.0:20260602:159915:0.0"
    assert decision_id("20260602") == "emotion-trend-159915-v1:20260602"
    try:
        order_intent_key("2026060", 0.5)
        raise AssertionError("坏日期应抛 ValueError")
    except ValueError:
        pass
    print("[OK] order_intent_key / decision_id 幂等键格式")
    # —— is_valid_target ——
    assert is_valid_target(0.0) and is_valid_target(0.5) and is_valid_target(1.0)
    assert not is_valid_target(0.3) and not is_valid_target("0.5") and not is_valid_target(None)
    print("[OK] is_valid_target")
    # —— replay_decision 对账重放（与 decide_target 一致；兼容 previous_target 字段名）——
    snap = {"current_rank": 0.55, "previous_rank": 0.40, "ma5": 3.0,
            "ma10": 2.0, "ma20": 2.5, "current_target": 0.0}
    assert replay_decision(snap) == decide_target(0.55, 0.40, 3.0, 2.0, 2.5, 0.0) == (0.5, "P50_UPCROSS_PROBE")
    snap2 = dict(snap, current_target=None, previous_target=0.0)
    assert replay_decision(snap2) == (0.5, "P50_UPCROSS_PROBE")
    try:
        replay_decision({"current_rank": 0.55})
        raise AssertionError("缺字段应抛 ValueError")
    except ValueError:
        pass
    print("[OK] replay_decision 重放 = decide_target 一致")
    print("ALL_ASSERTIONS_PASSED")


if __name__ == "__main__":
    _self_test()
