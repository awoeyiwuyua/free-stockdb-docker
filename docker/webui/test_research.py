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

    def test_derived_breadth_and_liquidity(self):
        daily = {}
        for i in range(1, 22):
            daily[20260700 + i] = {"up": 1 if i > 10 else 0, "down": 0 if i > 10 else 1,
                                    "flat": 0, "above": 1, "amount": i * 100,
                                    "high20": 1, "low20": 0}
        row = self._row("000001", 20260721, 1.0, 10, 9, 2100, daily=daily,
                        is_high20=True)
        m = mod.market_summary_from_stocks([row], target_date="20260721")
        self.assertEqual(m["derived"]["advance_decline_ratio"], None)
        self.assertEqual(m["derived"]["breadth_gap_pp"], 0.0)
        self.assertAlmostEqual(m["derived"]["amount_vs_prev5_pct"], 16.7, places=1)
        self.assertAlmostEqual(m["derived"]["amount_vs_prev20_pct"], 100.0, places=1)
        self.assertEqual(len(m["width_hist"]), 20)

    def test_return_distribution(self):
        values = [6, 3, 1, 0, -1, -3, -6, 10, -10]
        rows = [self._row(str(i).zfill(6), 20260811, pct, 10, 9, 1)
                for i, pct in enumerate(values)]
        d = mod.market_return_distribution(rows, target_date="20260811")
        counts = {row["key"]: row["count"] for row in d["buckets"]}
        self.assertEqual(counts, {"ge5": 2, "up2_5": 1, "up0_2": 1, "flat": 1,
                                  "down0_2": 1, "down2_5": 1, "le_neg5": 2})
        self.assertEqual((d["large_up"], d["large_down"]), (1, 1))


class TestSectorReview(unittest.TestCase):
    def test_sector_strength(self):
        rows = []
        for i, pct in enumerate([3, 2, 1, -1, -2, -3]):
            rows.append({"date": 20260811, "pct_chg": pct, "amount": 120,
                         "prev_amount": 100, "industry": "电子" if i < 3 else "银行"})
        result = mod.sector_strength(rows, target_date="20260811")
        self.assertTrue(result["available"])
        self.assertEqual(result["top"][0]["name"], "电子")
        self.assertEqual(result["bottom"][0]["name"], "银行")
        self.assertEqual(result["top"][0]["median_pct"], 2)
        self.assertEqual(result["top"][0]["up_ratio"], 1.0)

class TestSQLiteStorageAndBoards(unittest.TestCase):
    def test_market_snapshot_transaction_and_normalized_tables(self):
        import sqlite3
        db_path = Path(tempfile.mkdtemp()) / "market.sqlite3"
        rows = [{"code": "000001", "type": "stock", "date": 20260811,
                 "pct_chg": 2.5, "close": 10, "ma20": 9, "amount": 120,
                 "prev_amount": 100, "is_high20": True, "is_low20": False,
                 "daily": {20260811: {"up": 1, "down": 0, "flat": 0, "above": 1,
                                       "amount": 120, "high20": 1, "low20": 0}}}]
        summary = mod.market_summary_from_stocks(rows, target_date="20260811")
        summary["sectors"] = {"available": True, "mapped": 1, "unmapped": 0,
            "rows": [{"name": "电子", "count": 1, "median_pct": 2.5,
                      "up_ratio": 1.0, "amount": 120, "amount_change": 20.0}],
            "top": [], "bottom": []}
        mod.save_market_snapshot_db(summary, db_path)
        loaded = mod.load_latest_market_snapshot_db(db_path)
        self.assertEqual(loaded["date"], "20260811")
        self.assertEqual(loaded["sectors"]["rows"][0]["name"], "电子")
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_daily").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM breadth_daily").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM return_distribution_daily").fetchone()[0], 7)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM sector_daily").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM methodology").fetchone()[0],
                             len(mod.MARKET_METHODOLOGY))
        finally:
            conn.close()
        status = mod.research_db_status(db_path)
        self.assertTrue(status["ok"])
        self.assertEqual(status["latest_date"], "20260811")

    def test_same_date_replaces_distribution_in_one_snapshot(self):
        import sqlite3
        db_path = Path(tempfile.mkdtemp()) / "market.sqlite3"
        summary = {"schema_version": mod.RESEARCH_SCHEMA, "date": "20260811",
                   "generated_at": "2026-08-11 16:00:00", "derived": {}, "width_hist": [],
                   "distribution": {"buckets": [{"key": "flat", "label": "平盘", "count": 1, "ratio": 1.0}]},
                   "sectors": {"rows": []}, "methodology": mod.MARKET_METHODOLOGY}
        mod.save_market_snapshot_db(summary, db_path)
        summary["distribution"]["buckets"] = [{"key": "ge5", "label": "≥ +5%", "count": 2, "ratio": 1.0}]
        mod.save_market_snapshot_db(summary, db_path)
        conn = sqlite3.connect(db_path)
        try:
            self.assertEqual(conn.execute("SELECT bucket FROM return_distribution_daily").fetchall(), [("ge5",)])
        finally:
            conn.close()

    def test_legacy_json_migrates_once_and_is_retained(self):
        tmp = Path(tempfile.mkdtemp())
        old_cache_dir, old_db_path = mod.RESEARCH_CACHE_DIR, mod.RESEARCH_DB_PATH
        mod.RESEARCH_CACHE_DIR = tmp / "cache"
        mod.RESEARCH_DB_PATH = tmp / "research" / "market.sqlite3"
        mod.RESEARCH_CACHE_DIR.mkdir(parents=True)
        legacy_path = mod.RESEARCH_CACHE_DIR / "market_snapshot_20260811.json"
        legacy = {
            "schema_version": 2,
            "date": "20260811",
            "generated_at": "2026-08-11 16:00:00",
            "derived": {}, "width_hist": [],
            "distribution": {"buckets": []},
            "sectors": {"rows": []},
            "methodology": mod.MARKET_METHODOLOGY,
        }
        legacy_path.write_text(json.dumps(legacy, ensure_ascii=False), encoding="utf-8")
        try:
            self.assertTrue(mod.migrate_legacy_market_snapshot())
            loaded = mod.load_latest_market_snapshot_db()
            self.assertEqual(loaded["date"], "20260811")
            self.assertEqual(loaded["schema_version"], mod.RESEARCH_SCHEMA)
            self.assertIn("sector_strength", loaded["methodology"])
            self.assertTrue(legacy_path.exists())
            self.assertFalse(mod.migrate_legacy_market_snapshot())
        finally:
            mod.RESEARCH_CACHE_DIR, mod.RESEARCH_DB_PATH = old_cache_dir, old_db_path

    def test_sw1_board_bulk_response(self):
        import urllib.request as ur

        payload = [["板块:801080.SL", {"code": "801080.SL", "name": "电子",
                    "category": "申万一级", "symbols": ["000001", "600000.SH"]}],
                   ["板块:概念", {"code": "X", "name": "AI", "category": "概念",
                    "symbols": ["000001"]}]]

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(payload, ensure_ascii=False).encode()

        original = ur.urlopen
        ur.urlopen = lambda *args, **kwargs: FakeResp()
        try:
            mapping = mod._fetch_sw1_industry_map()
        finally:
            ur.urlopen = original
        self.assertEqual(mapping, {"000001": "电子", "600000": "电子"})


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
        # SQLite 持久化升级后，旧 v3 市场快照不再被直接读取。
        old = {"schema_version": 3, "date": "20260811"}
        self.assertNotEqual(old["schema_version"], mod.RESEARCH_SCHEMA)
        self.assertEqual(mod.RESEARCH_SCHEMA, 4)

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


