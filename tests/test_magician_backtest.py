#!/usr/bin/env python3
"""magician_backtest.py 回归测试（measure/summarize 纯逻辑，不读数据缓存）。

运行： python tests/test_magician_backtest.py
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import magician_backtest as MB  # noqa: E402


def make_arrays(code="TEST.SH", n=600, end="2026-04-01", price=50.0):
    """构造 n 根业务日 K 线数组（事件日 2026-01-05 位于序列中部）。"""
    dates = pd.date_range(end=end, periods=n, freq="B")
    idx = np.arange(n, dtype=float)
    close = price + idx * 0.2
    high = close + 1.0
    low = close - 1.0
    return {code: (dates.to_numpy(dtype="datetime64[ns]"),
                   (close - 0.3).astype(float), high.astype(float), low.astype(float),
                   close.astype(float), np.full(n, 2e8, dtype=float), np.full(n, 1e6, dtype=float))}


def base_event(code="TEST.SH", date="2026-01-05", entry=100.0, pivot=100.0):
    return {"code": code, "date": date, "status": "breakout", "entry": entry,
            "pivot": pivot, "base_low": 93.0, "depths": [20.0, 12.0, 6.0],
            "n_contractions": 3, "footprint": "12W 20/6 3T", "stage": "stage2", "rs": 80.0}


CFG = {"stop_pct": 7, "rr": 3, "min_contractions": 2, "rs_min": 0,
       "require_stage2": True, "entry": "breakout", "max_ext": 0.15}


class TestMeasure(unittest.TestCase):
    def test_win_target(self):
        arrays = make_arrays()
        code, (dates, o, h, l, c, vol, amt) = list(arrays.items())[0]
        i = int(np.searchsorted(dates, np.datetime64("2026-01-05")))
        c[i] = 100.0  # 事件日收盘 = 入场价
        for k in range(i + 1, len(dates)):  # 事件日后一路上涨
            o[k] = c[k - 1] * 1.01
            c[k] = c[k - 1] * 1.01
            h[k] = c[k] * 1.005
            l[k] = c[k] * 0.995
        ev = base_event()
        trades, s = MB.measure([ev], arrays, CFG)
        self.assertEqual(len(trades), 1)
        self.assertGreater(trades[0]["outcome_pct"], 0)
        self.assertTrue(trades[0]["hit20_before_stop"])

    def test_stop_loss(self):
        arrays = make_arrays()
        code, (dates, o, h, l, c, vol, amt) = list(arrays.items())[0]
        i = int(np.searchsorted(dates, np.datetime64("2026-01-05")))
        c[i] = 100.0  # 事件日收盘 = 入场价
        for k in range(i + 1, len(dates)):  # 事件日后持续下跌
            o[k] = c[k - 1] * 0.99
            c[k] = c[k - 1] * 0.99
            h[k] = c[k] * 1.001
            l[k] = c[k] * 0.99
        ev = base_event()
        trades, s = MB.measure([ev], arrays, CFG)
        self.assertEqual(len(trades), 1)
        self.assertLess(trades[0]["outcome_pct"], 0)
        self.assertAlmostEqual(trades[0]["outcome_pct"], -7.0, delta=0.01)

    def test_lockout_dedupe(self):
        arrays = make_arrays(end="2026-06-01")
        ev1 = base_event(date="2026-01-05")
        ev2 = base_event(date="2026-02-01")  # 与 ev1 间隔 < LOCKOUT
        trades, s = MB.measure([ev1, ev2], arrays, CFG)
        self.assertEqual(len(trades), 1)

    def test_extension_filter(self):
        arrays = make_arrays()
        ev = base_event(entry=120.0, pivot=100.0)  # 已超中枢点 20%
        trades, s = MB.measure([ev], arrays, CFG)
        self.assertEqual(len(trades), 0)

    def test_stage2_filter(self):
        arrays = make_arrays()
        ev = base_event()
        ev["stage"] = "stage4"
        trades, s = MB.measure([ev], arrays, CFG)
        self.assertEqual(len(trades), 0)

    def test_summarize_empty(self):
        s = MB.summarize([])
        self.assertEqual(s["n"], 0)
        self.assertIsNone(s["expectancy_pct"])

    def test_breakout_volume_filter(self):
        arrays = make_arrays()
        ev = base_event()
        ev["breakout_volume_confirmed"] = False
        cfg = dict(CFG, require_brv=True)
        trades, s = MB.measure([ev], arrays, cfg)
        self.assertEqual(len(trades), 0)

    def test_volume_dry_filter(self):
        arrays = make_arrays()
        ev = base_event()
        ev["volume_dry"] = False
        cfg = dict(CFG, require_dry=True)
        trades, s = MB.measure([ev], arrays, cfg)
        self.assertEqual(len(trades), 0)

    def test_volume_filters_pass_when_ok(self):
        arrays = make_arrays()
        ev = base_event()
        ev["volume_dry"] = True
        ev["breakout_volume_confirmed"] = True
        cfg = dict(CFG, require_brv=True, require_dry=True)
        trades, s = MB.measure([ev], arrays, cfg)
        self.assertEqual(len(trades), 1)


if __name__ == "__main__":
    unittest.main()
