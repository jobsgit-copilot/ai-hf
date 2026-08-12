#!/usr/bin/env python3
"""magician_fundamental.py 规则逻辑测试（不读实际大 pickle）。"""

import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'tools'))

import magician_fundamental as MF  # noqa: E402


def snap(**kw):
    base = {"code": "000001.SZ", "date": "2026-01-05", "has_ind": True,
            "roe_latest": 20.0, "roe_annual_avg": 20.0, "gpm_avg": 30.0,
            "dta_latest": 40.0, "ocf_med": 1.2, "has_inc": True,
            "rev_yoy": 25.0, "rev_yoy_prev": 20.0, "is_st": False,
            "delisted": False, "pb_pct": 50.0}
    base.update(kw)
    return base


class TestRules(unittest.TestCase):
    def test_all_pass(self):
        r = MF.apply_rules(snap())
        self.assertEqual(r["r_st"], "pass")
        self.assertEqual(r["r_debt"], "pass")
        self.assertEqual(r["r_ocf"], "pass")
        self.assertEqual(r["r_gpm"], "pass")
        self.assertEqual(r["r_roe"], "pass")
        self.assertEqual(r["g_rev"], "pass")
        self.assertEqual(r["g_accel"], "pass")
        self.assertEqual(r["v_pb"], "pass")

    def test_quality_failures(self):
        self.assertEqual(MF.apply_rules(snap(is_st=True))["r_st"], "fail")
        self.assertEqual(MF.apply_rules(snap(dta_latest=90.0))["r_debt"], "fail")
        self.assertEqual(MF.apply_rules(snap(ocf_med=0.2))["r_ocf"], "fail")
        self.assertEqual(MF.apply_rules(snap(gpm_avg=10.0))["r_gpm"], "fail")
        self.assertEqual(MF.apply_rules(snap(roe_annual_avg=5.0))["r_roe"], "fail")

    def test_rule_limits_override(self):
        # 阈值滑动：负债率上限 85→90 放行 88
        r = MF.apply_rules(snap(dta_latest=88.0), limits={"debt_max": 90.0})
        self.assertEqual(r["r_debt"], "pass")
        self.assertEqual(MF.apply_rules(snap(dta_latest=88.0))["r_debt"], "fail")
        # ROE 下限 8→6 放行 7
        r2 = MF.apply_rules(snap(roe_annual_avg=7.0), limits={"roe_min": 6.0})
        self.assertEqual(r2["r_roe"], "pass")

    def test_missing_data_treated_as_pass_for_quality(self):
        r = MF.apply_rules(snap(dta_latest=None, ocf_med=None, gpm_avg=None, roe_annual_avg=None))
        for k in ("r_debt", "r_ocf", "r_gpm", "r_roe"):
            self.assertEqual(r[k], "pass", k)

    def test_growth_requires_data(self):
        self.assertEqual(MF.apply_rules(snap(rev_yoy=None))["g_rev"], "fail")
        self.assertEqual(MF.apply_rules(snap(rev_yoy=10.0))["g_rev"], "fail")
        self.assertEqual(MF.apply_rules(snap(rev_yoy=25.0, rev_yoy_prev=None))["g_accel"], "fail")


class TestFunnelLevel(unittest.TestCase):
    def test_levels(self):
        good = MF.apply_rules(snap())
        self.assertTrue(MF.funnel_level(good)["F3"])
        self.assertTrue(MF.funnel_level(good)["F4"])

    def test_growth_gate(self):
        r = MF.apply_rules(snap(rev_yoy=5.0))
        f = MF.funnel_level(r)
        self.assertTrue(f["F1"])
        self.assertFalse(f["F2"])
        self.assertFalse(f["F3"])

    def test_quality_gate(self):
        r = MF.apply_rules(snap(roe_annual_avg=3.0))
        f = MF.funnel_level(r)
        self.assertFalse(f["F1"])


class TestRevAt(unittest.TestCase):
    def test_rev_at(self):
        gi = {"ann": np.array(["2025-04-25", "2025-10-25"], dtype="datetime64[ns]"),
              "end": np.array(["2025-03-31", "2025-09-30"], dtype="datetime64[ns]"),
              "rev": np.array([100.0, 120.0])}
        v = MF.FundamentalDB._rev_at(gi, np.datetime64("2025-03-31"), np.datetime64("2025-05-01"))
        self.assertEqual(v, 100.0)
        # 同期但未披露（ann 晚于查询日）→ None
        v2 = MF.FundamentalDB._rev_at(gi, np.datetime64("2025-03-31"), np.datetime64("2025-01-01"))
        self.assertIsNone(v2)


if __name__ == "__main__":
    unittest.main()