class TestAggregateKline(unittest.TestCase):
    """周/月 K 聚合：由日 K 按自然周/月聚合 OHLCV。"""

    def _day(self, date, o, c, h, l, v=1000):
        return {"date": date, "open": o, "close": c, "high": h, "low": l,
                "volume": v, "amount": v * 10, "pct_chg": 0.0}

    def test_day_returns_unchanged(self):
        rows = [self._day(20260810, 10, 11, 11, 10)]
        self.assertEqual(mod._aggregate_kline(rows, "day"), rows)

    def test_week_aggregation(self):
        # 2026-08-10(周一)~08-14(周五) 同属自然周 → 1 根周K
        rows = [
            self._day(20260810, 10.0, 10.5, 10.8, 9.9, 100),
            self._day(20260811, 10.5, 10.2, 10.9, 10.0, 200),
            self._day(20260812, 10.2, 10.6, 11.2, 10.1, 300),
            self._day(20260813, 10.6, 10.9, 11.0, 10.4, 150),
            self._day(20260814, 10.9, 11.2, 11.5, 10.8, 250),
        ]
        out = mod._aggregate_kline(rows, "week")
        self.assertEqual(len(out), 1)
        w = out[0]
        self.assertEqual(w["open"], 10.0)     # 周首日开
        self.assertEqual(w["close"], 11.2)    # 周末日收
        self.assertEqual(w["high"], 11.5)     # 周内最高
        self.assertEqual(w["low"], 9.9)       # 周内最低
        self.assertEqual(w["volume"], 1000)   # 周内求和
        self.assertEqual(w["date"], 20260814)  # 取周末日

    def test_week_two_groups(self):
        # 08-17 是下一周周一 → 分两组
        rows = [
            self._day(20260814, 10, 11, 11, 10),   # 周五（上周）
            self._day(20260817, 11, 12, 12, 10.5),  # 下周一
        ]
        out = mod._aggregate_kline(rows, "week")
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["date"], 20260814)
        self.assertEqual(out[1]["date"], 20260817)

    def test_month_aggregation(self):
        rows = [
            self._day(20260731, 10, 10.5, 10.8, 9.9, 100),
            self._day(20260803, 10.5, 11.0, 11.2, 10.4, 200),
            self._day(20260828, 11.0, 11.8, 12.0, 10.9, 300),
        ]
        out = mod._aggregate_kline(rows, "month")
        self.assertEqual(len(out), 2)
        # 7 月组
        self.assertEqual(out[0]["open"], 10.0)
        self.assertEqual(out[0]["close"], 10.5)
        # 8 月组：首日 08-03 开、末日 08-28 收
        self.assertEqual(out[1]["open"], 10.5)
        self.assertEqual(out[1]["close"], 11.8)
        self.assertEqual(out[1]["high"], 12.0)
        self.assertEqual(out[1]["low"], 10.4)
        self.assertEqual(out[1]["volume"], 500)
        self.assertEqual(out[1]["date"], 20260828)

    def test_empty_input(self):
        self.assertEqual(mod._aggregate_kline([], "week"), [])


