#!/usr/bin/env python3
"""magician_portfolio.py — P2 组合级回测：风险平价仓位 + 逐日净值 + 回撤

用法：
    python magician_portfolio.py run --events <events.json> --cache <live_cache.pkl> \
        --index <index_000300.pkl> --funnel F1 --regime 0 \
        --stop-pct 7 --rr 3 --min-contractions 2 --rs-min 0 --require-dry 1 \
        --risk-pct 1.5 --max-positions 6 --out report.md

于单笔回测的区别：这里按时间顺序模拟组合执行，
考虑现金/仓位容量/同时持仓数/风险预算复利，输出净值曲线与最大回撤。
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
import magician_fundamental as MF  # noqa: E402

LOCKOUT = 80


def _reconfigure_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


class Position:
    __slots__ = ("code", "entry_date", "entry_price", "stop", "target", "shares", "weight",
                 "entry_i", "hit20", "exit_day")

    def __init__(self, code, entry_date, entry_price, stop, target, shares, weight, entry_i):
        self.code = code
        self.entry_date = entry_date
        self.entry_price = entry_price
        self.stop = stop
        self.target = target
        self.shares = shares
        self.weight = weight
        self.entry_i = entry_i
        self.hit20 = False
        self.exit_day = None


def _entry_filters(cfg, ev):
    if ev["n_contractions"] < cfg["min_contractions"]:
        return False
    if ev["rs"] < cfg["rs_min"]:
        return False
    if cfg["require_stage2"] and ev["stage"] != "stage2":
        return False
    if ev["status"] != "breakout":
        return False
    if ev["entry"] > ev["pivot"] * (1 + cfg["max_ext"]):
        return False
    if cfg["require_brv"] and not ev.get("breakout_volume_confirmed"):
        return False
    if cfg["require_dry"] and ev.get("volume_dry") is not True:
        return False
    return True


def simulate(events, arrays, cfg, funnel_level="F1", regime_mode=0, risk_pct=1.5,
             max_positions=6, max_weight=25.0, horizon=60, breadth_th=0.6, full_invest=False,
             lockout=LOCKOUT, mc2_half=False, nody_half=False):
    """事件驱动的组合模拟。返回结果字典。"""
    # 交易日历：限制在事件区间内（避免 2018-2019 空转稀释 CAGR）
    ev_dates = np.sort(np.unique(np.array([e["date"] for e in events], dtype="datetime64[ns]")))
    d0, d1 = ev_dates[0], ev_dates[-1]
    all_dates = np.sort(np.unique(np.concatenate([arr[0] for arr in arrays.values()])))
    all_dates = all_dates[(all_dates >= d0) & (all_dates <= d1)]

    # 通过漏斗与环境标签的事件（按日期分组）
    fev = [e for e in events if e["funnel"].get(funnel_level)]
    if regime_mode:
        fev = [e for e in fev if e["regime"] is not None]
    fev.sort(key=lambda e: (e["date"], e["code"]))
    ev_by_day = {}
    for e in fev:
        ev_by_day.setdefault(e["date"], []).append(e)

    cash = 1.0
    positions = []  # 开仓中
    trades = []
    last_entry = {}  # code -> 上次建仓日索引
    equity_curve = []
    exposure_curve = []
    skipped_capacity = 0
    skipped_lockout = 0

    def mark_to_market(day_i):
        nonlocal cash
        val = cash
        for p in positions:
            arr = arrays[p.code]
            i = int(np.searchsorted(arr[0], all_dates[day_i], side="right") - 1)
            price = arr[4][i] if i >= 0 else p.entry_price
            val += p.shares * price
        return val

    t0 = time.time()
    for day_i, day in enumerate(all_dates):
        # 1) 出场（每日先处理）
        for p in list(positions):
            if day <= p.entry_date:
                continue
            arr = arrays[p.code]
            i = int(np.searchsorted(arr[0], day, side="right") - 1)
            if i < 0:
                continue
            o, h, l, c = arr[1][i], arr[2][i], arr[3][i], arr[4][i]
            if h >= p.entry_price * 1.2:
                p.hit20 = True
            p.exit_day = i - p.entry_i
            if l <= p.stop:
                exit_price = p.stop
                reason = "stop"
            elif h >= p.target:
                exit_price = p.target
                reason = "target"
            elif i - p.entry_i >= horizon:
                exit_price = c
                reason = "horizon"
            else:
                p.exit_day = None
                continue
            cash += p.shares * exit_price
            trades.append({"code": p.code, "entry_date": str(p.entry_date)[:10], "exit_date": str(day)[:10],
                           "entry": p.entry_price, "exit": exit_price, "weight": p.weight,
                           "outcome_pct": (exit_price / p.entry_price - 1) * 100,
                           "exit_day": p.exit_day, "reason": reason, "hit20": p.hit20})
            positions.remove(p)

        # 2) 建仓
        day_str = str(day)[:10]
        for ev in ev_by_day.get(day_str, []):
            code = ev["code"]
            if not _entry_filters(cfg, ev):
                continue
            if any(p.code == code for p in positions):
                continue
            if code in last_entry and day_i - last_entry[code] < lockout:
                skipped_lockout += 1
                continue
            stop = max(ev["pivot"] * (1 - cfg["stop_pct"] / 100.0), ev["base_low"] or ev["pivot"] * 0.9)
            entry = ev["entry"]
            if len(positions) >= max_positions:
                skipped_capacity += 1
                continue
            equity_now = cash + sum(p.shares * arrays[p.code][4][
                int(np.searchsorted(arrays[p.code][0], day, side="right") - 1)] for p in positions)
            if equity_now <= 0:
                continue
            risk_dist = (entry - stop) / entry if entry > stop else 0.10
            if full_invest:
                w = min(max_weight / 100.0, cash / equity_now)
            else:
                w = min((risk_pct / 100.0) / risk_dist, max_weight / 100.0)
            if regime_mode:
                coeff = 1.0 if (ev.get("idx_above_ma200") and (ev.get("breadth") or 0) >= breadth_th) else                         (0.8 if ev.get("idx_above_ma200") else 0.5)
                w *= coeff
            if mc2_half and ev.get("n_contractions", 0) < 3:
                w *= 0.5
            if nody_half and ev.get("volume_dry") is not True:
                w *= 0.5
            cost = w * equity_now
            if cash < cost:
                skipped_capacity += 1
                continue
            shares = cost / entry
            arr = arrays[code]
            i = int(np.searchsorted(arr[0], day, side="right") - 1)
            target = entry + cfg["rr"] * (entry - stop)
            positions.append(Position(code, day, entry, stop, target, shares, w, i))
            last_entry[code] = day_i
            cash -= cost

        # 3) 收盘市值与暴露
        equity_curve.append((day, mark_to_market(day_i)))
        exposure_curve.append(sum(p.weight for p in positions))
        if day_i % 250 == 0:
            print(f"  模拟进度 {day_i}/{len(all_dates)}  净值 "
                  f"{equity_curve[-1][1]:.3f}  持仓 {len(positions)}  已用 {time.time()-t0:.0f}s", flush=True)

    # 末日平仓
    for p in list(positions):
        arr = arrays[p.code]
        i = min(p.entry_i + horizon, len(arr[0]) - 1)
        price = arr[4][i]
        cash += p.shares * price
        trades.append({"code": p.code, "entry_date": str(p.entry_date)[:10], "exit_date": str(arr[0][i])[:10],
                       "entry": p.entry_price, "exit": price, "weight": p.weight,
                       "outcome_pct": (price / p.entry_price - 1) * 100,
                       "exit_day": i - p.entry_i, "reason": "end", "hit20": p.hit20})
        positions.remove(p)

    eq = pd.Series([v for _, v in equity_curve], index=[d for d, _ in equity_curve])
    res = summarize_portfolio(eq, trades, exposure_curve, skipped_capacity, skipped_lockout, cfg, funnel_level)
    res["equity"] = [[str(d)[:10], round(float(v), 4)] for d, v in zip(eq.index[::5], eq.values[::5])]
    res["exposure"] = [[str(d)[:10], round(float(v), 4)] for d, v in zip(eq.index[::5], exposure_curve[::5])]
    return res


def summarize_portfolio(eq, trades, exposure_curve, skipped_capacity, skipped_lockout, cfg, funnel_level):
    final = float(eq.iloc[-1])
    n_days = len(eq)
    cagr = (final ** (252.0 / max(1, n_days)) - 1) * 100
    peak = eq.cummax()
    dd = (eq / peak - 1)
    max_dd = float(dd.min()) * 100
    yr = pd.Series(eq.values, index=pd.to_datetime(eq.index))
    annual = {str(y): (grp.iloc[-1] / grp.iloc[0] - 1) * 100 for y, grp in yr.groupby(yr.index.year)}
    trades_s = pd.DataFrame(trades) if trades else pd.DataFrame()
    avg_exposure = float(np.mean(exposure_curve)) * 100 if exposure_curve else 0.0
    invested_days = sum(1 for x in exposure_curve if x > 1e-9)
    pct_days_invested = invested_days / len(exposure_curve) * 100 if exposure_curve else 0.0
    if len(trades_s):
        outcomes = trades_s["outcome_pct"]
        win_rate = float((outcomes > 0).mean()) * 100
        exp = float(outcomes.mean())
        avg_win = float(outcomes[outcomes > 0].mean())
        avg_loss = float(outcomes[outcomes <= 0].mean())
        hit20 = float(trades_s["hit20"].mean()) * 100
    else:
        win_rate = exp = avg_win = avg_loss = hit20 = None
    return {"final": final, "cagr": cagr, "max_dd": max_dd, "annual": annual,
            "n_trades": len(trades), "win_rate": win_rate, "expectancy": exp,
            "avg_win": avg_win, "avg_loss": avg_loss, "hit20": hit20,
            "avg_exposure": avg_exposure, "pct_days_invested": pct_days_invested,
            "skipped_capacity": skipped_capacity,
            "skipped_lockout": skipped_lockout, "trades": trades}


def cmd_run(args):
    events = json.loads(Path(args.events).read_text(encoding="utf-8"))
    df = MB.load_cache(args.cache)
    arrays = MB.build_arrays(df)

    t0 = time.time()
    db = MF.FundamentalDB(args.indicator, args.income, args.basic,
                          args.pb if Path(args.pb).exists() else None)
    for ev in events:
        ev["funnel"] = MF.funnel_level(MF.apply_rules(db.snapshot(ev["code"], ev["date"])))
    print(f"漏斗标签完成：{time.time()-t0:.0f}s")

    index_df = None
    if args.index and Path(args.index).exists():
        index_df = MB.load_index(args.index)
        breadth = MB.compute_breadth(df, [e["date"] for e in events])
        MB.tag_regime(events, index_df, breadth)

    cfg = {"stop_pct": args.stop_pct, "rr": args.rr, "min_contractions": args.min_contractions,
           "rs_min": args.rs_min, "require_stage2": True, "entry": "breakout", "max_ext": 0.15,
           "require_brv": args.require_brv, "require_dry": not (args.dy_off or args.nody_half)}

    res = simulate(events, arrays, cfg, funnel_level=args.funnel, regime_mode=args.regime,
                   risk_pct=args.risk_pct, max_positions=args.max_positions,
                   max_weight=args.max_weight, horizon=args.horizon, breadth_th=args.breadth_th,
                   full_invest=args.full_invest, lockout=args.lockout,
                   mc2_half=args.mc2_half, nody_half=args.nody_half)

    if args.json:
        out = {k: v for k, v in res.items() if k != "trades"}
        out["trades"] = res["trades"]
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    mode = "满仓" if args.full_invest else "风险平价"
    print(f"\n=== 组合回测结果（漏斗{args.funnel}，regime={args.regime}，{mode}）===")
    print(f"最终净值 {res['final']:.3f}  CAGR {res['cagr']:.1f}%  最大回撤 {res['max_dd']:.1f}%  "
          f"交易 {res['n_trades']} 笔  胜率 {res['win_rate']}%  期望 {res['expectancy']}%  "
          f"平均暴露 {res['avg_exposure']}%  现金占比 {100 - res['avg_exposure']:.1f}%  "
          f"有持仓天数 {res['pct_days_invested']:.0f}%  容量跳过 {res['skipped_capacity']}")
    print("分年收益: " + "  ".join(f"{y}={v:.1f}%" for y, v in sorted(res["annual"].items())))

    if args.out:
        mode = "满仓" if args.full_invest else "风险平价"
        lines = [f"# VCP 组合级回测（P2）：漏斗{args.funnel} 环境系数={args.regime} 仓位={mode}",
                 "",
                 f"> 开始资金 1.0；止损 {args.stop_pct:.0f}%；"
                 f"{'满仓：新仓尽量用到单只上限' if args.full_invest else '风险平价：单笔风险 ' + str(args.risk_pct) + '% → 单笔仓位约 ' + format(min(args.risk_pct / args.stop_pct * 100, args.max_weight), '.0f') + '%'}"
                 f"（单只上限{args.max_weight:.0f}%）；最多 {args.max_positions} 只持仓；同标的80日锁仓",
                 "",
                 "| 指标 | 值 |",
                 "|---|---|",
                 f"| 最终净值 | {res['final']:.3f} |",
                 f"| 年化CAGR | {res['cagr']:.1f}% |",
                 f"| 最大回撤 | {res['max_dd']:.1f}% |",
                 f"| 交易笔数 | {res['n_trades']} |",
                 f"| 胜率 | {res['win_rate']}% |",
                 f"| 单笔期望 | {res['expectancy']}% |",
                 f"| 平均仓位暴露 | {res['avg_exposure']}% |",
                 f"| 平均现金占比 | {100 - res['avg_exposure']:.1f}% |",
                 f"| 有持仓天数占比 | {res['pct_days_invested']:.1f}% |",
                 f"| 因容量跳过 | {res['skipped_capacity']} |",
                 "",
                 "| 年份 | 收益 |",
                 "|---|---|"]
        for y, v in sorted(res["annual"].items()):
            lines.append(f"| {y} | {v:.1f}% |")
        Path(args.out).write_text("\n".join(lines), encoding="utf-8")
        print(f"报告已保存: {args.out}")


def main():
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="股票魔法师 P2 组合级回测")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="运行组合模拟")
    p.add_argument("--events", required=True)
    p.add_argument("--cache", default=MB.DEFAULT_CACHE)
    p.add_argument("--index", default=r"D:\Users\projects\ai-berkshire\data\magician\index_000300.pkl")
    p.add_argument("--indicator", default=str(Path(MF.DEFAULT_SQUEEZE) / "finance_indicator.pkl"))
    p.add_argument("--income", default=str(Path(MF.DEFAULT_SQUEEZE) / "finance_income.pkl"))
    p.add_argument("--basic", default=str(Path(MF.DEFAULT_SQUEEZE) / "stock_basic.pkl"))
    p.add_argument("--pb", default=str(Path(MF.DEFAULT_SQUEEZE) / "finance_pb_weekly.pkl"))
    p.add_argument("--funnel", choices=["F0", "F1", "F2", "F4"], default="F1")
    p.add_argument("--regime", type=int, choices=[0, 1], default=0, help="是否应用环境仓位系数")
    p.add_argument("--breadth-th", type=float, default=0.60)
    p.add_argument("--stop-pct", type=float, default=7.0)
    p.add_argument("--rr", type=float, default=3.0)
    p.add_argument("--min-contractions", type=int, default=2)
    p.add_argument("--rs-min", type=float, default=0.0)
    p.add_argument("--require-dry", action="store_true", default=True)
    p.add_argument("--require-brv", action="store_true", default=False)
    p.add_argument("--risk-pct", type=float, default=1.5)
    p.add_argument("--full-invest", action="store_true", default=False,
                   help="满仓模式：新仓按可用现金尽量配到单只上限，不保留现金")
    p.add_argument("--lockout", type=int, default=LOCKOUT, help="同标的两次建仓最小间隔（交易日）")
    p.add_argument("--dy-off", action="store_true", default=False, help="关闭量能萎缩硬过滤（所有事件可入）")
    p.add_argument("--mc2-half", action="store_true", default=False,
                   help="混仓：n_contractions==2 的事件仓位减半（配合 --min-contractions 2）")
    p.add_argument("--nody-half", action="store_true", default=False,
                   help="量能分级：无量能萎缩事件仓位减半（同时关闭 dy 硬过滤）")
    p.add_argument("--max-positions", type=int, default=6)
    p.add_argument("--max-weight", type=float, default=25.0)
    p.add_argument("--horizon", type=int, default=60)
    p.add_argument("--json", action="store_true")
    p.add_argument("--out", default="")
    p.set_defaults(func=cmd_run)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
