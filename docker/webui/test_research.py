"""webui 运维面板（瘦身后）模块单元测试。

运行：cd docker/webui && python3 -m unittest test_research -v
或：  python3 -m unittest discover -s docker/webui -p 'test*.py' -v

覆盖：心跳 TTL 缓存、同步生效判定（_sync_effective）、reload 重载、
港股代码识别/mydb 写入与保留表拦截。
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("webui_app", str(_HERE / "app.py"))


def _load_module():
    os.environ.setdefault("DATA_DIR", "/tmp/webui-ut")
    mod = importlib.util.module_from_spec(SPEC)
    sys.modules["webui_app"] = mod
    SPEC.loader.exec_module(mod)
    return mod


mod = _load_module()


class TestTtlCaches(unittest.TestCase):
    """4s 心跳相关的 TTL 缓存：命中缓存不发请求，force=True 强制刷新。"""

    def setUp(self):
        import urllib.request as ur
        self._ur = ur
        self._orig_urlopen = ur.urlopen
        self._hits = 0

    def tearDown(self):
        self._ur.urlopen = self._orig_urlopen

    def _fake_urlopen(self, *a, **k):
        self._hits += 1

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                # 返回 000001 日K 两行，其中一行为最新日
                return json.dumps([{"date": 20260812}, {"date": 20260813}]).encode()

        return Resp()

    def _reset(self):
        mod._latest_date_cache.update(at=0.0, val=None)
        mod._code_stats_cache.update(at=0.0, val=None)
        mod._container_state_cache.update(at=0.0, val=None)

    def test_data_latest_date_cached(self):
        self._reset()
        self._ur.urlopen = self._fake_urlopen
        self.assertEqual(mod.data_latest_date(), "20260813")
        first_hits = self._hits  # 一次调用 = 近 3 月前缀数（95 天跨 4 个月）次 HTTP
        self.assertEqual(mod.data_latest_date(), "20260813")  # 命中缓存，零新增请求
        self.assertEqual(self._hits, first_hits)

    def test_data_latest_date_force_bypasses_cache(self):
        self._reset()
        self._ur.urlopen = self._fake_urlopen
        mod.data_latest_date()
        first_hits = self._hits
        mod.data_latest_date(force=True)  # 绕过缓存，重新请求
        self.assertEqual(self._hits, first_hits * 2)

    def test_code_stats_cached(self):
        self._reset()
        self._ur.urlopen = self._fake_urlopen
        mod.code_stats()
        self.assertEqual(self._hits, 1)  # 全市场代码列表 = 1 次 GET
        mod.code_stats()
        self.assertEqual(self._hits, 1)  # 命中缓存，零新增请求

    def test_container_state_cached_and_force(self):
        self._reset()
        calls = []
        orig = mod.docker_request
        mod.docker_request = lambda *a, **k: (calls.append(a),
                                              {"State": {"Status": "running"}, "Config": {"Image": "x"}})[1]
        try:
            mod.container_state()
            mod.container_state()          # 命中缓存，不再发 docker 请求
            self.assertEqual(len(calls), 1)
            mod.container_state(force=True)  # force 绕过缓存
            self.assertEqual(len(calls), 2)
            self.assertEqual(mod.container_state()["status"], "running")
            self.assertEqual(len(calls), 2)  # 回落到缓存
        finally:
            mod.docker_request = orig


class TestSyncEffective(unittest.TestCase):
    """同步是否真正生效的判定（零下载且数据未前进 → 未生效，防误报成功）。"""

    def test_downloads_gt_zero_effective(self):
        # 有下载即生效，即使日期未前进（如补历史）
        self.assertTrue(mod._sync_effective("20260811", "20260811", {"downloads": 5, "deletes": 0}))
        self.assertTrue(mod._sync_effective(None, "20260812", {"downloads": 1, "deletes": 0}))

    def test_date_advanced_effective(self):
        # 日期前进即生效，即使下载数未解析出来（不同版本同步器输出差异）
        self.assertTrue(mod._sync_effective("20260811", "20260812", {"downloads": None, "deletes": None}))
        self.assertTrue(mod._sync_effective("20260811", "20260812", {"downloads": 0, "deletes": 0}))

    def test_zero_download_same_date_ineffective(self):
        # 本次事故场景：manifest 解析失败，0 下载且日期未前进 → 未生效
        self.assertFalse(mod._sync_effective("20260811", "20260811", {"downloads": 0, "deletes": 0}))
        self.assertFalse(mod._sync_effective("20260811", "20260811", {"downloads": None, "deletes": None}))

    def test_missing_dates_ineffective_when_no_downloads(self):
        # 下载数为空且前后日期未知 → 无法证明生效
        self.assertFalse(mod._sync_effective(None, None, {"downloads": None, "deletes": None}))
        self.assertFalse(mod._sync_effective(None, None, {"downloads": 0, "deletes": 0}))

    def test_counts_empty_dict(self):
        self.assertTrue(mod._sync_effective("20260811", "20260812", {}))
        self.assertFalse(mod._sync_effective("20260811", "20260811", {}))

    def test_before_missing_after_present_no_downloads(self):
        # 无 before 快照（首次同步失败），无下载 → 不能算成功
        self.assertFalse(mod._sync_effective(None, "20260812", {"downloads": 0, "deletes": 0}))


class TestReloadStockdb(unittest.TestCase):
    """reload 热重载：向运行中的 stockdb 发命令重载快照（零中断）。"""

    def _fake(self, responses):
        """构造假 urlopen：按 URL 的 t 参数返回预设 JSON。"""
        import urllib.request, io

        class FakeResp:
            def __init__(self, body):
                self._b = body.encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False

        class FakeUrlOpener:
            def __init__(self): self.calls = []
            def __call__(self, url, timeout=10):
                self.calls.append(url)
                for key, body in responses.items():
                    if ("t=" + key) in url:
                        return FakeResp(body)
                raise urllib.error.HTTPError(url, 500, "bad", {}, io.BytesIO())

        return FakeUrlOpener()

    def test_both_remotes_reload(self):
        opener = self._fake({"0": '{"ok":true,"remote":0}', "1": '{"ok":true,"remote":1}'})
        import urllib.request
        orig = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            ok = mod.reload_stockdb()
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(sorted(ok), ["0", "1"])

    def test_partial_failure(self):
        # remote 1 失败 → 只返回成功的 0；不抛异常
        opener = self._fake({"0": '{"ok":true,"remote":0}', "1": "boom"})
        import urllib.request
        orig = urllib.request.urlopen
        urllib.request.urlopen = opener
        try:
            ok = mod.reload_stockdb()
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(ok, ["0"])

    def test_all_fail_returns_empty(self):
        import urllib.request
        orig = urllib.request.urlopen
        def boom(*a, **k): raise ConnectionError("conn refused")
        urllib.request.urlopen = boom
        try:
            ok = mod.reload_stockdb()
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(ok, [])


class TestHKAndMydb(unittest.TestCase):
    """港股代码识别、表名保留字拦截、港股日K解析。"""

    def test_hk_code_detection(self):
        self.assertTrue(mod._is_hk_code("00700"))
        self.assertTrue(mod._is_hk_code("hk00700"))
        self.assertTrue(mod._is_hk_code("09988"))
        self.assertFalse(mod._is_hk_code("600633"))  # A股
        self.assertFalse(mod._is_hk_code("510300"))  # ETF
        self.assertFalse(mod._is_hk_code("700"))     # 位数不足

    def test_hk_code_normalize(self):
        self.assertEqual(mod._normalize_hk_code("00700"), "00700")
        self.assertEqual(mod._normalize_hk_code("hk00700"), "00700")
        self.assertEqual(mod._normalize_hk_code("700"), "00700")
        self.assertEqual(mod._normalize_hk_code("09988"), "09988")

    def test_reserved_table_blocked(self):
        for t in ("日k", "日k:600633", "复权", "股票代码", "分钟k"):
            with self.assertRaises(ValueError):
                mod.validate_custom_table(t)

    def test_custom_table_ok(self):
        self.assertEqual(mod.validate_custom_table("hk日k"), "hk日k")
        self.assertEqual(mod.validate_custom_table("自定义指标"), "自定义指标")
        self.assertEqual(mod.validate_custom_table("自定义:因子"), "自定义:因子")

    def test_invalid_table_char(self):
        with self.assertRaises(ValueError):
            mod.validate_custom_table("a b")
        with self.assertRaises(ValueError):
            mod.validate_custom_table("../etc")

    def test_em_parse(self):
        # 东财 klines 格式: date,open,close,high,low,volume,amount
        import json
        fake = '{"data":{"klines":["2026-08-12,464.600,461.600,467.200,456.200,27593015,12679743488.000"]}}'
        # 直接用模块内函数解析（mock urlopen）
        import urllib.request as ur
        orig = ur.urlopen
        class FakeResp:
            def __init__(self, body): self._b = body.encode()
            def read(self): return self._b
            def __enter__(self): return self
            def __exit__(self, *a): return False
        ur.urlopen = lambda url, **kw: FakeResp(fake)
        try:
            rows = mod._hk_fetch_daily_em("00700")
        finally:
            ur.urlopen = orig
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["date"], 20260812)
        self.assertEqual(r["close"], 461.6)
        self.assertEqual(r["high"], 467.2)
        self.assertEqual(r["amount"], 12679743488.0)


if __name__ == "__main__":
    unittest.main()
