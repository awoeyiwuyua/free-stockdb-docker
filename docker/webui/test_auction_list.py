#!/usr/bin/env python3
"""test_auction_list — 清单计算单元测试（0.7.0）"""
import unittest

import auction_list as AL


def _pt(code, o, c, pc, is_st=False, status="TRADED", name="X"):
    return {"code": code, "name": name, "open": o, "close": c,
            "prev_close": pc, "is_st": is_st, "status": status}


class LimitPctTests(unittest.TestCase):
    def test_thresholds(self):
        self.assertAlmostEqual(AL.limit_pct("600000"), 0.10)
        self.assertAlmostEqual(AL.limit_pct("000001"), 0.10)
        self.assertAlmostEqual(AL.limit_pct("300001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("301001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("688001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("689001"), 0.20)
        self.assertAlmostEqual(AL.limit_pct("600000", is_st=True), 0.05)


class ComputeListTests(unittest.TestCase):
    def test_mixed_universe(self):
        pts = [
            _pt("600000", 10.5, 11.0, 10.0),                    # 主板 10% 非一字
            _pt("600001", 11.0, 11.0, 10.0),                    # 主板一字（排除）
            _pt("300001", 11.5, 12.0, 10.0),                    # 创业板 20%
            _pt("600002", 5.2, 5.25, 5.0, is_st=True),          # ST 5%
            _pt("600003", 10.1, 10.3, 10.0),                    # 未涨停
            _pt("600004", None, None, 10.0, status="SUSPENDED"),  # 停牌跳过
        ]
        r = AL.compute_limitup_list(pts)
        self.assertEqual(r["codes"], ["600000", "300001", "600002"])
        self.assertEqual(r["count"], 4)
        self.assertEqual(r["yizi_count"], 1)
        self.assertEqual(r["traded"], 5)
        # details 含一字板（对账用）
        detail_codes = [d["code"] for d in r["details"]]
        self.assertIn("600001", detail_codes)

    def test_tolerance_boundary(self):
        # +9.95%（主板）在 10%-0.001 容差内 → 判涨停
        r = AL.compute_limitup_list([_pt("600000", 10.0, 10.995, 10.0)])
        self.assertEqual(r["count"], 1)

    def test_empty(self):
        r = AL.compute_limitup_list([])
        self.assertEqual(r["codes"], [])
        self.assertEqual(r["count"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
