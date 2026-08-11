#!/usr/bin/env python3
"""vcp_detector.py — VCP（波动收缩模式）识别与买点检测。

方法论来源：docs/Magician/《股票魔法师：纵横天下股市的奥秘》b1_ch10（波动收缩规律）
设计文档：docs/股票魔法师Skills设计文档.md（章节 6.3）

VCP 判定要点（与原著一致）：
  - 上升趋势中的基底内，回调幅度逐次收缩（每次约为前次一半，2~6 次，通常 2~4 次）
  - 成交量随收缩逐级萎缩（供给出清）
  - 技术足迹格式：{宽度W}W {首段深度}/{末段深度} {次数}T，如 6W 32/6 3T
  - 买点 = 基底最高点（中枢点）放量突破

用法：
    python3 tools/vcp_detector.py detect 300308 [--days 60] [--json]
    python3 tools/vcp_detector.py scan 300308 600519 [--json]
    python3 tools/vcp_detector.py scan --pool 候选池文件 [--json]

数据来自 magician_data.py（ai2miniqmt xtquant 前复权日线 / 全市场缓存回退）。
本文件仅依赖 Python 标准库。
"""

import argparse
import json
import os
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS_DIR))

import magician_data as md  # noqa: E402

try:
    import numpy as np
    from numpy.lib.stride_tricks import sliding_window_view

    _HAS_NP = True
except ImportError:  # 无 numpy 环境使用纯标准库回退
    _HAS_NP = False

DEFAULT_DAYS = 60          # 基底分析窗口（交易日，约 12 周）
DEFAULT_K = 3              # 摆动低点/高点局部邻域半径
MIN_GAP = 5                # 两个摆动低点的最小间隔（交易日）
MIN_DEPTH_PCT = 2.0        # 有效收缩的最小深度（%）
MAX_DEPTH_PCT = 50.0       # 收缩最大深度（超过视为崩溃而非基底）
DEPTH_TOL = 1.05           # 相邻收缩深度允许的松弛系数（须递减）
VOL_TOL = 0.60             # 末段量能首段的上限系数（量能萎缩：末段 <= 首段*0.6，与设计文档一致）
BREAK_VOL_MULT = 1.5       # 突破日量能 vs 20日均量的倍数（量能确认）


def _reconfigure_stdout():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- 纯计算逻辑

def swing_lows(bars, k=DEFAULT_K):
    """返回摆动低点索引列表（low 为 ±k 邻域内最低）。"""
    lows = [b["low"] for b in bars]
    n = len(lows)
    if n <= 2 * k:
        return []
    if _HAS_NP:
        a = np.asarray(lows, dtype=float)
        w = sliding_window_view(a, 2 * k + 1)
        idx = np.flatnonzero(w[:, k] <= w.min(axis=1)) + k
        return [int(i) for i in idx]
    out = []
    for i in range(k, n - k):
        if all(lows[i] <= lows[j] for j in range(i - k, i + k + 1) if j != i):
            out.append(i)
    return out


def swing_highs(bars, k=DEFAULT_K):
    """返回摆动高点索引列表（high 为 ±k 邻域内最高）。"""
    highs = [b["high"] for b in bars]
    n = len(highs)
    if n <= 2 * k:
        return []
    if _HAS_NP:
        a = np.asarray(highs, dtype=float)
        w = sliding_window_view(a, 2 * k + 1)
        idx = np.flatnonzero(w[:, k] >= w.max(axis=1)) + k
        return [int(i) for i in idx]
    out = []
    for i in range(k, n - k):
        if all(highs[i] >= highs[j] for j in range(i - k, i + k + 1) if j != i):
            out.append(i)
    return out


def _dedupe_minima(bars, lows, gap=MIN_GAP):
    """两个低点间隔 < gap 时只保留更低的一个。"""
    if not lows:
        return []
    dedup = [lows[0]]
    for i in lows[1:]:
        if i - dedup[-1] < gap:
            if bars[i]["low"] < bars[dedup[-1]]["low"]:
                dedup[-1] = i
        else:
            dedup.append(i)
    return dedup


