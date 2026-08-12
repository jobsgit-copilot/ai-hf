#!/usr/bin/env python3
"""magician_fundamental.py — P1 基本面漏斗：点在时间快照 + 回测裁量边际增益

用法：
    python magician_fundamental.py funnel --events <events.json> --cache <live_cache.pkl> \
        --indicator finance_indicator.pkl --income finance_income.pkl \
        --basic stock_basic.pkl --pb finance_pb_weekly.pkl \
        --stops 7 --rrs 2 3 --mcs 2 3 --rs-min-list 0 90 --dry 0 1 --out report.md

规则源：magician-growth-fundamental（营收同比/加速）+ quality-screen 去劣 7 条的可量化部分
（负债率/现金流/毛利率/ST）。所有条件按 ann_date 做 point-in-time 对齐，禁止未来函数。
"""

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import magician_backtest as MB  # noqa: E402

DEFAULT_SQUEEZE = r"D:\Users\Documents\AI-Finance\squeeze"
RULE_DEBT_MAX = 85.0       # 资产负债率上限（%）
RULE_OCF_MIN = 0.7         # 经营现金流/净利润下限
RULE_GPM_MIN = 15.0        # 长期毛利率下限（%）
RULE_ROE_MIN = 8.0         # ROE 下限（%，年度均值）
RULE_REV_YOY_MIN = 15.0    # 营收同比下限（%）
RULE_NP_YOY_MIN = 25.0    # 归母净利润同比下限（%，第三梯队）
RULE_PB_PCT_MAX = 90.0     # PB 自身历史分位上限（%）


def _norm_code(c):
    c = str(c)
    if "." in c:
        return c.upper()
    return c


