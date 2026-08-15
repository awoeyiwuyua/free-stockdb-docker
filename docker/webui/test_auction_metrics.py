#!/usr/bin/env python3
"""test_auction_metrics — 指标/分位/序列单元测试（0.7.0）"""
import json
import unittest

import auction_metrics as AM


class ComputeMetricsTests(unittest.TestCase):
    def test_normal(self):
        snaps = [
            {"code": "a", "open_price": 11.0, "prev_close": 10.0},
            {"code": "b", "open_price": 10.5, "prev_close": 10.0},
            {"code": "d", "open_price": 9.5, "prev_close": 10.0},
        ]
        m = AM.compute_metrics(snaps)
        self.assertEqual(m["n_samples"], 3)
        self.assertAlmostEqual(m["premium_mean"], (0.1 + 0.05 - 0.05) / 3)
        self.assertAlmostEqual(m["success_rate"], 2 / 3)

    def test_none_values_excluded(self):
        snaps = [
            {"code": "a", "open_price": 11.0, "prev_close": 10.0},
            {"code": "s", "open_price": None, "prev_close": 10.0},
            {"code": "t", "open_price": 11.0, "prev_close": None},
        ]
        m = AM.compute_metrics(snaps)
        self.assertEqual(m["n_samples"], 1)
        self.assertAlmostEqual(m["premium_mean"], 0.1)
        self.assertAlmostEqual(m["success_rate"], 1.0)

    def test_empty(self):
        m = AM.compute_metrics([])
        self.assertEqual(m["n_samples"], 0)
        self.assertIsNone(m["premium_mean"])
        self.assertIsNone(m["success_rate"])


class PercentileRankTests(unittest.TestCase):
    def test_empty_series(self):
        self.assertIsNone(AM.percentile_rank(0.5, []))

    def test_extremes(self):
        series = [0.01, 0.02, 0.03]
        self.assertAlmostEqual(AM.percentile_rank(0.05, series), 1.0)
        self.assertAlmostEqual(AM.percentile_rank(0.005, series), 0.0)

    def test_middle_and_ties(self):
        series = [0.01, 0.02, 0.03]
        self.assertAlmostEqual(AM.percentile_rank(0.02, series), 0.5)  # 1 less + 0.5 equal
        series2 = [0.01, 0.02, 0.02, 0.03]
        self.assertAlmostEqual(AM.percentile_rank(0.02, series2), (1 + 0.5 * 2) / 4)


class SeriesTests(unittest.TestCase):
    def test_load_missing_or_corrupt(self):
        self.assertEqual(AM.load_series(lambda k: None, "premium_mean"), [])
        self.assertEqual(AM.load_series(lambda k: "not json", "premium_mean"), [])

    def test_append_and_trim(self):
        store = {}

        def rd(k):
            return store.get(k)

        def wr(k, v):
            store[k] = json.dumps(v, ensure_ascii=False)

        seq = AM.append_series(rd, wr, "premium_mean", 0.1, "20260817")
        self.assertEqual(seq, [0.1])
        for i in range(2, 62):
            AM.append_series(rd, wr, "premium_mean", i / 100, f"202609{i:02d}")
        final = AM.load_series(rd, "premium_mean")
        self.assertEqual(len(final), AM.WINDOW)          # 裁剪至 60
        self.assertEqual(final[-1], 0.61)                 # 最新值在尾部
        raw = json.loads(store[AM.series_key("premium_mean")])
        self.assertEqual(raw["window"], AM.WINDOW)
        self.assertEqual(raw["metric"], "premium_mean")

    def test_build_payload(self):
        snaps = [{"code": "a", "open_price": 11.0, "prev_close": 10.0}]
        series = {"premium_mean": [0.05, 0.06], "success_rate": [0.5]}
        p = AM.build_metrics_payload(snaps, series, "2026-08-17T09:26:00", "auction")
        self.assertEqual(p["value_source"], "auction")
        self.assertEqual(p["window"], AM.WINDOW)
        self.assertEqual(p["contract"], AM.METRIC_CONTRACT)
        self.assertIn("premium_mean", p["rank_60d"])
        self.assertAlmostEqual(p["metrics"]["premium_mean"], 0.1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