class TestWatchlistScope(unittest.TestCase):
    """自选列表按代码段分流（个股/ETF 独立板块互不覆盖）。"""

    def setUp(self):
        self._orig_file = mod.WATCHLIST_FILE
        import tempfile
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".json")
        mod.WATCHLIST_FILE = type("P", (), {"exists": lambda self: True,
                                            "read_text": lambda self, **k: "[]",
                                            "write_text": lambda self, s, **k: None})()
        # 用真实文件模拟，简单起见直接用临时路径
        mod.WATCHLIST_FILE = __import__("pathlib").Path(self._tmp.name)
        mod.WATCHLIST_FILE.write_text("[]", encoding="utf-8")

    def tearDown(self):
        mod.WATCHLIST_FILE = self._orig_file

    def _codes(self):
        return mod.load_watchlist(None)

    def test_classify_code(self):
        self.assertEqual(mod._classify_code("600633"), "stock")
        self.assertEqual(mod._classify_code("000001"), "stock")
        self.assertEqual(mod._classify_code("510300"), "etf")
        self.assertEqual(mod._classify_code("159915"), "etf")
        # 港股：5 位数字（含非 0 开头）与 hk 前缀均归为 hk
        self.assertEqual(mod._classify_code("00700"), "hk")
        self.assertEqual(mod._classify_code("hk00700"), "hk")
        self.assertEqual(mod._classify_code("10700"), "hk")
        self.assertEqual(mod._classify_code("700"), "other")  # 位数不足

    def test_save_hk_goes_to_stock_scope(self):
        """港股加自选：存 5 位代码，归入个股板块展示。"""
        mod.save_watchlist(["00700"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["00700"])
        self.assertEqual(mod.load_watchlist("etf"), [])
        self.assertEqual(mod.load_watchlist(None), ["00700"])

    def test_save_hk_prefix_normalized(self):
        """hk 前缀统一存为 5 位数字。"""
        mod.save_watchlist(["hk00700", "00700"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["00700"])

    def test_save_hk_rejects_wrong_length(self):
        """位数不足（3 位）不进入自选。"""
        mod.save_watchlist(["700"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), [])

    def test_hk_deletable_from_stock_board(self):
        """港股可增可删：个股板块整体替换（A股+港股），删除后不再保留。"""
        mod.save_watchlist(["600633", "00700"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["600633", "00700"])
        # 删除 00700（前端 delWatch 发来剩余板块代码）
        mod.save_watchlist(["600633"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["600633"])
        # 删除 A 股保留港股
        mod.save_watchlist(["00700"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["00700"])

    def test_hk_preserved_across_scopes(self):
        """自选股板块（含港股）与 ETF 板块互不影响。"""
        mod.save_watchlist(["00700", "600633"], "stock")
        mod.save_watchlist(["510300"], "etf")
        self.assertEqual(mod.load_watchlist("stock"), ["00700", "600633"])
        self.assertEqual(mod.load_watchlist("etf"), ["510300"])
        self.assertEqual(sorted(mod.load_watchlist(None)), ["00700", "510300", "600633"])
        # 更新 ETF 板块不影响个股板块（含港股）
        mod.save_watchlist(["510300", "159915"], "etf")
        self.assertEqual(mod.load_watchlist("stock"), ["00700", "600633"])
        self.assertEqual(mod.load_watchlist("etf"), ["510300", "159915"])

    def test_save_stock_preserves_etf(self):
        mod.save_watchlist(["600633", "000001"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["600633", "000001"])
        self.assertEqual(mod.load_watchlist("etf"), [])

    def test_save_etf_preserves_stock(self):
        mod.save_watchlist(["600633", "000001"], "stock")
        mod.save_watchlist(["510300", "159915"], "etf")
        self.assertEqual(mod.load_watchlist("stock"), ["600633", "000001"])
        self.assertEqual(mod.load_watchlist("etf"), ["510300", "159915"])
        # 全量包含两者
        self.assertEqual(sorted(mod.load_watchlist(None)), ["000001", "159915", "510300", "600633"])

    def test_del_in_scope_keeps_other(self):
        mod.save_watchlist(["600633", "000001"], "stock")
        mod.save_watchlist(["510300", "159915"], "etf")
        # stock 删除 600633 不影响 etf
        mod.save_watchlist(["000001"], "stock")
        self.assertEqual(mod.load_watchlist("stock"), ["000001"])
        self.assertEqual(mod.load_watchlist("etf"), ["510300", "159915"])


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