class FundamentalDB:
    """全市场 point-in-time 财务快照：按代码预处理，事件时等价查询。"""

    def __init__(self, indicator_pkl, income_pkl, basic_pkl, pb_pkl=None):
        t0 = time.time()
        ind = pd.read_pickle(indicator_pkl)
        inc = pd.read_pickle(income_pkl)
        if "n_income_attr_p" not in inc.columns:
            inc["n_income_attr_p"] = np.nan
        basic = pd.read_pickle(basic_pkl)
        self._basic = {}
        for r in basic.itertuples(index=False):
            name = str(getattr(r, "name") or "")
            self._basic[_norm_code(r.ts_code)] = {
                "is_st": "ST" in name.upper(),
                "delisted": pd.notna(getattr(r, "delist_date", None)),
            }

        ind["ts_code"] = ind["ts_code"].map(_norm_code)
        ind["ann_date"] = pd.to_datetime(ind["ann_date"])
        ind["end_date"] = pd.to_datetime(ind["end_date"])
        ind = ind.drop_duplicates(["ts_code", "end_date", "ann_date"]).sort_values(["ts_code", "ann_date", "end_date"])
        self._ind = {}
        for c, g in ind.groupby("ts_code", sort=False):
            self._ind[c] = {
                "ann": g["ann_date"].to_numpy(dtype="datetime64[ns]"),
                "roe": g["roe"].to_numpy(dtype=float),
                "gpm": g["grossprofit_margin"].to_numpy(dtype=float),
                "dta": g["debt_to_assets"].to_numpy(dtype=float),
                "ocf": g["ocf_to_profit"].to_numpy(dtype=float),
                "end": g["end_date"].to_numpy(dtype="datetime64[ns]"),
            }

        inc["ts_code"] = inc["ts_code"].map(_norm_code)
        inc["ann_date"] = pd.to_datetime(inc["ann_date"])
        inc["end_date"] = pd.to_datetime(inc["end_date"].astype(str).str[:8], format="%Y%m%d")
        inc = inc.drop_duplicates(["ts_code", "end_date", "ann_date"]).sort_values(["ts_code", "end_date", "ann_date"])
        self._inc = {}
        for c, g in inc.groupby("ts_code", sort=False):
            self._inc[c] = {
                "ann": g["ann_date"].to_numpy(dtype="datetime64[ns]"),
                "end": g["end_date"].to_numpy(dtype="datetime64[ns]"),
                "rev": g["revenue"].to_numpy(dtype=float),
                "np": g["n_income_attr_p"].to_numpy(dtype=float),
            }

        # 报表期映射：_prev=上一期(本财年前一期)，_yoy=去年同期(同月同日)
        for c, gi in self._inc.items():
            ends = np.unique(gi["end"])
            prev, yoy = {}, {}
            for e in ends:
                pos = int(np.searchsorted(ends, e, side="left")) - 1
                prev[e] = ends[pos] if pos >= 0 else None
                dt = pd.Timestamp(e)
                target = np.datetime64(pd.Timestamp(dt.year - 1, dt.month, dt.day))
                m = np.nonzero(ends == target)[0]
                yoy[e] = ends[m[0]] if len(m) else None
            gi["_prev"] = prev
            gi["_yoy"] = yoy

        self._pb = None
        if pb_pkl:
            pb = pd.read_pickle(pb_pkl)
            pb["ts_code"] = pb["ts_code"].map(_norm_code)
            pb["trade_date"] = pd.to_datetime(pb["trade_date"])
            pb = pb.drop_duplicates(["ts_code", "trade_date"]).sort_values(["ts_code", "trade_date"])
            self._pb = {}
            for c, g in pb.groupby("ts_code", sort=False):
                self._pb[c] = {"date": g["trade_date"].to_numpy(dtype="datetime64[ns]"),
                               "pb": g["pb"].to_numpy(dtype=float)}
        print(f"财务快照库构建完成：{len(self._ind)}只指标 / "
              f"{len(self._inc)}只收入 / {len(self._basic)}只基本面（{time.time()-t0:.1f}s）")

    def snapshot(self, code, date):
        d = np.datetime64(date)
        out = {"code": code, "date": str(date)[:10],
               "has_ind": False, "roe_latest": None, "roe_annual_avg": None,
               "gpm_avg": None, "dta_latest": None, "ocf_med": None,
               "has_inc": False, "rev_yoy": None, "rev_yoy_prev": None,
               "np_yoy": None, "np_yoy_prev": None,
               "sq_rev_yoy": None, "sq_rev_yoy_prev": None,
               "sq_np_yoy": None, "sq_np_yoy_prev": None,
               "is_st": False, "delisted": False, "pb_pct": None}

        b = self._basic.get(code)
        if b:
            out["is_st"] = b["is_st"]
            out["delisted"] = b["delisted"]

        g = self._ind.get(code)
        if g is not None:
            idx = int(np.searchsorted(g["ann"], d, side="right") - 1)
            if idx >= 0:
                n = idx + 1
                with np.errstate(invalid="ignore"):
                    out["has_ind"] = True
                    out["roe_latest"] = float(g["roe"][idx]) if g["roe"][idx] == g["roe"][idx] else None
                    ann_mask = g["end"][:n].astype("datetime64[M]").astype(int) % 12 == 11
                    ann_roe = g["roe"][:n][ann_mask]
                    out["roe_annual_avg"] = float(np.nanmean(ann_roe)) if np.isfinite(ann_roe).any() else None
                    out["gpm_avg"] = float(np.nanmean(g["gpm"][:n])) if np.isfinite(g["gpm"][:n]).any() else None
                    out["dta_latest"] = float(g["dta"][idx]) if g["dta"][idx] == g["dta"][idx] else None
                    ocf = g["ocf"][:n]
                    out["ocf_med"] = float(np.nanmedian(ocf)) if np.isfinite(ocf).any() else None

        gi = self._inc.get(code)
        if gi is not None:
            ann, end = gi["ann"], gi["end"]
            cand = np.nonzero(ann <= d)[0]
            if len(cand):
                # 最新已披露报表期
                li = cand[np.argmax(end[cand])]
                out["has_inc"] = True
                last_end = end[li]
                # 用报表期映射取“上一期”与“去年同期”，避免日期减法漂移（闰年/年报期）
                prev = gi["_prev"].get(last_end) if "_prev" in gi else None
                yoy_end = gi["_yoy"].get(last_end) if "_yoy" in gi else None
                prev_yoy = gi["_yoy"].get(prev) if (prev is not None and "_yoy" in gi) else None
                out["rev_yoy"] = self._pct_yoy(gi, last_end, yoy_end, d, "rev")
                out["rev_yoy_prev"] = self._pct_yoy(gi, prev, prev_yoy, d, "rev")
                out["np_yoy"] = self._pct_yoy(gi, last_end, yoy_end, d, "np")
                out["np_yoy_prev"] = self._pct_yoy(gi, prev, prev_yoy, d, "np")
                # 单季口径同比（第四梯队叠加）：单季值 = 该期累计 - 同财年上一期累计
                out["sq_rev_yoy"] = self._sq_yoy(gi, last_end, yoy_end, d, "rev")
                out["sq_rev_yoy_prev"] = self._sq_yoy(gi, prev, prev_yoy, d, "rev")
                out["sq_np_yoy"] = self._sq_yoy(gi, last_end, yoy_end, d, "np")
                out["sq_np_yoy_prev"] = self._sq_yoy(gi, prev, prev_yoy, d, "np")

        gp = self._pb.get(code) if self._pb else None
        if gp is not None:
            idx = int(np.searchsorted(gp["date"], d, side="right") - 1)
            if idx >= 0:
                cur = gp["pb"][idx]
                if cur == cur:
                    hist = gp["pb"][:idx + 1]
                    hist = hist[np.isfinite(hist)]
                    if len(hist):
                        out["pb_pct"] = float((hist < cur).mean() * 100)
        return out

    @staticmethod
    def _rev_at(gi, end, d):
        end_dates = gi["end"]
        m = np.nonzero(end_dates == end)[0]
        for i in m:
            if gi["ann"][i] <= d:
                v = gi["rev"][i]
                return float(v) if v == v else None
        return None

    @staticmethod
    def _pct_yoy(gi, cur, yoy, d, kind):
        """cur 期相对去年同期(同月同日)的同比增幅%；任一端缺失/去年基数非正返回 None。"""
        if cur is None or yoy is None:
            return None
        fn = FundamentalDB._rev_at if kind == "rev" else FundamentalDB._np_at
        vc = fn(gi, cur, d)
        vb = fn(gi, yoy, d)
        if vc is None or vb is None or vb <= 0:
            return None
        return float((vc / vb - 1) * 100)

    @staticmethod
    def _sq_at(gi, end, d, kind):
        """单季值 = 该期累计值 - 同财年上一期累计值；一季报(3月)无同财年上一期，直接取累计。"""
        fn = FundamentalDB._rev_at if kind == "rev" else FundamentalDB._np_at
        cum = fn(gi, end, d)
        if cum is None:
            return None
        if pd.Timestamp(end).month == 3:
            return cum
        prev = gi["_prev"].get(end)
        if prev is None:
            return None
        prev_cum = fn(gi, prev, d)
        if prev_cum is None:
            return None
        return cum - prev_cum

    @staticmethod
    def _sq_yoy(gi, cur, yoy, d, kind):
        """单季口径同比增幅%；去年单季基数非正返回 None。"""
        if cur is None or yoy is None:
            return None
        vc = FundamentalDB._sq_at(gi, cur, d, kind)
        vb = FundamentalDB._sq_at(gi, yoy, d, kind)
        if vc is None or vb is None or vb <= 0:
            return None
        return float((vc / vb - 1) * 100)

    @staticmethod
    def _np_at(gi, end, d):
        end_dates = gi["end"]
        m = np.nonzero(end_dates == end)[0]
        for i in m:
            if gi["ann"][i] <= d:
                v = gi["np"][i]
                return float(v) if v == v else None
        return None


