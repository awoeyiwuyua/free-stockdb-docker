#!/usr/bin/env python3
"""test_ops — 运营支撑（Phase 4.5 任务D）全离线单元测试

覆盖六块（全部离线：无网络 / 无真实 DATA_DIR 残留；上游版本探针走本地
http.server mock + patch urlopen）：
  - Alerts                  : add/list 顺序（最新在前）、当日去重、跨日放行、200 条
                          上限滚动、clear/count、文件持久化与损坏容错、级别校验、
                          notify_alert 惰性单例（绑定 DATA_DIR、复用实例）。
  - data_freshness_alert     : latest=None 分支、日期无法解析分支、滞后>阈值分支、
                          滞后<=阈值不告警、非交易日不告警、时钟超前不告警、
                          YYYY-MM-DD 支持；patch Alerts 实例断言 add 调用。
  - capture_mcp_call         : jsonl 逐行追加与 2000 行截断、内存 deque 500 上限、
                          list 最新在前、stats（ok_rate/avg_ms/p95_ms/by_tool）
                          计算正确、空窗口、重启后从 jsonl 惰性恢复、落盘失败降级。
  - fetch_upstream_release   : 本地 http.server mock 200 解析 tag_name、非 200
                          （本地 mock 500 / patch HTTPError）返回 None、网络异常
                          返回 None、TTL 缓存二次调用不再次请求（mock 计数）、
                          失败也缓存、force 绕过缓存。
  - paper_audit_report       : 临时 sqlite 最小 schema+数据：1 条正常 decision + 1 条
                          篡改 desired → replay_mismatches==1；重复 intent →
                          duplicate_intents；非法转移（1.0→0.5）→ illegal_transitions；
                          fills+broker_orders → slippage；快照 → nav_series；
                          fetch_benchmark 注入 → benchmark_series 归一化（含
                          YYYY-MM-DD 键兼容）；空库/零表库/缺失库 → 全 0/[] 不抛；
                          mode=ro 只读校验。
  - signal_status            : 好文件 7 项 checks 全 ok、缺文件 exists False、history_count
                          !=60、current_rank/metric_value/formal_usable/contract/
                          known_at/previous_rank 各类失败 checks、previous_rank 缺省
                          合法、契约注入、根节点非对象、非法 trade_date。

接线说明（Phase 4.5 返工 2）：
  被测运营支撑实现（Alerts / data_freshness_alert / capture_mcp_call /
  fetch_upstream_release / paper_audit_report / signal_status）已整体迁移至
  app.py 生产代码（模块级），本文件不再内嵌参考副本；测试 import app 并引用
  app.X 生产实现，setUp 把 app.DATA_DIR 打补丁到临时目录并复位 app 模块全局态
  （告警单例 / MCP deque / 版本探针缓存），保证离线、无残留、互不影响。
  53 项测试语义与行为基线不变。

运行（自测命令，须贴 Ran/OK 与最后几行）：
    cd docker/webui && /Users/xiahaihe/Claudecode/free-stockdb-docker/.venv/bin/python -m unittest test_ops -v
回归（test_paper + mcp 不受影响）：
    cd docker/webui && /Users/xiahaihe/Claudecode/free-stockdb-docker/.venv/bin/python -m unittest test_paper mcp.test_stockdb_mcp_server -v
"""

from __future__ import annotations

# =====================================================================
# 生产实现接线（Phase 4.5 返工 2）：被测运营支撑实现（Alerts /
# data_freshness_alert / capture_mcp_call / fetch_upstream_release /
# paper_audit_report / signal_status）已整体迁移至 app.py 生产代码（模块级），
# 本文件不再内嵌参考副本；测试 import app 并引用 app.X，setUp 把 app.DATA_DIR
# 打补丁到临时目录并复位 app 模块全局态，保证离线、无残留、互不影响。
# 53 项测试语义与行为基线不变（全部打到 app.py 真实函数）。
# =====================================================================
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request
import unittest
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

import app                       # 生产实现（被测对象）
import paper_core                # 纯逻辑（夹具构造：decision_id / order_intent_key）
from paper_core import STRATEGY_ID

# 静默 stdlib 无害噪声：urllib 探测非 2xx 时抛出的 HTTPError 内持临时文件，
# 对象被 GC 时 tempfile 模块发出的 ResourceWarning（本模块主动吞异常是预期行为）。
warnings.filterwarnings("ignore", category=ResourceWarning)




class _OpsTestCase(unittest.TestCase):
    """公共夹具：每用例独立临时目录 + DATA_DIR 隔离 + 模块全局态复位。

    防止用例间串扰：告警单例、MCP 内存 deque/加载标记/行数、版本探针缓存
    全部复位；DATA_DIR 指向临时目录，避免任何真实 /data 残留。
    """

    def setUp(self):
        # unittest runner 在 run 开始处 simplefilter("default") 会重置警告过滤器，
        # 故在每个用例 setUp 中重新注册：屏蔽 urllib 非 2xx 时 HTTPError 内临时
        # 文件被 GC 产生的无害 ResourceWarning（HTTPError 对象可能在任何时刻被
        # 回收，必须全局保持屏蔽）。
        warnings.filterwarnings("ignore", category=ResourceWarning)
        self.tmp = tempfile.mkdtemp(prefix="test_ops_")
        # 打补丁 app.DATA_DIR 到临时目录：被测函数全部读模块全局 DATA_DIR
        #（Alerts 单例 / mcp jsonl / 审计 / 信号目录），保证离线、无残留、互不影响。
        self._data_patch = mock.patch.object(app, "DATA_DIR", Path(self.tmp))
        self._data_patch.start()
        self.addCleanup(self._reset)

    def _reset(self):
        self._data_patch.stop()
        # 复位 app 模块全局态：告警单例 / MCP 内存 deque 与加载标记 / 版本探针缓存
        app._alerts_singleton = None
        app._mcp_deque.clear()
        app._mcp_loaded = False
        app._mcp_file_lines = 0
        app._RELEASE_CACHE.update(at=0.0, val=None)
        shutil.rmtree(self.tmp, ignore_errors=True)


