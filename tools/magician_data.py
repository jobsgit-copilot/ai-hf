#!/usr/bin/env python3
"""magician_data.py — 股票魔法师数据工具：日线 / 趋势模板8条 / 相对强度 / 四阶段。

方法论来源：docs/Magician/《股票魔法师》系列
  - 趋势模板 8 条、四阶段理论：b1_ch5（《纵横天下股市的奥秘》）
  - 相对强度 RS、领头羊：b1_ch9
设计文档：docs/股票魔法师Skills设计文档.md（章节 6.2）

用法：
    python3 tools/magician_data.py bars 600519 [--days 420] [--end 20260811] [--json]
    python3 tools/magician_data.py trend 600519 [--date 20260811] [--json]
    python3 tools/magician_data.py rs 300308 [--date 20260731] [--json]
    python3 tools/magician_data.py stage 600519 [--date 20260811] [--json]

数据后端（自动选择）：
  - 日线：ai2miniqmt 虚拟环境中的 xtquant（前复权、最新数据）
  - 相对强度：全市场日线缓存 raw_cache_full.pkl（后复权，2019 至今，横截面分位）
  两者均通过 tools/magician_xt.py 桥接脚本访问；本文件仅依赖 Python 标准库。

环境变量：
  AI2MINIQMT_PY   ai2miniqmt 虚拟环境 python 路径
  MAGICIAN_CACHE  全市场日线缓存 pkl 路径
"""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).resolve().parent
BRIDGE_SCRIPT = TOOLS_DIR / "magician_xt.py"

DEFAULT_AI2MINIQMT_PY = r"D:\Users\projects\ai2miniqmt\.venv\Scripts\python.exe"
DEFAULT_CACHE_PKL = r"D:\Users\Documents\AI-Finance\squeeze\raw_cache_full.pkl"

STAGE_NAMES = {
    "stage1": "第一阶段（筑底/忽略）",
    "stage2": "第二阶段（上升/唯一可买）",
    "stage3": "第三阶段（派发/危险）",
    "stage4": "第四阶段（下跌/禁止）",
}


def _reconfigure_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def normalize_code(code: str) -> str:
    """把 600519 / 600519.SH / 600519SH 统一成 600519.SH。"""
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