def apply_rules(snap, limits=None):
    """返回各规则判定：pass/fail/na。质量红线 na=放行；成长条件 na=不通过。

    limits 可选覆盖阈值：{"debt_max": 90.0, "ocf_min": 0.5, "gpm_min": 10.0,
    "roe_min": 6.0, "rev_yoy_min": 15.0, "pb_pct_max": 90.0}（用于第二梯队阈值扫描）。
    """
    L = {"debt_max": RULE_DEBT_MAX, "ocf_min": RULE_OCF_MIN, "gpm_min": RULE_GPM_MIN,
         "roe_min": RULE_ROE_MIN, "rev_yoy_min": RULE_REV_YOY_MIN,
         "np_yoy_min": RULE_NP_YOY_MIN, "pb_pct_max": RULE_PB_PCT_MAX}
    if limits:
        L.update(limits)
    rules = {}
    rules["r_st"] = "pass" if not (snap["is_st"] or snap["delisted"]) else "fail"
    d = snap["dta_latest"]
    rules["r_debt"] = "pass" if d is None or d <= L["debt_max"] else "fail"
    o = snap["ocf_med"]
    rules["r_ocf"] = "pass" if o is None or o >= L["ocf_min"] else "fail"
    m = snap["gpm_avg"]
    rules["r_gpm"] = "pass" if m is None or m >= L["gpm_min"] else "fail"
    r = snap["roe_annual_avg"]
    rules["r_roe"] = "pass" if r is None or r >= L["roe_min"] else "fail"
    y = snap["rev_yoy"]
    rules["g_rev"] = "pass" if y is not None and y >= L["rev_yoy_min"] else "fail"
    yp = snap["rev_yoy_prev"]
    rules["g_accel"] = "pass" if y is not None and yp is not None and y >= yp else "fail"
    ny = snap["np_yoy"]
    rules["g_np"] = "pass" if ny is not None and ny >= L["np_yoy_min"] else "fail"
    # 第四梯队：双成长连续两季度（营收/归母净利润当季与上一季同比均为正）
    rv0, rv1 = snap["rev_yoy"], snap["rev_yoy_prev"]
    np0, np1 = snap["np_yoy"], snap["np_yoy_prev"]
    dual2 = all(v is not None and v > 0 for v in (rv0, rv1, np0, np1))
    rules["g_dual2"] = "pass" if dual2 else "fail"
    rules["g_dual2_15"] = "pass" if all(v is not None and v >= 15.0 for v in (rv0, rv1, np0, np1)) else "fail"
    rules["g_dual1"] = "pass" if all(v is not None and v > 0 for v in (rv0, np0)) else "fail"
    srv0, srv1 = snap["sq_rev_yoy"], snap["sq_rev_yoy_prev"]
    snp0, snp1 = snap["sq_np_yoy"], snap["sq_np_yoy_prev"]
    sqdual2 = all(v is not None and v > 0 for v in (srv0, srv1, snp0, snp1))
    rules["g_sqdual2"] = "pass" if sqdual2 else "fail"
    rules["g_sqdual2_15"] = "pass" if all(v is not None and v >= 15.0 for v in (srv0, srv1, snp0, snp1)) else "fail"
    p = snap["pb_pct"]
    rules["v_pb"] = "pass" if p is None or p <= L["pb_pct_max"] else "fail"
    return rules