def analyze_vcp(bars, window_days=DEFAULT_DAYS, min_contractions=2, k=DEFAULT_K):
    """在最近 window_days 根 K 线内识别 VCP。返回判定字典。"""
    if len(bars) < window_days + 2:
        raise ValueError(f"行情样本不足（需要 >{window_days} 根，实际 {len(bars)}）")
    win = bars[-window_days:]
    all_lows = _dedupe_minima(win, swing_lows(win, k))
    all_highs = swing_highs(win, k)
    # 中枢点候选：其后存在回调低点的摆动高点（排除突破日这类无回调结构的新高）
    candidates = [i for i in all_highs if any(j > i for j in all_lows)]
    no_pivot = not candidates
    if candidates:
        pivot_idx_local = max(candidates, key=lambda i: win[i]["high"])
    else:
        pivot_idx_local = max(range(len(win)), key=lambda i: win[i]["high"])
    pivot = win[pivot_idx_local]["high"]
    pivot_date = win[pivot_idx_local]["date"]

    lows = [i for i in all_lows if i > pivot_idx_local and i - pivot_idx_local >= k]

    contractions = []
    for i in lows:
        depth = (pivot - win[i]["low"]) / pivot * 100
        if MIN_DEPTH_PCT <= depth <= MAX_DEPTH_PCT:
            seg = win[max(0, i - 4): i + 1]
            vol_avg = sum(b.get("volume") or 0 for b in seg) / len(seg)
            contractions.append({
                "date": win[i]["date"],
                "low": round(win[i]["low"], 3),
                "depth_pct": round(depth, 1),
                "vol_avg": round(vol_avg),
            })

    depths = [c["depth_pct"] for c in contractions]
    has_vcp = False
    reasons = []
    if no_pivot:
        reasons.append("窗口内无有效中枢点（高点后无回调结构）")
    if len(depths) < min_contractions:
        reasons.append(f"收缩次数不足（{len(depths)} < {min_contractions}）")
    else:
        seq_ok = all(depths[i + 1] <= depths[i] * DEPTH_TOL for i in range(len(depths) - 1))
        final_ok = depths[-1] <= depths[0] * 0.7
        if not seq_ok:
            reasons.append("收缩深度未逐次递减")
        if not final_ok:
            reasons.append("末次收缩相对首次未显著收窄（≤70%）")
        if seq_ok and final_ok:
            has_vcp = True

    # 量能收缩：末段收缩量能 <= 首段 * VOL_TOL；无成交量数据（缓存回退）时不判定
    volume_dry = None
    if len(contractions) >= 2:
        v0 = contractions[0]["vol_avg"]
        v1 = contractions[-1]["vol_avg"]
        if v0 > 0 and v1 > 0:
            volume_dry = v1 <= v0 * VOL_TOL
            if not volume_dry:
                reasons.append("量能未随收缩萎缩")

    # 足迹
    width_w = round(window_days / 5)
    footprint = None
    if len(depths) >= 2:
        footprint = f"{width_w}W {int(round(depths[0]))}/{int(round(depths[-1]))} {len(depths)}T"

    # 当前状态
    last_close = win[-1]["close"]
    status = "none"
    if has_vcp:
        if last_close > pivot:
            status = "breakout"
        elif last_close >= pivot * 0.95:
            status = "setup"
        else:
            status = "forming"

    # 突破量能确认（breakout 时）
    breakout_vol_ok = None
    if status == "breakout":
        avg20 = sum((b.get("volume") or 0) for b in win[-21:-1]) / max(1, len(win[-21:-1]))
        cur = win[-1].get("volume") or 0
        breakout_vol_ok = cur >= avg20 * BREAK_VOL_MULT

    # 阶段上下文（供参考，不作为否决条件）
    stage_context = "n/a"
    try:
        m = md.compute_indicators(bars, len(bars) - 1)
        stage_context = md.judge_stage(m)
    except Exception:
        pass

    stop_loss = round(max(pivot * 0.90, min(c["low"] for c in contractions)) if contractions else pivot * 0.90, 3)

    return {
        "code": bars[-1].get("_code", ""),
        "asof": win[-1]["date"],
        "window_days": window_days,
        "pivot": round(pivot, 3),
        "pivot_date": pivot_date,
        "base_low": round(min(c["low"] for c in contractions), 3) if contractions else None,
        "contractions": contractions,
        "depths": depths,
        "footprint": footprint,
        "volume_dry": volume_dry,
        "has_vcp": has_vcp,
        "reasons": reasons,
        "status": status,
        "last_close": round(last_close, 3),
        "breakout_volume_confirmed": breakout_vol_ok,
        "buy_trigger": f"收盘价突破中枢点 {round(pivot, 2)} 且放量" if has_vcp else None,
        "stop_loss": stop_loss,
        "stage_context": stage_context,
    }


# ---------------------------------------------------------------- 数据获取与子命令

