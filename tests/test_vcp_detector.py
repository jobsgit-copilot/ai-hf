#!/usr/bin/env python3
"""vcp_detector.py 回归测试（纯计算逻辑，不依赖网络）。

运行： python tests/test_vcp_detector.py
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import vcp_detector as V  # noqa: E402


def make_base_bars(runup=160, base=60, first_drop=0.30, vol0=2_000_000):
    """构造：上涨 runup 根 → 基底 base 根，内含 3 次深度递减的收缩。

    收缩结构（深度相对中枢点）：first_drop → first_drop*0.5 → first_drop*0.25，
    每段之间反弹至接近中枢点；量能从 vol0 逐段减半。
    """
    bars = []
    base_end = 20 + runup * 0.3  # 中枢点价位（上涨段终点）
    for i in range(runup):
        c = 20 + i * 0.3
        bars.append({"date": f"2025{(i % 12) + 1:02d}{i % 28 + 1:02d}", "open": c - 0.1,
                     "high": c + 0.2, "low": c - 0.2, "close": c, "volume": vol0, "amount": 1e7})
    depths = [first_drop, first_drop * 0.5, first_drop * 0.25]
    n_seg = base // 3
    vol = vol0
    di_global = 0
    for di, d in enumerate(depths):
        start = base_end * 0.99  # 反弹回到中枢点附近
        trough = start * (1 - d)
        steps = max(2, n_seg // 2)
        for s in range(steps):  # 下跌至 trough
            c = start + (trough - start) * (s + 1) / steps
            bars.append({"date": f"2026{di + 1:02d}{s + 1:02d}", "open": c + 0.1,
                         "high": max(c + 0.3, start), "low": c - 0.3, "close": c,
                         "volume": int(vol), "amount": 1e7})
        for s in range(n_seg - steps):  # 反弹回中枢点附近
            c = trough + (start - trough) * (s + 1) / (n_seg - steps)
            bars.append({"date": f"2026{di + 1:02d}{s + 10:02d}", "open": c - 0.1,
                         "high": c + 0.2, "low": c - 0.2, "close": c,
                         "volume": int(vol), "amount": 1e7})
        vol = int(vol * 0.5)
        di_global += 1
    # 最后一天：接近但未突破中枢点
    bars.append({"date": "20261231", "open": base_end * 0.98, "high": base_end * 0.99,
                 "low": base_end * 0.96, "close": base_end * 0.98,
                 "volume": int(vol * 0.3), "amount": 1e7})
    return bars


def make_downtrend_bars(n=220):
    """构造：持续下跌，回调深度逐次加深（应为非 VCP）。"""
    bars = []
    for i in range(n):
        c = 200 - i * 0.8
        bars.append({"date": f"2025{i % 12 + 1:02d}{i % 28 + 1:02d}", "open": c + 0.1,
                     "high": c + 0.5, "low": c - 0.5, "close": c,
                     "volume": 1_000_000, "amount": 1e7})
    return bars


class TestVcpDetect(unittest.TestCase):
    def test_recognizes_contracting_base(self):
        bars = make_base_bars()
        r = V.analyze_vcp(bars, window_days=60, min_contractions=2)
        self.assertTrue(r["has_vcp"], r["reasons"])
        self.assertGreaterEqual(len(r["depths"]), 2)
        self.assertTrue(r["volume_dry"])
        self.assertIn(r["status"], ("setup", "forming", "breakout"))
        self.assertIsNotNone(r["footprint"])
        self.assertEqual(r["stop_loss"], round(max(r["pivot"] * 0.90, r["base_low"]), 3))

    def test_rejects_widening_pullbacks(self):
        bars = make_downtrend_bars()
        r = V.analyze_vcp(bars, window_days=60, min_contractions=2)
        self.assertFalse(r["has_vcp"])
        self.assertEqual(r["status"], "none")

    def test_breakout_status(self):
        bars = make_base_bars()
        bars.pop()  # 去掉最后一根（接近中枢点）
        pivot = max(b["high"] for b in bars[-60:])
        bars.append({"date": "20270105", "open": pivot * 1.01, "high": pivot * 1.04,
                     "low": pivot * 0.99, "close": pivot * 1.02,
                     "volume": 5_000_000, "amount": 1e7})
        r = V.analyze_vcp(bars, window_days=60, min_contractions=2)
        self.assertTrue(r["has_vcp"], r["reasons"])
        self.assertEqual(r["status"], "breakout")
        self.assertTrue(r["breakout_volume_confirmed"])


    def test_insufficient_contractions(self):
        bars = make_base_bars(first_drop=0.05)  # 收缩太浅，多数被深度下限过滤
        r = V.analyze_vcp(bars, window_days=60, min_contractions=2)
        self.assertFalse(r["has_vcp"])

    def test_swing_points(self):
        bars = make_base_bars()
        lows = V.swing_lows(bars[-60:])
        self.assertGreaterEqual(len(lows), 2)


if __name__ == "__main__":
    unittest.main()