def funnel_level(rules):
    """漏斗分级：F0 全量 / F1 质量红线 / F2 +营收同比 / F2N +净利润同比(第三梯队) / F5 双成长 /
    F6 双成长连续2季>0(忽略全部F1) / F6R 同F6保留r_st / F6B 双成长连续2季≥15% / F6C 仅当季双成长>0 /
    F7 F1+单季双成长连续2季>0 / F7B F1+单季双成长连续2季≥15% / F6F1 F1+累计双成长连续2季>0 /
    F3 加速 / F4 PB分位"""
    f1 = all(rules[k] != "fail" for k in ("r_st", "r_debt", "r_ocf", "r_gpm", "r_roe"))
    f2 = f1 and rules["g_rev"] == "pass"
    f2n = f1 and rules["g_np"] == "pass"
    f5 = f2 and rules["g_np"] == "pass"
    f3 = f2 and rules["g_accel"] == "pass"
    f4 = f2 and rules["v_pb"] != "fail"
    f6 = rules["g_dual2"] == "pass"
    f6r = f6 and rules["r_st"] != "fail"
    f6b = rules["g_dual2_15"] == "pass"
    f6c = rules["g_dual1"] == "pass"
    f7 = f1 and rules["g_sqdual2"] == "pass"
    f7b = f1 and rules["g_sqdual2_15"] == "pass"
    f6f1 = f1 and rules["g_dual2"] == "pass"
    return {"F0": True, "F1": f1, "F2": f2, "F2N": f2n, "F5": f5,
            "F6": f6, "F6R": f6r, "F6B": f6b, "F6C": f6c,
            "F7": f7, "F7B": f7b, "F6F1": f6f1, "F3": f3, "F4": f4}


