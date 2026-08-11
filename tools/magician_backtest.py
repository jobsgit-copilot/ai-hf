#!/usr/bin/env python3
"""magician_backtest.py — VCP 策略历史回测与参数校准（Phase 4）。

运行环境：ai2miniqmt/.venv 的 python（需要 pandas；读取全市场日线缓存）。
数据源：D:\\Users\\Documents\\AI-Finance\\squeeze\\raw_cache_full.pkl（后复权，2019 至今）。

用法：
    python magician_backtest.py run --start 20200101 --end 20260401 --step 20
    python magician_backtest.py run --entry setup --rs-min 70 --require-stage2
    python magician_backtest.py sweep --start 20200101 --end 20260401 --step 20
    python magician_backtest.py run --smoke              # 快速冒烟测试（少量股票/日期）

说明与局限（详见输出报告）：
  - 以收盘价突破中枢点当日收盘价成交（近似；未模拟次日开盘滑点与涨跌停）
  - 结果度量从突破次日开始（T+1）
  - 全市场缓存仅含当前在市股票，存在幸存者偏差；无成交量字段，无法验证"量能萎缩"
  - 检测参数窗口 60 日、收缩≥2 次、深度递减、末次≤首次70%（与 magician_data 一致）
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

import numpy as np
import pandas as pd

import vcp_detector as VD  # noqa: E402

DEFAULT_CACHE = r"D:\Users\Documents\AI-Finance\squeeze\raw_cache_full.pkl"
WINDOW_DAYS = 60           # VCP 检测窗口（与 vcp_detector 默认一致）
WARMUP_BARS = 420          # 指标预热（MA200 + 缓冲）
MIN_AMOUNT = 100_000_000   # 流动性下限：近 60 日均成交额 >= 1 亿元
LOCKOUT = 80               # 同一标的两次建仓的最小间隔（交易日，避免同一基底重复计数）


# ---------------------------------------------------------------- 数据准备

def load_cache(path):
    df = pd.read_pickle(path)
    df["date"] = pd.to_datetime(df["date"])
    return df.sort_values(["code", "date"])


def build_arrays(df):
    """返回 {code: (dates, open, high, low, close, volume, amount)}，numpy 数组（升序）。

    无成交量列的数据源 volume=None（成交量确认类指标不可用）。
    """
    has_vol = "volume" in df.columns
    arrays = {}
    for code, g in df.groupby("code", sort=False):
        g = g.sort_values("date")
        vol = g["volume"].to_numpy(dtype=float) if has_vol else None
        arrays[code] = (
            g["date"].to_numpy(),
            g["open"].to_numpy(dtype=float),
            g["high"].to_numpy(dtype=float),
            g["low"].to_numpy(dtype=float),
            g["close"].to_numpy(dtype=float),
            vol,
            g["amount"].to_numpy(dtype=float),
        )
    return arrays


def select_universe(arrays, min_amount=MIN_AMOUNT, lookback=60):
    """按近 lookback 日平均成交额筛选，并剔除历史不足的标的。返回 (codes, min_rows)。"""
    out = []
    for code, (dates, o, h, l, c, vol, amt) in arrays.items():
        if len(dates) < WARMUP_BARS + WINDOW_DAYS:
            continue
        avg = amt[-lookback:].mean() if len(amt) >= lookback else amt.mean()
        if avg >= min_amount:
            out.append(code)
    return sorted(out)


def sample_dates(global_dates, start, end, step):
    """在 [start, end] 内每隔 step 个交易日取一个评估日（datetime64 数组，已排序）。"""
    dates = np.sort(global_dates)
    d0 = pd.Timestamp(str(start))
    d1 = pd.Timestamp(str(end))
    mask = (dates >= d0.to_datetime64()) & (dates <= d1.to_datetime64())
    dates = dates[mask]
    return dates[::step]


def rs_percentiles(df, eval_dates, windows=(252, 126, 63)):
    """对每个评估日预计算全市场横截面 N 日涨幅分位（0-99）。

    透视表向量化：一次 pivot 后仅做列切片，避免逐日全表过滤。
    返回 {window: {date_str: {code: pct}}}。
    """
    dates_all = pd.Series(df["date"].unique()).sort_values().to_numpy()
    close_pivot = df.pivot_table(index="code", columns="date", values="close")
    out = {w: {} for w in windows}
    for w in windows:
        for d in eval_dates:
            p = int(np.searchsorted(dates_all, d))
            s = p - w
            if s < 0:
                continue
            d0 = pd.Timestamp(dates_all[s])
            d1 = pd.Timestamp(d)
            if d0 not in close_pivot.columns or d1 not in close_pivot.columns:
                continue
            c0 = close_pivot[d0]
            c1 = close_pivot[d1]
            common = c0.notna() & c1.notna()
            ret = (c1[common] / c0[common] - 1).dropna()
            if ret.empty:
                continue
            pct = (ret.rank(pct=True) * 99).round(1)
            out[w][str(d)[:10]] = {k: float(v) for k, v in pct.items()}
    return out


# ---------------------------------------------------------------- 事件检测

def _detect_chunk(payload):
    """多进程工作函数：处理一个股票分块，返回该块的全部 VCP 事件。"""
    arrays, universe, eval_dates = payload
    events = []
    for d in eval_dates:
        d_str = str(d)[:10]
        for code in universe:
            dates, o, h, l, c, vol, amt = arrays[code]
            end_pos = int(np.searchsorted(dates, d, side="right") - 1)
            if end_pos < WARMUP_BARS:
                continue
            start_pos = end_pos - (WARMUP_BARS - 1)
            bars = []
            for i in range(start_pos, end_pos + 1):
                bars.append({"date": str(dates[i])[:10], "open": float(o[i]), "high": float(h[i]),
                             "low": float(l[i]), "close": float(c[i]),
                             "volume": float(vol[i]) if vol is not None else None,
                             "amount": float(amt[i])})
            try:
                r = VD.analyze_vcp(bars, window_days=WINDOW_DAYS, min_contractions=2)
            except Exception:
                continue
            if not r["has_vcp"]:
                continue
            events.append({
                "code": code,
                "date": d_str,
                "status": r["status"],
                "pivot": r["pivot"],
                "base_low": r["base_low"],
                "entry": float(c[end_pos]),
                "depths": r["depths"],
                "n_contractions": len(r["depths"]),
                "footprint": r["footprint"],
                "stage": r["stage_context"],
                "volume_dry": r["volume_dry"],
                "breakout_volume_confirmed": r["breakout_volume_confirmed"],
            })
    return events


def detect_events(arrays, universe, eval_dates, progress=True, workers=1):
    """对每个评估日×标的运行 VCP 检测，收集全部事件（含结构性字段）。

    workers>1 时按股票分块多进程并行（结果与串行一致）。
    """
    if workers > 1 and len(universe) > workers:
        import multiprocessing as mp
        chunks = np.array_split(np.asarray(universe, dtype=object), workers)
        payloads = [({c: arrays[c] for c in chunk.tolist()}, chunk.tolist(), eval_dates)
                    for chunk in chunks]
        t0 = time.time()
        with mp.Pool(workers) as pool:
            results = pool.map(_detect_chunk, payloads)
        events = [e for r in results for e in r]
        print(f"并行检测完成：{len(events)} 个 VCP 事件（{workers} workers，"
              f"已用 {time.time()-t0:.0f}s）", flush=True)
        return events

    events = []
    total = len(universe) * len(eval_dates)
    done = 0
    t0 = time.time()
    for d in eval_dates:
        d_str = str(d)[:10]
        for code in universe:
            dates, o, h, l, c, vol, amt = arrays[code]
            end_pos = int(np.searchsorted(dates, d, side="right") - 1)
            if end_pos < WARMUP_BARS:
                done += 1
                continue
            start_pos = end_pos - (WARMUP_BARS - 1)
            bars = []
            for i in range(start_pos, end_pos + 1):
                bars.append({"date": str(dates[i])[:10], "open": float(o[i]), "high": float(h[i]),
                             "low": float(l[i]), "close": float(c[i]),
                             "volume": float(vol[i]) if vol is not None else None,
                             "amount": float(amt[i])})
            try:
                r = VD.analyze_vcp(bars, window_days=WINDOW_DAYS, min_contractions=2)
            except Exception:
                done += 1
                continue
            if not r["has_vcp"]:
                done += 1
                continue
            events.append({
                "code": code,
                "date": d_str,
                "status": r["status"],
                "pivot": r["pivot"],
                "base_low": r["base_low"],
                "entry": float(c[end_pos]),
                "depths": r["depths"],
                "n_contractions": len(r["depths"]),
                "footprint": r["footprint"],
                "stage": r["stage_context"],
                "volume_dry": r["volume_dry"],
                "breakout_volume_confirmed": r["breakout_volume_confirmed"],
            })
            done += 1
        if progress:
            print(f"  检测进度 {done}/{total}  已用 {time.time()-t0:.0f}s  事件 {len(events)}", flush=True)
    return events


# ---------------------------------------------------------------- 结果度量

def measure(events, arrays, config, horizon=60, target20=0.20):
    """按配置度量每笔事件的结果。返回 (trades, summary)。

    口径：
      - 突破买入口径：仅在突破日收盘价不超过中枢点 + max_ext 时计入（避免追高）
      - 同一标的两次建仓间隔 >= LOCKOUT 交易日（避免同一基底重复计数）
    """
    stop_pct = config["stop_pct"] / 100.0
    rr = config["rr"]
    min_cont = config.get("min_contractions", 2)
    rs_min = config.get("rs_min", 0)
    require_stage2 = config.get("require_stage2", True)
    entry_mode = config.get("entry", "breakout")
    max_ext = config.get("max_ext", 0.15)
    require_brv = config.get("require_brv", False)
    require_dry = config.get("require_dry", False)

    trades = []
    last_entry_idx = {}  # code -> 上一次已接受建仓的全局 bar 索引
    for ev in sorted(events, key=lambda e: (e["code"], e["date"])):
        if ev["n_contractions"] < min_cont:
            continue
        if ev["rs"] < rs_min:
            continue
        if ev["stage"] != "stage2" and require_stage2:
            continue
        if entry_mode == "breakout":
            if ev["status"] != "breakout":
                continue
            if ev["entry"] > ev["pivot"] * (1 + max_ext):
                continue
            if require_brv and not ev.get("breakout_volume_confirmed"):
                continue
            if require_dry and ev.get("volume_dry") is not True:
                continue
        else:  # setup：接近中枢点（95%-100%）
            if ev["status"] not in ("setup", "breakout"):
                continue
            if ev["entry"] < ev["pivot"] * 0.95 or ev["entry"] > ev["pivot"]:
                continue

        dates, o, h, l, c, vol, amt = arrays[ev["code"]]
        i = int(np.searchsorted(dates, np.datetime64(ev["date"]), side="right") - 1)
        if i < 0 or i + 1 >= len(dates):
            continue
        last = last_entry_idx.get(ev["code"])
        if last is not None and i - last < LOCKOUT:
            continue
        entry = ev["entry"]
        stop = max(ev["pivot"] * (1 - stop_pct), ev["base_low"]) if ev["base_low"] else ev["pivot"] * (1 - stop_pct)
        risk = entry - stop
        if risk <= 0:
            continue
        target = entry + rr * risk
        target20p = entry * (1 + target20)

        n_fwd = min(horizon, len(dates) - i - 1)
        stop_day = None
        up_day = None
        outcome = None
        exit_day = None
        for j in range(1, n_fwd + 1):
            lo, hi = l[i + j], h[i + j]
            if stop_day is None and lo <= stop:
                stop_day = j
            if up_day is None and hi >= target20p:
                up_day = j
            if outcome is None:
                if lo <= stop:
                    outcome = (stop - entry) / entry
                    exit_day = j
                elif hi >= target:
                    outcome = (target - entry) / entry
                    exit_day = j
        if outcome is None:
            outcome = (c[i + n_fwd] - entry) / entry
            exit_day = n_fwd
        hit20 = up_day is not None and (stop_day is None or up_day < stop_day)
        trades.append({
            "code": ev["code"], "date": ev["date"], "status": ev["status"],
            "entry": round(entry, 3), "stop": round(stop, 3),
            "target": round(target, 3), "outcome_pct": round(outcome * 100, 2),
            "exit_day": exit_day, "hit20_before_stop": hit20,
            "depth_first": ev["depths"][0], "depth_last": ev["depths"][-1],
            "stage": ev["stage"],
        })
        last_entry_idx[ev["code"]] = i
    return trades, summarize(trades)


def _f(x):
    """numpy 标量转 python 浮点（None/NaN 保留为 None）。"""
    if x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if v != v else round(v, 2)


def summarize(trades):
    if not trades:
        return {"n": 0, "win_rate": None, "avg_win": None, "avg_loss": None,
                "expectancy_pct": None, "median_outcome": None, "hit20_rate": None}
    outs = np.array([t["outcome_pct"] for t in trades])
    wins = outs[outs > 0]
    losses = outs[outs <= 0]
    return {
        "n": len(trades),
        "win_rate": round(len(wins) / len(trades) * 100, 1),
        "avg_win": _f(wins.mean()) if len(wins) else None,
        "avg_loss": _f(losses.mean()) if len(losses) else None,
        "expectancy_pct": _f(outs.mean()),
        "median_outcome": _f(np.median(outs)),
        "hit20_rate": round(sum(1 for t in trades if t["hit20_before_stop"]) / len(trades) * 100, 1),
    }


# ---------------------------------------------------------------- 引擎与 CLI

def _config_label(cfg):
    return (f"stop{cfg['stop_pct']}%_rr{cfg['rr']}_mc{cfg.get('min_contractions', 2)}"
            f"_rs{cfg.get('rs_min', 0)}_s2{int(cfg.get('require_stage2', True))}"
            f"_x{int(round(cfg.get('max_ext', 0.15) * 100))}_bv{int(cfg.get('require_brv', False))}"
            f"_dy{int(cfg.get('require_dry', False))}_{cfg.get('entry', 'breakout')}")


def run_engine(events, arrays, configs, horizon=60):
    results = {}
    for cfg in configs:
        trades, summary = measure(events, arrays, cfg, horizon=horizon)
        results[_config_label(cfg)] = {"config": cfg, "summary": summary, "trades": trades}
    return results


def cmd_run(args):
    df = load_cache(args.cache)
    global_dates = df["date"].unique()
    arrays = build_arrays(df)
    universe = select_universe(arrays, args.min_amount)
    eval_dates = sample_dates(global_dates, args.start, args.end, args.step)
    if args.smoke:
        universe = universe[:80]
        eval_dates = eval_dates[:6]
    print(f"标的池 {len(universe)}，评估日 {len(eval_dates)}（{str(eval_dates[0])[:10]} ~ {str(eval_dates[-1])[:10]}）")

    rs = rs_percentiles(df, eval_dates) if args.rs_min else None
    if rs:
        print("RS 分位预计算完成（252/126/63 日）")

    events = detect_events(arrays, universe, eval_dates, workers=args.workers)
    print(f"检测完成：{len(events)} 个 VCP 事件")

    # 附加 RS 分位（主：252，回退 126/63）
    if rs:
        for ev in events:
            ev["rs"] = None
            for w in (252, 126, 63):
                v = rs[w].get(ev["date"], {}).get(ev["code"])
                if v is not None:
                    ev["rs"] = v
                    break
            ev["rs"] = ev["rs"] if ev["rs"] is not None else 0.0
    else:
        for ev in events:
            ev["rs"] = 0.0

    configs = [{"stop_pct": args.stop_pct, "rr": args.rr,
                "min_contractions": args.min_contractions,
                "rs_min": args.rs_min, "require_stage2": args.require_stage2,
                "entry": args.entry, "max_ext": args.max_ext,
                "require_brv": args.require_brv,
                "require_dry": args.require_dry}]
    results = run_engine(events, arrays, configs, horizon=args.horizon)

    if args.json:
        print(json.dumps({k: {"summary": v["summary"]} for k, v in results.items()},
                         ensure_ascii=False, indent=2))
    else:
        for label, r in results.items():
            s = r["summary"]
            print(f"\n=== {label} ===")
            print(f"交易数 {s['n']}  胜率 {s['win_rate']}%  平均盈利 {s['avg_win']}%  "
                  f"平均亏损 {s['avg_loss']}%  期望 {s['expectancy_pct']}%  中位数 {s['median_outcome']}%  "
                  f"先达+20%比率 {s['hit20_rate']}%")

    if args.dump_events and events:
        out_path = Path(args.dump_events)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(events, f, ensure_ascii=False, indent=2)
        print(f"事件已导出: {out_path}")


def cmd_sweep(args):
    df = load_cache(args.cache)
    arrays = build_arrays(df)
    if args.load_events:
        events = json.loads(Path(args.load_events).read_text(encoding="utf-8"))
        print(f"加载事件 {len(events)}（跳过检测与 RS 预计算）")
    else:
        global_dates = df["date"].unique()
        universe = select_universe(arrays, args.min_amount)
        eval_dates = sample_dates(global_dates, args.start, args.end, args.step)
        print(f"标的池 {len(universe)}，评估日 {len(eval_dates)}")
        rs = rs_percentiles(df, eval_dates)
        events = detect_events(arrays, universe, eval_dates, workers=args.workers)
        for ev in events:
            ev["rs"] = None
            for w in (252, 126, 63):
                v = rs[w].get(ev["date"], {}).get(ev["code"])
                if v is not None:
                    ev["rs"] = v
                    break
            ev["rs"] = ev["rs"] if ev["rs"] is not None else 0.0
        print(f"检测完成：{len(events)} 个 VCP 事件")
        if args.dump_events:
            Path(args.dump_events).write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
            print(f"事件已缓存: {args.dump_events}")

    configs = []
    for entry in ("breakout",):
        for stop_pct in args.stops:
            for rr in args.rrs:
                for mc in args.mcs:
                    for rs_min in args.rs_min_list:
                        for s2 in args.stage2:
                            for x in args.max_exts:
                                for brv in args.brv:
                                    for dry in args.dry:
                                        configs.append({"stop_pct": stop_pct, "rr": rr,
                                                        "min_contractions": mc, "rs_min": rs_min,
                                                        "require_stage2": s2, "entry": entry,
                                                        "max_ext": x, "require_brv": bool(brv),
                                                        "require_dry": bool(dry)})
    results = run_engine(events, arrays, configs, horizon=args.horizon)

    rows = []
    for label, r in results.items():
        s = r["summary"]
        rows.append({"config": label, **{k: s[k] for k in ("n", "win_rate", "avg_win", "avg_loss",
                                                           "expectancy_pct", "median_outcome", "hit20_rate")}})
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return

    print("\n=== 参数校准表（按期望值降序）===")
    print(f"{'配置':<42}{'笔数':>6}{'胜率%':>7}{'均盈%':>7}{'均亏%':>7}{'期望%':>8}{'中位%':>8}{'先+20%':>7}")
    for row in sorted(rows, key=lambda r: -(r["expectancy_pct"] or -999)):
        print(f"{row['config']:<42}{row['n']:>6}{str(row['win_rate']):>7}{str(row['avg_win']):>7}"
              f"{str(row['avg_loss']):>7}{row['expectancy_pct']:>8.2f}{str(row['median_outcome']):>8}{str(row['hit20_rate']):>7}")

    if args.out:
        out_path = Path(args.out)
        report = [f"# VCP 策略参数校准表（{args.start} ~ {args.end}）",
                  "", "| 配置 | 笔数 | 胜率% | 均盈% | 均亏% | 期望% | 中位% | 先达+20%% |",
                  "|---|---|---|---|---|---|---|---|"]
        for row in sorted(rows, key=lambda r: -(r["expectancy_pct"] or -999)):
            report.append(f"| {row['config']} | {row['n']} | {row['win_rate']} | {row['avg_win']} | "
                          f"{row['avg_loss']} | {row['expectancy_pct']} | {row['median_outcome']} | {row['hit20_rate']} |")
        report += ["", "> 说明：收盘价成交近似、T+1、无成交量确认、幸存者偏差、未计涨跌停与滑点。",
                   "> 命中 20% 先于止损的比率是 Minervini 检验口径。"]
        out_path.write_text("\n".join(report), encoding="utf-8")
        print(f"校准表已保存: {out_path}")


def _reconfigure_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def cmd_cache(args):
    """从 xtquant 拉取全市场前复权日线（含成交量），构建回测数据缓存。"""
    try:
        from xtquant import xtdata
    except Exception as e:
        raise RuntimeError(f"需要 ai2miniqmt 虚拟环境（xtquant）：{e}") from e
    xtdata.enable_hello = False
    stocks = xtdata.get_stock_list_in_sector("沪深A股")
    if not stocks:
        raise RuntimeError("未获取到沪深A股列表（QMT 未连接？）")
    frames = []
    t0 = time.time()
    batch = 500
    for i in range(0, len(stocks), batch):
        chunk = stocks[i:i + batch]
        bars = xtdata.get_market_data_ex([], chunk, period="1d",
                                         start_time=args.start, end_time=args.end,
                                         dividend_type="front")
        for c in chunk:
            df = bars.get(c)
            if df is None or df.empty:
                continue
            frames.append(pd.DataFrame({
                "code": c,
                "date": pd.to_datetime(df.index),
                "open": df["open"].to_numpy(dtype=float),
                "high": df["high"].to_numpy(dtype=float),
                "low": df["low"].to_numpy(dtype=float),
                "close": df["close"].to_numpy(dtype=float),
                "volume": df["volume"].to_numpy(dtype=float),
                "amount": df["amount"].to_numpy(dtype=float),
            }))
        done = min(i + batch, len(stocks))
        if done % (batch * 3) == 0 or done == len(stocks):
            print(f"  缓存构建 {done}/{len(stocks)}  已用 {time.time()-t0:.0f}s", flush=True)
    out = pd.concat(frames, ignore_index=True)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_pickle(out_path)
    print(f"已保存: {out_path}  （{len(out)} 行, {out['code'].nunique()} 只, "
          f"{str(out['date'].min())[:10]} ~ {str(out['date'].max())[:10]}，前复权）")


def main():
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="VCP 策略历史回测与参数校准（Phase 4）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--cache", default=os.environ.get("MAGICIAN_CACHE", DEFAULT_CACHE))
    common.add_argument("--start", default="20200101")
    common.add_argument("--end", default="20260401")
    common.add_argument("--step", type=int, default=20, help="评估日间隔（交易日）")
    common.add_argument("--horizon", type=int, default=60, help="持有观察期（交易日）")
    common.add_argument("--min-amount", type=float, default=MIN_AMOUNT, help="近60日均成交额下限")
    common.add_argument("--json", action="store_true")

    p_run = sub.add_parser("run", parents=[common])
    p_run.add_argument("--stop-pct", type=float, default=7.0)
    p_run.add_argument("--rr", type=float, default=2.0)
    p_run.add_argument("--min-contractions", type=int, default=2)
    p_run.add_argument("--rs-min", type=float, default=0.0, help="RS 分位下限（0=不过滤）")
    p_run.add_argument("--require-stage2", action="store_true", default=True)
    p_run.add_argument("--entry", choices=["breakout", "setup"], default="breakout")
    p_run.add_argument("--max-ext", type=float, default=0.15, help="突破后最大延伸（相对中枢点），超过视为追高")
    p_run.add_argument("--require-brv", action="store_true", default=False,
                       help="要求突破日放量确认（需带成交量数据缓存）")
    p_run.add_argument("--require-dry", action="store_true", default=False,
                       help="要求量能萎缩（末段收缩量能 <= 首段 60%）")
    p_run.add_argument("--smoke", action="store_true", help="快速冒烟测试")
    p_run.add_argument("--workers", type=int, default=1, help="检测多进程数（日度评估建议 4-8）")
    p_run.add_argument("--dump-events", default="", help="导出事件 JSON 路径")
    p_run.set_defaults(func=cmd_run)

    p_sweep = sub.add_parser("sweep", parents=[common])
    p_sweep.add_argument("--stops", type=float, nargs="+", default=[7.0, 8.0, 10.0])
    p_sweep.add_argument("--rrs", type=float, nargs="+", default=[2.0, 3.0])
    p_sweep.add_argument("--mcs", type=int, nargs="+", default=[2, 3])
    p_sweep.add_argument("--rs-min-list", type=float, nargs="+", default=[0.0, 70.0, 90.0])
    p_sweep.add_argument("--stage2", type=int, nargs="+", choices=[0, 1], default=[1])
    p_sweep.add_argument("--max-exts", type=float, nargs="+", default=[0.15])
    p_sweep.add_argument("--brv", type=int, nargs="+", choices=[0, 1], default=[0],
                         help="是否要求突破日放量确认（0=否 1=是）")
    p_sweep.add_argument("--dry", type=int, nargs="+", choices=[0, 1], default=[0],
                         help="是否要求量能萎缩（0=否 1=是）")
    p_sweep.add_argument("--out", default="", help="校准表 Markdown 输出路径")
    p_sweep.add_argument("--workers", type=int, default=1, help="检测多进程数（日度评估建议 4-8）")
    p_sweep.add_argument("--dump-events", default="", help="缓存事件 JSON（跳过下次检测）")
    p_sweep.add_argument("--load-events", default="", help="从缓存 JSON 加载事件，跳过检测/RS")

    p_cache = sub.add_parser("cache", help="从 xtquant 构建带成交量的日线缓存")
    p_cache.add_argument("--cache", default=os.environ.get("MAGICIAN_CACHE", DEFAULT_CACHE))
    p_cache.add_argument("--start", default="20180101")
    p_cache.add_argument("--end", default=time.strftime("%Y%m%d"))
    p_cache.add_argument("--out", default=r"D:\Users\projects\ai-berkshire\data\magician\live_cache.pkl")
    p_cache.set_defaults(func=cmd_cache)
    p_sweep.set_defaults(func=cmd_sweep)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
