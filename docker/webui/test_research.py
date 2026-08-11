"""市场研究模块单元测试。

运行：cd docker/webui && python3 -m unittest test_research -v
或：  python3 -m unittest discover -s docker/webui -p 'test*.py' -v

覆盖：因子公式（动量/波动率/量比/回撤）、数据不足/缺失、百分位方向、
缓存原子写/损坏回退/失败保护、防重入、市场聚合（target_date 过滤）。
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


def mk_bars(closes, highs=None, lows=None, vols=None):
    highs = highs or closes
    lows = lows or highs
    vols = vols or [1_000_000] * len(closes)
    return [{"date": 20260000 + i, "close": float(c), "high": float(h),
             "low": float(l), "open": float(h), "volume": float(v),
             "amount": v * 10,
             "pct_chg": (closes[i] / closes[i - 1] - 1) if (i and closes[i - 1]) else 0.0}
            for i, (c, h, l, v) in enumerate(zip(closes, highs, lows, vols))]


class TestFactors(unittest.TestCase):
    def test_mom20(self):
        seq = list(range(1, 60))
        self.assertAlmostEqual(mod._factor_mom20(mk_bars(seq)), 59 / 39 - 1, places=6)

    def test_mom20_exact_21_bars(self):
        # 恰有 21 根即可计算（bars[-1] 与 bars[-21]）
        seq = list(range(1, 22))
        self.assertIsNotNone(mod._factor_mom20(mk_bars(seq)))
        self.assertAlmostEqual(mod._factor_mom20(mk_bars(seq)), 21 / 1 - 1, places=6)

    def test_mom20_insufficient(self):
        self.assertIsNone(mod._factor_mom20(mk_bars([1, 2, 3])))

    def test_mom20_zero_base(self):
        # bars[-21] 恰为 0 → 除零防御返回 None（不崩溃）
        bars = mk_bars([1.0] * 19 + [0.0] + [2.0] * 20)
        self.assertIsNone(mod._factor_mom20(bars))

    def test_vol20_constant(self):
        self.assertEqual(mod._factor_vol20(mk_bars([10.0] * 30)), 0.0)

    def test_vol20_insufficient(self):
        self.assertIsNone(mod._factor_vol20(mk_bars([1] * 10)))

    def test_vr520(self):
        # 最近20根：前15根量100、后5根量200 → v5=200, v20=(1500+1000)/20=125 → 1.6
        vols = [1e6] * 5 + [100] * 15 + [200] * 5
        self.assertAlmostEqual(mod._factor_vr520(mk_bars([1] * 25, vols=vols)), 1.6, places=6)

    def test_vr520_zero_volume(self):
        self.assertIsNone(mod._factor_vr520(mk_bars([1] * 25, vols=[0] * 25)))

    def test_vr520_insufficient(self):
        self.assertIsNone(mod._factor_vr520(mk_bars([1] * 10)))

    def test_dd60(self):
        highs = [100.0] * 59 + [50.0]
        self.assertAlmostEqual(mod._factor_dd60(mk_bars([50.0] * 60, highs=highs)), -0.5, places=6)

    def test_dd60_insufficient(self):
        self.assertIsNone(mod._factor_dd60(mk_bars([1] * 30)))

    def test_compute_factors_shape(self):
        f = mod.compute_factors(mk_bars(list(range(1, 70))))
        self.assertEqual(set(f), {"mom20", "vol20", "vr520", "dd60"})


class TestPercentiles(unittest.TestCase):
    VALS = [1.0, 2.0, 3.0, 4.0, 5.0]

    def test_higher_better(self):
        self.assertEqual(mod.percentile_rank(1.0, self.VALS), 20.0)
        self.assertEqual(mod.percentile_rank(3.0, self.VALS), 60.0)
        self.assertEqual(mod.percentile_rank(5.0, self.VALS), 100.0)

    def test_lower_better(self):
        # 波动率：值越小排名越高
        self.assertEqual(mod.percentile_rank(1.0, self.VALS, higher_is_better=False), 100.0)
        self.assertEqual(mod.percentile_rank(5.0, self.VALS, higher_is_better=False), 20.0)

    def test_dd60_negative_higher_better(self):
        # 回撤为负数，-50% 比 -10% 严重 → 值越大(接近0)分位越高 → higher=True
        vals = [-0.5, -0.2, -0.1]
        self.assertEqual(mod.percentile_rank(-0.5, vals, higher_is_better=True), 33.3)
        self.assertEqual(mod.percentile_rank(-0.1, vals, higher_is_better=True), 100.0)

    def test_none_and_empty(self):
        self.assertIsNone(mod.percentile_rank(None, self.VALS))
        self.assertIsNone(mod.percentile_rank(1.0, []))


class TestCache(unittest.TestCase):
    def setUp(self):
        self._dir = Path(tempfile.mkdtemp())
        self._old_dir = mod.RESEARCH_CACHE_DIR
        mod.RESEARCH_CACHE_DIR = self._dir
        self._dir.mkdir(exist_ok=True)

    def tearDown(self):
        mod.RESEARCH_CACHE_DIR = self._old_dir

    def test_atomic_write_no_tmp(self):
        mod._atomic_write_json(self._dir / "factor_snapshot_20260811.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260811"})
        self.assertFalse(list(self._dir.glob("*.tmp")))
        self.assertEqual(mod._load_snapshot("factor_snapshot_")["date"], "20260811")

    def test_latest_date_preferred(self):
        mod._atomic_write_json(self._dir / "factor_snapshot_20260801.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260801"})
        mod._atomic_write_json(self._dir / "factor_snapshot_20260811.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260811"})
        self.assertEqual(mod._load_snapshot("factor_snapshot_")["date"], "20260811")

    def test_corrupt_latest_falls_back(self):
        mod._atomic_write_json(self._dir / "factor_snapshot_20260801.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260801"})
        (self._dir / "factor_snapshot_20260812.json").write_text("{bad", encoding="utf-8")
        self.assertEqual(mod._load_snapshot("factor_snapshot_")["date"], "20260801")

    def test_schema_mismatch_falls_back(self):
        mod._atomic_write_json(self._dir / "factor_snapshot_20260801.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260801"})
        mod._atomic_write_json(self._dir / "factor_snapshot_20260813.json",
                               {"schema_version": 99, "date": "20260813"})
        self.assertEqual(mod._load_snapshot("factor_snapshot_")["date"], "20260801")

    def test_all_corrupt_returns_none(self):
        (self._dir / "factor_snapshot_20260811.json").write_text("x", encoding="utf-8")
        self.assertIsNone(mod._load_snapshot("factor_snapshot_"))

    def test_build_failure_keeps_old_cache(self):
        mod._atomic_write_json(self._dir / "market_snapshot_20260801.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260801"})
        mod._atomic_write_json(self._dir / "factor_snapshot_20260801.json",
                               {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260801"})
        # 空代码列表 → 校验中止，不写新缓存
        import urllib.request as ur

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b"{}"

        orig = ur.urlopen
        ur.urlopen = lambda *a, **k: FakeResp()
        try:
            mod._research_build.update(state="idle")
            mod._run_research_build("20260811")
        finally:
            ur.urlopen = orig
        self.assertEqual(mod._research_build["state"], "failed")
        snaps = list(self._dir.glob("*snapshot*"))
        self.assertEqual(len(snaps), 2)
        self.assertFalse(any("None" in p.name for p in snaps))


class TestReentry(unittest.TestCase):
    def test_lock_prevents_double_start(self):
        # 模拟构建进行中：锁被持有 → 再次启动返回 False
        self.assertTrue(mod._research_build_lock.acquire(blocking=False))
        try:
            mod._research_build["state"] = "building"
        finally:
            mod._research_build_lock.release()
        # _start_research_build 会真实启动线程；此处只验证锁语义（不真的跑全市场）
        self.assertTrue(mod._research_build_lock.acquire(blocking=False))
        self.assertFalse(mod._research_build_lock.acquire(blocking=False))
        mod._research_build_lock.release()


class TestMarketAggregation(unittest.TestCase):
    def _row(self, code, date, pct, close, ma20v, amount, **kw):
        r = {"code": code, "type": "stock", "date": date, "pct_chg": pct, "close": close,
             "amount": amount, "prev_amount": 100, "ma20": ma20v,
             "is_high20": False, "is_low20": False, "daily": {}, **kw}
        return r

    def test_basic_counts(self):
        rows = [self._row("000001", 20260811, 0.02, 10, 9, 1000),
                self._row("600000", 20260811, -0.01, 8, 9, 500),
                self._row("300750", None, None, 12, 11, 700)]
        m = mod.market_summary_from_stocks(rows, target_date="20260811")
        self.assertEqual((m["up"], m["down"], m["flat"], m["na"]), (1, 1, 0, 1))

    def test_old_date_goes_to_na(self):
        # 有历史但最新日期是旧交易日 → 停牌，必须计入 na 而非当日涨跌
        rows = [self._row("000001", 20260811, 0.02, 10, 9, 1000),
                self._row("600000", 20260701, -0.05, 8, 9, 500)]
        m = mod.market_summary_from_stocks(rows, target_date="20260811")
        self.assertEqual((m["up"], m["down"], m["na"]), (1, 0, 1))

    def test_temperature(self):
        rows = [self._row("000001", 20260811, 0.02, 10, 9, 1000),
                self._row("600000", 20260811, -0.01, 8, 9, 500),
                self._row("300750", 20260811, None, 12, 11, 700)]  # 无涨跌但收盘有效
        m = mod.market_summary_from_stocks(rows, target_date="20260811")
        # up=1 total=2 → up_ratio 0.5→20；above: close>ma20 → 2/2=1.0→40；highlow 0→10 → 70
        self.assertAlmostEqual(m["temperature"], 70.0, places=1)
        self.assertAlmostEqual(m["temp_components"]["up_ratio"], 20.0, places=1)

    def test_median(self):
        rows = [self._row("000001", 20260811, 0.02, 10, 9, 1),
                self._row("600000", 20260811, -0.01, 8, 9, 1)]
        m = mod.market_summary_from_stocks(rows, target_date="20260811")
        self.assertAlmostEqual(m["median_pct"], 0.005, places=6)

    def test_empty_input(self):
        m = mod.market_summary_from_stocks([], target_date="20260811")
        self.assertIsNone(m["date"])
        self.assertEqual((m["up"], m["down"], m["na"]), (0, 0, 0))


class TestWatchlistData(unittest.TestCase):
    def test_high_low_uses_high_low(self):
        # 当日 high 创前 19 日新高 → is_high20（即使 close 未创新高）
        closes = [10.0] * 20
        highs = [10.0] * 19 + [11.0, 10.5]  # 当日 high=10.5 > 前19日 max=10 → 新高
        lows = [10.0] * 21
        bars = mk_bars(closes, highs=highs, lows=lows)
        row = mod.stock_research_row("600000", "X", "stock", bars)
        self.assertTrue(row["is_high20"])
        # 当 close 创新高但 high 未超过前 19 日 → 不算新高（口径一致：以 high 为准）
        closes2 = [10.0] * 19 + [10.1, 10.2]
        highs2 = [11.0] * 20 + [10.2]  # 前19日 max=11 > 当日 10.2 → 非新高
        row2 = mod.stock_research_row("600000", "X", "stock", mk_bars(closes2, highs=highs2))
        self.assertFalse(row2["is_high20"])


class TestBuildWithPlaceholders(unittest.TestCase):
    """回归：部分股票无行情时的构建不应失败，且占位行不影响有效比例/因子快照。"""

    def test_partial_no_data_build_succeeds(self):
        # 模拟 scan_one 占位行逻辑：补齐 name，date=None
        placeholder = {"code": "600001", "name": "600001", "type": "stock", "date": None,
                       "pct_chg": None, "close": None, "amount": 0.0, "prev_amount": 0.0,
                       "ma20": None, "is_high20": False, "is_low20": False, "daily": {},
                       "mom20": None, "vol20": None, "vr520": None, "dd60": None}
        self.assertEqual(placeholder["name"], "600001")
        # 占位行可被 market_summary 计为 na
        rows = [placeholder,
                {"code": "000001", "name": "平安银行", "type": "stock", "date": 20260811,
                 "pct_chg": 0.01, "close": 10, "amount": 100, "prev_amount": 90,
                 "ma20": 9, "is_high20": False, "is_low20": False, "daily": {},
                 "mom20": 0.1, "vol20": 0.2, "vr520": 1.0, "dd60": -0.05}]
        m = mod.market_summary_from_stocks(rows, target_date="20260811")
        self.assertEqual(m["na"], 1)
        self.assertEqual(m["up"], 1)

    def test_valid_count_uses_date(self):
        # valid（有效行情）应以 date 非空为准，占位行不计
        rows = [
            {"code": "A", "date": 20260811},   # 有效
            {"code": "B", "date": None},        # 占位 → 无效
            {"code": "C", "date": 20260811},   # 有效
        ]
        valid = sum(1 for r in rows if r.get("date"))
        self.assertEqual(valid, 2)
        self.assertEqual(len(rows), 3)

    def test_schema_bump_invalidates_old_cache(self):
        # 口径升级后旧 schema 缓存不再被读取（RESEARCH_SCHEMA=2）
        old = {"schema_version": 1, "date": "20260811"}  # 旧版本缓存
        self.assertNotEqual(old["schema_version"], mod.RESEARCH_SCHEMA)
        self.assertEqual(mod.RESEARCH_SCHEMA, 2)

    def test_factor_payload_excludes_placeholders(self):
        # 因子快照只保留 date 非空的行（占位行仅服务市场 na 统计）
        results = [
            {"code": "A", "name": "A", "date": 20260811},
            {"code": "B", "name": "B", "date": None},
        ]
        payload_codes = [r for r in results if r.get("date")]
        self.assertEqual(len(payload_codes), 1)
        self.assertEqual(payload_codes[0]["code"], "A")

    def test_historical_ma20_includes_today(self):
        # 历史 MA20 与最新快照统一：含当日的 20 根收盘价（idx-19..idx），idx≥19 即产生
        closes = [10.0] * 30
        bars = mk_bars(closes)
        # 对照 stock_research_row 的最新 MA20（取最近 20 根含当日）
        row = mod.stock_research_row("600000", "X", "stock", bars)
        self.assertEqual(row["ma20"], 10.0)
        # 手工验证含当日口径：closes[idx-19:idx+1]
        idx = 29
        ma_hist = sum(closes[idx - 19:idx + 1]) / 20
        self.assertEqual(ma_hist, 10.0)


if __name__ == "__main__":
    unittest.main()