def cmd_funnel(args):
    events = json.loads(Path(args.events).read_text(encoding="utf-8"))
    if args.sector:
        from magician_sector import annotate_events as _annotate_sector
        events = _annotate_sector(events)
        print("板块字段已附加（--sector）")
    df = MB.load_cache(args.cache)
    arrays = MB.build_arrays(df)

    db = FundamentalDB(args.indicator, args.income, args.basic,
                       args.pb if Path(args.pb).exists() else None)
    t0 = time.time()
    n_rule = {"r_st": 0, "r_debt": 0, "r_ocf": 0, "r_gpm": 0, "r_roe": 0,
              "g_rev": 0, "g_accel": 0, "g_np": 0, "v_pb": 0}
    n_snap = 0
    levels = {"F0": 0, "F1": 0, "F2": 0, "F2N": 0, "F5": 0, "F3": 0, "F4": 0}
    for ev in events:
        snap = db.snapshot(ev["code"], ev["date"])
        rules = apply_rules(snap, {"np_yoy_min": args.np_min})
        ev["snap"] = snap
        ev["funnel"] = funnel_level(rules)
        n_snap += 1
        for k in n_rule:
            n_rule[k] += (rules[k] == "pass")
        for k in levels:
            levels[k] += bool(ev["funnel"][k])
    print(f"快照完成：{n_snap} 个事件（{time.time()-t0:.0f}s）")
    print(f"  各规则通过率：" + "  ".join(f"{k}={n_rule[k]}/{n_snap} ({n_rule[k]/max(1,n_snap)*100:.0f}%)" for k in n_rule))
    print(f"  漏斗分级通过：" + "  ".join(f"{k}={v}" for k, v in levels.items()))

    groups = {k: [e for e in events if e["funnel"][k]] for k in ("F0", "F1", "F2", "F2N", "F5", "F3", "F4")}
    configs = []
    for stop_pct in args.stops:
        for rr in args.rrs:
            for mc in args.mcs:
                for rs_min in args.rs_min_list:
                    for brv in args.brv:
                        for dry in args.dry:
                            configs.append({"stop_pct": stop_pct, "rr": rr, "min_contractions": mc,
                                            "rs_min": rs_min, "require_stage2": True,
                                            "entry": "breakout", "max_ext": 0.15,
                                            "require_brv": bool(brv), "require_dry": bool(dry)})

    rows = []
    for cfg in configs:
        label = MB._config_label(cfg)
        for gname, evs in groups.items():
            trades, s = MB.measure(evs, arrays, cfg, horizon=args.horizon)
            rows.append({"level": gname, "config": label, "n": s["n"], "win_rate": s["win_rate"],
                         "avg_win": s["avg_win"], "avg_loss": s["avg_loss"],
                         "expectancy_pct": s["expectancy_pct"], "median_outcome": s["median_outcome"],
                         "hit20_rate": s["hit20_rate"]})

    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    print("\n=== 基本面漏斗回测（按期望降序）===")
    print(f"{'漏斗':<6}{'配置':<38}{'笔数':>6}{'胜率%':>7}{'均盈%':>7}{'均亏%':>7}{'期望%':>8}{'中位%':>8}{'先+20%':>7}")
    for r in sorted(rows, key=lambda x: -(x["expectancy_pct"] or -999)):
        exp = "-" if r["expectancy_pct"] is None else f"{r['expectancy_pct']:>8.2f}"
        print(f"{r['level']:<6}{r['config']:<38}{r['n']:>6}{str(r['win_rate']):>7}{str(r['avg_win']):>7}"
              f"{str(r['avg_loss']):>7}{exp}{str(r['median_outcome']):>8}{str(r['hit20_rate']):>7}")

    if getattr(args, "out_enriched", ""):
        Path(args.out_enriched).write_text(
            json.dumps(events, ensure_ascii=False), encoding="utf-8")
        print(f"富化事件已保存: {args.out_enriched}")

    if args.out:
        out_path = Path(args.out)
        rep = [f"# VCP 策略基本面漏斗回测（P1，{args.start_hint}）",
               "",
               f"> 事件：{len(events)} 个；各规则通过率：" + "  ".join(f"{k}={n_rule[k]}" for k in n_rule),
               f"> 漏斗分级：" + "  ".join(f"{k}={v}" for k, v in levels.items()),
               f"> 规则：质量红线（ST/资产负债率≤{RULE_DEBT_MAX:.0f}%/现金流利润比≥{RULE_OCF_MIN}/毛利率≥{RULE_GPM_MIN:.0f}%/ROE≥{RULE_ROE_MIN:.0f}%）；成长：营收同比≥{RULE_REV_YOY_MIN:.0f}% 且加速；估值：PB自身分位≤{RULE_PB_PCT_MAX:.0f}%（PB 仅 2021+ 有数据）",
               "",
               "| 漏斗 | 配置 | 笔数 | 胜率% | 均盈% | 均亏% | 期望% | 中位% | 先达+20% |",
               "|---|---|---|---|---|---|---|---|---|---|"]
        for r in sorted(rows, key=lambda x: -(x["expectancy_pct"] or -999)):
            rep.append(f"| {r['level']} | {r['config']} | {r['n']} | {r['win_rate']} | {r['avg_win']} | "
                       f"{r['avg_loss']} | {r['expectancy_pct']} | {r['median_outcome']} | {r['hit20_rate']} |")
        out_path.write_text("\n".join(rep), encoding="utf-8")
        print(f"报告已保存: {out_path}")


