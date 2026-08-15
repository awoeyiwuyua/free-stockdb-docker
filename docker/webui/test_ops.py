#!/usr/bin/env python3
"""test_ops — 运营支撑（Phase 4.5 任务D）全离线单元测试

覆盖四块（全部离线：无网络 / 无真实 DATA_DIR 残留；上游版本探针走本地
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

接线说明（Phase 4.5 返工 2 / 0.8.0 收敛）：
  被测运营支撑实现（Alerts / data_freshness_alert / capture_mcp_call /
  fetch_upstream_release）已整体迁移至 app.py 生产代码（模块级），本文件不再
  内嵌参考副本；测试 import app 并引用 app.X 生产实现，setUp 把 app.DATA_DIR
  打补丁到临时目录并复位 app 模块全局态（告警单例 / MCP deque / 版本探针缓存），
  保证离线、无残留、互不影响。
  0.8.0 起模拟盘整体下线：paper_audit_report / signal_status 相关测试随
  test_paper.py 一并移除，49 项测试语义与行为基线不变。

运行（自测命令，须贴 Ran/OK 与最后几行）：
    cd docker/webui && /Users/xiahaihe/Claudecode/free-stockdb-docker/.venv/bin/python -m unittest test_ops -v
回归（mcp 不受影响）：
    cd docker/webui && /Users/xiahaihe/Claudecode/free-stockdb-docker/.venv/bin/python -m unittest mcp.test_stockdb_mcp_server -v
"""

from __future__ import annotations

# =====================================================================
# 生产实现接线（Phase 4.5 返工 2 / 0.8.0 收敛）：被测运营支撑实现（Alerts /
# data_freshness_alert / capture_mcp_call / fetch_upstream_release）已整体迁移至
# app.py 生产代码（模块级），本文件不再内嵌参考副本；测试 import app 并引用
# app.X，setUp 把 app.DATA_DIR 打补丁到临时目录并复位 app 模块全局态，
# 保证离线、无残留、互不影响。
# 0.8.0 起模拟盘整体下线：49 项测试语义与行为基线不变（全部打到 app.py 真实函数）。
# =====================================================================
import datetime
import io
import json
import time
import os
import shutil
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
        app._mydb_rd._rd = None  # 0.8.10：rd 连接缓存复位（防用例间串扰）
        shutil.rmtree(self.tmp, ignore_errors=True)


# =====================================================================
# 0) mydb 读写链路（真实函数 + 假 rd）：归一化 / 串行化 / 失败自愈
# =====================================================================
class _FakeRd:
    """最小 rd 替身：get/keys/set/do 计数 + 内存数据，可注入失败。"""

    def __init__(self):
        self.data = {}      # {(table, key): value}
        self.calls = []     # [("get"|"keys"|"set", table, key_or_pattern)]

    def get(self, table, key):
        self.calls.append(("get", table, key))
        v = self.data.get((table, key))
        return v() if callable(v) else v

    def keys(self, table, pattern="*"):
        self.calls.append(("keys", table, pattern))
        if table == "*":
            return [f"{t}:{k}" for (t, k) in self.data]
        return [f"{t}:{k}" for (t, k) in self.data if t == table]

    def set(self, table, key, value):
        self.calls.append(("set", table, key))
        self.data[(table, key)] = value
        return self  # 链式 .do()

    def do(self):
        return self


class _QueryResultLike:
    """pybao QueryResult 形态替身：带 keys/all 属性，dict(v) 可转换。"""

    def __init__(self, d):
        self._d = d

    def keys(self):
        return self._d.keys()

    def all(self):
        return None

    def __getitem__(self, k):
        return self._d[k]

    def __iter__(self):
        return iter(self._d.items())


