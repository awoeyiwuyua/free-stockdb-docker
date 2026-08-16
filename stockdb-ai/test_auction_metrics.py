#!/usr/bin/env python3
"""test_auction_metrics — 指标/分位/序列单元测试（0.7.0）"""
import json
import unittest

from core import auction_metrics as AM  # 领域层真身（0.9.8 根目录 shim 已删）


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
    """用户口径（2026-08-16 拍板）：此前 60 个有效观测中严格低于当日值天数 / 60；
    不足 60 个观测 → None。"""

    def test_empty_series(self):
        self.assertIsNone(AM.percentile_rank(0.5, []))

    def test_insufficient_history_returns_none(self):
        """不足 60 个有效观测 → None（定义不适用，不硬算近似）。"""
        for n in (1, 10, 59):
            series = [0.01 + i / 1000 for i in range(n)]
            self.assertIsNone(AM.percentile_rank(0.05, series), f"n={n}")

    def test_full_window_extremes(self):
        """满 60 观测：全部低于 → 1.0；全部高于 → 0.0。"""
        series = [0.01 + i / 1000 for i in range(AM.WINDOW)]
        self.assertAlmostEqual(AM.percentile_rank(0.99, series), 1.0)
        self.assertAlmostEqual(AM.percentile_rank(-0.01, series), 0.0)

    def test_full_window_strict_less(self):
        """严格小于计数：等值不计入（54 个严格低于 → 54/60 = 0.9）。"""
        series = [0.01] * 6 + [0.05] * 54          # 60 观测：6 低 + 54 等
        self.assertAlmostEqual(AM.percentile_rank(0.05, series), 6 / 60)
        series2 = [0.01] * 54 + [0.05] * 6         # 60 观测：54 低 + 6 等
        self.assertAlmostEqual(AM.percentile_rank(0.05, series2), 54 / 60)

    def test_full_window_windows_to_last_60(self):
        """超出 60 个观测时只取最近 60 个（滚动窗口裁剪）。"""
        series = [0.0] * 5 + [0.01 + i / 1000 for i in range(AM.WINDOW)]
        # 前 5 个 0.0 被挤出窗口；窗口内 60 个观测均高于 -0.01
        self.assertAlmostEqual(AM.percentile_rank(-0.01, series), 0.0)


class StrengthLabelTests(unittest.TestCase):
    """强弱标签（用户口径）：strong rank≥0.90 / weak rank≤0.10 / neutral 其余。"""

    def test_thresholds(self):
        self.assertEqual(AM.strength_label(0.90), "strong")   # 恰 54/60
        self.assertEqual(AM.strength_label(1.0), "strong")
        self.assertEqual(AM.strength_label(0.10), "weak")     # 恰 6/60
        self.assertEqual(AM.strength_label(0.0), "weak")
        self.assertEqual(AM.strength_label(0.5), "neutral")
        self.assertEqual(AM.strength_label(0.89), "neutral")
        self.assertEqual(AM.strength_label(0.11), "neutral")

    def test_none_rank(self):
        self.assertIsNone(AM.strength_label(None))


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
        # 短序列（不足 60 观测）→ rank 与强弱标签均为 None
        self.assertIsNone(p["rank_60d"]["premium_mean"])
        self.assertIsNone(p["strength_60d"]["premium_mean"])
        self.assertIn("strength_60d", p)

    def test_build_payload_full_window_labels(self):
        """满 60 观测 → rank 计算 + 强弱标签产出（strong 边界 54/60）。"""
        snaps = [{"code": "a", "open_price": 11.0, "prev_close": 10.0}]  # 溢价 0.1
        series = {"premium_mean": [0.01] * 54 + [0.2] * 6,  # 54 低 + 6 高
                  "success_rate": [0.5]}
        p = AM.build_metrics_payload(snaps, series, "2026-08-17T09:26:00", "auction")
        self.assertAlmostEqual(p["rank_60d"]["premium_mean"], 54 / 60)
        self.assertEqual(p["strength_60d"]["premium_mean"], "strong")
        self.assertIsNone(p["rank_60d"]["success_rate"])
        self.assertIsNone(p["strength_60d"]["success_rate"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
