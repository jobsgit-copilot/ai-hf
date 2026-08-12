# -*- coding: utf-8 -*-
"""magician_tier4_sq.py — 第四梯队(续)：F1 质量红线 + 单季报口径双成长连续两季 叠加。

用法（ai-berkshire 目录，venv python）：
    D:\\Users\\projects\\ai2miniqmt\\.venv\\Scripts\\python.exe tools\\magician_tier4_sq.py \
        --events <富化事件.json> --out reports/magician-vcp-tier4-sq-20260812.md

漏斗定义：
  F7   = F1 质量红线 + 营收/归母净利润"单季口径"当季与上一季同比均>0（连续两季）
  F7B  = F1 + 单季双成长连续两季均>=15%
  F6F1 = F1 + 累计口径双成长连续两季>0（口径对照）
其余条件与 v7 完全一致：mc3 + dy分级 + RS优先 + 固定RR3 + 锁仓80/6仓/风险1.5%。
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

VARIANTS = [
    ("v7 基线 F1（对照）", "F1"),
    ("F7 F1+单季双成长2Q>0", "F7"),
    ("F7B F1+单季双成长2Q>=15", "F7B"),
    ("F6F1 F1+累计双成长2Q>0", "F6F1"),
]


def run_window(events, arrays, window, cfg, funnel):
    w0, w1 = window
    evs = [e for e in events if w0 <= e["date"] <= w1]
    res = MP.simulate(evs, arrays, dict(cfg), funnel_level=funnel,
                      regime_mode=0, risk_pct=1.5, max_positions=6, max_weight=25.0,
                      horizon=60, full_invest=False, lockout=80,
                      mc2_half=False, nody_half=True, friction=0.0, rs_priority=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True)
    ap.add_argument("--out", default="reports/magician-vcp-tier4-sq-20260812.md")
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

    levels = ["F1", "F7", "F7B", "F6F1"]
    for ev in events:
        ev["funnel"] = MF.funnel_level(MF.apply_rules(ev["_snap"]))

    counts = {}
    for lv in levels:
        counts[lv] = {}
        for wname, w in (("训练", TRAIN), ("样本外", OOS)):
            counts[lv][wname] = sum(1 for e in events
                                    if w[0] <= e["date"] <= w[1] and e["funnel"].get(lv))

    rows = []
    for vname, funnel in VARIANTS:
        for wname, window in (("训练", TRAIN), ("样本外", OOS)):
            res = run_window(events, arrays, window, BASE_CFG, funnel)
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
        "# 股票魔法师 第四梯队(续)：F1 + 单季报双成长连续两季 叠加（2026-08-12）",
        "",
        "> 基线：v7（F1 + 收缩≥3 + dy分级 + RS优先 + 固定RR3，锁仓80/6仓/风险1.5%）",
        "> 样本：日度 VCP 事件（截到 2026-07-31）；训练窗 2020-2023，样本外 2024-2026",
        "> 单季口径：单季营收/净利 = 该期累计 - 同财年上一期累计（一季报即本身），再做同比",
        "> 变体：F7 F1+单季双成长连续2季>0；F7B F1+单季双成长连续2季均>=15%；F6F1 F1+累计双成长连续2季>0（口径对照）",
        "",
        "| 变体 | 漏斗 | 窗口 | 事件通过数 | 净值 | CAGR% | 回撤% | C/D | 交易 | 胜率% | 期望% | 暴露% | 持仓天数% |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        n = counts[r["funnel"]][r["window"]]
        lines.append(f"| {r['variant']} | {r['funnel']} | {r['window']} | {n} | {r['final']:.2f} | {r['cagr']:.1f} | "
                     f"{r['max_dd']:.1f} | {r['cd']:.2f} | {r['n_trades']} | {r['win_rate']:.1f} | "
                     f"{r['expectancy']:.2f} | {r['avg_exposure']:.0f} | {r['pct_inv']:.0f} |")
    out = Path(args.out)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"报告已保存: {out}")


if __name__ == "__main__":
    main()