class _LimitReferenceTests(_OpsTestCase):
    """0.8.14：pre_close 被未来除权因子回溯污染 → 重建法定涨跌停参考价。"""

    def test_rebuild_pure(self):
        """反推公式：ref = pre_close × cum_latest / cum_D（000100 实测数字）。"""
        from mcp.board_metrics import rebuild_limit_reference_price as r
        # 000100：pre_close=4.207（污染后），cum_D=3.019，cum_latest=3.079
        # → 4.207×3.079/3.019 = 4.289 ≈ 真实昨收 4.29
        self.assertAlmostEqual(r(4.207, 3.019, 3.079), 4.207 * 3.079 / 3.019, places=3)
        # 无因子事件/等因子：原样返回（未污染）
        self.assertEqual(r(10.0, 1.0, 1.0), 10.0)
        self.assertEqual(r(10.0, 3.019, 3.019), 10.0)
        # 非法输入防御
        self.assertEqual(r(10.0, None, 3.0), 10.0)
        self.assertEqual(r(10.0, 0, 3.0), 10.0)

    def test_get_fq_cum(self):
        """因子表查询：cum_at_date（二分）+ cum_latest；无事件/未知代码 → None。"""
        import mcp.pybao_tools as pt
        fake = mock.Mock()
        fake._fq_dates = {"000100": ["20040614", "20260611"], "600000": []}
        fake._fq_cums = {"000100": [1.012, 3.079], "600000": []}
        with mock.patch.object(pt, "get_sdk_client", return_value=fake):
            self.assertEqual(pt.get_fq_cum("000100", "20260507"), (1.012, 3.079))
            self.assertEqual(pt.get_fq_cum("000100", "20260611"), (3.079, 3.079))
            self.assertEqual(pt.get_fq_cum("000100", "20040101"), (1.0, 3.079))  # 早于首事件 → 1.0
            self.assertIsNone(pt.get_fq_cum("600000", "20260507"))  # 无因子事件
            self.assertIsNone(pt.get_fq_cum("999999", "20260507"))
        with mock.patch.object(pt, "get_sdk_client", return_value=None):
            self.assertIsNone(pt.get_fq_cum("000100", "20260507"))  # SDK 不可用

    def test_fix_limit_reference_applied(self):
        """重建后涨停判定修正：污染 pre_close 漏判 → 重建后命中。"""
        pts = [{"code": "600000", "name": "X", "open": 4.6, "close": 4.72,
                "prev_close": 4.207, "high": 4.72, "low": 4.6,
                "is_st": False, "status": "TRADED"}]
        with mock.patch("mcp.pybao_tools.get_fq_cum", return_value=(3.019, 3.079)):
            fixed = app._auction_fix_limit_reference(pts, "20260507")
        self.assertAlmostEqual(fixed[0]["prev_close"], 4.207 * 3.079 / 3.019, places=3)
        from auction_list import compute_limitup_list
        # 重建后：真实昨收 4.289 → 涨停价 round(4.289×1.1,2)=4.72 == close → 命中
        self.assertEqual(compute_limitup_list(fixed)["count"], 1)
        # 未重建：污染昨收 4.207 → 涨停价 4.63 ≠ close 4.72 → 漏判（污染现场）
        self.assertEqual(compute_limitup_list(pts)["count"], 0)

    def test_fix_limit_reference_fallback(self):
        """因子表不可用 → 原样返回（未除权股票无影响）。"""
        pts = [{"code": "600000", "prev_close": 10.0, "close": 11.0}]
        with mock.patch("mcp.pybao_tools.get_fq_cum", return_value=None):
            self.assertEqual(app._auction_fix_limit_reference(pts, "20260507"), pts)


