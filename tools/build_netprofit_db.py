#!/usr/bin/env python3
"""build_netprofit_db.py — 第三梯队：补齐净利润到 point-in-time 财务快照。

在 ai2miniqmt 虚拟环境中运行（依赖 tushare + TS_TOKEN）：
    D:\\Users\\projects\\ai2miniqmt\\.venv\\Scripts\\python.exe tools/build_netprofit_db.py

代码全集来自 stock_basic.pkl（5534 只沪深A股）；输出 finance_income.pkl 扩展为
[ts_code, end_date, ann_date, revenue, n_income_attr_p, n_income, basic_eps]。
净利润同比（np_yoy）将由 magician_fundamental.py 按 ann_date 做 point-in-time 计算。
"""
import argparse
import time
from pathlib import Path

import pandas as pd

SQ = Path(r"D:\Users\Documents\AI-Finance\squeeze")
ENV = Path(r"D:\Users\projects\ai2miniqmt\20_bars\.env")
FIELDS = "ts_code,end_date,ann_date,revenue,n_income_attr_p,n_income,basic_eps"


def load_token() -> str:
    token = ""
    if ENV.exists():
        for line in ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("TS_TOKEN="):
                token = line.split("=", 1)[1].strip().strip("\"'")
    if not token:
        raise RuntimeError("未找到 TS_TOKEN（ai2miniqmt/20_bars/.env）")
    return token


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SQ / "finance_income.pkl"))
    ap.add_argument("--start", default="20190101")
    ap.add_argument("--end", default="20260812")
    ap.add_argument("--smoke", type=int, default=0, help="仅拉取前 N 只（调试）")
    args = ap.parse_args()

    import tushare as ts
    pro = ts.pro_api(load_token())

    basic = pd.read_pickle(SQ / "stock_basic.pkl")
    codes = sorted(basic["ts_code"].unique().tolist())
    if args.smoke:
        codes = codes[: args.smoke]
    print(f"共 {len(codes)} 只标的，拉取 {args.start}~{args.end}", flush=True)

    frames = []
    t0 = time.time()
    for i, c in enumerate(codes):
        for attempt in range(4):
            try:
                df = pro.income(ts_code=c, start_date=args.start, end_date=args.end, fields=FIELDS)
                if df is not None and not df.empty:
                    frames.append(df)
                break
            except Exception as e:
                if attempt == 3:
                    print(f"[跳过] {c}: {str(e)[:80]}", flush=True)
                else:
                    time.sleep(2 + attempt * 3)
        time.sleep(0.35)
        if (i + 1) % 200 == 0 or i + 1 == len(codes):
            print(f"{i + 1}/{len(codes)}  {time.time()-t0:.0f}s", flush=True)

    new = pd.concat(frames, ignore_index=True)
    new = new.drop_duplicates(["ts_code", "end_date", "ann_date"]).sort_values(["ts_code", "ann_date", "end_date"])
    new.to_pickle(args.out)
    print(f"完成：{len(new)} 行 → {args.out}", flush=True)


if __name__ == "__main__":
    main()
