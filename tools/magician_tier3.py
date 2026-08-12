# -*- coding: utf-8 -*-
"""magician_tier3.py — 第三梯队：净利润成长 + 板块联动 组合层扫描（训练窗/样本外）。

用法（ai-berkshire 目录，系统 python）：
    python tools/magician_tier3.py --events <富化事件.json> --out reports/magician-vcp-tier3-20260812.md

约定：事件需已含板块字段（magician_sector）；样本统一截到 2026-07-31（与板块指数/RS 缓存对齐）。
v7 基线 = F1 + mc3 + dy分级 + RS优先 + 固定RR3。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import magician_backtest as MB
import magician_fundamental as MF
import magician_portfolio as MP

SQ = Path(r"D:\Users\Documents\AI-Finance\squeeze")
CACHE = Path(r"D:\Users\projects\ai-berkshire\data\magician\live_cache.pkl")
TRAIN = ("2020-01-01", "2023-12-31")
OOS = ("2024-01-01", "2026-07-31")

BASE_CFG = {"stop_pct": 7.0, "rr": 3.0, "min_contractions": 3, "rs_min": 0.0,
            "require_stage2": True, "entry": "breakout", "max_ext": 0.15,
            "require_brv": False, "require_dry": False,
            "sector_rs_min": None, "sector_res_min": None}


def variants():
    out = []
    def add(name, funnel, **kw):
        cfg = dict(BASE_CFG)
        cfg.update(kw)
        out.append((name, funnel, cfg))
    # 全部基于 v7 机制（dy分级+RS优先），require_dry 保持 False
    add("v7 基线 F1", "F1")
    add("F1 + 板块RS≥70", "F1", sector_rs_min=70.0)
    add("F1 + 板块共振≥3", "F1", sector_res_min=3)
    add("F2 营收≥15（对照）", "F2")
    add("F2 + 板块共振≥3", "F2", sector_res_min=3)
    add("F5 + 板块RS≥90", "F5", sector_rs_min=90.0)
    add("F5 + 共振≥5", "F5", sector_res_min=5)
    for np_min in (15.0, 20.0, 25.0, 30.0):
        add("F2N 净利≥%d" % np_min, "F2N")
        add("F5 双成长≥%d" % np_min, "F5")
    return out


def run_window(events, arrays, window, cfg, funnel):
    w0, w1 = window
    evs = [e for e in events if w0 <= e["date"] <= w1]
    res = MP.simulate(evs, arrays, dict(cfg), funnel_level=funnel,
                      regime_mode=0, risk_pct=1.5, max_positions=6, max_weight=25.0,
                      horizon=60, full_invest=False, lockout=80,
                      mc2_half=False, nody_half=True, friction=0.0, rs_priority=True)
    return res


def np_th_of(name, default):
    for tok in name.split():
        tok = tok.replace("净利", "").replace("≥", "")
        try:
            return float(tok)
        except ValueError:
            continue
    return default


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", default="reports/magician-vcp-tier3-20260812.md")
    ap.add_argument("--np-min", type=float, default=25.0)
    args = ap.parse_args()

    events = json.loads(Path(args.events).read_text(encoding="utf-8"))
    events = [e for e in events if e["date"] <= "2026-07-31"]
    print(f"事件截断到 2026-07-31：{len(events)}", flush=True)

    df = MB.load_cache(CACHE)
    arrays = MB.build_arrays(df)
    db = MF.FundamentalDB(SQ / "finance_indicator.pkl", SQ / "finance_income.pkl",
                          SQ / "stock_basic.pkl", SQ / "finance_pb_weekly.pkl")
    t0 = time.time()
    for ev in events:
        snap = db.snapshot(ev["code"], ev["date"])
        ev["_snap"] = snap
        ev["np_yoy"] = snap["np_yoy"]
        ev["rev_yoy"] = snap["rev_yoy"]
    print(f"财务快照完成（{time.time()-t0:.0f}s）", flush=True)

    def tag(np_th):
        for ev in events:
            ev["funnel"] = MF.funnel_level(MF.apply_rules(ev["_snap"], {"np_yoy_min": np_th}))

    rows = []
    cur_th = None
    for vname, funnel, cfg in variants():
        np_th = np_th_of(vname, args.np_min)
        if cur_th != np_th:
            tag(np_th)
            cur_th = np_th
        for wname, window in (("训练", TRAIN), ("样本外", OOS)):
            res = run_window(events, arrays, window, cfg, funnel)
            rows.append({
                "variant": vname, "funnel": funnel, "window": wname,
                "final": res["final"], "cagr": res["cagr"], "max_dd": res["max_dd"],
                "cd": res["cagr"] / max(abs(res["max_dd"]), 1e-9),
                "n_trades": res["n_trades"], "win_rate": res["win_rate"],
                "expectancy": res["expectancy"], "avg_exposure": res["avg_exposure"],
                "pct_inv": res["pct_days_invested"],
            })
            print(f"{vname} [{wname}] 净值{res['final']:.2f} CAGR {res['cagr']:.1f}% "
                  f"回撤 {res['max_dd']:.1f}% C/D {rows[-1]['cd']:.2f} 交易 {res['n_trades']}",
                  flush=True)

    lines = [
        "# 股票魔法师 第三梯队：净利润成长 + 板块联动（2026-08-12）",
        "",
        "> 基线：v7（F1 + 收缩≥3 + dy分级 + RS优先 + 固定RR3，锁仓80/6仓/风险1.5%）",
        "> 样本：日度 VCP 事件（截到 2026-07-31）；训练窗 2020-2023，样本外 2024-2026",
        "> 净利润：归母净利润同比（point-in-time，阈值敏感性 15/20/25/30）；营收：最新季度同比≥15%",
        "> 板块联动：sector_rs=行业126日涨幅在31个申万一级行业中的分位；共振=同行业近20日VCP(breakout/setup)标的数",
        "> 退市/次新股：本轮明确不考虑（ST 已由 r_st 排除；未加次新/退市过滤）",
        "",
        "| 变体 | 漏斗 | 窗口 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% | 暴露% | 持仓天数% |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(f"| {r['variant']} | {r['funnel']} | {r['window']} | {r['final']:.2f} | {r['cagr']:.1f} | "
                     f"{r['max_dd']:.1f} | {r['cd']:.2f} | {r['n_trades']} | {r['win_rate']:.1f} | "
                     f"{r['expectancy']:.2f} | {r['avg_exposure']:.0f} | {r['pct_inv']:.0f} |")
    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已保存: {out}")


if __name__ == "__main__":
    main()