class _MydbRdTests(_OpsTestCase):
    """mydb 读写：QueryResult/JSON 串归一化、并发串行化、失败丢弃连接自愈。"""

    def test_read_queryresult_normalized(self):
        """rd.get 返回 QueryResult 形态 → 读出原生 dict（不再序列化崩）。"""
        rd = _FakeRd()
        rd.data[("t", "k")] = _QueryResultLike({"metrics": {"a": 1}, "n": 2})
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        r = app.mydb_read("t", "k")
        self.assertEqual(r["value"], {"metrics": {"a": 1}, "n": 2})

    def test_read_json_string_parsed(self):
        """rd.get 返回 JSON 字符串 → 解析为 dict（0.8.6 历史形态兼容）。"""
        rd = _FakeRd()
        rd.data[("t", "k")] = json.dumps({"v": [1, 2]})
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        self.assertEqual(app.mydb_read("t", "k")["value"], {"v": [1, 2]})

    def test_read_nonjson_string_returns_none(self):
        """非 JSON 字符串 → value None（不抛序列化错误，不返回裸对象）。"""
        rd = _FakeRd()
        rd.data[("t", "k")] = "not-json"
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        self.assertIsNone(app.mydb_read("t", "k")["value"])

    def test_read_list_all_keys(self):
        """key 缺省：列出表内全部键值，值为归一化 dict。"""
        rd = _FakeRd()
        rd.data[("t", "20260814")] = {"a": 1}
        rd.data[("t", "20260815")] = _QueryResultLike({"b": 2})
        rd.data[("t2", "x")] = {"c": 3}
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        r = app.mydb_read("t", "")
        self.assertEqual(len(r["keys"]), 2)
        self.assertEqual(r["values"], {"t:20260814": {"a": 1}, "t:20260815": {"b": 2}})

    def test_write_readback_normalized(self):
        """写入原生 dict → 回读校验逐键一致（0.8.6 存值类型契约）。"""
        rd = _FakeRd()
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        r = app.mydb_write("t", [("k1", {"v": 1}), ("k2", {"v": 2})])
        self.assertEqual(r["written"], 2)
        self.assertEqual(r["readback"], [{"v": 1}, {"v": 2}])
        self.assertEqual(rd.data[("t", "k1")], {"v": 1})

    def test_concurrent_reads_serialized(self):
        """多线程并发读 → 锁保证同一时刻至多一条 rd 请求（单连接防交错）。"""
        active, max_active = [], [0]
        guard = threading.Lock()
        rd = _FakeRd()
        rd.data[("t", "k")] = {"v": 1}

        def slow_get(table, key):
            with guard:
                active.append(1)
                max_active[0] = max(max_active[0], len(active))
            time.sleep(0.02)
            with guard:
                active.pop()
            return rd.data.get((table, key))

        rd.get = slow_get
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        results = []

        def worker():
            results.append(app.mydb_read("t", "k")["value"])

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(max_active[0], 1)  # 全程串行
        self.assertEqual(results, [{"v": 1}] * 4)

    def test_failure_drops_connection_and_recovers(self):
        """rd 调用异常 → 丢弃缓存连接；下一次调用重新 init 后恢复正常。"""
        good = _FakeRd()
        good.data[("t", "k")] = {"a": 1}
        bad = _FakeRd()

        def boom(table, key):
            raise RuntimeError("socket wedged")

        bad.get = boom
        app._mydb_rd._rd = bad
        with self.assertRaises(RuntimeError):
            app.mydb_read("t", "k")
        self.assertIsNone(app._mydb_rd._rd)  # 缓存已丢弃（自愈前提）
        fake_mod = mock.Mock()
        fake_mod.init.return_value = good
        with mock.patch.object(app, "_mydb_import", return_value=fake_mod):
            r = app.mydb_read("t", "k")
        self.assertEqual(r["value"], {"a": 1})
        self.addCleanup(app._mydb_rd_reset)

    def test_hk_klines_serialized_and_normalized(self):
        """hk_klines：vals 读取持锁 + QueryResult 归一化（0.8.10 纳入锁面）。"""
        rd = _FakeRd()
        rd.data[("hk日k", "00700")] = _QueryResultLike({"date": "20260814", "close": 1.5})
        app._mydb_rd._rd = rd
        self.addCleanup(app._mydb_rd_reset)
        # _FakeRd 缺 vals：补一个返回列表的 vals
        rd.vals = lambda table, code, pattern: [rd.data.get((table, code))]
        rows = app.hk_klines("00700")
        self.assertEqual(rows, [{"date": "20260814", "close": 1.5}])


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
# Phase 5 M0：前端静态服务 / SPA 回退 / legacy 逃生通道 / overview 聚合
# 直连 app.Handler.do_GET（FakeConn 提供 rfile/wfile），断言真实路由行为。
# =====================================================================
class _FakeConn:
    """构造 BaseHTTPRequestHandler 所需的最小连接对象（rfile/wfile 均为内存）。"""

    def __init__(self):
        self.rfile = io.BytesIO()
        self.wfile = io.BytesIO()

    def makefile(self, mode, *args):
        return self.rfile if mode == "rb" else self.wfile

    def sendall(self, data):
        self.wfile.write(data)


