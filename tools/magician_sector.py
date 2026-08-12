#!/usr/bin/env python3
"""magician_sector.py — 第三梯队：板块联动（行业归属 + 板块动量 + 板块共振）。

数据源（squeeze 目录）：
  - index_member.pkl:     申万一级行业成分股及进出时间（point-in-time 行业归属）
  - industry_sw.pkl:      行业代码→名称
  - industry_index_daily.pkl: 申万一级行业指数日线（2019~2026-07-31）

输出字段（annotate_events 附加到事件）：
  - industry / industry_name: 事件日所在申万一级行业（point-in-time，缺失=None）
  - sector_ret_63 / sector_ret_126: 行业指数 63/126 交易日涨幅
  - sector_above_ma200: 行业指数是否站上 MA200
  - sector_rs: 行业 126 日涨幅在 31 个一级行业中的分位（0-100）
  - sector_n_vcp_20d: 近 20 个交易日同行业 VCP(breakout/setup) 事件涉及的不同标的数（板块共振）

约定：数据缺失（无行业归属 / 行业指数未覆盖）一律为 None，规则层按"缺失不否决"处理。
"""
import bisect
from pathlib import Path

import numpy as np
import pandas as pd

DEFAULT_SQUEEZE = Path(r"D:\Users\Documents\AI-Finance\squeeze")
LOOKBACK_DAYS = 20
WINDOWS = (63, 126)


def _norm(c):
    return str(c).upper()


