#!/usr/bin/env python3
"""test_paper — 模拟盘 Phase 4 模块全离线单元测试（任务：docker/webui/test_paper.py）

覆盖四个模块（全部离线，无网络 / 无真实 pybao / 无真实文件系统残留）：
  - paper_core   : decide_target 四规则全分支+边界（rank 恰 0.50/0.90、ma 相等）、
                   状态机 5 合法转移+非法 ValueError、compute_delta 100 取整、
                   sell_quantity T+1 上限、order_intent_key/decision_id 格式、
                   replay_decision 重放一致性。
  - paper_db     : :memory: 9 表 CRUD、create_decision/create_intent 防重返回 False、
                   transition_intent_status WHERE 守卫、get_latest_qualified_signal 派生、
                   快照历史、事件追加。
  - mx_client    : 本地 http.server mock（成功 / 401→DEPENDENCY_UNAVAILABLE /
                   113→RATE_LIMITED / 404→INVALID_ARGUMENT / 网络→INTERNAL_ERROR）、
                   place_order payload（market/limit/100 倍数 ValueError）、masked_key。
  - paper_engine : 可控 clock + fake fetch_daily + fake mx + 内存 DB：
                   happy path 全时间轴（08:45→15:05）、趋势不合格不写库、
                   信号文件缺失/字段不合格→DATA_NOT_QUALIFIED 不生成订单、decision 防重、
                   执行窗外不执行（14:49:59/14:56:31 拒绝、14:50:00/14:56:30 放行）、
                   部分成交只按 fills 更新账本、T+1 卖出仅 available_to_sell、
                   paused/trading_enabled=False 不放行、MX 异常不重发（事件记原码）、
                   重放偏差告警。

运行（必须贴最后 5 行输出）：
    cd docker/webui && .venv/bin/python -m unittest test_paper -v
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

# === paper_core ===
import paper_core
from paper_core import (
    STRATEGY_ID, STRATEGY_VERSION, SYMBOL, POSITION_STATES, LOT_SIZE,
    DECISION_TIME, EXEC_WINDOW_START, EXEC_CUTOFF, STOP_CHASE, RECONCILE,
    MODEL_NAV_DEFAULT,
    SIGNAL_READY, DECIDED, EXECUTION_PENDING, SUBMITTED, PARTIALLY_FILLED,
    FILLED, UNFILLED, UNFILLED_AT_CUTOFF, RECONCILED, DATA_NOT_QUALIFIED,
    BUY_HALF, BUY_FULL, SELL_ALL, NO_ORDER,
    REASON_WEAK_STATE_CONFIRMED, REASON_STRONG_TREND_CONFIRMED,
    REASON_P50_UPCROSS_PROBE, REASON_HOLD,
    ERROR_INVALID_ARGUMENT, ERROR_NOT_PUBLISHED,
    ERROR_DEPENDENCY_UNAVAILABLE, ERROR_RATE_LIMITED, ERROR_INTERNAL_ERROR,
    is_valid_target, decide_target, state_transition, compute_delta,
    sell_quantity, order_intent_key, decision_id, replay_decision,
)
# === paper_db ===
from paper_db import PaperDB, ACTION_BUY_HALF, ACTION_BUY_FULL, ACTION_SELL_ALL
# === mx_client ===
from mx_client import MXClient, MXError
# === paper_engine（复用其全 mock 基础设施：可控时钟 / 假券商 / 假 fetch_daily） ===
from paper_engine import PaperEngine, _FakeClock, _FakeMX, _FakeFetchDaily, FAILED

T = "20260804"  # 周二；T-1 = 20260803（周一，交易日）


# =====================================================================
# 1) paper_core —— 纯逻辑
# =====================================================================
class PaperCoreTest(unittest.TestCase):
    """paper_core：四规则全分支+边界 / 状态机 / 数量 / 幂等键 / 重放。"""

    def test_core_freeze_constants(self):
        """冻结契约常量逐字一致（策略/生命周期/错误码）。"""
        self.assertEqual(STRATEGY_ID, "emotion-trend-159915-v1")
        self.assertEqual(STRATEGY_VERSION, "1.0.0")
        self.assertEqual(SYMBOL, "159915")
        self.assertEqual(POSITION_STATES, (0.0, 0.5, 1.0))
        self.assertEqual(LOT_SIZE, 100)
        self.assertEqual(DECISION_TIME, "09:27")
        self.assertEqual(EXEC_WINDOW_START, "14:50:00")
        self.assertEqual(EXEC_CUTOFF, "14:56:30")
        self.assertEqual(STOP_CHASE, "14:57:00")
        self.assertEqual(RECONCILE, "15:05:00")
        self.assertEqual(MODEL_NAV_DEFAULT, 100000.0)
        self.assertEqual(len(paper_core.ERROR_CODES), 8)

    def test_decide_rule1_weak_state(self):
        """规则1：弱势状态确认 → 空仓（含边界 rank=0.49/0.50、ma 相等不触发）。"""
        self.assertEqual(decide_target(0.30, 0.50, 1.0, 1.5, 2.0, 1.0),
                         (0.0, REASON_WEAK_STATE_CONFIRMED))
        self.assertEqual(decide_target(0.49, 0.50, 1.0, 1.5, 2.0, 0.5),
                         (0.0, REASON_WEAK_STATE_CONFIRMED))  # rank < 0.50 边界下
        # rank == 0.50 不满足 < 0.50 → 规则1 不触发 → 规则4 保持
        self.assertEqual(decide_target(0.50, 0.60, 2.0, 2.5, 2.5, 0.5),
                         (0.5, REASON_HOLD))
        # ma5 == ma10 不满足 ma5 < ma10 → 规则1 不触发 → 保持
        self.assertEqual(decide_target(0.30, 0.60, 1.5, 1.5, 2.0, 0.5),
                         (0.5, REASON_HOLD))

    def test_decide_rule2_strong_trend(self):
        """规则2：强势趋势确认 → 满仓（含边界 rank=0.90、ma10==ma20 不触发）。"""
        self.assertEqual(decide_target(0.95, 0.50, 3.0, 2.0, 1.0, 0.0),
                         (1.0, REASON_STRONG_TREND_CONFIRMED))
        self.assertEqual(decide_target(0.90, 0.50, 3.0, 2.0, 1.0, 0.5),
                         (1.0, REASON_STRONG_TREND_CONFIRMED))  # rank == 0.90 边界
        self.assertEqual(decide_target(0.90, 0.50, 3.0, 2.0, 2.0, 0.5),
                         (0.5, REASON_HOLD))  # ma10 == ma20 → 严格递增不满足
        self.assertEqual(decide_target(0.89, 0.50, 3.0, 2.0, 1.0, 0.5),
                         (0.5, REASON_HOLD))  # rank 0.89 < 0.90 边界下

    def test_decide_rule3_p50_upcross(self):
        """规则3：P50 上穿试探 → 半仓（含边界 rank=0.50、previous_rank=0.50 不触发）。"""
        self.assertEqual(decide_target(0.55, 0.40, 3.0, 2.0, 2.5, 0.0),
                         (0.5, REASON_P50_UPCROSS_PROBE))
        self.assertEqual(decide_target(0.50, 0.40, 3.0, 2.0, 2.5, 0.0),
                         (0.5, REASON_P50_UPCROSS_PROBE))  # current_rank == 0.50 边界
        # previous_rank == 0.50 不满足 < 0.50 → 规则3 不触发
        self.assertEqual(decide_target(0.60, 0.50, 3.0, 2.0, 2.5, 0.0),
                         (0.0, REASON_HOLD))
        # 非空仓 current_target=0.5 → 规则3 不触发
        self.assertEqual(decide_target(0.60, 0.40, 3.0, 2.0, 2.5, 0.5),
                         (0.5, REASON_HOLD))

    def test_decide_rule4_hold(self):
        """规则4：其余情况保持当前目标（含 rank=0.50 + ma5<ma10 的组合）。"""
        self.assertEqual(decide_target(0.50, 0.60, 2.0, 2.5, 2.5, 0.0),
                         (0.0, REASON_HOLD))  # rank 恰 0.50 但 ma5<ma10 → 规则4
        self.assertEqual(decide_target(0.60, 0.40, 2.0, 2.0, 2.5, 0.5),
                         (0.5, REASON_HOLD))
        self.assertEqual(decide_target(0.80, 0.80, 2.0, 1.9, 2.0, 1.0),
                         (1.0, REASON_HOLD))
        self.assertEqual(decide_target(0.20, 0.20, 1.5, 1.5, 1.5, 0.5),
                         (0.5, REASON_HOLD))  # 全均线相等 → 保持

    def test_decide_rule2_priority_over_rule3(self):
        """优先级：规则2 先于规则3（0→1 满仓而非半仓试探）。"""
        self.assertEqual(decide_target(0.95, 0.30, 3.0, 2.0, 1.0, 0.0),
                         (1.0, REASON_STRONG_TREND_CONFIRMED))

    def test_decide_invalid_current_target(self):
        """防御：current_target 非法（0.3 等）→ ValueError。"""
        for bad in (0.3, 0.7, 2.0, -0.5):
            with self.assertRaises(ValueError):
                decide_target(0.60, 0.40, 2.0, 2.0, 2.5, bad)

    def test_state_transition_legal_five(self):
        """状态机：5 类合法转移。"""
        self.assertEqual(state_transition(0.0, 0.5), BUY_HALF)
        self.assertEqual(state_transition(0.0, 1.0), BUY_FULL)
        self.assertEqual(state_transition(0.5, 1.0), BUY_HALF)
        self.assertEqual(state_transition(0.5, 0.0), SELL_ALL)
        self.assertEqual(state_transition(1.0, 0.0), SELL_ALL)

    def test_state_transition_no_order_unchanged(self):
        """状态机：目标不变 → NO_ORDER（3 种保持）。"""
        self.assertEqual(state_transition(0.0, 0.0), NO_ORDER)
        self.assertEqual(state_transition(0.5, 0.5), NO_ORDER)
        self.assertEqual(state_transition(1.0, 1.0), NO_ORDER)

    def test_state_transition_illegal_valueerror(self):
        """状态机：非法组合（1→0.5 / 非法仓位）→ ValueError。"""
        for prev, desired in ((1.0, 0.5), (0.3, 0.5), (0.5, 0.3), (0.7, 1.0)):
            with self.assertRaises(ValueError):
                state_transition(prev, desired)

    def test_compute_delta_rounding_and_sign(self):
        """compute_delta：目标/调仓数量 + 100 向下取整 + 正负号。"""
        self.assertEqual(compute_delta(100000.0, 0.5, 3.0, 0), (16600, 16600))
        self.assertEqual(compute_delta(100000.0, 1.0, 1.0, 0), (100000, 100000))
        self.assertEqual(compute_delta(100000.0, 1.0, 3.0, 500), (33300, 32800))
        self.assertEqual(compute_delta(100000.0, 0.0, 2.0, 1000), (0, -1000))
        # 目标数量恒为 100 的整数倍
        tq, _ = compute_delta(99999.0, 0.5, 0.33, 0)
        self.assertEqual(tq % 100, 0)

    def test_compute_delta_invalid_args(self):
        """compute_delta：nav/ref_price 非法 → ValueError。"""
        with self.assertRaises(ValueError):
            compute_delta(0.0, 0.5, 1.0, 0)
        with self.assertRaises(ValueError):
            compute_delta(100000.0, 0.5, 0.0, 0)
        with self.assertRaises(ValueError):
            compute_delta(100000.0, 0.3, 1.0, 0)  # 非法 desired

    def test_sell_quantity_t1_cap_and_rounding(self):
        """sell_quantity：max(0, min(-delta, available)) + 整手向下取整（T+1 上限）。"""
        self.assertEqual(sell_quantity(-500, 300), 300)   # 可卖封顶
        self.assertEqual(sell_quantity(-500, 150), 100)   # 不足一手截断
        self.assertEqual(sell_quantity(-50, 500), 0)      # 负数方向取 0
        self.assertEqual(sell_quantity(-100, 99), 0)      # 可卖不足一手
        self.assertEqual(sell_quantity(-100, 100), 100)   # 恰好一手
        self.assertEqual(sell_quantity(0, 500), 0)        # delta>=0 → 0
        self.assertEqual(sell_quantity(500, 500), 0)      # 需买不卖

    def test_order_intent_key_format(self):
        """order_intent_key：冻结格式 + 坏日期/坏仓位 ValueError。"""
        self.assertEqual(
            order_intent_key("20260602", 0.5),
            "emotion-trend-159915-v1:1.0.0:20260602:159915:0.5")
        self.assertEqual(
            order_intent_key("20260602", 1.0),
            "emotion-trend-159915-v1:1.0.0:20260602:159915:1.0")
        self.assertEqual(
            order_intent_key("20260602", 0.0),
            "emotion-trend-159915-v1:1.0.0:20260602:159915:0.0")
        with self.assertRaises(ValueError):
            order_intent_key("2026060", 0.5)
        with self.assertRaises(ValueError):
            order_intent_key("20260602", 0.3)

    def test_decision_id_format(self):
        """decision_id：每日唯一格式 + 坏日期 ValueError。"""
        self.assertEqual(decision_id("20260602"), "emotion-trend-159915-v1:20260602")
        self.assertEqual(decision_id("20260804"), "emotion-trend-159915-v1:20260804")
        with self.assertRaises(ValueError):
            decision_id("2026060")
        with self.assertRaises(ValueError):
            decision_id("abcd1234")

    def test_is_valid_target(self):
        """is_valid_target：合法集合成员判定。"""
        self.assertTrue(is_valid_target(0.0) and is_valid_target(0.5)
                        and is_valid_target(1.0))
        self.assertFalse(is_valid_target(0.3) or is_valid_target("0.5")
                         or is_valid_target(None))

    def test_replay_decision_consistency(self):
        """replay_decision：与 decide_target 一致 + previous_target 字段名兼容。"""
        snap = {"current_rank": 0.55, "previous_rank": 0.40, "ma5": 3.0,
                "ma10": 2.0, "ma20": 2.5, "current_target": 0.0}
        self.assertEqual(replay_decision(snap),
                         decide_target(0.55, 0.40, 3.0, 2.0, 2.5, 0.0))
        self.assertEqual(replay_decision(snap)[0], 0.5)
        self.assertEqual(replay_decision(snap)[1], REASON_P50_UPCROSS_PROBE)
        snap2 = dict(snap, current_target=None, previous_target=0.0)
        self.assertEqual(replay_decision(snap2), replay_decision(snap))

    def test_replay_decision_missing_fields(self):
        """replay_decision：缺少必需字段 / 缺少目标字段 → ValueError。"""
        with self.assertRaises(ValueError):
            replay_decision({"current_rank": 0.55})
        with self.assertRaises(ValueError):
            replay_decision({"current_rank": 0.55, "previous_rank": 0.4,
                             "ma5": 1.0, "ma10": 2.0, "ma20": 3.0})  # 无 current_target
        with self.assertRaises(ValueError):
            replay_decision({"current_rank": 0.55, "previous_rank": 0.4,
                             "ma5": 1.0, "ma10": 2.0, "ma20": 3.0,
                             "current_target": 0.3})  # 非法目标


# =====================================================================
# 2) paper_db —— :memory: 持久层
# =====================================================================
class PaperDBTest(unittest.TestCase):
    """paper_db：9 表 CRUD / 幂等防重 / WHERE 守卫 / 派生 / 快照历史 / 事件追加。"""

    _TABLES = {
        "signal_snapshots", "trend_snapshots", "strategy_decisions",
        "order_intents", "broker_orders", "fills", "portfolio_snapshots",
        "daily_reconciliations", "system_events",
    }

    def setUp(self):
        self.db = PaperDB.connect(":memory:")

    def tearDown(self):
        self.db.close()

    def test_tables_created_nine(self):
        """建库即建 9 张表（:memory:）。"""
        names = {r["name"] for r in self.db._fetch_all(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%'")}
        self.assertEqual(names, self._TABLES)
        self.assertEqual(len(names), 9)

    def test_signal_snapshot_crud_overwrite(self):
        """signal_snapshots：投递 + 当日覆盖（追加式修订，行数不变）。"""
        self.assertTrue(self.db.insert_signal(
            trade_date="20260801", current_rank=0.35, metric_value=58.2,
            history_count=60, formal_usable=1,
            source_contract_version="emotion-v1", known_at="2026-08-01 09:26:10"))
        self.assertTrue(self.db.insert_signal(
            trade_date="20260801", current_rank=0.36, metric_value=59.0,
            history_count=60, formal_usable=1,
            source_contract_version="emotion-v1", known_at="2026-08-01 09:26:11"))
        self.assertEqual(self.db._count("signal_snapshots"), 1)  # 当日覆盖非新增
        row = self.db._fetch_one(
            "SELECT * FROM signal_snapshots WHERE trade_date='20260801'")
        self.assertEqual(row["current_rank"], 0.36)

    def test_get_latest_qualified_signal_derivation(self):
        """get_latest_qualified_signal：合格过滤 + 严格早于 + previous_rank 派生口径。"""
        self.db.insert_signal(trade_date="20260801", current_rank=0.35,
                              metric_value=58.2, history_count=60, formal_usable=1,
                              source_contract_version="emotion-v1",
                              known_at="2026-08-01 09:26:10")
        self.db.insert_signal(trade_date="20260802", current_rank=0.55,
                              previous_rank=0.35, metric_value=66.4,
                              history_count=60, formal_usable=1,
                              source_contract_version="emotion-v1",
                              known_at="2026-08-02 09:26:05")
        self.db.insert_signal(trade_date="20260803", current_rank=0.30,
                              metric_value=41.0, history_count=60, formal_usable=0,
                              source_contract_version="emotion-v1",
                              known_at="2026-08-03 09:26:00")
        self.db.insert_signal(trade_date="20260803", current_rank=0.31,
                              metric_value=42.0, history_count=60, formal_usable=1,
                              source_contract_version="emotion-v1",
                              known_at="2026-08-03 09:27:20")
        self.assertEqual(self.db._count("signal_snapshots"), 3)
        latest = self.db.get_latest_qualified_signal("20260804")
        self.assertIsNotNone(latest)
        self.assertEqual(latest["trade_date"], "20260803")
        self.assertEqual(latest["current_rank"], 0.31)  # 覆盖后最新值生效
        prev = self.db.get_latest_qualified_signal("20260802")
        self.assertEqual(prev["trade_date"], "20260801")  # 严格早于，不含当日
        self.assertEqual(prev["current_rank"], 0.35)
        self.assertIsNone(self.db.get_latest_qualified_signal("20260801"))  # 无更早

    def test_trend_snapshot_crud(self):
        """trend_snapshots：写入 + 当日覆盖 + 读取 / 无记录 None。"""
        self.assertTrue(self.db.insert_trend(
            trade_date="20260803", ma5=1.21, ma10=1.19, ma20=1.17,
            bar_count=120, last_bar_date="20260803", known_at="2026-08-03 09:25:40"))
        self.assertTrue(self.db.insert_trend(
            trade_date="20260803", ma5=1.22, ma10=1.19, ma20=1.17,
            bar_count=120, last_bar_date="20260803", known_at="2026-08-03 09:26:00"))
        trend = self.db.get_trend("20260803")
        self.assertEqual(trend["ma5"], 1.22)
        self.assertIsNone(self.db.get_trend("20260801"))
        self.assertEqual(self.db._count("trend_snapshots"), 1)

    def test_create_decision_dedupe(self):
        """create_decision：首次 True / 重复 False / 原记录不可覆盖。"""
        did = "emotion-trend-159915-v1:20260803"
        self.assertTrue(self.db.create_decision(
            decision_id=did, trade_date="20260803", previous_rank=0.55,
            current_rank=0.31, ma5=1.22, ma10=1.19, ma20=1.17,
            previous_target=0.0, desired_target=0.0,
            reason_code="WEAK_STATE_CONFIRMED"))
        self.assertFalse(self.db.create_decision(
            decision_id=did, trade_date="20260803", current_rank=0.99,
            ma5=9.9, ma10=9.9, ma20=9.9, previous_target=0.0,
            desired_target=1.0, reason_code="HACKED"))
        row = self.db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE decision_id=?", (did,))
        self.assertEqual(row["desired_target"], 0.0)  # 未被覆盖
        self.assertEqual(row["reason_code"], "WEAK_STATE_CONFIRMED")

    def test_create_intent_dedupe(self):
        """create_intent：首次 True / 重复 False（intent_key 幂等去重）。"""
        ik = "emotion-trend-159915-v1:1.0.0:20260803:159915:0.0"
        self.assertTrue(self.db.create_intent(
            intent_key=ik, decision_id="emotion-trend-159915-v1:20260803",
            trade_date="20260803", symbol=SYMBOL, desired_target=0.0,
            action=ACTION_SELL_ALL, target_qty=0, delta_qty=-50000,
            price_type="market"))
        self.assertFalse(self.db.create_intent(
            intent_key=ik, decision_id="emotion-trend-159915-v1:20260803",
            trade_date="20260803", symbol=SYMBOL, desired_target=1.0,
            action=ACTION_BUY_FULL, target_qty=60000, delta_qty=60000,
            price_type="market"))
        self.assertEqual(self.db._count("order_intents"), 1)
        row = self.db._fetch_one(
            "SELECT * FROM order_intents WHERE intent_key=?", (ik,))
        self.assertEqual(row["desired_target"], 0.0)

    def test_transition_intent_status_guard(self):
        """transition_intent_status：WHERE status=old 守卫（old 不匹配 → False）。"""
        ik = "emotion-trend-159915-v1:1.0.0:20260803:159915:0.0"
        self.db.create_intent(intent_key=ik,
                              decision_id="emotion-trend-159915-v1:20260803",
                              trade_date="20260803", symbol=SYMBOL,
                              desired_target=0.0, action=ACTION_SELL_ALL,
                              target_qty=0, delta_qty=-50000,
                              price_type="market")
        self.assertTrue(self.db.transition_intent_status(ik, EXECUTION_PENDING,
                                                         SUBMITTED))
        self.assertFalse(self.db.transition_intent_status(ik, EXECUTION_PENDING,
                                                          SUBMITTED))  # 已非 old
        self.assertTrue(self.db.transition_intent_status(ik, SUBMITTED, FILLED))
        self.assertTrue(self.db.transition_intent_status(ik, FILLED, RECONCILED))
        self.assertFalse(self.db.transition_intent_status(ik, UNFILLED,
                                                          RECONCILED))
        row = self.db._fetch_one(
            "SELECT * FROM order_intents WHERE intent_key=?", (ik,))
        self.assertEqual(row["status"], RECONCILED)

    def test_broker_order_upsert_single_row(self):
        """broker_orders：同 order_id 修订保持单行，最新状态生效。"""
        self.db.create_intent(intent_key="ik1",
                              decision_id="emotion-trend-159915-v1:20260803",
                              trade_date="20260803", symbol=SYMBOL,
                              desired_target=0.0, action=ACTION_SELL_ALL,
                              target_qty=0, delta_qty=-50000, price_type="market")
        self.assertTrue(self.db.upsert_broker_order(
            order_id="MX20260803001", intent_key="ik1", trade_date="20260803",
            symbol=SYMBOL, action="sell", quantity=50000, price_type="market",
            status=SUBMITTED, submitted_at="2026-08-03 14:52:10"))
        self.assertTrue(self.db.upsert_broker_order(
            order_id="MX20260803001", intent_key="ik1", trade_date="20260803",
            symbol=SYMBOL, action="sell", quantity=50000, price_type="market",
            status=FILLED, submitted_at="2026-08-03 14:52:10"))
        self.assertEqual(self.db._count("broker_orders"), 1)
        row = self.db._fetch_one(
            "SELECT * FROM broker_orders WHERE order_id='MX20260803001'")
        self.assertEqual(row["status"], FILLED)

    def test_fills_idempotent(self):
        """fills：fill_id 幂等去重（重复返回 False）+ get_fills 按日查询。"""
        self.assertTrue(self.db.insert_fill(
            fill_id="FL20260803001", order_id="MX20260803001",
            trade_date="20260803", symbol=SYMBOL, fill_qty=50000,
            fill_price=1.18, fill_time="2026-08-03 14:53:02"))
        self.assertFalse(self.db.insert_fill(
            fill_id="FL20260803001", order_id="MX20260803001",
            trade_date="20260803", symbol=SYMBOL, fill_qty=99999,
            fill_price=9.9))  # 成交历史不可覆盖
        fills = self.db.get_fills("20260803")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fill_qty"], 50000)
        self.assertEqual(self.db.get_fills("20260804"), [])

    def test_portfolio_snapshot_overwrite_history(self):
        """portfolio_snapshots：当日覆盖 + get_snapshots(limit) 降序历史。"""
        self.db.snapshot_portfolio(trade_date="20260803", nav=100500.0,
                                   position_qty=0, available_to_sell_qty=0)
        self.db.snapshot_portfolio(trade_date="20260803", nav=100800.0,
                                   position_qty=0, available_to_sell_qty=0)
        self.db.snapshot_portfolio(trade_date="20260804", nav=101000.0,
                                   position_qty=0, available_to_sell_qty=0)
        snaps = self.db.get_snapshots(1)
        self.assertEqual(len(snaps), 1)
        self.assertEqual(snaps[0]["trade_date"], "20260804")  # 最新在前
        snaps2 = self.db.get_snapshots(10)
        self.assertEqual([s["trade_date"] for s in snaps2],
                         ["20260804", "20260803"])  # 降序
        day3 = self.db._fetch_one(
            "SELECT * FROM portfolio_snapshots WHERE trade_date='20260803'")
        self.assertEqual(day3["nav"], 100800.0)  # 当日覆盖生效
        with self.assertRaises(ValueError):
            self.db.get_snapshots(0)

    def test_reconciliation_overwrite(self):
        """daily_reconciliations：upsert 当日覆盖。"""
        self.db.upsert_reconciliation(trade_date="20260803", target_qty=0,
                                      actual_qty=0, deviation=0,
                                      filled_qty=50000, unfilled_qty=0,
                                      notes="SELL_ALL 已成交")
        self.db.upsert_reconciliation(trade_date="20260803", target_qty=0,
                                      actual_qty=0, deviation=0,
                                      filled_qty=50000, unfilled_qty=0,
                                      notes="对账通过")
        rc = self.db._fetch_one(
            "SELECT * FROM daily_reconciliations WHERE trade_date='20260803'")
        self.assertEqual(rc["notes"], "对账通过")
        self.assertEqual(self.db._count("daily_reconciliations"), 1)

    def test_system_events_append_only(self):
        """system_events：仅追加 + 非法级别 ValueError。"""
        self.db.add_event(event="DECISION_CREATED", trade_date="20260803",
                          timepoint="09:27", level="INFO", detail="决策已落库")
        self.db.add_event(event="ORDER_SUBMITTED", trade_date="20260803",
                          timepoint="14:52", level="INFO")
        self.db.add_event(event="RECONCILE_OK", trade_date="20260803",
                          timepoint="15:05", level="ERROR", detail="对账一致")
        with self.assertRaises(ValueError):
            self.db.add_event(event="BAD_LEVEL", level="FATAL")
        events = self.db._fetch_all("SELECT * FROM system_events ORDER BY id")
        self.assertEqual(len(events), 3)  # 追加三行，无覆盖
        self.assertEqual([e["event"] for e in events],
                         ["DECISION_CREATED", "ORDER_SUBMITTED", "RECONCILE_OK"])

    def test_open_intents_filter(self):
        """get_open_intents：仅返回非终态（EXECUTION_PENDING/SUBMITTED/PARTIALLY_FILLED）。"""
        for i, (dt, status) in enumerate([
                ("20260804", EXECUTION_PENDING),
                ("20260804", SUBMITTED),
                ("20260804", PARTIALLY_FILLED),
                ("20260804", FILLED)]):
            self.assertTrue(self.db.create_intent(
                intent_key=f"ik-{i}", decision_id=f"did-{i}",
                trade_date=dt, symbol=SYMBOL, desired_target=0.5,
                action=ACTION_BUY_HALF, target_qty=100, delta_qty=100,
                price_type="market", status=status))
        open_intents = self.db.get_open_intents("20260804")
        self.assertEqual(len(open_intents), 3)
        self.assertEqual(self.db.get_open_intents("20260803"), [])

    def test_validation_valueerror(self):
        """参数/状态校验 → 中文 ValueError。"""
        with self.assertRaises(ValueError):
            self.db.create_decision(decision_id="", trade_date="20260805")
        with self.assertRaises(ValueError):
            self.db.create_decision(decision_id="did-x", trade_date="20260805",
                                    desired_target=0.3)  # 非法仓位
        with self.assertRaises(ValueError):
            self.db.insert_signal(trade_date="20260805", current_rank=0.5,
                                  metric_value=1.0, history_count=60,
                                  formal_usable=2,
                                  source_contract_version="emotion-v1",
                                  known_at="2026-08-05 09:26:00")
        with self.assertRaises(ValueError):
            self.db.create_intent(intent_key="k", decision_id="d",
                                  trade_date="20260805", symbol=SYMBOL,
                                  desired_target=0.5, action="HOLD",
                                  target_qty=100, delta_qty=100,
                                  price_type="market")  # NO_ORDER 不产生意图
        with self.assertRaises(ValueError):
            self.db.insert_trend(trade_date="20260805", ma5=-1.0, ma10=1.0,
                                 ma20=1.0, bar_count=20,
                                 last_bar_date="20260804",
                                 known_at="2026-08-05 08:45:00")
        with self.assertRaises(ValueError):
            self.db.snapshot_portfolio(trade_date="20260805", nav=0.0,
                                       position_qty=0)

    def test_close_idempotent(self):
        """close：幂等可重复调用 + is_closed。"""
        self.assertFalse(self.db.is_closed)
        self.db.close()
        self.assertTrue(self.db.is_closed)
        self.db.close()  # 二次关闭不抛
        self.assertTrue(self.db.is_closed)


# =====================================================================
# 3) mx_client —— 本地 http.server mock
# =====================================================================
class _MockMXHandler(BaseHTTPRequestHandler):
    """本地 mock：按响应队列逐次应答，记录请求供断言。"""

    responses: list = []   # 队列 [(status, body_dict), ...]
    requests: list = []    # 记录 {"path","payload","apikey"}

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        type(self).requests.append({
            "path": self.path,
            "payload": payload,
            "apikey": self.headers.get("apikey"),
        })
        if type(self).responses:
            status, body = type(self).responses.pop(0)
        else:
            status, body = 500, {"code": 500, "message": "mock 未配置响应"}
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


class MXClientTest(unittest.TestCase):
    """mx_client：错误码映射 / payload / 掩码（全部走 127.0.0.1 本地 mock）。"""

    KEY = "12345678ABCDEFGH"

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockMXHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _MockMXHandler.responses.clear()
        _MockMXHandler.requests.clear()

    def _client(self, **kw):
        return MXClient(apikey=self.KEY, base_url=self.base, timeout=5.0, **kw)

    def _respond(self, status, body):
        _MockMXHandler.responses.append((status, body))

    # ---------------- 成功路径 ----------------
    def test_mx_success_payload_and_header(self):
        """成功：place_order market payload 结构 + apikey 请求头透传。"""
        self._respond(200, {"code": 0, "data": {"orderId": "MX1", "ok": True}})
        resp = self._client().place_order("buy", "159915", 100, "MARKET")
        self.assertEqual(resp["code"], 0)
        last = _MockMXHandler.requests[-1]
        self.assertEqual(last["payload"],
                         {"type": "buy", "stockCode": "159915",
                          "quantity": 100, "useMarketPrice": True})
        self.assertEqual(last["apikey"], self.KEY)
        self.assertTrue(last["path"].endswith("/mockTrading/trade"))

    def test_mx_limit_payload(self):
        """成功：place_order limit payload 带 price 且 useMarketPrice=False。"""
        self._respond(200, {"code": 0, "data": {"orderId": "MX2"}})
        self._client().place_order("buy", "159915", 100, "LIMIT", price=1.5)
        last = _MockMXHandler.requests[-1]
        self.assertEqual(last["payload"],
                         {"type": "buy", "stockCode": "159915",
                          "quantity": 100, "useMarketPrice": False, "price": 1.5})

    # ---------------- 错误码映射 ----------------
    def test_mx_401_dependency_unavailable(self):
        """401 → DEPENDENCY_UNAVAILABLE（hint：检查 apikey），异常不含密钥。"""
        self._respond(401, {"code": 401, "message": "无效 apikey"})
        with self.assertRaises(MXError) as cm:
            self._client().get_balance()
        exc = cm.exception
        self.assertEqual(exc.code, ERROR_DEPENDENCY_UNAVAILABLE)
        self.assertEqual(exc.hint, "检查 apikey")
        self.assertNotIn(self.KEY, str(exc))  # apikey 永不回显

    def test_mx_113_rate_limited(self):
        """响应 code=113 → RATE_LIMITED。"""
        self._respond(200, {"code": 113, "message": "请求过于频繁"})
        with self.assertRaises(MXError) as cm:
            self._client().get_positions()
        self.assertEqual(cm.exception.code, ERROR_RATE_LIMITED)

    def test_mx_404_unbound_invalid_argument(self):
        """404 + 未绑定提示 → INVALID_ARGUMENT（hint：绑定模拟账户）。"""
        self._respond(404, {"code": 404, "message": "模拟账户未绑定，请先绑定"})
        with self.assertRaises(MXError) as cm:
            self._client().get_orders()
        exc = cm.exception
        self.assertEqual(exc.code, ERROR_INVALID_ARGUMENT)
        self.assertEqual(exc.hint, "绑定模拟账户")

    def test_mx_404_plain_dependency_unavailable(self):
        """404 但非未绑定（接口地址有误）→ DEPENDENCY_UNAVAILABLE。"""
        self._respond(404, {"code": 404, "message": "接口不存在"})
        with self.assertRaises(MXError) as cm:
            self._client().get_orders()
        self.assertEqual(cm.exception.code, ERROR_DEPENDENCY_UNAVAILABLE)

    def test_mx_network_error_internal(self):
        """网络异常（端口未监听）→ INTERNAL_ERROR。"""
        c = MXClient(apikey=self.KEY, base_url="http://127.0.0.1:1", timeout=1.0)
        with self.assertRaises(MXError) as cm:
            c.get_balance()
        self.assertEqual(cm.exception.code, ERROR_INTERNAL_ERROR)
        self.assertNotIn(self.KEY, str(cm.exception))

    # ---------------- 参数校验 ----------------
    def test_mx_place_order_validation_valueerror(self):
        """place_order 参数校验 ValueError（action/100 倍数/price_type/限价）。"""
        c = self._client()
        for bad in (
            lambda: c.place_order("hold", "159915", 100, "MARKET"),
            lambda: c.place_order("buy", "159915", 150, "MARKET"),  # 非 100 倍数
            lambda: c.place_order("buy", "159915", 0, "MARKET"),
            lambda: c.place_order("buy", "159915", 100, "FOK"),
            lambda: c.place_order("buy", "159915", 100, "LIMIT"),       # 无 price
            lambda: c.place_order("buy", "159915", 100, "LIMIT", price=-1),
        ):
            with self.assertRaises(ValueError):
                bad()
        self.assertEqual(_MockMXHandler.requests, [])  # 校验失败不下发

    # ---------------- masked_key / 未配置 ----------------
    def test_mx_masked_key_rules(self):
        """masked_key：前4后4 / 超短全掩码 / 未配置。"""
        self.assertEqual(self._client().masked_key, "1234****EFGH")
        self.assertNotIn(self.KEY, self._client().masked_key)
        c_short = MXClient(apikey="shortkey", base_url=self.base)
        self.assertEqual(c_short.masked_key, "****")

    def test_mx_no_apikey_invalid_argument(self):
        """无任何 apikey 来源 → 未配置；调用抛 MXError(INVALID_ARGUMENT)。"""
        with tempfile.TemporaryDirectory(prefix="mx_nokey_") as d:
            with mock.patch.dict(os.environ,
                                 {"MX_APIKEY": "", "DATA_DIR": d},
                                 clear=False):
                c = MXClient(base_url=self.base)
                self.assertEqual(c.masked_key, "未配置")
                with self.assertRaises(MXError) as cm:
                    c.get_balance()
                self.assertEqual(cm.exception.code, ERROR_INVALID_ARGUMENT)


# =====================================================================
# 4) paper_engine —— 可控 clock + fake fetch_daily + fake mx + 内存 DB
# =====================================================================
class PaperEngineTest(unittest.TestCase):
    """paper_engine：全时间轴 / 不合格不写库 / 防重 / 窗口边界 / T+1 / 异常不重发。"""

    T_DT = datetime.datetime(2026, 8, 4, 8, 45, 0)

    def _make_env(self, config=None, quote=None, positions=None, balance=None,
                  auto_fill=False, fetch=None, reverse=False):
        """构造 (db, mx, clock, engine)：内存 DB + fake mx + fake fetch + 可控时钟。"""
        tmpdir = tempfile.mkdtemp(prefix="test_paper_engine_")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        db = PaperDB.connect(":memory:")
        self.addCleanup(db.close)
        mx = _FakeMX(quote=quote, positions=positions, balance=balance,
                     auto_fill=auto_fill)
        clock = _FakeClock(self.T_DT)
        fetch_daily = fetch or _FakeFetchDaily(reverse=reverse)
        engine = PaperEngine(db=db, mx=mx, fetch_daily=fetch_daily,
                             signal_dir=tmpdir, clock=clock,
                             config={"trading_enabled": True, **(config or {})})
        return db, mx, clock, engine

    def _write_signal(self, engine, trade_date, **over):
        data = {"current_rank": 0.55, "previous_rank": 0.40, "metric_value": 66.4,
                "history_count": 60, "formal_usable": True,
                "source_contract_version": "emotion-v1",
                "known_at": f"{trade_date[:4]}-{trade_date[4:6]}-"
                            f"{trade_date[6:]} 09:26:05"}
        data.update(over)
        with open(os.path.join(engine.signal_dir, f"{trade_date}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _run_to_decision(self, db, mx, clock, engine, over=None):
        """跑到 09:27 决策（08:45 趋势 + 写信号 + 09:27 冻结决策）。"""
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        self._write_signal(engine, "20260804", **(over or {}))
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        return engine.run_timepoint("09:27")

    def _event_codes(self, db, trade_date):
        return [e["event"] for e in db._fetch_all(
            "SELECT * FROM system_events WHERE trade_date=?", (trade_date,))]

    # ---------------- 构造校验 ----------------
    def test_engine_constructor_validation(self):
        """PaperEngine 构造：非法依赖注入 → ValueError。"""
        db = PaperDB.connect(":memory:")
        mx = _FakeMX()
        clock = _FakeClock(self.T_DT)
        with self.assertRaises(ValueError):
            PaperEngine(db=db, mx=mx, fetch_daily=None, signal_dir="/tmp/x",
                        clock=clock)
        with self.assertRaises(ValueError):
            PaperEngine(db=db, mx=mx, fetch_daily=lambda c, s, e: {},
                        signal_dir="", clock=clock)
        with self.assertRaises(ValueError):
            PaperEngine(db=db, mx=mx, fetch_daily=lambda c, s, e: {},
                        signal_dir="/tmp/x", clock=None)
        with self.assertRaises(ValueError):
            PaperEngine(db=db, mx=mx, fetch_daily=lambda c, s, e: {},
                        signal_dir="/tmp/x", clock=clock,
                        config={"model_nav": 0})
        db.close()

    # ---------------- happy path 全时间轴 ----------------
    def test_engine_happy_path_full_timeline(self):
        """happy path：08:45 → 09:27 → 09:28 → 14:45 → 14:50 → 14:57 → 15:05。"""
        db, mx, clock, engine = self._make_env(auto_fill=True)
        # 08:45 趋势
        self.assertTrue(engine.run_timepoint("08:45"))
        trend = db.get_trend("20260804")
        self.assertIsNotNone(trend)
        self.assertGreater(trend["ma5"], trend["ma10"])  # 递增收盘 → ma5 > ma10
        self.assertGreaterEqual(trend["bar_count"], 20)
        self.assertEqual(trend["last_bar_date"], "20260803")  # T-1 周一
        # 09:27 冻结 + 决策（规则3：0→0.5）
        self._write_signal(engine, "20260804")
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        decision = engine.run_timepoint("09:27")
        self.assertIsInstance(decision, dict)
        self.assertEqual(decision["desired_target"], 0.5)
        self.assertEqual(decision["reason_code"], REASON_P50_UPCROSS_PROBE)
        self.assertEqual(decision["previous_target"], 0.0)  # 无历史 → 0.0
        # 09:28 决策确认
        clock.set(datetime.datetime(2026, 8, 4, 9, 28, 0))
        self.assertEqual(engine.run_timepoint("09:28")["decision_id"],
                         decision["decision_id"])
        # 14:45 执行前风控（含参考价快照）
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        self.assertEqual(engine._ref_price_snapshot, 1.0)
        # 14:50 窗口下单
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 30))
        res = engine.run_timepoint("14:50")
        self.assertTrue(res[0], res)
        intents = db._fetch_all(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(len(intents), 1)
        self.assertEqual(intents[0]["status"], SUBMITTED)
        self.assertEqual(intents[0]["action"], BUY_HALF)
        orders = db._fetch_all(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")
        self.assertEqual(len(orders), 1)
        self.assertEqual(orders[0]["quantity"], intents[0]["delta_qty"])
        self.assertEqual(orders[0]["status"], SUBMITTED)
        # 14:57 停止追价（auto_fill 已成交 → 不标 UNFILLED_AT_CUTOFF）
        clock.set(datetime.datetime(2026, 8, 4, 14, 57, 0))
        sr = engine.run_timepoint("14:57")
        self.assertTrue(sr[0])
        self.assertEqual(sr[1]["marked_unfilled"], 0)
        # 15:05 收盘对账
        clock.set(datetime.datetime(2026, 8, 4, 15, 5, 0))
        rr = engine.run_timepoint("15:05")
        self.assertTrue(rr[0], rr)
        self.assertEqual(rr[1]["replay_ok"], True)
        final = db._fetch_one(
            "SELECT * FROM order_intents WHERE intent_key=?",
            (intents[0]["intent_key"],))
        self.assertEqual(final["status"], RECONCILED)
        brow = db._fetch_one("SELECT * FROM broker_orders WHERE order_id=?",
                             (orders[0]["order_id"],))
        self.assertEqual(brow["status"], FILLED)
        fills = db.get_fills("20260804")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fill_qty"], orders[0]["quantity"])
        snap = db._fetch_one(
            "SELECT * FROM portfolio_snapshots WHERE trade_date='20260804'")
        self.assertEqual(snap["position_qty"], orders[0]["quantity"])
        self.assertAlmostEqual(snap["nav"], 100000.0, delta=1.0)
        rc = db._fetch_one(
            "SELECT * FROM daily_reconciliations WHERE trade_date='20260804'")
        self.assertEqual(rc["deviation"], 0)  # 全部成交 → target == actual

    # ---------------- 趋势不合格不写库 ----------------
    def test_engine_trend_not_qualified_no_write(self):
        """趋势不合格（行数<20 / 最后一行非交易日 / 不连续）→ 不写趋势库。"""
        # 行数 < 20
        db, mx, clock, engine = self._make_env(
            fetch=lambda code, s, e: {"bars": [{"date": s.strftime("%Y%m%d"),
                                                "close": 1.0} for _ in range(5)]})
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertFalse(engine.run_timepoint("08:45"))
        self.assertIsNone(db.get_trend("20260804"))          # 不写趋势快照
        self.assertEqual(db._count("trend_snapshots"), 0)    # 库内零行
        self.assertIn(DATA_NOT_QUALIFIED,
                      self._event_codes(db, "20260804"))
        # 最后一行 != 最近交易日
        def fetch_short(code, s, e):
            return {"bars": _FakeFetchDaily()(code, s, e)["bars"][:-1]}
        db2, mx2, clock2, engine2 = self._make_env(fetch=fetch_short)
        clock2.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertFalse(engine2.run_timepoint("08:45"))
        self.assertEqual(db2._count("trend_snapshots"), 0)
        # K 线中间缺一个有效交易日（不连续）
        def fetch_hole(code, s, e):
            bars = _FakeFetchDaily()(code, s, e)["bars"]
            return {"bars": [b for i, b in enumerate(bars) if i != 30]}
        db3, mx3, clock3, engine3 = self._make_env(fetch=fetch_hole)
        clock3.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertFalse(engine3.run_timepoint("08:45"))
        self.assertEqual(db3._count("trend_snapshots"), 0)
        self.assertIn(DATA_NOT_QUALIFIED,
                      self._event_codes(db3, "20260804"))

    # ---------------- 信号不合格 → DATA_NOT_QUALIFIED 不生成订单 ----------------
    def test_engine_signal_missing_data_not_qualified(self):
        """信号文件缺失 → DATA_NOT_QUALIFIED（原始码 NOT_PUBLISHED 入库，不生成订单）。"""
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        r = engine.run_timepoint("09:27")
        self.assertEqual(r, DATA_NOT_QUALIFIED)
        codes = self._event_codes(db, "20260804")
        self.assertIn(ERROR_NOT_PUBLISHED, codes)  # 原始 8 码原样入库
        self.assertIn(DATA_NOT_QUALIFIED, codes)
        self.assertIsNone(db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'"))
        self.assertEqual(db.get_open_intents("20260804"), [])

    def test_engine_signal_invalid_field_no_order(self):
        """信号字段不合格（history_count/契约版本）→ DATA_NOT_QUALIFIED，不生成订单。"""
        # history_count=59
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        self._write_signal(engine, "20260804", history_count=59)
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        self.assertEqual(engine.run_timepoint("09:27"), DATA_NOT_QUALIFIED)
        ev = db._fetch_all("SELECT * FROM system_events WHERE trade_date='20260804'"
                           " AND event='DATA_NOT_QUALIFIED'")
        self.assertTrue(any("history_count" in (e["detail"] or "") for e in ev), ev)
        self.assertIsNone(db._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'"))
        self.assertEqual(db.get_open_intents("20260804"), [])
        # 不受支持的契约版本
        db2, mx2, clock2, engine2 = self._make_env()
        clock2.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine2.run_timepoint("08:45"))
        self._write_signal(engine2, "20260804",
                           source_contract_version="emotion-v9")
        clock2.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        self.assertEqual(engine2.run_timepoint("09:27"), DATA_NOT_QUALIFIED)
        self.assertIsNone(db2._fetch_one(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'"))
        self.assertEqual(db2.get_open_intents("20260804"), [])

    # ---------------- decision 防重 ----------------
    def test_engine_decision_dedupe(self):
        """decision 防重：同日重复决策不覆盖、不产生第二行，事件告警。"""
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))
        self._write_signal(engine, "20260804")
        clock.set(datetime.datetime(2026, 8, 4, 9, 27, 0))
        d1 = engine.run_timepoint("09:27")
        self.assertIsInstance(d1, dict)
        d2 = engine.run_timepoint("09:27")  # 再次投递
        self.assertEqual(d2["decision_id"], d1["decision_id"])
        self.assertEqual(d2["desired_target"], d1["desired_target"])
        rows = db._fetch_all(
            "SELECT * FROM strategy_decisions WHERE trade_date='20260804'")
        self.assertEqual(len(rows), 1)  # 不产生第二行
        self.assertIn("DECISION_DUPLICATE", self._event_codes(db, "20260804"))

    # ---------------- 执行窗外不执行（边界） ----------------
    def test_engine_exec_window_boundaries(self):
        """执行窗口边界：14:49:59/14:56:31 拒绝、14:50:00/14:56:30 放行。"""
        # 窗口前 14:49:59 拒绝 → 14:50:00 放行（首单提交）
        db, mx, clock, engine = self._make_env()
        self._run_to_decision(db, mx, clock, engine)
        r1 = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 49, 59))
        self.assertFalse(r1[0])
        self.assertIn("执行窗口外", r1[1])
        self.assertEqual(mx.place_order_calls, [])
        r2 = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 50, 0))
        self.assertTrue(r2[0], r2)  # 恰在窗口起点 → 放行
        self.assertEqual(len(mx.place_order_calls), 1)
        # 恰在窗口终点 14:56:30 → 放行
        db2, mx2, clock2, engine2 = self._make_env()
        self._run_to_decision(db2, mx2, clock2, engine2)
        r3 = engine2.step_execute(datetime.datetime(2026, 8, 4, 14, 56, 30))
        self.assertTrue(r3[0], r3)
        # 截止后 14:56:31 → 拒绝（无意图、无委托、无下单调用）
        db3, mx3, clock3, engine3 = self._make_env()
        self._run_to_decision(db3, mx3, clock3, engine3)
        r4 = engine3.step_execute(datetime.datetime(2026, 8, 4, 14, 56, 31))
        self.assertFalse(r4[0])
        self.assertIn("执行窗口外", r4[1])
        self.assertEqual(len(db3._fetch_all(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")), 0)
        self.assertEqual(len(db3._fetch_all(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")), 0)
        self.assertEqual(mx3.place_order_calls, [])

    def test_engine_no_decision_skipped(self):
        """窗口内但当日无决策 → 跳过不发单（EXEC_SKIPPED）。"""
        db, mx, clock, engine = self._make_env()
        clock.set(datetime.datetime(2026, 8, 4, 8, 45, 0))
        self.assertTrue(engine.run_timepoint("08:45"))  # 只有趋势，无信号无决策
        r = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 50, 0))
        self.assertFalse(r[0])
        self.assertIn("无决策", r[1])
        self.assertIn("EXEC_SKIPPED", self._event_codes(db, "20260804"))
        self.assertEqual(mx.place_order_calls, [])

    # ---------------- 部分成交只按 fills 更新账本 ----------------
    def test_engine_partial_fill_ledger_by_fills(self):
        """部分成交：账本只按 fills 更新（非全量委托），偏差/未成交如实记录。"""
        db, mx, clock, engine = self._make_env(auto_fill=False)
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertTrue(res[0], res)
        order_qty = int(db._fetch_one(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")["quantity"])
        self.assertEqual(order_qty, 50000)  # 目标 0.5 × 10 万 @ 1.0
        # 注入券商回报：仅部分成交 20000 / 50000
        mx.orders[0].update({
            "status": "partial_filled",
            "filledQuantity": 20000,
            "fillList": [{"fillId": "FL90000001", "fillQty": 20000,
                          "fillPrice": 1.0, "fillTime": "14:53:00", "fee": 0.0}],
        })
        mx.positions = [{"symbol": SYMBOL, "quantity": 20000,
                         "available_to_sell_qty": 0}]
        mx.balance = {"cash": 80000.0}
        clock.set(datetime.datetime(2026, 8, 4, 14, 57, 0))
        sr = engine.run_timepoint("14:57")
        self.assertTrue(sr[0])
        self.assertEqual(sr[1]["marked_unfilled"], 0)  # 部分成交留给收盘对账
        clock.set(datetime.datetime(2026, 8, 4, 15, 5, 0))
        rr = engine.run_timepoint("15:05")
        self.assertTrue(rr[0], rr)
        # fills 只落部分成交量（20000，非 50000）
        fills = db.get_fills("20260804")
        self.assertEqual(len(fills), 1)
        self.assertEqual(fills[0]["fill_qty"], 20000)
        self.assertEqual(rr[1]["fills_added"], 1)
        # 账本按 fills 更新：持仓 20000、偏差 30000、未成交 30000
        snap = db._fetch_one(
            "SELECT * FROM portfolio_snapshots WHERE trade_date='20260804'")
        self.assertEqual(snap["position_qty"], 20000)
        rc = db._fetch_one(
            "SELECT * FROM daily_reconciliations WHERE trade_date='20260804'")
        self.assertEqual(rc["deviation"], 30000)
        self.assertEqual(rc["filled_qty"], 20000)
        self.assertEqual(rc["unfilled_qty"], 30000)
        brow = db._fetch_one(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")
        self.assertEqual(brow["status"], PARTIALLY_FILLED)
        intent = db._fetch_one(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(intent["status"], RECONCILED)

    # ---------------- T+1 卖出仅 available_to_sell ----------------
    def test_engine_t1_sell_cap(self):
        """T+1：卖出量 = min(-delta, available_to_sell)，只卖可卖部分。"""
        db, mx, clock, engine = self._make_env(
            positions=[{"symbol": SYMBOL, "quantity": 60000,
                        "available_to_sell_qty": 20000}],
            reverse=True)  # 递减收盘 → ma5 < ma10
        db.create_decision(decision_id=decision_id("20260803"),
                           trade_date="20260803", previous_rank=0.4,
                           current_rank=0.9, ma5=3.0, ma10=2.0, ma20=1.0,
                           previous_target=0.0, desired_target=1.0,
                           reason_code="STRONG_TREND_CONFIRMED")
        d = self._run_to_decision(db, mx, clock, engine,
                                  over={"current_rank": 0.30,
                                        "previous_rank": 0.60})
        self.assertIsInstance(d, dict)
        self.assertEqual(d["desired_target"], 0.0)  # 规则1 清仓
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertTrue(res[0], res)
        intent = db._fetch_one(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(intent["action"], SELL_ALL)
        order = db._fetch_one(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")
        self.assertEqual(order["action"], "sell")
        self.assertEqual(order["quantity"], 20000)  # T+1：仅可卖 20000（delta=-60000）
        # 14:57 未成交 → UNFILLED_AT_CUTOFF
        clock.set(datetime.datetime(2026, 8, 4, 14, 57, 0))
        sr = engine.run_timepoint("14:57")
        self.assertTrue(sr[0])
        self.assertEqual(sr[1]["marked_unfilled"], 1)
        st = db._fetch_one(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(st["status"], UNFILLED_AT_CUTOFF)

    # ---------------- paused / trading_enabled=False 不放行 ----------------
    def test_engine_paused_blocked(self):
        """paused=True：风控不放行 + 窗口不下单。"""
        db, mx, clock, engine = self._make_env(config={"paused": True})
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertFalse(ok)
        self.assertFalse([c for c in report["checks"]
                          if c["name"] == "paused"][0]["ok"])
        self.assertIn("RISK_BLOCKED", self._event_codes(db, "20260804"))
        r = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 50, 0))
        self.assertFalse(r[0])
        self.assertIn("模拟盘已暂停", r[1])
        self.assertEqual(db.get_open_intents("20260804"), [])
        self.assertEqual(mx.place_order_calls, [])

    def test_engine_trading_disabled_blocked(self):
        """trading_enabled=False：风控不放行 + 窗口不下单。"""
        db, mx, clock, engine = self._make_env(config={"trading_enabled": False})
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertFalse(ok)
        self.assertFalse([c for c in report["checks"]
                          if c["name"] == "trading_enabled"][0]["ok"])
        self.assertIn("RISK_BLOCKED", self._event_codes(db, "20260804"))
        r = engine.step_execute(datetime.datetime(2026, 8, 4, 14, 50, 0))
        self.assertFalse(r[0])
        self.assertIn("模拟盘未启用", r[1])
        self.assertEqual(db.get_open_intents("20260804"), [])

    # ---------------- MX 异常不重发（事件记原始码） ----------------
    def test_engine_mx_error_no_resend_original_code(self):
        """MX 异常：事件记原始 8 码 + 意图 FAILED，同 intent 重试成功且不重发。"""
        db, mx, clock, engine = self._make_env()
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        mx.place_order_error = MXError(code=ERROR_RATE_LIMITED,
                                       message="模拟限流")
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        r1 = engine.run_timepoint("14:50")
        self.assertFalse(r1[0])
        self.assertIn("RATE_LIMITED", r1[1])
        st = db._fetch_one(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(st["status"], FAILED)  # 意图置 FAILED
        codes = self._event_codes(db, "20260804")
        self.assertIn(ERROR_RATE_LIMITED, codes)  # 事件记原始码
        self.assertEqual(len(mx.place_order_calls), 1)
        self.assertEqual(len(db._fetch_all(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")), 0)
        # 同 intent 重试成功
        mx.place_order_error = None
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 30))
        r2 = engine.run_timepoint("14:50")
        self.assertTrue(r2[0], r2)
        st2 = db._fetch_one(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")
        self.assertEqual(st2["status"], SUBMITTED)
        self.assertEqual(len(db._fetch_all(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")), 1)
        self.assertEqual(len(mx.place_order_calls), 2)

    # ---------------- 意图幂等不重发 ----------------
    def test_engine_intent_dedup_no_resend(self):
        """意图幂等：已 SUBMITTED 重复投递 → dedup，不重发委托。"""
        db, mx, clock, engine = self._make_env(auto_fill=False)
        self._run_to_decision(db, mx, clock, engine)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        r1 = engine.run_timepoint("14:50")
        self.assertTrue(r1[0], r1)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 20))
        r2 = engine.run_timepoint("14:50")  # 重复投递
        self.assertTrue(r2[0], r2)
        self.assertEqual(r2[1].get("dedup"), True)
        self.assertEqual(len(mx.place_order_calls), 1)  # 只下了一单
        self.assertEqual(len(db._fetch_all(
            "SELECT * FROM broker_orders WHERE trade_date='20260804'")), 1)
        self.assertEqual(len(db._fetch_all(
            "SELECT * FROM order_intents WHERE trade_date='20260804'")), 1)

    # ---------------- 目标不变不下单 ----------------
    def test_engine_no_order_target_unchanged(self):
        """desired == previous_target → NO_ORDER 不下单。"""
        db, mx, clock, engine = self._make_env()
        db.create_decision(decision_id=decision_id("20260803"),
                           trade_date="20260803", previous_rank=0.55,
                           current_rank=0.55, ma5=1.22, ma10=1.19, ma20=1.17,
                           previous_target=0.0, desired_target=0.0,
                           reason_code="HOLD")
        d = self._run_to_decision(db, mx, clock, engine,
                                  over={"current_rank": 0.55,
                                        "previous_rank": 0.55})
        self.assertIsInstance(d, dict)
        self.assertEqual(d["desired_target"], 0.0)
        clock.set(datetime.datetime(2026, 8, 4, 14, 45, 0))
        ok, report = engine.run_timepoint("14:45")
        self.assertTrue(ok, report)
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        res = engine.run_timepoint("14:50")
        self.assertFalse(res[0])
        self.assertIn("目标不变", res[1])
        self.assertIn("NO_ORDER", self._event_codes(db, "20260804"))
        self.assertEqual(db.get_open_intents("20260804"), [])
        self.assertEqual(mx.place_order_calls, [])

    # ---------------- 重放偏差告警 ----------------
    def test_engine_replay_mismatch_warn(self):
        """决策输入被篡改 → 重放不一致 → REPLAY_MISMATCH 告警。"""
        db, mx, clock, engine = self._make_env(auto_fill=True)
        self._run_to_decision(db, mx, clock, engine)
        # 篡改决策输入 current_rank（正常 0.55 → desired 0.5）→ 重放得 0.0 不一致
        did = decision_id("20260804")
        db._conn.execute("UPDATE strategy_decisions SET current_rank = 0.20"
                         " WHERE decision_id = ?", (did,))
        db._conn.commit()
        clock.set(datetime.datetime(2026, 8, 4, 14, 50, 0))
        self.assertTrue(engine.run_timepoint("14:50")[0])
        clock.set(datetime.datetime(2026, 8, 4, 15, 5, 0))
        rr = engine.run_timepoint("15:05")
        self.assertTrue(rr[0], rr)
        self.assertEqual(rr[1]["replay_ok"], False)
        self.assertIn("REPLAY_MISMATCH", self._event_codes(db, "20260804"))

    # ---------------- 未知时点 ----------------
    def test_engine_unknown_timepoint(self):
        """run_timepoint 未知时点 → ValueError。"""
        _, _, _, engine = self._make_env()
        with self.assertRaises(ValueError):
            engine.run_timepoint("12:00")


if __name__ == "__main__":
    unittest.main(verbosity=2)
