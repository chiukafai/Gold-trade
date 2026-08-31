# -*- coding: utf-8 -*-
"""
黄金交易模拟器（虚拟盘 · 不涉及任何真实资金）— CLI 入口
========================================================
核心逻辑（指标/策略/模拟引擎/回测）已抽到 strategy_engine.py（项目内部共享模块，
可进 Docker）。本文件只保留：数据源 Provider + 命令行入口。

数据源（注意边界）：
  - THSProvider   : 同花顺黄金数据专家实时行情（真实当前价/昨收锚点）
                    ⚠️ 依赖 Windows 本机 WorkBuddy 技能路径，仅本地 CLI 可用，不进 Docker
  - CSVProvider   : 你导入的任意真实日线 OHLCV（用于多轮回测）
  - SyntheticProvider : 演示用模拟行情数据（明确标注，非真实行情）

运行示例：
  python gold_simulator.py --mode synthetic --points 1200 --validate
  python gold_simulator.py --mode live          # 用真实伦敦金现锚点跑一盘（仅 Windows 本地）
  python gold_simulator.py --mode csv --csv path/to/gold_daily.csv
"""

from __future__ import annotations
import argparse
import json
import math
import os
import random
import subprocess
import sys
from datetime import datetime, timedelta

# 复用共享策略引擎
from strategy_engine import (
    Bar, Params, Strategy, Simulator, walk_forward, param_sweep,
    print_report, run_backtest,
)


# ============================================================
# 1. 数据源 Provider
# ============================================================
class THSProvider:
    """同花顺黄金数据专家实时行情（真实当前价锚点）。仅 Windows 本地 CLI 可用。"""
    SKILL_DIR = r"C:\Users\Perfect\.workbuddy\plugins\marketplaces\experts\plugins\ths-gold-data-expert\skills\ths-gold-market-quote"
    SYMBOLS = {"london-gold": "伦敦金现", "au9999": "AU9999", "autd": "AUTD"}

    @staticmethod
    def fetch_live(symbol: str = "london-gold") -> dict:
        """返回 {name, last, prev_close, volume, ts} 真实数据。"""
        py = r"C:\Users\Perfect\.workbuddy\binaries\python\versions\3.13.12\python.exe"
        try:
            out = subprocess.run(
                [py, "scripts/query.py", "--symbol", symbol],
                cwd=THSProvider.SKILL_DIR, capture_output=True, text=True, timeout=30,
            ).stdout
            d = json.loads(out)
            it = d["items"][0]
            lv = it["latest"]
            return {
                "name": it["name"],
                "last": float(lv["latest_price"]),
                "prev_close": float(it.get("base_price", lv["latest_price"])),
                "volume": float(lv.get("volume", 0) or 0),
                "ts": lv.get("timestamp_local", ""),
                "points": it.get("point_count", 0),
            }
        except Exception as e:
            return {"error": str(e)}


class CSVProvider:
    """加载真实日线 OHLCV（至少含 date,close，可选 open/high/low/volume）。"""
    @staticmethod
    def load(path: str) -> list[Bar]:
        bars = []
        with open(path, encoding="utf-8-sig") as f:
            header = f.readline().strip().split(",")
            idx = {h.lower(): i for i, h in enumerate(header)}
            ci = idx.get("close") or idx.get("收盘")
            oi = idx.get("open") or idx.get("开盘")
            hi = idx.get("high") or idx.get("最高")
            lo = idx.get("low") or idx.get("最低")
            vi = idx.get("volume") or idx.get("成交量")
            di = idx.get("date") or idx.get("日期") or 0
            for line in f:
                p = line.strip().split(",")
                if len(p) <= max(ci, di):
                    continue
                try:
                    c = float(p[ci])
                    b = Bar(t=datetime.now(), close=c)
                    if oi is not None: b.high = float(p[hi]) if hi is not None else c
                    if lo is not None: b.low = float(p[lo]) if lo is not None else c
                    if vi is not None: b.volume = float(p[vi] or 0)
                    if di is not None:
                        try: b.t = datetime.strptime(p[di], "%Y-%m-%d")
                        except Exception: pass
                    bars.append(b)
                except ValueError:
                    continue
        return bars


