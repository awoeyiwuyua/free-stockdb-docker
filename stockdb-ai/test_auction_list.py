#!/usr/bin/env python3
"""test_auction_list — 清单计算单元测试（0.7.0；0.8.12 对齐生产 board_metrics 口径）"""
import unittest

from core import auction_list as AL  # 领域层真身（0.9.8 根目录 shim 已删）


def _pt(code, o, c, pc, is_st=False, status="TRADED", name="X", high=None, low=None):
    return {"code": code, "name": name, "open": o, "close": c,
            "prev_close": pc, "is_st": is_st, "status": status,
            "high": high if high is not None else c,
            "low": low if low is not None else c}


class LimitPctTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertAlmostEqual(AL.limit_pct("600000"), 0.10)
        self.assertAlmostEqual(AL.limit_pct("000001"), 0.10)
        self.assertAlmostEqual(AL.limit_pct("300001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("301001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("688001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("689001"), 0.20)
        # is_st 参数保留兼容但不再生效：一律按代码段判定（ST 由 compute 层排除）
        self.assertAlmostEqual(AL.limit_pct("600000", is_st=True), 0.10)

    def test_unrecognized_regime(self):
        """北交所（4/8 开头）/非沪深 A 股 → None（排除）。"""
        self.assertIsNone(AL.limit_pct("830001"))
        self.assertIsNone(AL.limit_pct("430001"))
        self.assertIsNone(AL.limit_pct("920001"))
        self.assertIsNone(AL.limit_pct("510300"))  # ETF


class ComputeListTests(unittest.TestCase):
    def test_mixed_universe(self):
        pts = [
            _pt("600000", 10.5, 11.0, 10.0),                    # 主板 10% 封板非一字
            _pt("600001", 11.0, 11.0, 10.0, high=11.0, low=11.0),  # 主板一字（开高低收全等涨停价）
            _pt("300001", 11.5, 12.0, 10.0),                    # 创业板 20% 封板
            _pt("600002", 5.2, 5.25, 5.0, is_st=True),          # ST 5% → 排除
            _pt("600003", 10.1, 10.3, 10.0),                    # 未封板 → 排除
            _pt("830001", 12.9, 13.0, 10.0),                    # 北交所 → 排除
            _pt("600004", None, None, 10.0, status="SUSPENDED"),  # 停牌跳过
        ]
        r = AL.compute_limitup_list(pts)
        self.assertEqual(r["codes"], ["600000", "300001"])
        self.assertEqual(r["count"], 3)   # 涨停总数含一字板：600000 + 600001 + 300001
        self.assertEqual(r["yizi_count"], 1)
        self.assertEqual(r["traded"], 6)

    def test_t字板_kept(self):
        """T 字板（开/低 ≠ 涨停价，收=涨停价）保留：一字板需开高低收全等。"""
        pts = [
            _pt("600000", 10.5, 11.0, 10.0, high=11.0, low=10.6),  # 开盘未封死，尾盘封板
            _pt("600001", 11.0, 11.0, 10.0, high=11.0, low=11.0),  # 真一字
        ]
        r = AL.compute_limitup_list(pts)
        self.assertEqual(r["codes"], ["600000"])
        self.assertEqual(r["yizi_count"], 1)
        self.assertFalse(r["details"][0]["yizi"])
        self.assertTrue(r["details"][1]["yizi"])

    def test_cent_tick_tolerance(self):
        """涨停价分位容差：收盘 10.995 vs 涨停价 11.0（差 0.005）→ 判封板。"""
        r = AL.compute_limitup_list([_pt("600000", 10.0, 10.995, 10.0)])
        self.assertEqual(r["count"], 1)

    def test_band_edge_not_sealed_excluded(self):
        """涨幅带边缘但未封板（close != 涨停价）→ 排除（严格封板语义，0.8.12）。"""
        # prev=10.0 → 涨停价 11.0；收盘 11.05（+10.5%，带内）但 ≠ 11.0 → 炸板排除
        r = AL.compute_limitup_list([_pt("600000", 10.9, 11.05, 10.0)])
        self.assertEqual(r["count"], 0)

    def test_empty(self):
        r = AL.compute_limitup_list([])
        self.assertEqual(r["codes"], [])
        self.assertEqual(r["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
