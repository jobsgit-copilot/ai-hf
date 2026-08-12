# -*- coding: utf-8 -*-
"""magician_tier4_v8.py — F6F1（v8候选）入库评审：Step1 邻域 / Step2 成本执行 / Step3 历史结构。

用法（venv python）：
    D:\\Users\\projects\\ai2miniqmt\\.venv\\Scripts\\python.exe tools\\magician_tier4_v8.py \
        --events <富化事件.json> --out reports/magician-vcp-v8-gate-20260813.md

判定按 8.5 节设计：
  Step1 邻域：F6F1 阈值 0/5/10/15 + 上一季开关（F6F1C），要求样本外平台、训练 C/D>=0.35
  Step2 成本：0.2/0.5% 摩擦；一字涨跌停成交约束（--limit-exec）
  Step3 历史：按年 2020-2026H1 + 行情(指数MA200 up/down)分段 + 与 rs_priority/nody_half 交互
"""
import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

import magician_backtest as MB
import magician_fundamental as MF
import magician_portfolio as MP

SQ = Path(r"D:\Users\Documents\AI-Finance\squeeze")
CACHE = Path(r"D:\Users\projects\ai-berkshire\data\magician\live_cache.pkl")
INDEX = Path(r"D:\Users\projects\ai-berkshire\data\magician\index_000300.pkl")
TRAIN = ("2020-01-01", "2023-12-31")
OOS = ("2024-01-01", "2026-07-31")
FULL = ("2020-01-01", "2026-07-31")

BASE_CFG = {"stop_pct": 7.0, "rr": 3.0, "min_contractions": 3, "rs_min": 0.0,
            "require_stage2": True, "entry": "breakout", "max_ext": 0.15,
            "require_brv": False, "require_dry": False,
            "sector_rs_min": None, "sector_res_min": None}


def run(events, arrays, window, cfg, funnel, friction=0.0, rs_priority=True,
        nody_half=True, limit_exec=False):
    w0, w1 = window
    evs = [e for e in events if w0 <= e["date"] <= w1]
    res = MP.simulate(evs, arrays, dict(cfg), funnel_level=funnel,
                      regime_mode=0, risk_pct=1.5, max_positions=6, max_weight=25.0,
                      horizon=60, full_invest=False, lockout=80,
                      mc2_half=False, nody_half=nody_half, friction=friction,
                      rs_priority=rs_priority, limit_exec=limit_exec)
    return res


