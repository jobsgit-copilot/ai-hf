#!/usr/bin/env python3
"""magician_data.py 回归测试（纯计算逻辑，不依赖网络/xtquant）。

运行： python tests/test_magician_data.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import magician_data as M  # noqa: E402


def make_bars(n=320, start=100.0, step=0.5):
    """构造 n 根单调上涨的日线（open/high/low/close 同步上行）。"""
    bars = []
    for i in range(n):
        base = start + step * i
        bars.append({
            "date": f"2025-{i % 12 + 1:02d}-{i % 28 + 1:02d}",
            "open": round(base - 0.2, 3),
            "high": round(base + 0.5, 3),
            "low": round(base - 0.5, 3),
            "close": round(base, 3),
            "volume": 100000,
            "amount": 1e7,
        })
    return bars


class TestNormalizeCode(unittest.TestCase):
    def test_suffix_inference(self):
        self.assertEqual(M.normalize_code("600519"), "600519.SH")
        self.assertEqual(M.normalize_code("300308"), "300308.SZ")
        self.assertEqual(M.normalize_code("688001"), "688001.SH")
        self.assertEqual(M.normalize_code("830799"), "830799.BJ")
        self.assertEqual(M.normalize_code("000001.SZ"), "000001.SZ")
        self.assertEqual(M.normalize_code("600519SH"), "600519.SH")


class TestTrendRules(unittest.TestCase):
    def test_uptrend_passes_all(self):
        bars = make_bars()
        m = M.compute_indicators(bars, len(bars) - 1)
        m["rs"] = 85.0
        rules, passed = M.trend_rules(m)
        self.assertEqual(passed, 8, [r["detail"] for r in rules if not r["passed"]])
        self.assertEqual(M.judge_stage(m), "stage2")

    def test_downtrend_fails(self):
        bars = make_bars(320, start=300.0, step=-0.6)
        m = M.compute_indicators(bars, len(bars) - 1)
        m["rs"] = 20.0
        rules, passed = M.trend_rules(m)
        self.assertLess(passed, 4)
        self.assertEqual(M.judge_stage(m), "stage4")

    def test_insufficient_history(self):
        bars = make_bars(n=100)
        m = M.compute_indicators(bars, len(bars) - 1)
        self.assertIsNone(m["ma200"])
        rules, passed = M.trend_rules(m)
        self.assertLess(passed, 8)
        self.assertFalse(rules[1]["passed"])

    def test_rs_threshold(self):
        bars = make_bars()
        m = M.compute_indicators(bars, len(bars) - 1)
        m["rs"] = 65.0
        rules, passed = M.trend_rules(m)
        self.assertFalse(rules[7]["passed"])
        self.assertEqual(passed, 7)
        stage = M.judge_stage(m)
        verdict, reason = M.make_verdict(stage, passed)
        self.assertEqual(verdict, "watch")

    def test_verdict_non_stage2(self):
        bars = make_bars(320, start=300.0, step=-0.6)
        m = M.compute_indicators(bars, len(bars) - 1)
        m["rs"] = 90.0
        rules, passed = M.trend_rules(m)
        stage = M.judge_stage(m)
        verdict, reason = M.make_verdict(stage, passed)
        self.assertEqual(verdict, "fail")


if __name__ == "__main__":
    unittest.main()