def _run_bridge(args, timeout=180) -> dict:
    """在 ai2miniqmt 虚拟环境中执行桥接脚本并解析 JSON。"""
    py = os.environ.get("AI2MINIQMT_PY", DEFAULT_AI2MINIQMT_PY)
    if not Path(py).exists():
        raise RuntimeError(
            f"未找到 ai2miniqmt 虚拟环境 python：{py}\n"
            f"可用环境变量 AI2MINIQMT_PY 指定路径。"
        )
    proc = subprocess.run(
        [py, str(BRIDGE_SCRIPT)] + [str(a) for a in args],
        capture_output=True,
        timeout=timeout,
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", errors="replace").strip() or out.strip()
        raise RuntimeError(f"数据桥接失败：{err}")
    start, end = out.find("{"), out.rfind("}")
    if start == -1 or end == -1:
        raise RuntimeError(f"数据桥接输出异常：{out.strip()[:200]}")
    return json.loads(out[start : end + 1])


def load_bars(code: str, days: int = 420, end: str = "") -> dict:
    args = ["bars", code, "--days", str(days)]
    if end:
        args += ["--end", end]
    return _run_bridge(args)


def load_rs(code: str, asof: str = "") -> dict:
    args = ["rs", code]
    if asof:
        args += ["--date", asof]
    return _run_bridge(args)


# ---------------------------------------------------------------- 指标计算

def _ma_at(closes, idx, n):
    """closes[idx-n+1..idx] 的简单均值；样本不足返回 None。"""
    if idx + 1 < n:
        return None
    return round(sum(closes[idx + 1 - n : idx + 1]) / n, 3)


def compute_indicators(bars, idx):
    """在 bars[idx] 处计算趋势指标。bars 为按日期升序的 dict 列表。"""
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    close = closes[idx]
    ma50 = _ma_at(closes, idx, 50)
    ma150 = _ma_at(closes, idx, 150)
    ma200 = _ma_at(closes, idx, 200)
    ma200_prev = _ma_at(closes, idx - 20, 200)
    win_start = max(0, idx - 251)
    low52 = min(lows[win_start : idx + 1])
    high52 = max(highs[win_start : idx + 1])
    ma200_rising = None if (ma200 is None or ma200_prev is None) else ma200 > ma200_prev
    dist_low52 = (close / low52 - 1) * 100 if low52 else None
    dist_high52 = (high52 / close - 1) * 100 if close else None
    return {
        "date": bars[idx]["date"],
        "close": close,
        "ma50": ma50,
        "ma150": ma150,
        "ma200": ma200,
        "ma200_prev": ma200_prev,
        "ma200_rising": ma200_rising,
        "low52": low52,
        "high52": high52,
        "dist_low52_pct": round(dist_low52, 2) if dist_low52 is not None else None,
        "dist_high52_pct": round(dist_high52, 2) if dist_high52 is not None else None,
        "rs": None,
    }


def trend_rules(m):
    """趋势模板 8 条（b1_ch5）。返回 (rules, passed_count)。"""
    def _num(x):
        return round(x, 2) if x is not None else None

    rules = [
        {"id": 1, "name": "收盘价 > 150 日均线",
         "passed": m["close"] > m["ma150"] if m["ma150"] else False,
         "detail": f"close={_num(m['close'])} ma150={_num(m['ma150'])}"},
        {"id": 2, "name": "收盘价 > 200 日均线",
         "passed": m["close"] > m["ma200"] if m["ma200"] else False,
         "detail": f"close={_num(m['close'])} ma200={_num(m['ma200'])}"},
        {"id": 3, "name": "200 日均线上行 ≥ 1 个月（20 个交易日）",
         "passed": bool(m["ma200_rising"]),
         "detail": f"ma200={_num(m['ma200'])} 20日前={_num(m['ma200_prev'])}"},
        {"id": 4, "name": "50 日均线 > 150 日均线 > 200 日均线",
         "passed": bool(m["ma50"] and m["ma150"] and m["ma200"] and m["ma50"] > m["ma150"] > m["ma200"]),
         "detail": f"ma50={_num(m['ma50'])} ma150={_num(m['ma150'])} ma200={_num(m['ma200'])}"},
        {"id": 5, "name": "收盘价 > 50 日均线",
         "passed": m["close"] > m["ma50"] if m["ma50"] else False,
         "detail": f"close={_num(m['close'])} ma50={_num(m['ma50'])}"},
        {"id": 6, "name": "距 52 周低点 ≥ 25%",
         "passed": (m["dist_low52_pct"] or 0) >= 25,
         "detail": f"low52={_num(m['low52'])} 距离={_num(m['dist_low52_pct'])}%"},
        {"id": 7, "name": "距 52 周高点 ≤ 25%",
         "passed": (m["dist_high52_pct"] if m["dist_high52_pct"] is not None else 999) <= 25,
         "detail": f"high52={_num(m['high52'])} 距离={_num(m['dist_high52_pct'])}%"},
        {"id": 8, "name": "相对强度 RS ≥ 70",
         "passed": (m["rs"] or 0) >= 70,
         "detail": f"RS={_num(m['rs'])}"},
    ]
    return rules, sum(1 for r in rules if r["passed"])


def judge_stage(m):
    """四阶段启发式判定（供人工复核，非教科书精确分类）。"""
    if m["ma200"] is None or m["close"] is None:
        return "unknown"
    if m["close"] < m["ma200"]:
        return "stage4" if m["ma200_rising"] is False else "stage1"
    if m["close"] < m["ma50"]:
        return "stage3" if m["ma200_rising"] else "stage1"
    if m["ma50"] and m["ma150"] and m["ma50"] > m["ma150"] > m["ma200"]:
        return "stage2"
    return "stage1"


def make_verdict(stage, passed, total=8):
    if stage != "stage2":
        return "fail", f"非第二阶段（{STAGE_NAMES.get(stage, stage)}）"
    if passed < total:
        return "watch", f"第二阶段但趋势模板 {passed}/{total} 未全过"
    return "pass", f"趋势模板 {passed}/{total} 全过，符合第二阶段买入条件"


# ---------------------------------------------------------------- 子命令

def cmd_bars(args):
    code = normalize_code(args.code)
    data = load_bars(code, args.days, args.end or "")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"标的: {data['code']}  来源: {data['source']}({data['adjust']})")
    print(f"{'日期':<12}{'开盘':>10}{'最高':>10}{'最低':>10}{'收盘':>10}{'成交量':>12}")
    for b in data["bars"][-args.show :]:
        vol = b["volume"] if b["volume"] is not None else 0
        print(f"{b['date']:<12}{b['open']:>10.2f}{b['high']:>10.2f}{b['low']:>10.2f}{b['close']:>10.2f}{vol:>12.0f}")


def _resolve_asof(bars, date):
    if not date:
        return bars[-1]["date"]
    dates = [b["date"] for b in bars]
    if date in dates:
        return date
    before = [d for d in dates if d <= date]
    if before:
        return before[-1]
    raise RuntimeError(f"日期 {date} 早于可用行情 {dates[0]}")