# =====================================================================
# 1) Alerts —— 告警中心
# =====================================================================
class AlertsTest(_OpsTestCase):
    """Alerts：add/list 顺序 / 当日去重 / 跨日放行 / 200 上限滚动 / clear /
    count / 持久化 / 级别校验；notify_alert 惰性单例。"""

    def _alerts(self, name="alerts.json"):
        return app.Alerts.init(os.path.join(self.tmp, name))

    def test_alerts_add_and_list_order(self):
        """add 追加、list 最新在前、limit 生效、返回条目字段齐全。"""
        a = self._alerts()
        a.add("info", "系统", "第一条")
        a.add("warning", "数据", "第二条")
        a.add("error", "引擎", "第三条")
        self.assertEqual(a.count(), 3)
        self.assertEqual([e["message"] for e in a.list(2)], ["第三条", "第二条"])
        self.assertEqual(a.list(1)[0]["message"], "第三条")
        self.assertEqual(len(a.list()), 3)
        e = a.list(1)[0]
        self.assertEqual(set(e), {"ts", "level", "source", "message"})
        self.assertEqual(e["level"], "error")
        self.assertEqual(e["ts"][:10], datetime.date.today().isoformat())

    def test_alerts_same_day_dedup(self):
        """当日去重：同 (date, source, message) 幂等返回既有条目、不新增。"""
        a = self._alerts()
        e1 = a.add("warning", "数据", "重复消息")
        e2 = a.add("warning", "数据", "重复消息")
        self.assertIs(e2, e1)              # 返回同一对象
        self.assertEqual(a.count(), 1)
        # 去重键 = (date, source, message)，不含 level：同源同消息换级别仍去重
        e3 = a.add("error", "数据", "重复消息")
        self.assertIs(e3, e1)
        self.assertEqual(a.count(), 1)
        # 同消息不同 source → 允许新增
        a.add("error", "引擎", "重复消息")
        self.assertEqual(a.count(), 2)

    def test_alerts_cross_day_allowed(self):
        """跨日放行：同日去重、跨日允许再次出现（patch _now_iso 控制日期）。"""
        times = iter(["2026-08-03T10:00:00", "2026-08-03T11:00:00",
                      "2026-08-04T10:00:00"])
        with mock.patch.object(app, "_now_iso", side_effect=lambda: next(times)):
            a = self._alerts()
            a.add("info", "系统", "跨日消息")   # 08-03
            a.add("info", "系统", "跨日消息")   # 08-03 同日 → 去重
            self.assertEqual(a.count(), 1)
            a.add("info", "系统", "跨日消息")   # 08-04 跨日 → 新增
            self.assertEqual(a.count(), 2)
            self.assertTrue(a.list(1)[0]["ts"].startswith("2026-08-04"))

    def test_alerts_roll_200_cap(self):
        """200 条上限滚动：超出保留最新 200 条，最旧淘汰。"""
        a = self._alerts("roll.json")
        for i in range(205):
            a.add("info", "系统", f"滚动消息 {i}")
        self.assertEqual(a.count(), app.MAX_ALERTS)
        self.assertEqual(a.list(1)[0]["message"], "滚动消息 204")  # 最新保留
        msgs = {e["message"] for e in a.list(app.MAX_ALERTS + 50)}
        self.assertNotIn("滚动消息 0", msgs)   # 最旧淘汰
        self.assertIn("滚动消息 204", msgs)

    def test_alerts_clear(self):
        """clear：清空并落盘（文件保持存在、内容为 []）。"""
        a = self._alerts()
        a.add("info", "系统", "待清空")
        a.add("warning", "数据", "待清空二")
        a.clear()
        self.assertEqual(a.count(), 0)
        self.assertEqual(a.list(), [])
        with open(a.path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), [])

    def test_alerts_persist_reload(self):
        """持久化：新实例加载同一文件，条数与顺序一致。"""
        path = os.path.join(self.tmp, "persist.json")
        a = app.Alerts.init(path)
        a.add("info", "系统", "A")
        a.add("warning", "数据", "B")
        a2 = app.Alerts.init(path)
        self.assertEqual(a2.count(), 2)
        self.assertEqual([e["message"] for e in a2.list()], ["B", "A"])

    def test_alerts_corrupt_file_tolerant(self):
        """文件损坏 / 非数组 → 空列表不抛，仍可继续写入。"""
        path = os.path.join(self.tmp, "bad.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 JSON")
        a = app.Alerts.init(path)
        self.assertEqual(a.count(), 0)
        a.add("info", "系统", "恢复后仍可写")
        self.assertEqual(a.count(), 1)
        # 非数组根节点同样容错
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        a2 = app.Alerts.init(path)
        self.assertEqual(a2.count(), 0)

    def test_alerts_level_validation(self):
        """级别别名归一化；非法级别 / 空 source / 空 message → 中文 ValueError。"""
        a = self._alerts()
        a.add("warn", "系统", "别名")      # warn → warning
        self.assertEqual(a.list(1)[0]["level"], "warning")
        a.add("INFO", "系统", "大写转小写")
        self.assertEqual(a.list(1)[0]["level"], "info")
        with self.assertRaises(ValueError):
            a.add("fatal", "系统", "非法级别")
        with self.assertRaises(ValueError):
            a.add("info", "   ", "空 source")
        with self.assertRaises(ValueError):
            a.add("info", "系统", "   ")

    def test_alerts_list_limit_edge(self):
        """list limit 边界：0 / 负数 / 非数字 → 回退默认 50。"""
        a = self._alerts()
        for i in range(5):
            a.add("info", "系统", f"m{i}")
        self.assertEqual(len(a.list(0)), 5)
        self.assertEqual(len(a.list(-3)), 5)
        self.assertEqual(len(a.list("abc")), 5)

    def test_notify_alert_lazy_singleton(self):
        """notify_alert 惰性单例：首次调用创建、绑定 DATA_DIR、复用同一实例。"""
        app._alerts_singleton = None
        try:
            self.assertIsNone(app._alerts_singleton)      # 惰性：未调用前为 None
            e = app.notify_alert("error", "系统", "单例告警")
            self.assertIsNotNone(app._alerts_singleton)
            self.assertIs(app._get_alerts(), app._alerts_singleton)   # 复用
            self.assertEqual(app._alerts_singleton.path,
                             os.path.join(self.tmp, "alerts.json"))
            self.assertTrue(os.path.isfile(os.path.join(self.tmp, "alerts.json")))
            self.assertEqual(e["level"], "error")
            app.notify_alert("error", "系统", "单例告警")  # 当日去重生效
            self.assertEqual(app._alerts_singleton.count(), 1)
            # env 变更不影响已绑定实例（惰性只绑定首次创建时的 DATA_DIR）
            with mock.patch.dict(os.environ, {"DATA_DIR": "/elsewhere"}):
                app.notify_alert("info", "系统", "第二条")
            self.assertEqual(app._alerts_singleton.count(), 2)
            self.assertFalse(os.path.isfile("/elsewhere/alerts.json"))
        finally:
            app._alerts_singleton = None