def _reconfigure_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def main():
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="股票魔法师 P1 基本面漏斗回测")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("funnel", help="基本面漏斗裁量边际增益")
    p.add_argument("--events", required=True, help="事件 JSON（magician_backtest --dump-events）")
    p.add_argument("--cache", default=MB.DEFAULT_CACHE)
    p.add_argument("--indicator", default=Path(DEFAULT_SQUEEZE) / "finance_indicator.pkl")
    p.add_argument("--income", default=Path(DEFAULT_SQUEEZE) / "finance_income.pkl")
    p.add_argument("--basic", default=Path(DEFAULT_SQUEEZE) / "stock_basic.pkl")
    p.add_argument("--pb", default=str(Path(DEFAULT_SQUEEZE) / "finance_pb_weekly.pkl"))
    p.add_argument("--stops", type=float, nargs="+", default=[7.0])
    p.add_argument("--rrs", type=float, nargs="+", default=[3.0])
    p.add_argument("--mcs", type=int, nargs="+", default=[2, 3])
    p.add_argument("--rs-min-list", type=float, nargs="+", default=[0.0])
    p.add_argument("--brv", type=int, nargs="+", choices=[0, 1], default=[0])
    p.add_argument("--dry", type=int, nargs="+", choices=[0, 1], default=[0, 1])
    p.add_argument("--np-min", type=float, default=RULE_NP_YOY_MIN, help="归母净利润同比下限（第三梯队，默认25%）")
    p.add_argument("--horizon", type=int, default=60)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default="")
    p.add_argument("--start-hint", default="2020-01~2026-08，日度评估")
    p.add_argument("--sector", action="store_true", help="附加板块联动字段（magician_sector）")
    p.add_argument("--out-enriched", default="", help="保存富化事件 JSON（snap/funnel/板块）")
    p.set_defaults(func=cmd_funnel)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