def _build_trend(code, asof):
    data = load_bars(code, days=420, end=asof or "")
    blist = data["bars"]
    if not blist:
        raise RuntimeError(f"未获取到 {code} 的日线数据")
    resolved = _resolve_asof(blist, asof)
    idx = [b["date"] for b in blist].index(resolved)
    m = compute_indicators(blist, idx)
    rs = load_rs(code, resolved)
    m["rs"] = rs.get("rs")
    rules, passed = trend_rules(m)
    stage = judge_stage(m)
    verdict, reason = make_verdict(stage, passed)
    return {
        "code": code,
        "asof": resolved,
        "source": data.get("source", ""),
        "adjust": data.get("adjust", ""),
        "metrics": m,
        "rs": {k: v for k, v in rs.items() if k != "code"},
        "rules": rules,
        "passed_count": passed,
        "stage": stage,
        "stage_name": STAGE_NAMES.get(stage, stage),
        "verdict": verdict,
        "reason": reason,
    }


def cmd_trend(args):
    code = normalize_code(args.code)
    result = _build_trend(code, args.date or "")
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return
    m = result["metrics"]
    rs = result["rs"]
    print(f"标的: {result['code']}  数据截至: {result['asof']}  "
          f"来源: {result['source']}({result['adjust']})")
    print(f"收盘: {m['close']:.2f}   MA50: {m['ma50']}   MA150: {m['ma150']}   MA200: {m['ma200']}")
    print(f"52周低/高: {m['low52']} / {m['high52']}   "
          f"距低点 {m['dist_low52_pct']}%   距高点 {m['dist_high52_pct']}%")
    if rs.get("rs") is not None:
        print(f"RS: {rs['rs']}（126日={rs.get('rs_126')} 252日={rs.get('rs_252')}；"
              f"全市场 {rs.get('universe')} 只，缓存截至 {rs.get('asof')}）")
    print(f"趋势模板: {result['passed_count']}/8 通过")
    for r in result["rules"]:
        mark = "x" if r["passed"] else " "
        print(f"  [{mark}] {r['id']}. {r['name']}  ({r['detail']})")
    print(f"阶段: {result['stage_name']}")
    print(f"结论: {result['verdict'].upper()} — {result['reason']}")


def cmd_rs(args):
    code = normalize_code(args.code)
    data = load_rs(code, args.date or "")
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    print(f"标的: {data['code']}  截至: {data['asof']}  来源: {data['source']}")
    for w in (63, 126, 252):
        key = f"rs_{w}"
        if data.get(key) is not None:
            ret = data.get(f"ret_{w}")
            ret_s = f"  近{w}日涨幅 {ret}%" if ret is not None else ""
            print(f"  {w}日相对强度分位: {data[key]}{ret_s}")
    print(f"  主 RS（用于趋势模板第8条）: {data.get('rs')}")
    print(f"  全市场 {data.get('universe')} 只标的参与排序；缓存最新日期 {data.get('cache_max_date')}")


def cmd_stage(args):
    code = normalize_code(args.code)
    result = _build_trend(code, args.date or "")
    if args.json:
        print(json.dumps({"code": result["code"], "asof": result["asof"],
                          "stage": result["stage"], "stage_name": result["stage_name"],
                          "verdict": result["verdict"]}, ensure_ascii=False, indent=2))
        return
    print(f"{result['code']} 截至 {result['asof']}: {result['stage_name']}  "
          f"({result['verdict']} — {result['reason']})")


def main():
    _reconfigure_stdout()
    parser = argparse.ArgumentParser(description="股票魔法师数据工具（趋势模板/RS/阶段）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_bars = sub.add_parser("bars", help="日线 OHLCV（前复权）")
    p_bars.add_argument("code")
    p_bars.add_argument("--days", type=int, default=420)
    p_bars.add_argument("--end", default="", help="YYYYMMDD，默认最新")
    p_bars.add_argument("--show", type=int, default=10, help="非 JSON 模式显示最近 N 根")
    p_bars.add_argument("--json", action="store_true")
    p_bars.set_defaults(func=cmd_bars)

    p_trend = sub.add_parser("trend", help="趋势模板 8 条 + 阶段判定")
    p_trend.add_argument("code")
    p_trend.add_argument("--date", default="", help="评估日期 YYYYMMDD，默认最新")
    p_trend.add_argument("--json", action="store_true")
    p_trend.set_defaults(func=cmd_trend)

    p_rs = sub.add_parser("rs", help="全市场相对强度分位（0-99）")
    p_rs.add_argument("code")
    p_rs.add_argument("--date", default="", help="评估日期 YYYYMMDD，默认缓存最新")
    p_rs.add_argument("--json", action="store_true")
    p_rs.set_defaults(func=cmd_rs)

    p_stage = sub.add_parser("stage", help="四阶段判定")
    p_stage.add_argument("code")
    p_stage.add_argument("--date", default="", help="评估日期 YYYYMMDD，默认最新")
    p_stage.add_argument("--json", action="store_true")
    p_stage.set_defaults(func=cmd_stage)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