# =====================================================================
# 2) data_freshness_alert —— 数据新鲜度告警
# =====================================================================
class FreshnessAlertTest(_OpsTestCase):
    """data_freshness_alert：latest None / 日期无法解析 / 滞后超阈 / 阈值内不告警 /
    非交易日不告警 / 时钟超前 / YYYY-MM-DD；patch Alerts 实例断言 add 调用。"""

    def setUp(self):
        super().setUp()
        self.alerts = app.Alerts.init(os.path.join(self.tmp, "fresh.json"))

    def _days_ago(self, n, fmt="%Y%m%d"):
        return (datetime.date.today() - datetime.timedelta(days=n)).strftime(fmt)

    def test_freshness_latest_none_branch(self):
        """分支1：latest_date=None → 探针失败告警（warning/数据；当日去重）。"""
        app.data_freshness_alert(None, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        top = self.alerts.list()[0]
        self.assertEqual((top["level"], top["source"]), ("warning", "数据"))
        self.assertEqual(top["message"], "行情数据不可用（探针失败）")
        app.data_freshness_alert(None, False, alerts=self.alerts)  # 当日去重
        self.assertEqual(self.alerts.count(), 1)

    def test_freshness_unparsable_date_branch(self):
        """分支1：日期无法解析 → 同样探针失败告警。"""
        app.data_freshness_alert("20-08-04", True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        self.assertEqual(self.alerts.list()[0]["message"],
                         "行情数据不可用（探针失败）")

    def test_freshness_lag_over_threshold(self):
        """分支2：滞后 5 天 > 阈值 2（交易日）→ 滞后告警。"""
        d5 = self._days_ago(5)
        app.data_freshness_alert(d5, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        self.assertEqual(self.alerts.list()[0]["message"],
                         f"行情数据已滞后 5 天（最新 {d5}）")

    def test_freshness_lag_within_threshold_no_alert(self):
        """滞后 1 天 ≤ 阈值不告警；lag == 阈值（恰 2 天）也不告警。"""
        app.data_freshness_alert(self._days_ago(1), True, alerts=self.alerts)
        app.data_freshness_alert(self._days_ago(2), True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_non_trading_day_no_alert(self):
        """非交易日：即使滞后超阈也不告警（休市数据不更新属正常）。"""
        app.data_freshness_alert(self._days_ago(5), False, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_future_date_no_alert(self):
        """时钟超前（滞后为负）→ 不告警。"""
        future = (datetime.date.today() + datetime.timedelta(days=1)).strftime("%Y%m%d")
        app.data_freshness_alert(future, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 0)

    def test_freshness_dash_date_supported(self):
        """YYYY-MM-DD 输入支持；滞后 4 天 > 2 → 告警。"""
        d4 = self._days_ago(4, fmt="%Y-%m-%d")
        app.data_freshness_alert(d4, True, alerts=self.alerts)
        self.assertEqual(self.alerts.count(), 1)
        self.assertIn("已滞后 4 天", self.alerts.list()[0]["message"])

    def test_freshness_alerts_add_patched(self):
        """patch Alerts 实例断言 add 调用：None→告警 / 非交易日→不调用 /
        阈值内→不调用 / 滞后超阈→精确参数 / 滞后 0→不调用。"""
        d5 = self._days_ago(5)
        d1 = self._days_ago(1)
        with mock.patch.object(app.Alerts, "add") as m_add:
            fa = app.Alerts.init(os.path.join(self.tmp, "mock_fresh.json"))
            app.data_freshness_alert(None, True, alerts=fa)
            m_add.assert_called_once_with("warning", "数据",
                                          "行情数据不可用（探针失败）")
            m_add.reset_mock()
            app.data_freshness_alert(d5, False, alerts=fa)   # 非交易日 → 不调用
            m_add.assert_not_called()
            app.data_freshness_alert(d1, True, alerts=fa)    # 阈值内 → 不调用
            m_add.assert_not_called()
            app.data_freshness_alert(d5, True, alerts=fa)    # 滞后超阈 → 调用
            m_add.assert_called_once_with(
                "warning", "数据", f"行情数据已滞后 5 天（最新 {d5}）")
            m_add.reset_mock()
            app.data_freshness_alert(datetime.date.today().isoformat(),
                                     True, alerts=fa)        # 滞后 0 → 不调用
            m_add.assert_not_called()


# =====================================================================
# 3) capture_mcp_call / list_mcp_calls / mcp_stats
# =====================================================================
class MCPCaptureTest(_OpsTestCase):
    """capture_mcp_call：jsonl 追加/2000 行截断、deque 500 上限、list 顺序、
    stats 计算、空窗口、重启惰性恢复、字段归一化、落盘失败降级。"""

    def _capture(self, tool, ok=True, elapsed_ms=10, **kw):
        rec = {"tool": tool, "ok": ok, "is_error": not ok,
               "elapsed_ms": elapsed_ms, "bytes": 100}
        rec.update(kw)
        app.capture_mcp_call(rec)

    def _file_lines(self):
        path = os.path.join(self.tmp, "mcp_calls.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [ln for ln in f if ln.strip()]

    def test_capture_appends_jsonl(self):
        """jsonl 逐行追加；字段归一化为 6 键；冗余键丢弃；ok 缺省 = not is_error；
        ts 缺省补当前时间。"""
        app.capture_mcp_call({"tool": "get_kline", "ok": True, "is_error": False,
                              "elapsed_ms": 120, "bytes": 500,
                              "extra": "冗余键丢弃"})
        app.capture_mcp_call({"tool": "screen_stocks", "is_error": True,
                              "elapsed_ms": 900, "bytes": 50})   # ok 缺省 → False
        app.capture_mcp_call({"tool": "no_ts"})                   # ts 缺省
        lines = self._file_lines()
        self.assertEqual(len(lines), 3)
        r0 = json.loads(lines[0])
        self.assertEqual(set(r0), {"ts", "tool", "ok", "is_error",
                                   "elapsed_ms", "bytes"})
        self.assertEqual(r0["tool"], "get_kline")
        self.assertEqual(r0["elapsed_ms"], 120)
        r1 = json.loads(lines[1])
        self.assertEqual((r1["ok"], r1["is_error"]), (False, True))
        r2 = json.loads(lines[2])
        self.assertTrue(r2["ts"])
        self.assertEqual(r2["tool"], "no_ts")

    def test_capture_truncate_2000_lines(self):
        """jsonl 超上限截断：保留尾部（上限临时调小为 5 验证）。"""
        with mock.patch.object(app, "MCP_CALLS_FILE_MAX_LINES", 5):
            for i in range(8):
                self._capture(f"t{i}")
        lines = self._file_lines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(json.loads(lines[-1])["tool"], "t7")   # 保留最新
        self.assertEqual(json.loads(lines[0])["tool"], "t3")    # 最旧淘汰

    def test_capture_deque_500_cap(self):
        """内存 deque 500 上限：超出后 list/stats 只看最新 500 条。"""
        for i in range(550):
            self._capture(f"t{i}", ok=True, elapsed_ms=i)
        self.assertEqual(len(app._mcp_deque), 500)
        lst = app.list_mcp_calls(1000)
        self.assertEqual(len(lst), 500)
        self.assertEqual(lst[0]["tool"], "t549")   # 最新在前
        self.assertEqual(lst[-1]["tool"], "t50")   # 最旧 = 第 50 条（前 50 被淘汰）
        self.assertEqual(app.mcp_stats()["total"], 500)

    def test_mcp_list_order(self):
        """app.list_mcp_calls 最新在前；limit 生效。"""
        self._capture("a", elapsed_ms=1)
        self._capture("b", elapsed_ms=2)
        self._capture("c", elapsed_ms=3)
        self.assertEqual([r["tool"] for r in app.list_mcp_calls(2)], ["c", "b"])
        self.assertEqual(app.list_mcp_calls(1)[0]["tool"], "c")
        self.assertEqual(len(app.list_mcp_calls(100)), 3)

    def test_mcp_stats_compute(self):
        """stats：ok_rate / avg_ms / p95_ms / by_tool 计算正确。"""
        for ms in (10, 30, 50, 70, 90):      # a：5 次，4 成功
            self._capture("a", ok=ms != 50, elapsed_ms=ms)
        for ms in (20, 40, 60, 80, 100):     # b：5 次，4 成功
            self._capture("b", ok=ms != 60, elapsed_ms=ms)
        st = app.mcp_stats()
        self.assertEqual(st["total"], 10)
        self.assertEqual(st["ok_rate"], 0.8)          # 8/10
        self.assertEqual(st["avg_ms"], 55.0)          # (10+..+90+20+..+100)/10
        self.assertEqual(st["p95_ms"], 100.0)         # ceil(0.95*10)-1 → 第 10 小
        self.assertEqual([b["tool"] for b in st["by_tool"]], ["a", "b"])
        self.assertEqual(st["by_tool"][0],
                         {"tool": "a", "n": 5, "ok": 4, "avg_ms": 50.0})
        self.assertEqual(st["by_tool"][1],
                         {"tool": "b", "n": 5, "ok": 4, "avg_ms": 60.0})

    def test_mcp_stats_empty(self):
        """空窗口 → total 0、ok_rate/avg/p95 均 None、by_tool []。"""
        self.assertEqual(app.mcp_stats(),
                         {"total": 0, "ok_rate": None, "avg_ms": None,
                          "p95_ms": None, "by_tool": []})

    def test_mcp_restart_lazy_restore(self):
        """进程重启（清 deque + 复位加载标记）→ 从 jsonl 惰性恢复最新记录。"""
        self._capture("a", elapsed_ms=1)
        self._capture("b", elapsed_ms=2)
        app._mcp_deque.clear()
        app._mcp_loaded = False
        app._mcp_file_lines = 0
        lst = app.list_mcp_calls(10)
        self.assertEqual([r["tool"] for r in lst], ["b", "a"])
        self.assertEqual(app.mcp_stats()["total"], 2)

    def test_mcp_write_failure_degrades_gracefully(self):
        """落盘失败（DATA_DIR 指向普通文件）→ 静默降级，内存统计照常。"""
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")
        with mock.patch.object(app, "DATA_DIR", Path(blocker)):
            self._capture("get_kline", elapsed_ms=7)
            self.assertEqual(len(app.list_mcp_calls(10)), 1)
            self.assertEqual(app.mcp_stats()["total"], 1)
        self.assertFalse(os.path.isfile(os.path.join(blocker, "mcp_calls.jsonl")))


# =====================================================================
# 4) app.fetch_upstream_release —— 上游最新版本探针
# =====================================================================
class _MockGitHubHandler(BaseHTTPRequestHandler):
    """本地 mock GitHub releases/latest：按响应队列逐次应答，记录请求路径。"""

    responses: list = []   # 队列 [(status, body_dict), ...]
    requests: list = []    # 记录请求路径

    def do_GET(self):
        type(self).requests.append(self.path)
        if type(self).responses:
            status, body = type(self).responses.pop(0)
        else:
            status, body = 500, {"message": "mock 未配置响应"}
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):  # 静默
        pass


class FetchReleaseTest(_OpsTestCase):
    """fetch_upstream_release：本地 http.server mock 200 解析 tag_name、非 200 /
    异常返回 None、TTL 缓存二次调用不再次请求（mock 计数）。"""

    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _MockGitHubHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}/releases/latest"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        super().setUp()
        _MockGitHubHandler.responses.clear()
        _MockGitHubHandler.requests.clear()
        self.url_patcher = mock.patch.object(app, "GITHUB_RELEASE_URL", self.base)
        self.url_patcher.start()
        self.addCleanup(self.url_patcher.stop)

    def test_release_200_parses_tag_name(self):
        """本地 mock 200：解析 tag_name / html_url / published_at。"""
        _MockGitHubHandler.responses.append((200, {
            "tag_name": "v1.2.3",
            "html_url": "https://github.com/hello245m/free-stockdb/releases/tag/v1.2.3",
            "published_at": "2026-08-01T00:00:00Z"}))
        r = app.fetch_upstream_release(force=True)
        self.assertEqual(r["tag_name"], "v1.2.3")
        self.assertEqual(r["html_url"],
                         "https://github.com/hello245m/free-stockdb/releases/tag/v1.2.3")
        self.assertEqual(r["published_at"], "2026-08-01T00:00:00Z")
        self.assertEqual(_MockGitHubHandler.requests, ["/releases/latest"])

    def test_release_non_200_returns_none(self):
        """非 200（本地 mock 500）→ None 不抛。"""
        _MockGitHubHandler.responses.append((500, {"message": "boom"}))
        self.assertIsNone(app.fetch_upstream_release(force=True))
        self.assertEqual(len(_MockGitHubHandler.requests), 1)

    def test_release_http_error_returns_none(self):
        """HTTPError（403，patch 层面）→ None 不抛。"""
        err = urllib.error.HTTPError(app.GITHUB_RELEASE_URL, 403, "Forbidden",
                                     {}, None)
        with mock.patch.object(urllib.request, "urlopen", side_effect=err):
            self.assertIsNone(app.fetch_upstream_release(force=True))

    def test_release_network_error_returns_none(self):
        """网络异常（urlopen 抛 OSError）→ None 不抛。"""
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=OSError("网络不可达")):
            self.assertIsNone(app.fetch_upstream_release(force=True))

    def test_release_ttl_cache_no_second_request(self):
        """TTL 缓存：二次调用不再发请求（mock 计数 == 1）；force 绕过缓存。"""
        fake = mock.MagicMock()
        fake.__enter__.return_value = fake      # 模拟 `with urlopen(...) as resp`
        fake.__exit__.return_value = False
        fake.read.return_value = b'{"tag_name":"v9.9.9"}'
        with mock.patch.object(urllib.request, "urlopen",
                               return_value=fake) as m:
            r1 = app.fetch_upstream_release(force=True)
            r2 = app.fetch_upstream_release()      # TTL 命中：不再请求
            self.assertEqual(r1, r2)
            self.assertEqual(m.call_count, 1)
            app.fetch_upstream_release(force=True)  # force 绕过缓存
            self.assertEqual(m.call_count, 2)

    def test_release_failure_cached_then_force(self):
        """失败也缓存（不反复打上游）；force 可绕过重新探测。"""
        with mock.patch.object(urllib.request, "urlopen",
                               side_effect=OSError("网络不可达")) as m:
            self.assertIsNone(app.fetch_upstream_release(force=True))
            self.assertIsNone(app.fetch_upstream_release())   # 失败缓存命中
            self.assertEqual(m.call_count, 1)
            self.assertIsNone(app.fetch_upstream_release(force=True))
            self.assertEqual(m.call_count, 2)


# =====================================================================
# 5) paper_audit_report —— 模拟盘只读审计
# =====================================================================
class AuditReportTest(_OpsTestCase):
    """paper_audit_report：最小库全字段（重放/重复/非法转移/滑点/净值/基准）、
    空库全 0、零表库、缺失库不抛、mode=ro 只读校验。"""

    # 最小 schema：与 paper_db 9 表列对齐（本审计只用到 5 表）；
    # strategy_decisions / order_intents 刻意不带主键 —— 模拟历史/损坏库中
    # 可能出现的重复 decision_id / intent_key，以验证 duplicate_* 守卫
    # （正式 paper_db 这两列均为主键，正常重复数恒为 0）。
    SCHEMA = """
    CREATE TABLE strategy_decisions (
        decision_id TEXT, strategy_id TEXT, strategy_version TEXT,
        trade_date TEXT, symbol TEXT, previous_rank REAL, current_rank REAL,
        ma5 REAL, ma10 REAL, ma20 REAL, previous_target REAL,
        desired_target REAL, reason_code TEXT, signal_known_at TEXT,
        status TEXT, created_at TEXT);
    CREATE TABLE order_intents (
        intent_key TEXT, decision_id TEXT, trade_date TEXT, symbol TEXT,
        desired_target REAL, action TEXT, target_qty INTEGER, delta_qty INTEGER,
        price_type TEXT, status TEXT, created_at TEXT, updated_at TEXT);
    CREATE TABLE broker_orders (
        order_id TEXT PRIMARY KEY, intent_key TEXT, trade_date TEXT, symbol TEXT,
        action TEXT, quantity INTEGER, price_type TEXT, price REAL, status TEXT,
        submitted_at TEXT, raw_response TEXT);
    CREATE TABLE fills (
        fill_id TEXT PRIMARY KEY, order_id TEXT, trade_date TEXT, symbol TEXT,
        fill_qty INTEGER, fill_price REAL, fee REAL, fill_time TEXT, raw TEXT);
    CREATE TABLE portfolio_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, trade_date TEXT, nav REAL,
        position_qty INTEGER, position_mv REAL, available_cash REAL,
        available_to_sell_qty INTEGER, created_at TEXT);
    """

    @classmethod
    def setUpClass(cls):
        cls.audit_dir = tempfile.mkdtemp(prefix="test_ops_audit_")
        cls.db_path = os.path.join(cls.audit_dir, "paper.sqlite3")
        cls._build_full_db(cls.db_path)
        cls.empty_path = os.path.join(cls.audit_dir, "empty.sqlite3")
        conn = sqlite3.connect(cls.empty_path)
        conn.executescript(cls.SCHEMA)
        conn.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.audit_dir, ignore_errors=True)

    @classmethod
    def _build_full_db(cls, path):
        """最小库：2 决策（1 正常 + 1 篡改 desired）+ 5 意图（1 重复键 / 1 非法
        转移 / 1 无关联决策）+ 3 委托（2 限价 + 1 市价）+ 3 成交 + 2 快照。"""
        conn = sqlite3.connect(path)
        conn.executescript(cls.SCHEMA)
        d1, d2 = paper_core.decision_id("20260803"), paper_core.decision_id("20260802")
        k1 = paper_core.order_intent_key("20260803", 0.5)
        k2 = paper_core.order_intent_key("20260802", 0.5)
        k4 = paper_core.order_intent_key("20260803", 1.0)
        k5 = paper_core.order_intent_key("20260804", 0.5)
        base_cols = ("decision_id, strategy_id, strategy_version, trade_date, symbol, "
                     "previous_rank, current_rank, ma5, ma10, ma20, previous_target, "
                     "desired_target, reason_code, status, created_at")
        # 决策1（正常）：0.40→0.55 上穿 0.50，均线多头 → 重放 0.5 == 存储 0.5
        conn.execute(f"INSERT INTO strategy_decisions ({base_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d1, STRATEGY_ID, "1.0.0", "20260803", "159915",
                      0.40, 0.55, 3.0, 2.0, 2.5, 0.0, 0.5, "P50_UPCROSS_PROBE",
                      "DECIDED", "2026-08-03 09:27:00"))
        # 决策2（被篡改）：previous_target=1.0，存储 desired=0.5，重放 1.0 ≠ 0.5
        conn.execute(f"INSERT INTO strategy_decisions ({base_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     (d2, STRATEGY_ID, "1.0.0", "20260802", "159915",
                      0.80, 0.80, 2.0, 1.9, 2.0, 1.0, 0.5, "HOLD",
                      "DECIDED", "2026-08-02 09:27:00"))
        int_cols = ("intent_key, decision_id, trade_date, symbol, desired_target, "
                    "action, target_qty, delta_qty, price_type, status, created_at, updated_at")
        # 意图1/2：同一 intent_key（重复组）+ 合法转移 (0.0→0.5)
        conn.execute(f"INSERT INTO order_intents ({int_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (k1, d1, "20260803", "159915", 0.5, "BUY_HALF", 16600, 16600,
                      "limit", "FILLED", "2026-08-03 14:50:00", "2026-08-03 15:05:00"))
        conn.execute(f"INSERT INTO order_intents ({int_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (k1, d1, "20260803", "159915", 0.5, "BUY_HALF", 16600, 16600,
                      "limit", "SUBMITTED", "2026-08-03 14:50:01", "2026-08-03 14:50:01"))
        # 意图3：非法转移 (1.0→0.5)（对账重放同一行也判定不一致）
        conn.execute(f"INSERT INTO order_intents ({int_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (k2, d2, "20260802", "159915", 0.5, "SELL_ALL", 0, -50000,
                      "limit", "UNFILLED", "2026-08-02 14:50:00", "2026-08-02 14:57:00"))
        # 意图4：合法转移 (0.0→1.0)，市价，部分成交
        conn.execute(f"INSERT INTO order_intents ({int_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (k4, d1, "20260803", "159915", 1.0, "BUY_FULL", 60000, 60000,
                      "market", "PARTIALLY_FILLED", "2026-08-03 14:51:00", "2026-08-03 15:05:00"))
        # 意图5：无关联决策（previous_target 缺失 → 转移判定跳过）
        conn.execute(f"INSERT INTO order_intents ({int_cols}) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (k5, "no-such-decision", "20260804", "159915", 0.5, "BUY_HALF",
                      16600, 16600, "limit", "UNFILLED",
                      "2026-08-04 14:50:00", "2026-08-04 15:05:00"))
        # 委托：B1/B2 限价（滑点样本）、B3 市价（应被排除）
        conn.execute("INSERT INTO broker_orders (order_id, intent_key, trade_date, symbol, "
                     "action, quantity, price_type, price, status, submitted_at, raw_response) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("B1", k1, "20260803", "159915", "buy", 16600, "limit", 2.00,
                      "FILLED", "2026-08-03 14:50:10", None))
        conn.execute("INSERT INTO broker_orders (order_id, intent_key, trade_date, symbol, "
                     "action, quantity, price_type, price, status, submitted_at, raw_response) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("B2", k2, "20260802", "159915", "sell", 50000, "limit", 2.50,
                      "FILLED", "2026-08-02 14:50:10", None))
        conn.execute("INSERT INTO broker_orders (order_id, intent_key, trade_date, symbol, "
                     "action, quantity, price_type, price, status, submitted_at, raw_response) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     ("B3", k4, "20260803", "159915", "buy", 60000, "market", None,
                      "PARTIALLY_FILLED", "2026-08-03 14:51:10", None))
        # 成交：F1 滑点 0.01、F2 滑点 0.02、F3 市价（排除）
        conn.execute("INSERT INTO fills (fill_id, order_id, trade_date, symbol, fill_qty, "
                     "fill_price, fee, fill_time, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("F1", "B1", "20260803", "159915", 16600, 2.02, 0.0,
                      "2026-08-03 14:53:00", None))
        conn.execute("INSERT INTO fills (fill_id, order_id, trade_date, symbol, fill_qty, "
                     "fill_price, fee, fill_time, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("F2", "B2", "20260802", "159915", 30000, 2.45, 0.0,
                      "2026-08-02 14:53:00", None))
        conn.execute("INSERT INTO fills (fill_id, order_id, trade_date, symbol, fill_qty, "
                     "fill_price, fee, fill_time, raw) VALUES (?,?,?,?,?,?,?,?,?)",
                     ("F3", "B3", "20260803", "159915", 10000, 1.0, 0.0,
                      "2026-08-03 14:54:00", None))
        # 快照：净值曲线（升序）
        conn.execute("INSERT INTO portfolio_snapshots (trade_date, nav, position_qty) "
                     "VALUES (?,?,?)", ("20260802", 100000.0, 0))
        conn.execute("INSERT INTO portfolio_snapshots (trade_date, nav, position_qty) "
                     "VALUES (?,?,?)", ("20260803", 101000.0, 16600))
        conn.commit()
        conn.close()

    @staticmethod
    def _bench(dates):
        """基准注入：08-02 基点为 100，08-03 为 110（→ 归一化 1.0 / 1.1）。"""
        return {d: (100.0 if d == "20260802" else 110.0) for d in dates}

    def _report(self, **kw):
        return app.paper_audit_report(self.db_path, **kw)

    def test_audit_replay_mismatch(self):
        """1 条正常 + 1 条被篡改 desired → replay_mismatches==1（含示例明细）。"""
        rep = self._report()
        self.assertEqual(rep["total_decisions"], 2)
        self.assertEqual(rep["replay_mismatches"]["count"], 1)
        ex = rep["replay_mismatches"]["examples"][0]
        self.assertEqual(ex["decision_id"], paper_core.decision_id("20260802"))
        self.assertEqual(ex["desired_target"], 0.5)
        self.assertEqual(ex["replayed_desired"], 1.0)
        self.assertIn("不一致", ex["reason"])

    def test_audit_duplicate_decisions_and_intents(self):
        """重复 intent_key → duplicate_intents==1；decision_id 正常 → 0。"""
        rep = self._report()
        self.assertEqual(rep["duplicate_intents"], 1)
        self.assertEqual(rep["duplicate_decisions"], 0)

    def test_audit_illegal_transitions(self):
        """非法转移（previous 1.0 → desired 0.5）→ illegal_transitions==1。"""
        rep = self._report()
        self.assertEqual(rep["illegal_transitions"]["count"], 1)
        ex = rep["illegal_transitions"]["examples"][0]
        self.assertEqual((ex["previous"], ex["desired"]), (1.0, 0.5))
        self.assertEqual(ex["trade_date"], "20260802")

    def test_audit_slippage(self):
        """fills+broker_orders：限价 2 笔滑点 (0.01, 0.02)，市价排除 → avg 0.015。"""
        rep = self._report()
        self.assertEqual(rep["slippage"], {"n": 2, "avg_slippage": 0.015})

    def test_audit_order_status_counts(self):
        """生命周期状态计数 / 未成交 / 部分成交。"""
        rep = self._report()
        self.assertEqual(rep["order_status_counts"],
                         {"FILLED": 1, "SUBMITTED": 1, "UNFILLED": 2,
                          "PARTIALLY_FILLED": 1})
        self.assertEqual(rep["unfilled_count"], 2)
        self.assertEqual(rep["partial_count"], 1)

    def test_audit_nav_series(self):
        """快照 → nav_series 升序。"""
        rep = self._report()
        self.assertEqual(rep["nav_series"],
                         [{"date": "20260802", "nav": 100000.0},
                          {"date": "20260803", "nav": 101000.0}])

    def test_audit_benchmark_series_normalized(self):
        """fetch_benchmark 注入 → benchmark_series 归一化（同基点 1.0）。"""
        rep = self._report(fetch_benchmark=self._bench)
        self.assertEqual(rep["benchmark_series"],
                         [{"date": "20260802", "value": 1.0},
                          {"date": "20260803", "value": 1.1}])

    def test_audit_benchmark_dash_keys(self):
        """基准注入返回 YYYY-MM-DD 键 → 归一化同样正确（键兼容）。"""
        rep = self._report(fetch_benchmark=lambda dates: {
            "2026-08-02": 100.0, "2026-08-03": 110.0})
        self.assertEqual(rep["benchmark_series"],
                         [{"date": "20260802", "value": 1.0},
                          {"date": "20260803", "value": 1.1}])

    def test_audit_benchmark_none_exception_non_dict(self):
        """fetch_benchmark 缺省 / 抛异常 / 返回非 dict → benchmark_series []。"""
        self.assertEqual(self._report()["benchmark_series"], [])
        def boom(dates):
            raise RuntimeError("基准不可用")
        self.assertEqual(self._report(fetch_benchmark=boom)["benchmark_series"], [])
        self.assertEqual(self._report(fetch_benchmark=lambda d: "不是 dict")
                         ["benchmark_series"], [])
        # nav_series 不受基准影响
        self.assertEqual(len(self._report()["nav_series"]), 2)

    def test_audit_empty_db_all_zero(self):
        """空库（有 schema 无数据）→ 全 0 / [] / None。"""
        rep = app.paper_audit_report(self.empty_path)
        self.assertEqual(rep["total_decisions"], 0)
        self.assertEqual(rep["replay_mismatches"], {"count": 0, "examples": []})
        self.assertEqual(rep["duplicate_decisions"], 0)
        self.assertEqual(rep["duplicate_intents"], 0)
        self.assertEqual(rep["illegal_transitions"], {"count": 0, "examples": []})
        self.assertEqual(rep["order_status_counts"], {})
        self.assertEqual(rep["slippage"], {"n": 0, "avg_slippage": None})
        self.assertEqual(rep["unfilled_count"], 0)
        self.assertEqual(rep["partial_count"], 0)
        self.assertEqual(rep["nav_series"], [])
        self.assertEqual(rep["benchmark_series"], [])
        self.assertTrue(rep["generated_at"])

    def test_audit_no_tables_db_all_zero(self):
        """零表 sqlite 文件 → 全 0 / [] 不抛（逐查询守卫）。"""
        path = os.path.join(self.audit_dir, "notables.sqlite3")
        sqlite3.connect(path).close()
        rep = app.paper_audit_report(path)
        self.assertEqual(rep["total_decisions"], 0)
        self.assertEqual(rep["order_status_counts"], {})
        self.assertEqual(rep["nav_series"], [])
        self.assertEqual(rep["benchmark_series"], [])
        self.assertEqual(rep["replay_mismatches"], {"count": 0, "examples": []})

    def test_audit_missing_db_no_throw(self):
        """路径不存在 → 空报告不抛（连接级失败容错）。"""
        missing = app.paper_audit_report(
            os.path.join(self.audit_dir, "nope.sqlite3"))
        self.assertEqual(missing["total_decisions"], 0)
        self.assertEqual(missing["replay_mismatches"], {"count": 0, "examples": []})
        self.assertEqual(missing["nav_series"], [])
        self.assertTrue(missing["generated_at"])

    def test_audit_ro_connect_readonly(self):
        """mode=ro 连接拒绝写库（只读审计不写库）。"""
        ro = app._ro_connect(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                ro.execute("CREATE TABLE _hack (x)")
        finally:
            ro.close()


# =====================================================================
# 6) signal_status —— 信号文件体检
# =====================================================================
class SignalStatusTest(_OpsTestCase):
    """signal_status：好文件 7 项 checks 全 ok / 缺文件 exists False /
    history_count!=60 等各类失败 checks / previous_rank 缺省合法 / 契约注入 /
    根节点非对象 / 非法 trade_date。"""

    def setUp(self):
        super().setUp()
        self.sig_dir = os.path.join(self.tmp, "signals")
        os.makedirs(self.sig_dir, exist_ok=True)

    def _write(self, trade_date, data):
        with open(os.path.join(self.sig_dir, f"{trade_date}.json"), "w",
                  encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)

    def _good(self, **over):
        d = {"current_rank": 0.55, "previous_rank": 0.40, "metric_value": 66.4,
             "history_count": 60, "formal_usable": True,
             "source_contract_version": "emotion-v1",
             "known_at": "2026-08-04 09:26:05"}
        d.update(over)
        return d

    CHECKS = {"current_rank_present", "metric_value_present", "history_count_ok",
              "formal_usable_ok", "contract_supported", "known_at_ok",
              "previous_rank_ok"}

    def test_signal_good_file_all_ok(self):
        """好文件：exists/parsed True、error None、7 项 checks 全 ok、fields 完整。"""
        self._write("20260804", self._good())
        sg = app.signal_status(self.sig_dir, "20260804")
        self.assertTrue(sg["exists"])
        self.assertTrue(sg["parsed"])
        self.assertIsNone(sg["error"])
        self.assertEqual(set(sg["checks"]), self.CHECKS)   # 7 项校验点一个不落
        self.assertTrue(all(c["ok"] for c in sg["checks"].values()), sg["checks"])
        self.assertEqual(sg["fields"]["current_rank"], 0.55)
        self.assertEqual(sg["fields"]["source_contract_version"], "emotion-v1")
        self.assertEqual(sg["path"], os.path.join(self.sig_dir, "20260804.json"))

    def test_signal_missing_file(self):
        """缺文件：exists False、parsed False、error 含「不存在」、fields/checks 空。"""
        sm = app.signal_status(self.sig_dir, "20260899")
        self.assertFalse(sm["exists"])
        self.assertFalse(sm["parsed"])
        self.assertIn("不存在", sm["error"])
        self.assertEqual(sm["fields"], {})
        self.assertEqual(sm["checks"], {})

    def test_signal_history_count_fail(self):
        """history_count != 60 → history_count_ok False；其余 6 项不受影响。"""
        self._write("20260804", self._good(history_count=59))
        sg = app.signal_status(self.sig_dir, "20260804")
        self.assertFalse(sg["checks"]["history_count_ok"]["ok"])
        self.assertIn("59", sg["checks"]["history_count_ok"]["reason"])
        for k in self.CHECKS - {"history_count_ok"}:
            self.assertTrue(sg["checks"][k]["ok"], k)

    def test_signal_failure_checks(self):
        """各类失败 checks：rank 非法 / metric 缺失 / formal_usable 非 true /
        契约不受支持 / known_at 早于 09:25 / rank 越界 / previous_rank 非法。"""
        # current_rank 非数字 + metric_value 缺失 + formal_usable False + 契约 v9
        self._write("20260805", self._good(current_rank="非数字",
                                           metric_value=None,
                                           formal_usable=False,
                                           source_contract_version="emotion-v9"))
        sb = app.signal_status(self.sig_dir, "20260805")
        self.assertFalse(sb["checks"]["current_rank_present"]["ok"])
        self.assertIn("缺失或非法", sb["checks"]["current_rank_present"]["reason"])
        self.assertFalse(sb["checks"]["metric_value_present"]["ok"])
        self.assertIn("缺失或非法", sb["checks"]["metric_value_present"]["reason"])
        self.assertFalse(sb["checks"]["formal_usable_ok"]["ok"])
        self.assertIn("必须为 true", sb["checks"]["formal_usable_ok"]["reason"])
        self.assertFalse(sb["checks"]["contract_supported"]["ok"])
        self.assertIn("emotion-v9", sb["checks"]["contract_supported"]["reason"])
        # known_at 早于当日 09:25 → known_at_ok False
        self._write("20260806", self._good(known_at="2026-08-06 09:24:59"))
        s6 = app.signal_status(self.sig_dir, "20260806")
        self.assertFalse(s6["checks"]["known_at_ok"]["ok"])
        self.assertIn("早于", s6["checks"]["known_at_ok"]["reason"])
        # current_rank / previous_rank 越界 0~1 → 各自 False
        self._write("20260807", self._good(current_rank=1.5, previous_rank=-0.1))
        s7 = app.signal_status(self.sig_dir, "20260807")
        self.assertIn("超出 0~1 范围",
                      s7["checks"]["current_rank_present"]["reason"])
        self.assertIn("超出 0~1 范围", s7["checks"]["previous_rank_ok"]["reason"])
        # metric_value 非数字 / previous_rank 非数字 → 各自 False
        self._write("20260808", self._good(metric_value="abc",
                                           previous_rank="非数字"))
        s8 = app.signal_status(self.sig_dir, "20260808")
        self.assertFalse(s8["checks"]["metric_value_present"]["ok"])
        self.assertFalse(s8["checks"]["previous_rank_ok"]["ok"])
        self.assertIn("可转 float", s8["checks"]["previous_rank_ok"]["reason"])

    def test_signal_previous_rank_missing_ok(self):
        """previous_rank 缺省 → 合法（引擎从库派生，不判非法）。"""
        self._write("20260804", self._good(previous_rank=None))
        sg = app.signal_status(self.sig_dir, "20260804")
        self.assertTrue(sg["checks"]["previous_rank_ok"]["ok"])
        self.assertIn("派生", sg["checks"]["previous_rank_ok"]["reason"])

    def test_signal_contracts_injection(self):
        """supported_contracts 注入：v1 文件对 v2-only 校验集不受支持。"""
        self._write("20260804", self._good())
        sc = app.signal_status(self.sig_dir, "20260804",
                               supported_contracts=["emotion-v2"])
        self.assertFalse(sc["checks"]["contract_supported"]["ok"])
        sc2 = app.signal_status(self.sig_dir, "20260804",
                                supported_contracts=["emotion-v1", "emotion-v2"])
        self.assertTrue(sc2["checks"]["contract_supported"]["ok"])

    def test_signal_root_not_object(self):
        """根节点非对象：exists True、parsed False、error 注明「对象」。"""
        self._write("20260804", [1, 2, 3])
        sg = app.signal_status(self.sig_dir, "20260804")
        self.assertTrue(sg["exists"])
        self.assertFalse(sg["parsed"])
        self.assertIn("对象", sg["error"])
        self.assertEqual(sg["fields"], {})

    def test_signal_invalid_trade_date(self):
        """非法 trade_date（非 8 位数字）→ 中文 ValueError。"""
        with self.assertRaises(ValueError):
            app.signal_status(self.sig_dir, "2026-8-4")
        with self.assertRaises(ValueError):
            app.signal_status(self.sig_dir, "2026080X")


if __name__ == "__main__":
    unittest.main(verbosity=2)