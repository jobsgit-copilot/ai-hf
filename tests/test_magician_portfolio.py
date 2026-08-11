#!/usr/bin/env python3
"""magician_portfolio.py 回归测试（simulate/summarize_portfolio 纯逻辑，不读数据缓存）。

运行： python -m pytest tests/test_magician_portfolio.py
"""

import os
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import magician_portfolio as MP  # noqa: E402


def make_arrays(code="TEST.SH", n=200, end="2026-06-30", price=100.0):
    """构造 n 根业务日 K 线数组（事件日 2026-01-05 位于序列中部）。"""
    dates = pd.date_range(end=end, periods=n, freq="B")
    close = np.full(n, price, dtype=float)
    high = close + 1.0
    low = close - 1.0
    return {code: (dates.to_numpy(dtype="datetime64[ns]"),
                   (close - 0.5).astype(float), high.astype(float), low.astype(float),
                   close.astype(float), np.full(n, 2e8, dtype=float), np.full(n, 1e6, dtype=float))}


def ramp_after(code, arrays, date="2026-01-05", up=True):
    """事件日收盘=100，其后每日涨/跌 1%（up=True 涨、False 跌）。"""
    dates, o, h, l, c, vol, amt = arrays[code]
    i = int(np.searchsorted(dates, np.datetime64(date)))
    c[i] = 100.0
    for k in range(i + 1, len(dates)):
        o[k] = c[k - 1] * (1.01 if up else 0.99)
        c[k] = c[k - 1] * (1.01 if up else 0.99)
        h[k] = c[k] * 1.005
        l[k] = c[k] * (0.995 if up else 0.99)


def base_event(code="TEST.SH", date="2026-01-05", entry=100.0, pivot=100.0):
    return {"code": code, "date": date, "status": "breakout", "entry": entry,
            "pivot": pivot, "base_low": 93.0, "depths": [20.0, 12.0, 6.0],
            "n_contractions": 3, "stage": "stage2", "rs": 80.0,
            "volume_dry": True, "breakout_volume_confirmed": True,
            "funnel": {"F0": True, "F1": True, "F2": False}, "regime": None}


def dummy_event(date="2026-06-01"):
    """不通过 F1 漏斗的事件：只用于把事件日历延长到目标/止损触发窗口。"""
    ev = base_event(code="DUMMY.SH", date=date)
    ev["funnel"]["F1"] = False
    return ev


CFG = {"stop_pct": 7, "rr": 3, "min_contractions": 2, "rs_min": 0,
       "require_stage2": True, "entry": "breakout", "max_ext": 0.15,
       "require_brv": False, "require_dry": True}


class TestSimulate(unittest.TestCase):
    def test_target_win(self):
        arrays = make_arrays()
        ramp_after("TEST.SH", arrays, up=True)  # 止损93/目标121，约20日触达
        res = MP.simulate([base_event(), dummy_event()], arrays, CFG, funnel_level="F1")
        self.assertEqual(res["n_trades"], 1)
        self.assertEqual(res["trades"][0]["reason"], "target")
        self.assertAlmostEqual(res["trades"][0]["outcome_pct"], 21.0, delta=0.01)
        self.assertGreater(res["final"], 1.0)

    def test_stop_loss(self):
        arrays = make_arrays()
        ramp_after("TEST.SH", arrays, up=False)  # 下跌 1%/日，约8日触发止损93
        res = MP.simulate([base_event(), dummy_event()], arrays, CFG, funnel_level="F1")
        self.assertEqual(res["n_trades"], 1)
        self.assertEqual(res["trades"][0]["reason"], "stop")
        self.assertAlmostEqual(res["trades"][0]["outcome_pct"], -7.0, delta=0.01)
        self.assertLess(res["final"], 1.0)

    def test_funnel_filters_event(self):
        arrays = make_arrays()
        ramp_after("TEST.SH", arrays, up=True)
        ev = base_event()
        ev["funnel"]["F1"] = False  # 只过 F0，F1 漏斗下应被排除
        res = MP.simulate([ev], arrays, CFG, funnel_level="F1")
        self.assertEqual(res["n_trades"], 0)
        self.assertEqual(res["final"], 1.0)

    def test_lockout_skips_reentry(self):
        arrays = make_arrays()
        ramp_after("TEST.SH", arrays, up=False)  # 第一笔约8日止损出场
        ev1 = base_event(date="2026-01-05")
        ev2 = base_event(date="2026-02-05")  # 间隔<80交易日
        res = MP.simulate([ev1, ev2, dummy_event()], arrays, CFG, funnel_level="F1")
        self.assertEqual(res["n_trades"], 1)
        self.assertGreaterEqual(res["skipped_lockout"], 1)

    def test_capacity_skips_second_position(self):
        arrays = make_arrays("TEST.SH")
        arrays.update(make_arrays("TEST2.SH"))
        for code in arrays:
            ramp_after(code, arrays, up=True)
        ev1 = base_event("TEST.SH")
        ev2 = base_event("TEST2.SH")
        res = MP.simulate([ev1, ev2], arrays, CFG, funnel_level="F1", max_positions=1)
        self.assertEqual(res["n_trades"], 1)
        self.assertGreaterEqual(res["skipped_capacity"], 1)

    def test_regime_coefficient_halves_exposure(self):
        arrays = make_arrays()
        ramp_after("TEST.SH", arrays, up=True)
        ev = base_event()
        ev["regime"] = "down"
        ev["idx_above_ma200"] = False
        res0 = MP.simulate([ev], arrays, CFG, funnel_level="F1", regime_mode=0)
        res1 = MP.simulate([ev], arrays, CFG, funnel_level="F1", regime_mode=1)
        self.assertAlmostEqual(res1["avg_exposure"], res0["avg_exposure"] / 2, delta=1.0)


class TestSummarizePortfolio(unittest.TestCase):
    def test_empty(self):
        eq = pd.Series([1.0, 1.01, 1.0], index=pd.to_datetime(["2026-01-05", "2026-02-05", "2026-03-05"]))
        s = MP.summarize_portfolio(eq, [], [0.0], 0, 0, CFG, "F1")
        self.assertEqual(s["n_trades"], 0)
        self.assertIsNone(s["win_rate"])
        self.assertIsNone(s["expectancy"])

    def test_stats(self):
        eq = pd.Series([1.0, 1.02, 1.01, 1.05], index=pd.to_datetime(["2026-01-05", "2026-02-05", "2026-03-05", "2026-04-05"]))
        trades = [{"outcome_pct": 21.0, "hit20": True}, {"outcome_pct": -7.0, "hit20": False}]
        s = MP.summarize_portfolio(eq, trades, [0.2, 0.2, 0.2], 0, 0, CFG, "F1")
        self.assertEqual(s["n_trades"], 2)
        self.assertEqual(s["win_rate"], 50.0)
        self.assertAlmostEqual(s["avg_win"], 21.0)
        self.assertAlmostEqual(s["avg_loss"], -7.0)
        self.assertAlmostEqual(s["expectancy"], 7.0)
        self.assertEqual(s["hit20"], 50.0)
        self.assertAlmostEqual(s["avg_exposure"], 20.0)


if __name__ == "__main__":
    unittest.main()