class SyntheticProvider:
    """演示用模拟行情数据（明确标注，非真实行情）。
    正弦周期 + 噪声，使价格有涨有跌、RSI 在 30~70 间循环，
    足以演示'逢低吸纳/高位套现'双向信号。"""
    @staticmethod
    def gen(n: int = 1000, seed: int = 42, start: float = 4600.0) -> list[Bar]:
        random.seed(seed)
        bars = []
        price = start
        t = datetime(2025, 1, 1)
        for i in range(n):
            cycle = 0.16 * math.sin(i / 20.0) + 0.07 * math.sin(i / 6.0)
            target = start * (1 + cycle)
            price = price * 0.6 + target * 0.4
            price *= (1 + random.gauss(0, 0.004))
            price = max(price, 1.0)
            hi = price * (1 + abs(random.gauss(0, 0.003)))
            lo = price * (1 - abs(random.gauss(0, 0.003)))
            vol = max(0, random.gauss(1000, 300) + (300 if abs(cycle) > 0.08 else 0))
            bars.append(Bar(t=t + timedelta(days=i), close=price, high=hi, low=lo, volume=vol))
        return bars


# ============================================================
# 2. CLI
# ============================================================
def main():
    ap = argparse.ArgumentParser(description="黄金虚拟交易模拟器")
    ap.add_argument("--mode", choices=["synthetic", "live", "csv"], default="synthetic")
    ap.add_argument("--csv", help="CSV 路径（mode=csv 时必填）")
    ap.add_argument("--points", type=int, default=1000)
    ap.add_argument("--symbol", default="london-gold")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--validate", action="store_true", help="多轮 walk-forward + 参数扫描")
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    print("⚠️ 虚拟模拟 · 不涉及任何真实资金 · 信号仅供策略验证，不构成投资建议")
    live_info = None
    if args.mode == "live":
        li = THSProvider.fetch_live(args.symbol)
        if "error" in li:
            print("实时行情获取失败：", li["error"], "→ 退回模拟行情演示")
            bars = SyntheticProvider.gen(args.points, args.seed)
        else:
            live_info = li
            bars = SyntheticProvider.gen(args.points, args.seed, start=li["last"])
            print(f"已载入真实锚点：{li['name']} 当前 {li['last']}（历史K线用模拟数据延续，明确标注）")
    elif args.mode == "csv":
        if not args.csv or not os.path.exists(args.csv):
            print("请提供有效 --csv 路径"); sys.exit(1)
        bars = CSVProvider.load(args.csv)
        print(f"已载入 CSV 真实日线：{len(bars)} 根")
    else:
        bars = SyntheticProvider.gen(args.points, args.seed)
        print(f"模拟行情数据（演示用，非真实行情）：{len(bars)} 根")

    base = Params()
    sim = Simulator(capital=args.capital, leverage=args.leverage)
    res = sim.run(bars, Strategy(base))
    print_report(f"单盘模拟（{args.mode}）", bars, res, live_info)

    if args.validate:
        print("\n>>> 多轮 walk-forward 验证（窗口250，步长250）:")
        for name, st in walk_forward(bars, base, win=250, step=250):
            print(f"  {name}: 笔数{st['笔数']} 胜率{st['胜率%']}% 收益{st['总收益率%']}% "
                  f"回撤{st['最大回撤%']}% 盈亏比{st['盈亏比']}")
        print("\n>>> 参数扫描（RSI 超卖/超买组合）:")
        grid = {"rsi_oversold": [25, 30, 35], "rsi_overbought": [65, 70, 75]}
        best = None
        for params, st in param_sweep(bars, base, grid):
            tag = f"超卖{params['rsi_oversold']}/超买{params['rsi_overbought']}"
            print(f"  {tag}: 笔数{st['笔数']} 胜率{st['胜率%']}% 收益{st['总收益率%']}% "
                  f"回撤{st['最大回撤%']}% 盈亏比{st['盈亏比']}")
            if best is None or st["总收益率%"] > best[1]["总收益率%"]:
                best = (tag, st)
        if best:
            print(f"  ★ 最优参数组合: {best[0]} 收益率{best[1]['总收益率%']}%")


if __name__ == "__main__":
    main()