def _do_get(path: str):
    """直连 do_GET，返回 (status, headers_dict, body_bytes)。"""
    conn = _FakeConn()
    handler = app.Handler(conn, ("127.0.0.1", 1), None)
    handler.command = "GET"
    handler.request_version = "HTTP/1.1"
    handler.protocol_version = "HTTP/1.1"
    handler.requestline = f"GET {path} HTTP/1.1"
    handler.headers = {}
    handler.path = path
    handler.do_GET()
    raw = conn.wfile.getvalue()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    headers = {}
    for line in head.split(b"\r\n")[1:]:
        k, _, v = line.partition(b": ")
        headers[k.decode().lower()] = v.decode()
    return status, headers, body


class _StaticServingTests(_OpsTestCase):
    """M0 静态服务：根路径 HTML、legacy 逃生通道、路径穿越防护、overview 聚合。"""

    def test_root_returns_html(self):
        status, headers, body = _do_get("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["content-type"])
        self.assertIn(b"<html", body)

    def test_legacy_escape_hatch(self):
        """旧面板完整保留：/legacy 返回原 PAGE（标题不变）。"""
        status, _, body = _do_get("/legacy")
        self.assertEqual(status, 200)
        self.assertIn("stockdb 控制台".encode(), body)

    def test_path_traversal_blocked(self):
        """路径穿越不吐真实文件：落入 SPA 回退（index.html），绝不泄露 /etc/passwd。"""
        status, _, body = _do_get("/../../etc/passwd")
        self.assertEqual(status, 200)
        self.assertNotIn(b"root:", body)
        self.assertIn(b"<html", body)

    def test_unknown_api_404_unchanged(self):
        status, _, _ = _do_get("/api/nonexistent")
        self.assertEqual(status, 404)

    def test_overview_aggregation(self):
        """/api/overview 四块聚合齐全，version 块带 ui_mode。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/overview")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        for key in ("health", "alerts", "mcp", "version"):
            self.assertIn(key, payload)
        self.assertIn("count", payload["alerts"])
        self.assertEqual(payload["version"]["ui_mode"], app.WEBUI_UI)

    def test_version_ui_mode(self):
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/version")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertIn("ui_mode", payload)
        self.assertIn(payload["ui_mode"], ("spa", "legacy"))


class _DiagTests(_OpsTestCase):
    """Phase 5.1 /api/diag：一键诊断聚合（五检查 + 环境块，单块降级不 500）。"""

    def test_diag_structure(self):
        """五项检查齐全 + env 关键字段 + all_ok 汇总。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        names = [c["name"] for c in payload["checks"]]
        self.assertEqual(names, ["upstream_github", "stockdb_service",
                                 "pybao", "disk", "calendar"])
        for c in payload["checks"]:
            self.assertIn("label", c)
            self.assertIn("ok", c)
            self.assertIn("note", c)
        self.assertIsInstance(payload["all_ok"], bool)
        for key in ("python", "arch", "webui_version", "ui_mode", "data_latest", "uptime_seconds"):
            self.assertIn(key, payload["env"])
        self.assertEqual(payload["env"]["webui_version"], app.WEBUI_VERSION)

    def test_diag_upstream_degraded(self):
        """上游不可达 → upstream_github ok=False 且整体仍 200（单块降级）。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        up = next(c for c in payload["checks"] if c["name"] == "upstream_github")
        self.assertFalse(up["ok"])
        self.assertIn("降级", up["note"])

    def test_diag_pybao_check(self):
        """pybao 检查 = 三个模块 find_spec 全命中（无 pybao 时为 False 也合法）。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        py = next(c for c in payload["checks"] if c["name"] == "pybao")
        self.assertIsInstance(py["ok"], bool)


class _DataLatestDateTests(_OpsTestCase):
    """Phase 5.1 稳定性：data_latest_date 失败缓存 + 并发单飞（防多标签切换打瘫后端）。"""

    def setUp(self):
        super().setUp()
        app._latest_date_cache.update(at=0.0, val=None)
        app._stockdb_breaker.update(fails=0, open_until=0.0)  # 熔断器全局态复位，防用例串扰

    def test_failure_result_is_cached(self):
        """探测失败（None）也缓存：8s 内不重复打 stockdb（此前失败不缓存会风暴重探）。"""
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            first = app.data_latest_date()
            second = app.data_latest_date()
        self.assertIsNone(first)
        self.assertIsNone(second)
        # 两次调用只打了一轮探测（缓存命中，未再 urlopen）
        with mock.patch("urllib.request.urlopen") as m:
            app.data_latest_date()
            app.data_latest_date()
        self.assertEqual(m.call_count, 0)

    def test_single_flight_concurrent_probe(self):
        """并发探测单飞：一路在跑时其余调用立即返回缓存，urlopen 只被调一轮。"""
        def _slow_urlopen(*a, **k):
            time.sleep(0.4)
            resp = mock.MagicMock()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            resp.read.return_value = json.dumps([{"date": "20260814"}]).encode()
            return resp

        with mock.patch("urllib.request.urlopen", side_effect=_slow_urlopen) as m:
            t = threading.Thread(target=app.data_latest_date, kwargs={"force": True})
            t.start()
            time.sleep(0.05)  # 确保线程已持有探测锁
            second = app.data_latest_date(force=True)  # 锁被占 → 立即返回缓存（None）
            t.join()
            count_after_first_round = m.call_count
            third = app.data_latest_date()  # 探测完成且写入缓存 → 命中，不再 urlopen
            count_after_third = m.call_count
        self.assertIsNone(second)  # 缓存尚未写入，合法（下一轮轮询拿到新值）
        self.assertGreaterEqual(count_after_first_round, 3)  # 一轮探测 = 3~4 个月前缀
        self.assertEqual(count_after_third, count_after_first_round)  # 缓存命中零新探测
        self.assertEqual(third, "20260814")

    def test_success_cache_hit(self):
        """成功后 8s 内命中缓存，不再 urlopen。"""
        with mock.patch("urllib.request.urlopen") as m:
            resp = mock.MagicMock()
            resp.__enter__ = mock.MagicMock(return_value=resp)
            resp.__exit__ = mock.MagicMock(return_value=False)
            resp.read.return_value = json.dumps([{"date": "20260814"}]).encode()
            m.side_effect = lambda *a, **k: resp
            first = app.data_latest_date()
            second = app.data_latest_date()
        self.assertEqual(first, "20260814")
        self.assertEqual(second, "20260814")


class _StockdbGateTests(_OpsTestCase):
    """Phase 5.1 并发卫生：熔断器 + 信号量（stockdb 上游访问闸口）。"""

    def setUp(self):
        super().setUp()
        app._stockdb_breaker.update(fails=0, open_until=0.0)
        app._latest_date_cache.update(at=0.0, val=None)

    def test_breaker_opens_after_failures(self):
        """连续 threshold 次失败 → 熔断打开：后续探针快速降级、零 urlopen。"""
        app._stockdb_breaker.update(threshold=2, cooldown=300.0)
        with mock.patch("urllib.request.urlopen", side_effect=OSError("down")):
            for _ in range(2):
                with self.assertRaises(OSError):
                    app.stockdb_fetch("/?cmd=get&t=x", timeout=1, breaker=True)
        self.assertTrue(app._stockdb_breaker_open())
        with mock.patch("urllib.request.urlopen") as m:
            app.data_latest_date()  # 熔断中 → 直接返回缓存 None，不打网络
        self.assertEqual(m.call_count, 0)

    def test_breaker_recovers_after_cooldown(self):
        """冷却期后熔断关闭：恢复探测。"""
        app._stockdb_breaker.update(fails=3, open_until=time.time() + 300.0)
        self.assertTrue(app._stockdb_breaker_open())
        with mock.patch.object(app.time, "time", return_value=time.time() + 301.0):
            self.assertFalse(app._stockdb_breaker_open())

    def test_breaker_success_resets(self):
        """探测成功 → 失败计数复位。"""
        app._stockdb_breaker.update(fails=2, open_until=0.0)
        resp = mock.MagicMock()
        resp.__enter__ = mock.MagicMock(return_value=resp)
        resp.__exit__ = mock.MagicMock(return_value=False)
        resp.read.return_value = b"[]"
        with mock.patch("urllib.request.urlopen", return_value=resp):
            app.stockdb_fetch("/?cmd=vals&t=x", timeout=1, breaker=True)
        self.assertEqual(app._stockdb_breaker["fails"], 0)
        self.assertFalse(app._stockdb_breaker_open())

    def test_semaphore_limits_concurrency(self):
        """信号量满 → 立即 RuntimeError（不阻塞等待堆积线程）。"""
        with mock.patch.object(app, "_stockdb_gate", threading.Semaphore(1)):
            gate = app._stockdb_gate
            gate.acquire()
            try:
                with self.assertRaises(RuntimeError):
                    app.stockdb_fetch("/?cmd=get&t=x", timeout=1)
            finally:
                gate.release()

    def test_diag_note_shows_gate_state(self):
        """诊断的 stockdb_service 项注明闸口状态。"""
        with mock.patch.object(app, "fetch_upstream_release", return_value=None):
            status, _, body = _do_get("/api/diag")
        self.assertEqual(status, 200)
        note = next(c["note"] for c in json.loads(body.decode())["checks"]
                    if c["name"] == "stockdb_service")
        self.assertIn("上游闸口", note)


class _AuctionBackfillTests(_OpsTestCase):
    """0.8.1 历史序列回填：冷启动修复（首跑前序列为空 → 分位无分母）。

    夹具设计：每个交易日 D 的点集 = 当日非一字涨停股（全字段，供 D+1 清单）
    + 昨日清单股的溢价点（open/prev_close，供 D 日溢价）。三对股票 X/Y/Z 串起三天。
    """

    POINTS = {
        # 0813：Z 当日涨停（供 0814 清单）+ Y 溢价点（open 9.5；prev_close=999 毒值——
        # 0.8.13 起分母改用 T-1 收盘 11.0，毒值用于证明未回退到 pre_close 字段）
        "20260813": [
            {"code": "600004", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
            {"code": "600003", "open": 9.5, "prev_close": 999.0},
        ],
        # 0812：Y 当日涨停（供 0813 清单）+ X 溢价点（open 12.1 → 12.1/11.0-1=+10%）
        "20260812": [
            {"code": "600003", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
            {"code": "600002", "open": 12.1, "prev_close": 999.0},
        ],
        # 0811：X 当日涨停（供 0812 清单）
        "20260811": [
            {"code": "600002", "open": 10.5, "close": 11.0, "prev_close": 10.0, "is_st": False, "status": "TRADED"},
        ],
        # 0814：Z 溢价点（open 11.0 → 11.0/11.0-1=0%）
        "20260814": [
            {"code": "600004", "open": 11.0, "prev_close": 999.0},
        ],
    }

    def setUp(self):
        super().setUp()
        self.store = {}
        app._auction_backfill_state.update(running=False, started=None,
                                           finished=None, result=None)

        def fake_snapshot(args):
            return {"points": self.POINTS.get(args.get("date"), [])}

        # 契约替身（0.8.6 教训）：真实 pybao 只存原生对象——JSON 字符串会被静默存空。
        # 替身若再收到字符串直接炸，逼测试暴露"存值类型"回归，而不是像真机那样悄悄变 {}。
        def fake_write(table, items):
            self.store.setdefault(table, {})
            for k, v in items:
                if isinstance(v, str):
                    raise AssertionError(
                        f"mydb 契约违反：值不能是 JSON 字符串（{table}/{k}），必须存原生对象")
                self.store[table][str(k)] = v

        def fake_read(table, key=""):
            rows = self.store.get(table, {})
            if key:
                return {"table": table, "key": key, "value": rows.get(key)}
            return {"table": table, "key": key,
                    "values": {str(k): v for k, v in rows.items()}}

        self._patch_snap = mock.patch.object(app, "_auction_query_snapshot", side_effect=fake_snapshot)
        self._patch_write = mock.patch.object(app, "mydb_write", side_effect=fake_write)
        self._patch_read = mock.patch.object(app, "mydb_read", side_effect=fake_read)
        self._patch_latest = mock.patch.object(app, "data_latest_date", return_value="20260814")
        self._patch_prev = mock.patch.object(app, "_auction_prev_trade_date", side_effect={
            "20260814": "20260813", "20260813": "20260812",
            "20260812": "20260811", "20260811": "20260810",
        }.get)
        for p in (self._patch_snap, self._patch_write, self._patch_read,
                  self._patch_latest, self._patch_prev):
            p.start()
        self.addCleanup(lambda: [p.stop() for p in (self._patch_snap, self._patch_write,
                                                    self._patch_read, self._patch_latest,
                                                    self._patch_prev)])

    def _load_series(self, metric):
        # round-trip：走真实读取链（app._auction_series_read → mydb_read 契约替身）
        return app._auction_load_series(app._auction_series_read, metric)

    def test_backfill_builds_series_and_daily_metrics(self):
        r = app.auction_run_backfill(days=3)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["backfilled_days"], 3)
        # 序列时间正序（0.8.13：分母 = T-1 收盘 11.0，毒值 999 未生效即证明）：
        # 0812: 12.1/11.0-1=+10% → 0813: 9.5/11.0-1=-13.64% → 0814: 11.0/11.0-1=0%
        vals = self._load_series("premium_mean")
        self.assertEqual(len(vals), 3)
        self.assertAlmostEqual(vals[0], 0.10)
        self.assertAlmostEqual(vals[1], 9.5 / 11.0 - 1.0)
        self.assertAlmostEqual(vals[2], 0.0)
        # 逐日指标（kline 口径）：0.8.11 起分位口径 = 此前 60 有效观测严格低于天数/60，
        # 3 天回填历史不足 → 全部 rank/strength 为 None（首个满分母分位在序列满 60 后）
        d0 = self._metrics("20260812")
        self.assertEqual(d0["value_source"], "kline")
        self.assertIsNone(d0["rank_60d"]["premium_mean"])
        self.assertIsNone(d0["strength_60d"]["premium_mean"])
        d1 = self._metrics("20260813")
        self.assertIsNone(d1["rank_60d"]["premium_mean"])
        self.assertIsNone(d1["strength_60d"]["premium_mean"])
        d2 = self._metrics("20260814")
        self.assertIsNone(d2["rank_60d"]["premium_mean"])
        self.assertIsNone(d2["strength_60d"]["premium_mean"])
        # 成功率序列（0.8.13 口径）：+10% → 1.0；-13.64% → 0.0；0% → 0.0
        self.assertEqual(self._load_series("success_rate"), [1.0, 0.0, 0.0])

    def _metrics(self, d8):
        # round-trip：走 mydb_read 契约替身（与真实读取语义一致）
        v = app.mydb_read(f"打板指标:{d8}", "metrics").get("value")
        return v if isinstance(v, dict) else json.loads(v)

    def test_backfill_idempotent(self):
        app.auction_run_backfill(days=3)
        first = self._load_series("premium_mean")
        app.auction_run_backfill(days=3)
        self.assertEqual(self._load_series("premium_mean"), first)

    def test_route_backfill(self):
        """POST /api/auction/run {"task":"backfill","days":3} → 200 异步启动，后台完成后状态落库。"""
        # 注意：BaseRequestHandler.__init__ 会自动跑一次 handle()（把空 rfile 当请求行）。
        # 构造后再换入带 body 的新 rfile 与干净 wfile，避免 body 被 parse_request 吃掉。
        conn = _FakeConn()
        handler = app.Handler(conn, ("127.0.0.1", 1), None)
        body = json.dumps({"task": "backfill", "days": 3}).encode()
        conn.wfile = io.BytesIO()
        handler.wfile = conn.wfile
        handler.rfile = io.BytesIO(body)
        handler.command = "POST"
        handler.request_version = "HTTP/1.1"
        handler.protocol_version = "HTTP/1.1"
        handler.requestline = "POST /api/auction/run HTTP/1.1"
        handler.path = "/api/auction/run"
        handler.headers = {"Content-Length": str(len(body))}
        handler.do_POST()
        raw = conn.wfile.getvalue()
        head, _, resp_body = raw.partition(b"\r\n\r\n")
        status = int(head.split(b" ", 2)[1])
        payload = json.loads(resp_body.decode())
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertTrue(payload.get("async"))
        # 等待后台 worker 完成（patched 依赖下毫秒级）
        deadline = time.time() + 5
        while app._auction_backfill_state["running"] and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(app._auction_backfill_state["running"])
        self.assertEqual(app._auction_backfill_state["result"]["backfilled_days"], 3)

    def test_points_for_codes_chunks_at_200(self):
        """清单 >200 只时分块拉取：每批 ≤200，合并去重。"""
        captured = []

        def fake_snapshot_capture(args):
            captured.append(list(args.get("codes") or []))
            return {"points": [{"code": c} for c in (args.get("codes") or [])]}

        with mock.patch.object(app, "_auction_query_snapshot", side_effect=fake_snapshot_capture):
            pts = app._auction_points_for_codes(
                "20260814", [f"{600000 + i}" for i in range(250)])
        self.assertEqual(len(captured), 2)
        self.assertEqual(len(captured[0]), 200)
        self.assertEqual(len(captured[1]), 50)
        self.assertEqual(len(pts), 250)

    def test_backfill_guard_single_flight(self):
        """回填进行中再触发 → 拒绝并返回进行中提示（防并发两份重扫描）。"""
        app._auction_backfill_state.update(running=True, started="2026-08-15T00:00:00")
        r = app.auction_run_backfill_async(3)
        self.assertFalse(r["ok"])
        self.assertIn("已在运行中", r["reason"])

    def test_status_endpoint(self):
        """GET /api/auction/status 返回回填状态与日级守卫。"""
        status, _, body = _do_get("/api/auction/status")
        self.assertEqual(status, 200)
        payload = json.loads(body.decode())
        self.assertIn("backfill", payload)
        self.assertIn("running", payload["backfill"])


if __name__ == "__main__":
    unittest.main(verbosity=2)