def _load(code, days, end=""):
    data = md.load_bars(md.normalize_code(code), days=max(days + 250, 420), end=end)
    bars = data["bars"]
    if not bars:
        raise RuntimeError(f"未获取到 {code} 的日线数据")
    for b in bars:
        b["_code"] = data["code"]
    return data, bars


def cmd_detect(args):
    data, bars = _load(args.code, args.days, args.end)
    result = analyze_vcp(bars, window_days=args.days, min_contractions=args.min_contractions)
    result["code"] = data["code"]
    result["source"] = f"{data.get('source')}({data.get('adjust')})"
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    print(f"标的: {result['code']}  数据截至: {result['asof']}  来源: {result['source']}")
    print(f"中枢点(pivot): {result['pivot']}（{result['pivot_date']}）  基底低点: {result['base_low']}")
    print(f"收缩序列: {result['depths']}%")
    if result["footprint"]:
        print(f"技术足迹: {result['footprint']}（{result['window_days']}日窗口）")
    vd = result["volume_dry"]
    print(f"量能收缩: {'是' if vd else ('无成交量数据' if vd is None else '否')}")
    print(f"VCP: {'成立' if result['has_vcp'] else '不成立'}")
    if result["reasons"]:
        print("  原因: " + "; ".join(result["reasons"]))
    print(f"状态: {result['status']}  阶段上下文: {result['stage_context']}")
    if result["buy_trigger"]:
        print(f"买点: {result['buy_trigger']}  止损: {result['stop_loss']}")
    if result["breakout_volume_confirmed"] is not None:
        print(f"突破量能确认: {'是' if result['breakout_volume_confirmed'] else '否（量能不足）'}")


def _load_pool(pool_path):
    path = Path(pool_path)
    if not path.exists():
        raise RuntimeError(f"候选池文件不存在：{pool_path}")
    raw = path.read_text(encoding="utf-8-sig")
    try:
        obj = json.loads(raw)
    except Exception:
        obj = None
    codes = []
    if isinstance(obj, list):
        codes = [str(c) for c in obj]
    elif isinstance(obj, dict):
        for v in obj.values():
            codes.extend(str(c) for c in (v if isinstance(v, list) else [v]))
    else:
        codes = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    return codes


def cmd_scan(args):
    codes = list(args.codes)
    if args.pool:
        codes.extend(_load_pool(args.pool))
    codes = [md.normalize_code(c) for c in codes]
    if not codes:
        raise RuntimeError("未提供任何股票代码")
    results = []
    for c in codes:
        try:
            data, bars = _load(c, args.days, args.end)
            r = analyze_vcp(bars, window_days=args.days, min_contractions=args.min_contractions)
            r["code"] = data["code"]
            results.append(r)
        except Exception as e:
            results.append({"code": c, "error": str(e), "has_vcp": False, "status": "error"})
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return
    print(f"{'代码':<12}{'状态':<10}{'足迹':<14}{'中枢点':>10}{'收缩':<20}{'阶段':<8}")
    for r in results:
        if r.get("error"):
            print(f"{r['code']:<12}{'ERROR':<10}{r['error'][:50]}")
            continue
        fp = r["footprint"] or "-"
        depths = "/".join(str(int(d)) for d in r["depths"]) or "-"
        print(f"{r['code']:<12}{r['status']:<10}{fp:<14}{r['pivot']:>10.2f}{depths:<20}{r['stage_context']:<8}")


def main():
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="VCP（波动收缩模式）识别与买点检测")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_detect = sub.add_parser("detect", help="检测单只股票的 VCP")
    p_detect.add_argument("code")
    p_detect.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p_detect.add_argument("--min-contractions", type=int, default=2)
    p_detect.add_argument("--end", default="", help="截至日期 YYYYMMDD（历史验证/回测用）")
    p_detect.add_argument("--json", action="store_true")
    p_detect.set_defaults(func=cmd_detect)

    p_scan = sub.add_parser("scan", help="批量扫描候选池")
    p_scan.add_argument("codes", nargs="*")
    p_scan.add_argument("--pool", default="")
    p_scan.add_argument("--days", type=int, default=DEFAULT_DAYS)
    p_scan.add_argument("--min-contractions", type=int, default=2)
    p_scan.add_argument("--end", default="", help="截至日期 YYYYMMDD（历史验证/回测用）")
    p_scan.add_argument("--json", action="store_true")
    p_scan.set_defaults(func=cmd_scan)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
