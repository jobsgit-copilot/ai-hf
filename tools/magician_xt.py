#!/usr/bin/env python3
"""magician_xt.py — 数据桥接脚本（须在 ai2miniqmt 虚拟环境中执行）。

magician_data.py 通过 subprocess 调用本脚本；本脚本依赖 pandas + xtquant，
请使用 ai2miniqmt/.venv 的 python 运行。

用法：
    python magician_xt.py bars 600519.SH --days 420 [--end 20260811]
    python magician_xt.py rs 300308.SZ [--date 20260731]

数据源：
  - bars: xtquant 前复权日线（最新）；失败时回退全市场缓存 pkl（后复权，无成交量）
  - rs:   全市场缓存 pkl 横截面分位（0-99），窗口 63/126/252 个交易日

约定：stdout 只输出 JSON；诊断信息写 stderr。
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_CACHE = r"D:\Users\Documents\AI-Finance\squeeze\raw_cache_full.pkl"
CACHE = os.environ.get("MAGICIAN_CACHE", DEFAULT_CACHE)
RS_WINDOWS = (63, 126, 252)  # 交易日窗口，主 RS 优先 252（12个月，IBD 口径）


def _e(msg):
    print(msg, file=sys.stderr)


def _out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False))
    sys.stdout.write("\n")


def normalize_code(code: str) -> str:
    code = code.strip().upper()
    if code.endswith(("SH", "SZ", "BJ")) and "." not in code:
        code = code[:-2]
    if "." in code:
        head, suffix = code.split(".", 1)
        suffix = suffix if suffix in ("SH", "SZ", "BJ") else "SH"
    elif code.startswith(("6", "9")):
        suffix = "SH"
    elif code.startswith(("0", "2", "3")):
        suffix = "SZ"
    else:
        suffix = "BJ"
    return f"{code.split('.')[0]}.{suffix}"


# ---------------------------------------------------------------- bars（xtquant）

def _back_calendar_days(end_ymd: str, trading_days: int) -> str:
    """按 trading_days 个交易日估算起始日历日（留足缓冲）。"""
    dt = datetime.strptime(end_ymd, "%Y%m%d")
    return (dt - timedelta(days=int(trading_days * 1.6) + 40)).strftime("%Y%m%d")


def _df_to_bars(code, df, source, adjust):
    bars = []
    for idx, row in df.iterrows():
        bars.append({
            "date": str(idx)[:10],
            "open": round(float(row["open"]), 3),
            "high": round(float(row["high"]), 3),
            "low": round(float(row["low"]), 3),
            "close": round(float(row["close"]), 3),
            "volume": int(round(float(row.get("volume", 0) or 0))),
            "amount": round(float(row.get("amount", 0) or 0), 2),
        })
    return {"code": code, "source": source, "adjust": adjust, "bars": bars}


def cmd_bars(code, days, end=""):
    try:
        from xtquant import xtdata
    except Exception as e:
        _e(f"xtquant 不可用（{e}），回退本地缓存")
        return cmd_bars_cache(code, days, end)
    try:
        xtdata.enable_hello = False
        end_ymd = end or datetime.now().strftime("%Y%m%d")
        start_ymd = _back_calendar_days(end_ymd, days)
        xtdata.download_history_data(code, period="1d", incrementally=True)
        got = xtdata.get_market_data_ex(
            [], [code], period="1d",
            start_time=start_ymd, end_time=end_ymd, dividend_type="front",
        )
        df = got.get(code)
        if df is not None and not df.empty:
            bars = _df_to_bars(code, df, "xtquant", "front")
            bars["bars"] = bars["bars"][-days:]
            return bars
        _e(f"xtquant 未返回 {code} 的数据，回退本地缓存")
    except Exception as e:
        _e(f"xtquant 请求失败（{e}），回退本地缓存")
    return cmd_bars_cache(code, days, end)


def cmd_bars_cache(code, days, end=""):
    import pandas as pd
    df = pd.read_pickle(CACHE)
    sub = df[df["code"] == code]
    if sub.empty:
        raise RuntimeError(f"缓存中无 {code}（{CACHE}）")
    if end:
        sub = sub[sub["date"] <= pd.Timestamp(str(end))]
    sub = sub.sort_values("date").tail(days)
    bars = []
    for r in sub.itertuples():
        bars.append({
            "date": str(r.date)[:10],
            "open": round(float(r.open), 3),
            "high": round(float(r.high), 3),
            "low": round(float(r.low), 3),
            "close": round(float(r.close), 3),
            "volume": None,
            "amount": round(float(r.amount), 2),
        })
    return {"code": code, "source": "cache", "adjust": "back", "bars": bars,
            "cache_max_date": str(df["date"].max())[:10]}


# ---------------------------------------------------------------- rs（全市场缓存）

def _closes_at(df, day):
    sub = df[df["date"] == pd.Timestamp(day)]
    return sub.set_index("code")["close"]


def cmd_rs(code, date=""):
    df = pd.read_pickle(CACHE)
    dates = np.sort(df["date"].unique())
    max_date = dates[-1]
    asof = pd.Timestamp(str(date)) if date else max_date
    if asof > max_date:
        _e(f"请求日期 {str(asof)[:10]} 晚于缓存最新 {str(max_date)[:10]}，按缓存最新计算")
        asof = max_date
    pos = int(np.searchsorted(dates, asof, side="right") - 1)
    if pos < 0:
        raise RuntimeError(f"日期 {str(asof)[:10]} 早于缓存起点 {str(dates[0])[:10]}")

    out = {"code": code, "asof": str(dates[pos])[:10], "source": "cache",
           "cache_max_date": str(max_date)[:10]}
    counts = {}
    for w in RS_WINDOWS:
        key = f"rs_{w}"
        s = pos - w
        if s < 0:
            out[key] = None
            continue
        c0 = _closes_at(df, dates[s])
        c1 = _closes_at(df, dates[pos])
        common = c0.index.intersection(c1.index)
        ret = c1[common] / c0[common] - 1
        valid = ret[np.isfinite(ret)]
        counts[w] = int(valid.count())
        if valid.empty:
            out[key] = None
            continue
        pct = (valid.rank(pct=True) * 99).round(1)
        out[key] = float(pct.get(code)) if code in pct.index else None
        if code in valid.index:
            out[f"ret_{w}"] = round(float(valid[code]) * 100, 2)

    primary = next((f"rs_{w}" for w in (252, 126, 63) if out.get(f"rs_{w}") is not None), None)
    out["primary"] = primary
    out["rs"] = out.get(primary)
    if primary:
        out["universe"] = counts[int(primary.split("_")[1])]
        return out
    out["universe"] = 0
    raise RuntimeError(f"缓存中无 {code}（{CACHE}）")



def main():
    parser = argparse.ArgumentParser(description="magician_data.py 数据桥接（xtquant + 全市场缓存）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bars = sub.add_parser("bars", help="日线 OHLCV")
    p_bars.add_argument("code")
    p_bars.add_argument("--days", type=int, default=420)
    p_bars.add_argument("--end", default="")
    p_bars.set_defaults(func=cmd_bars)

    p_rs = sub.add_parser("rs", help="相对强度分位")
    p_rs.add_argument("code")
    p_rs.add_argument("--date", default="")
    p_rs.set_defaults(func=cmd_rs)

    args = parser.parse_args()
    code = normalize_code(args.code)
    if args.cmd == "bars":
        result = args.func(code, args.days, args.end)
    else:
        result = args.func(code, args.date)
    _out(result)


if __name__ == "__main__":
    main()