class SectorDB:
    def __init__(self, member_pkl, sw_pkl, idx_daily_pkl):
        mem = pd.read_pickle(member_pkl)
        mem["in_date"] = pd.to_datetime(mem["in_date"])
        mem["out_date"] = pd.to_datetime(mem["out_date"])
        # 每股的 (in_date, out_date, index_code) 列表，按 in_date 升序
        self._mem = {}
        for r in mem.itertuples(index=False):
            code = _norm(r.con_code)
            self._mem.setdefault(code, []).append(
                (r.in_date, r.out_date if pd.notna(r.out_date) else None, r.index_code))
        for v in self._mem.values():
            v.sort(key=lambda x: x[0])

        sw = pd.read_pickle(sw_pkl)
        self._names = dict(zip(sw["index_code"], sw["industry_name"]))

        idx = pd.read_pickle(idx_daily_pkl)
        idx["trade_date"] = pd.to_datetime(idx["trade_date"])
        self._idx_dates = {}
        self._idx_close = {}
        for c, g in idx.groupby("ts_code"):
            g = g.sort_values("trade_date")
            self._idx_dates[c] = g["trade_date"].to_numpy(dtype="datetime64[ns]")
            self._idx_close[c] = g["close"].to_numpy(dtype=float)

        self._all_dates = sorted({d for arr in self._idx_dates.values() for d in arr})
        self._date_pos = {d: i for i, d in enumerate(self._all_dates)}
        self._ret_cache = {}   # (industry, window) -> {date_index: ret}
        self._ma200_cache = {}  # industry -> {date_index: above}

    def industry_of(self, code, date):
        """返回事件日所在行业 (index_code, name)；无归属返回 None。"""
        for in_d, out_d, idx_code in self._mem.get(_norm(code), []):
            if in_d <= date and (out_d is None or out_d > date):
                return idx_code, self._names.get(idx_code)
        return None

    def _ret_at(self, industry, date, window):
        arr_d = self._idx_dates.get(industry)
        if arr_d is None or len(arr_d) <= window:
            return None
        i = int(np.searchsorted(arr_d, np.datetime64(date), side="right") - 1)
        if i < window:
            return None
        close = self._idx_close[industry]
        return float(close[i] / close[i - window] - 1) * 100

    def _above_ma200_at(self, industry, date):
        arr_d = self._idx_dates.get(industry)
        close = self._idx_close[industry]
        if arr_d is None or len(arr_d) < 200:
            return None
        i = int(np.searchsorted(arr_d, np.datetime64(date), side="right") - 1)
        if i < 199:
            return None
        ma = close[i - 199: i + 1].mean()
        return bool(close[i] > ma)

    def sector_rs_at(self, date):
        """当日 31 行业 126 日涨幅的分位排名：{index_code: pct}。"""
        d = np.datetime64(date)
        p = self._date_pos.get(d)
        if p is None:
            return None
        rets = {}
        for ind in self._idx_dates:
            i = int(np.searchsorted(self._idx_dates[ind], d, side="right") - 1)
            if i >= 126:
                close = self._idx_close[ind]
                rets[ind] = close[i] / close[i - 126] - 1
        if not rets:
            return None
        vals = np.array(sorted(rets.values()))
        out = {}
        for ind, r in rets.items():
            out[ind] = float((vals < r).mean() * 100)
        return out

    def annotate(self, events):
        """为事件列表附加板块字段（原地修改并返回 events）。"""
        # 板块共振：按行业分组统计近 LOOKBACK_DAYS 内 VCP(breakout/setup) 的标的总数
        from collections import defaultdict
        by_ind = defaultdict(list)  # index_code -> [event, ...]
        for ev in events:
            ind = self.industry_of(ev["code"], pd.Timestamp(ev["date"]))
            ev["industry"] = ind[0] if ind else None
            ev["industry_name"] = ind[1] if ind else None
            ev["sector_ret_63"] = self._ret_at(ind[0], ev["date"], 63) if ind else None
            ev["sector_ret_126"] = self._ret_at(ind[0], ev["date"], 126) if ind else None
            ev["sector_above_ma200"] = self._above_ma200_at(ind[0], ev["date"]) if ind else None
            ev["sector_n_vcp_20d"] = None
            if ind:
                by_ind[ind[0]].append(ev)

        # 行业 126 日涨幅分位（按事件日期取值）
        rs_cache = {}
        for ev in events:
            ind = ev["industry"]
            if ind is None:
                ev["sector_rs"] = None
                continue
            date = pd.Timestamp(ev["date"])
            key = date
            if key not in rs_cache:
                rs_cache[key] = self.sector_rs_at(date)
            rr = rs_cache[key]
            ev["sector_rs"] = rr.get(ind) if rr else None

        # 板块共振：同行业近 20 交易日 VCP(breakout/setup) 唯一标的数
        for ind, evs in by_ind.items():
            evs.sort(key=lambda e: e["date"])
            dates = [e["date"] for e in evs]
            for k, ev in enumerate(evs):
                lo = bisect.bisect_left(dates, (pd.Timestamp(ev["date"]) - pd.Timedelta(days=40)).strftime("%Y-%m-%d"), 0, k + 1)
                codes = set()
                for e in evs[lo:k + 1]:
                    if e["status"] in ("breakout", "setup"):
                        codes.add(e["code"])
                ev["sector_n_vcp_20d"] = len(codes)
        return events


def annotate_events(events, squeeze_dir=DEFAULT_SQUEEZE):
    db = SectorDB(squeeze_dir / "index_member.pkl", squeeze_dir / "industry_sw.pkl",
                  squeeze_dir / "industry_index_daily.pkl")
    return db.annotate(events)


if __name__ == "__main__":
    import json
    import sys
    events = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if len(sys.argv) > 2:
        out = Path(sys.argv[2])
    else:
        out = Path(sys.argv[1]).with_suffix(".sector.json")
    annotate_events(events)
    out.write_text(json.dumps(events, ensure_ascii=False), encoding="utf-8")
    print(f"板块字段已附加：{len(events)} 事件 → {out}")
    from collections import Counter
    print("行业覆盖率:", round(sum(1 for e in events if e["industry"]) / len(events) * 100, 1), "%")
    print("sector_rs 覆盖:", round(sum(1 for e in events if e["sector_rs"] is not None) / len(events) * 100, 1), "%")
    print("sector_n_vcp_20d 分布:", Counter(e["sector_n_vcp_20d"] for e in events).most_common(6))