def fmt(res):
    cd = res["cagr"] / max(abs(res["max_dd"]), 1e-9)
    return {"final": res["final"], "cagr": res["cagr"], "dd": res["max_dd"], "cd": cd,
            "nt": res["n_trades"], "wr": res["win_rate"], "exp": res["expectancy"],
            "expo": res["avg_exposure"], "inv": res["pct_days_invested"],
            "skl": res.get("skipped_limit", 0)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", default="reports/magician-vcp-v8-gate-20260813.md")
    ap.add_argument("--step", default="all", choices=["all", "step1", "step2", "step3"])
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
        ev["_snap"] = db.snapshot(ev["code"], ev["date"])
    print(f"财务快照完成（{time.time()-t0:.0f}s）", flush=True)

    # 指数 MA200 环境
    ix = pd.read_pickle(INDEX)
    ix["date"] = pd.to_datetime(ix["date"])
    ix["ma200"] = ix["close"].rolling(200).mean()
    ix = ix.dropna(subset=["ma200"])
    above = dict(zip(ix["date"].dt.strftime("%Y-%m-%d"), (ix["close"] > ix["ma200"]).tolist()))
    for ev in events:
        ev["_above"] = above.get(ev["date"])

    def tag(th):
        for ev in events:
            ev["funnel"] = MF.funnel_level(MF.apply_rules(ev["_snap"], {"dual2_min": th}))

    def count(th, funnel):
        tag(th)
        c = {}
        for wname, w in (("训练", TRAIN), ("样本外", OOS)):
            c[wname] = sum(1 for e in events if w[0] <= e["date"] <= w[1] and e["funnel"].get(funnel))
        return c

    rows = []  # (section, variant, window, r)

    # ---------- Step 1：阈值邻域 + 上一季开关 ----------
    print(f"== Step 1 邻域 (step={args.step}) ==", flush=True)
    counts = {}
    if args.step in ("all", "step1"):
        for th in (0, 5, 10, 15):
            counts[f"F6F1@th{th}"] = count(th, "F6F1")
            for wname, w in (("训练", TRAIN), ("样本外", OOS)):
                r = run(events, arrays, w, BASE_CFG, "F6F1")
                rows.append(("Step1", f"F6F1 th={th}", wname, fmt(r)))
                print(f"F6F1 th={th} [{wname}] C/D {fmt(r)['cd']:.2f} CAGR {fmt(r)['cagr']:.1f}% 交易 {fmt(r)['nt']}", flush=True)
        counts["F6F1C(上一季关)"] = count(0, "F6F1C")
        for wname, w in (("训练", TRAIN), ("样本外", OOS)):
            r = run(events, arrays, w, BASE_CFG, "F6F1C")
            rows.append(("Step1", "F6F1C 上一季关", wname, fmt(r)))
            print(f"F6F1C [{wname}] C/D {fmt(r)['cd']:.2f} CAGR {fmt(r)['cagr']:.1f}% 交易 {fmt(r)['nt']}", flush=True)

    # ---------- Step 2：摩擦 + 一字涨跌停成交约束 ----------
    print(f"== Step 2 成本执行 (step={args.step}) ==", flush=True)
    tag(0)
    if args.step in ("all", "step2"):
        for funnel in ("F1", "F6F1"):
            for fr in (0.0, 0.002, 0.005):
                for wname, w in (("训练", TRAIN), ("样本外", OOS)):
                    r = run(events, arrays, w, BASE_CFG, funnel, friction=fr)
                    rows.append(("Step2", f"{funnel} friction{fr*100:.1f}%", wname, fmt(r)))
            for le in (False, True):
                for wname, w in (("训练", TRAIN), ("样本外", OOS)):
                    r = run(events, arrays, w, BASE_CFG, funnel, limit_exec=le)
                    rows.append(("Step2", f"{funnel} limit_exec={int(le)}", wname, fmt(r)))
            print(f"Step2 {funnel} done", flush=True)

    # ---------- Step 3：按年 / 行情 / 交互 ----------
    print(f"== Step 3 历史结构 (step={args.step}) ==", flush=True)
    years = [(2020, ("2020-01-01", "2020-12-31")), (2021, ("2021-01-01", "2021-12-31")),
             (2022, ("2022-01-01", "2022-12-31")), (2023, ("2023-01-01", "2023-12-31")),
             (2024, ("2024-01-01", "2024-12-31")), (2025, ("2025-01-01", "2025-12-31")),
             ("2026H1", ("2026-01-01", "2026-07-31"))]
    if args.step in ("all", "step3"):
        for yname, w in years:
            for funnel in ("F1", "F6F1"):
                r = run(events, arrays, w, BASE_CFG, funnel)
                rows.append(("Step3-y", f"{yname} {funnel}", str(yname), fmt(r)))
        up_ev = [e for e in events if e["_above"] is True]
        dn_ev = [e for e in events if e["_above"] is False]
        print(f"行情事件：up {len(up_ev)} / down {len(dn_ev)}", flush=True)
        for tag2, sub in (("up", up_ev), ("down", dn_ev)):
            for funnel in ("F1", "F6F1"):
                r = run(sub, arrays, FULL, BASE_CFG, funnel)
                rows.append(("Step3-r", f"{tag2} {funnel}", "2020-2026", fmt(r)))
        for funnel in ("F1", "F6F1"):
            for wname, w in (("训练", TRAIN), ("样本外", OOS)):
                r = run(events, arrays, w, BASE_CFG, funnel, rs_priority=False)
                rows.append(("Step3-i", f"{funnel} rs_priority=off", wname, fmt(r)))
                r = run(events, arrays, w, BASE_CFG, funnel, nody_half=False)
                rows.append(("Step3-i", f"{funnel} nody_half=off", wname, fmt(r)))

    # ---------- 报告 ----------
    lines = [
        "# 股票魔法师 v8 候选（F6F1）入库评审：Step1 邻域 / Step2 成本执行 / Step3 历史结构（2026-08-13）",
        "",
        "> 基线：v7（F1 + 收缩≥3 + dy分级 + RS优先 + 固定RR3，锁仓80/6仓/风险1.5%，摩擦0）",
        "> F6F1 = F1 质量红线 + 营收/归母净利润**累计口径**当季与上一季同比均>0",
        "> 样本：日度 VCP 事件（2020-01~2026-07）；训练窗 2020-2023，样本外 2024-2026",
        "",
        "## Step 1 阈值邻域与上一季开关（防过拟合主闸）",
        "",
        "| 变体 | 窗口 | 事件通过 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% | 暴露% |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    hdr = {"Step1": lines}
    def add_row(lines_, s, v, w, r):
        lines_.append(f"| {v} | {w} | {counts.get(v, {}).get('训练' if w=='训练' else '样本外', '')} | "
                      f"{r['final']:.2f} | {r['cagr']:.1f} | {r['dd']:.1f} | {r['cd']:.2f} | "
                      f"{r['nt']} | {r['wr']:.1f} | {r['exp']:.2f} | {r['expo']:.0f} |")

    for s, v, w, r in rows:
        if s == "Step1":
            add_row(lines, s, v, w, r)

    lines += ["", "## Step 2 摩擦与成交约束", "",
              "| 变体 | 窗口 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% | 暴露% | 一字跳过 |",
              "|---|---|---|---|---|---|---|---|---|---|---|"]
    for s, v, w, r in rows:
        if s == "Step2":
            lines.append(f"| {v} | {w} | {r['final']:.2f} | {r['cagr']:.1f} | {r['dd']:.1f} | {r['cd']:.2f} | "
                         f"{r['nt']} | {r['wr']:.1f} | {r['exp']:.2f} | {r['expo']:.0f} | {r['skl']} |")

    lines += ["", "## Step 3 按年 / 行情 / 机制交互", "",
              "### 3.1 按年分段（独立窗口）", "",
              "| 年份 | 漏斗 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s, v, w, r in rows:
        if s == "Step3-y":
            lines.append(f"| {w} | {v.split(' ',1)[1]} | {r['final']:.2f} | {r['cagr']:.1f} | {r['dd']:.1f} | "
                         f"{r['cd']:.2f} | {r['nt']} | {r['wr']:.1f} | {r['exp']:.2f} |")
    lines += ["", "### 3.2 行情分段（沪深300 MA200，全样本 2020-2026）", "",
              "| 行情 | 漏斗 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% |",
              "|---|---|---|---|---|---|---|---|---|"]
    for s, v, w, r in rows:
        if s == "Step3-r":
            lines.append(f"| {v.split(' ')[0]} | {v.split(' ')[1]} | {r['final']:.2f} | {r['cagr']:.1f} | {r['dd']:.1f} | "
                         f"{r['cd']:.2f} | {r['nt']} | {r['wr']:.1f} | {r['exp']:.2f} |")
    lines += ["", "### 3.3 与现有机制交互（rs_priority / nody_half 开关）", "",
              "| 变体 | 窗口 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% | 暴露% |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for s, v, w, r in rows:
        if s == "Step3-i":
            lines.append(f"| {v} | {w} | {r['final']:.2f} | {r['cagr']:.1f} | {r['dd']:.1f} | {r['cd']:.2f} | "
                         f"{r['nt']} | {r['wr']:.1f} | {r['exp']:.2f} | {r['expo']:.0f} |")

    if args.step == "all":
        out = Path(args.out)
        out.write_text("\n".join(lines), encoding="utf-8")
        print(f"报告已保存: {out}")
    else:
        print(f"[partial step={args.step}] 共 {len(rows)} 行结果（未写完整报告）")


if __name__ == "__main__":
    main()
