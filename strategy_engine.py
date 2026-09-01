# -*- coding: utf-8 -*-
"""
共享策略引擎（gold-trade 项目内部模块，无 Windows 依赖，可进 Docker）
========================================================================
从 gold_simulator.py 抽出的纯逻辑：
  - 数据结构 Bar
  - 技术指标 sma/ema/macd/rsi/bollinger/obv
  - 策略 Strategy（多指标共振 + 逢低吸纳/高位套现 + 量能确认）
  - 模拟引擎 Simulator（持仓/盈亏/手续费/杠杆）
  - 多轮验证 walk_forward / param_sweep
  - bars_from_history_points：把项目行情管道 fetch_gold_history 的结果转成 Bar
  - run_backtest：一键回测并返回 JSON 友好的结果（供网页 /api/backtest 用）

THSProvider / CSVProvider / SyntheticProvider 等数据源留在 gold_simulator.py
（含 Windows 本地路径，不进容器）。本模块只负责"有 Bar 之后怎么算"。
"""

from __future__ import annotations
import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime


# ============================================================
# 1. 数据结构
# ============================================================
@dataclass
class Bar:
    t: datetime
    close: float
    volume: float = 0.0
    high: float = 0.0
    low: float = 0.0

    def ohlc(self) -> "Bar":
        if self.high == 0 and self.low == 0:
            self.high = self.close
            self.low = self.close
        return self


def bars_from_history_points(points: list[dict]) -> list[Bar]:
    """把 fetch_gold_history() 返回的列表转成 Bar 序列。
    points 每项: {price(收盘), high, low, volume, full_date(%Y-%m-%d)}。"""
    bars = []
    for pt in points or []:
        raw = pt.get("full_date") or pt.get("date") or ""
        try:
            t = datetime.strptime(raw, "%Y-%m-%d")
        except Exception:
            t = datetime.now()
        close = float(pt.get("price") or pt.get("close") or 0.0)
        high = float(pt.get("high") or close)
        low = float(pt.get("low") or close)
        vol = float(pt.get("volume") or 0)
        bars.append(Bar(t=t, close=close, high=high, low=low, volume=vol))
    return bars


# ============================================================
# 2. 技术指标（纯 Python，无第三方依赖）
# ============================================================
def sma(s: list[float], n: int) -> list[float]:
    out = [float("nan")] * len(s)
    cum = 0.0
    for i, v in enumerate(s):
        cum += v
        if i >= n:
            cum -= s[i - n]
        if i >= n - 1:
            out[i] = cum / n
    return out

def ema(s: list[float], n: int) -> list[float]:
    out = [float("nan")] * len(s)
    k = 2 / (n + 1)
    prev = s[0]
    for i, v in enumerate(s):
        prev = v if i == 0 else v * k + prev * (1 - k)
        out[i] = prev
    return out

def macd(s: list[float], fast=12, slow=26, sig=9):
    ef, es = ema(s, fast), ema(s, slow)
    dif = [ef[i] - es[i] for i in range(len(s))]
    dea = ema(dif, sig)
    hist = [dif[i] - dea[i] for i in range(len(s))]
    return dif, dea, hist

def rsi(s: list[float], n=14) -> list[float]:
    out = [float("nan")] * len(s)
    gains = losses = 0.0
    for i in range(1, len(s)):
        ch = s[i] - s[i - 1]
        g = max(ch, 0); l = max(-ch, 0)
        if i <= n:
            gains += g; losses += l
            if i == n:
                gains /= n; losses /= n
        else:
            gains = (gains * (n - 1) + g) / n
            losses = (losses * (n - 1) + l) / n
        if i >= n:
            rs = (gains / losses) if losses != 0 else float("inf")
            out[i] = 100 - 100 / (1 + rs) if losses != 0 else 100.0
    return out

def bollinger(s: list[float], n=20, k=2.0):
    mid = sma(s, n)
    up = [float("nan")] * len(s); lo = [float("nan")] * len(s)
    for i in range(n - 1, len(s)):
        w = s[i - n + 1: i + 1]
        m = sum(w) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        up[i] = m + k * sd; lo[i] = m - k * sd
    return mid, up, lo

def obv(close: list[float], vol: list[float]) -> list[float]:
    out = [0.0] * len(close)
    for i in range(1, len(close)):
        if close[i] > close[i - 1]:
            out[i] = out[i - 1] + vol[i]
        elif close[i] < close[i - 1]:
            out[i] = out[i - 1] - vol[i]
        else:
            out[i] = out[i - 1]
    return out


# ============================================================
# 3. 策略：多指标共振 + 逢低吸纳/高位套现
# ============================================================
@dataclass
class Params:
    fast_ma: int = 5
    slow_ma: int = 20
    rsi_n: int = 14
    rsi_oversold: float = 30.0
    rsi_overbought: float = 70.0
    bb_n: int = 20
    bb_k: float = 2.0
    vol_confirm: bool = True
    stop_pct: float = 0.0      # 0=关闭
    take_pct: float = 0.0      # 0=关闭


class Strategy:
    def __init__(self, p: Params):
        self.p = p

    def indicators(self, bars: list[Bar]):
        c = [b.close for b in bars]
        v = [b.volume for b in bars]
        ma_f = sma(c, self.p.fast_ma)
        ma_s = sma(c, self.p.slow_ma)
        _, _, hist = macd(c)
        r = rsi(c, self.p.rsi_n)
        _, bb_u, bb_l = bollinger(c, self.p.bb_n, self.p.bb_k)
        ov = obv(c, v)
        return c, v, ma_f, ma_s, hist, r, bb_u, bb_l, ov

    def decide(self, i: int, ind, pos: int, entry: float) -> tuple[str, str]:
        """返回 (action, reason)。action ∈ {BUY, SELL_CLOSE, SHORT, COVER, HOLD}。"""
        c, v, ma_f, ma_s, hist, r, bb_u, bb_l, ov = ind
        p = self.p
        price = c[i]
        if any(math.isnan(x) for x in [ma_f[i], ma_s[i], r[i], bb_u[i]]):
            return "HOLD", "指标未就绪"
        uptrend = ma_f[i] > ma_s[i]
        downtrend = ma_f[i] < ma_s[i]
        # 量能确认
        vol_ok = True
        if p.vol_confirm and i >= 20:
            avg_v = sum(v[i - 20:i]) / 20
            vol_ok = v[i] > avg_v * 1.2 if avg_v > 0 else True
        # 拐点：MACD 红柱刚转负（多头动能衰竭）
        bear_turn = (i >= 1 and hist[i - 1] > 0 and hist[i] <= 0)
        bull_turn = (i >= 1 and hist[i - 1] < 0 and hist[i] >= 0)

        # ---- 已持多仓：高位套现 ----
        if pos == 1:
            if p.take_pct > 0 and price >= entry * (1 + p.take_pct):
                return "SELL_CLOSE", f"达止盈 +{p.take_pct*100:.1f}% → 套现"
            if p.stop_pct > 0 and price <= entry * (1 - p.stop_pct):
                return "SELL_CLOSE", f"触止损 -{p.stop_pct*100:.1f}% → 离场"
            if r[i] >= p.rsi_overbought or price >= bb_u[i] or (bear_turn and price < entry):
                return "SELL_CLOSE", f"高位套现(RSI={r[i]:.0f}/触上轨/拐点) 平多"
            if downtrend:
                return "SELL_CLOSE", "趋势翻空 → 平多避险"
            return "HOLD", "持有多仓"

        # ---- 已持空仓：低位回补 ----
        if pos == -1:
            if p.take_pct > 0 and price <= entry * (1 - p.take_pct):
                return "COVER", f"达止盈 -{p.take_pct*100:.1f}% → 回补"
            if p.stop_pct > 0 and price >= entry * (1 + p.stop_pct):
                return "COVER", f"触止损 +{p.stop_pct*100:.1f}% → 回补"
            if r[i] <= p.rsi_oversold or price <= bb_l[i] or (bull_turn and price > entry):
                return "COVER", f"低位回补(RSI={r[i]:.0f}/触下轨/拐点) 平空"
            if uptrend:
                return "COVER", "趋势翻多 → 回补"
            return "HOLD", "持有空仓"

        # ---- 空仓：按趋势方向开仓（震荡市双向均可）----
        flat = abs(ma_f[i] - ma_s[i]) / ma_s[i] < 0.002 if ma_s[i] else False
        if (uptrend or flat) and vol_ok:
            if r[i] <= p.rsi_oversold or price <= bb_l[i]:
                return "BUY", f"逢低吸纳(RSI={r[i]:.0f}超卖/触下轨) 开多"
        if (downtrend or flat) and vol_ok:
            if r[i] >= p.rsi_overbought or price >= bb_u[i]:
                return "SHORT", f"高位做空(RSI={r[i]:.0f}超买/触上轨) 开空"
        return "HOLD", "观望"


# ============================================================
# 4. 模拟引擎（含持仓/盈亏/手续费/杠杆）
# ============================================================
@dataclass
class Trade:
    side: str
    open_t: datetime
    open_px: float
    close_t: datetime = None
    close_px: float = 0.0
    pnl: float = 0.0
    reason: str = ""

@dataclass
class SimResult:
    signals: list = field(default_factory=list)
    trades: list = field(default_factory=list)
    equity: list = field(default_factory=list)
    final_equity: float = 0.0
    stats: dict = field(default_factory=dict)
    friction_summary: dict = field(default_factory=dict)


@dataclass
class FrictionConfig:
    """摩擦成本配置（滑点 + 手续费 + 延期补偿费）。
    所有滑点单位为基点（bp），1bp = 0.01%。"""
    fee_rate: float = 0.0004           # 手续费率（万分之四）
    slippage_bps: float = 0.0          # 固定滑点（基点），默认0即关闭
    slippage_mode: str = "fixed"       # fixed / random
    slippage_random_range: tuple = None  # 随机滑点范围 (min_bp, max_bp)
    deferred_fee_rate: float = 0.0     # 延期补偿费率（Au(T+D)特有，默认关闭）
    include_deferred_fee: bool = False # 是否计入延期补偿费


class Simulator:
    def __init__(self, capital=1_000_000.0, fee=0.0004, leverage=1.0, size_pct=0.3, friction=None):
        self.capital0 = capital
        self.fee = fee
        self.leverage = leverage
        self.size_pct = size_pct  # 每次开仓占用本金比例
        # 摩擦成本配置（向后兼容：不传则用默认值，滑点为0即无影响）
        # 支持传入 dict 或 FrictionConfig 对象（API 调用时传 dict，内部调用传对象）
        if friction is None:
            self.friction = FrictionConfig(fee_rate=fee)
        elif isinstance(friction, FrictionConfig):
            self.friction = friction
        elif isinstance(friction, dict):
            self.friction = FrictionConfig(
                fee_rate=float(friction.get("fee_rate", fee)),
                slippage_bps=float(friction.get("slippage_bps", 0)),
                slippage_mode=friction.get("slippage_mode", "fixed"),
                slippage_random_range=tuple(friction.get("slippage_random_range", [])) if friction.get("slippage_random_range") else None,
                deferred_fee_rate=float(friction.get("deferred_fee_rate", 0.00015)),
                include_deferred_fee=bool(friction.get("include_deferred_fee", False)),
            )
        else:
            self.friction = FrictionConfig(fee_rate=fee)
        # 摩擦成本累计追踪
        self._total_fees = 0.0
        self._total_slippage = 0.0
        self._total_deferred_fees = 0.0

    def _apply_slippage(self, price, side):
        """应用滑点调整成交价。
        买入方向（BUY/COVER）：成交价向上偏移（更高价买入）
        卖出方向（SELL_CLOSE/SHORT）：成交价向下偏移（更低价卖出）"""
        if self.friction.slippage_bps <= 0 and self.friction.slippage_mode != "random":
            return price
        if self.friction.slippage_mode == "random" and self.friction.slippage_random_range:
            import random
            slip_bp = random.uniform(*self.friction.slippage_random_range)
        else:
            slip_bp = self.friction.slippage_bps
        slip_pct = slip_bp / 10000.0  # bp转百分比
        if side in ("BUY", "COVER"):
            return price * (1 + slip_pct)
        else:
            return price * (1 - slip_pct)

    def _charge_deferred_fee(self, cash, pos, lot, entry_price, bar_price):
        """计收延期补偿费（Au(T+D)特有）。简化为多头每日支付万分之延期费率。
        返回扣除后的现金余额。"""
        if not self.friction.include_deferred_fee or self.friction.deferred_fee_rate <= 0:
            return cash
        if pos == 1 and lot > 0:  # 持有多仓，按持仓市值扣除
            fee = lot * bar_price * self.friction.deferred_fee_rate
            cash -= fee
            self._total_deferred_fees += fee
        return cash

    def run(self, bars: list[Bar], strat: Strategy) -> SimResult:
        ind = strat.indicators(bars)
        cash = self.capital0
        pos = 0
        entry = 0.0  # 开仓成交价（含滑点）
        entry_raw = 0.0  # 开仓原始价（用于滑点追踪）
        lot = 0.0
        equity = []
        signals = []
        trades = []
        cur = None
        # 重置摩擦累计
        self._total_fees = 0.0
        self._total_slippage = 0.0
        self._total_deferred_fees = 0.0
        for i in range(len(bars)):
            action, reason = strat.decide(i, ind, pos, entry)
            price = bars[i].close
            if action == "BUY" and pos == 0:
                exec_price = self._apply_slippage(price, "BUY")
                lot = cash * self.size_pct * self.leverage / exec_price
                fee = lot * exec_price * self.friction.fee_rate
                cash -= fee
                self._total_fees += fee
                slip_cost = (exec_price - price) * lot
                self._total_slippage += slip_cost
                pos = 1; entry = exec_price; entry_raw = price
                cur = Trade("long", bars[i].t, exec_price, reason=reason)
                signals.append((bars[i].t, exec_price, "开多", reason, pos, cash))
            elif action == "SHORT" and pos == 0:
                exec_price = self._apply_slippage(price, "SHORT")
                lot = cash * self.size_pct * self.leverage / exec_price
                fee = lot * exec_price * self.friction.fee_rate
                cash -= fee
                self._total_fees += fee
                slip_cost = (price - exec_price) * lot
                self._total_slippage += slip_cost
                pos = -1; entry = exec_price; entry_raw = price
                cur = Trade("short", bars[i].t, exec_price, reason=reason)
                signals.append((bars[i].t, exec_price, "开空", reason, pos, cash))
            elif action == "SELL_CLOSE" and pos == 1:
                exec_price = self._apply_slippage(price, "SELL_CLOSE")
                fee = lot * exec_price * self.friction.fee_rate
                pnl = (exec_price - entry) * lot - fee
                cash += pnl
                self._total_fees += fee
                slip_cost = (price - exec_price) * lot
                self._total_slippage += slip_cost
                cur.close_t = bars[i].t; cur.close_px = exec_price; cur.pnl = pnl
                trades.append(cur)
                signals.append((bars[i].t, exec_price, "平多", reason, 0, cash))
                pos = 0; entry = 0.0; entry_raw = 0.0; cur = None
            elif action == "COVER" and pos == -1:
                exec_price = self._apply_slippage(price, "COVER")
                fee = lot * exec_price * self.friction.fee_rate
                pnl = (entry - exec_price) * lot - fee
                cash += pnl
                self._total_fees += fee
                slip_cost = (exec_price - price) * lot
                self._total_slippage += slip_cost
                cur.close_t = bars[i].t; cur.close_px = exec_price; cur.pnl = pnl
                trades.append(cur)
                signals.append((bars[i].t, exec_price, "平空", reason, 0, cash))
                pos = 0; entry = 0.0; entry_raw = 0.0; cur = None
            else:
                signals.append((bars[i].t, price, "持有", reason, pos, cash))
            # 每日延期补偿费（仅持多时扣除）
            cash = self._charge_deferred_fee(cash, pos, lot, entry, price)
            # 盯市权益
            m2m = cash
            if pos == 1:
                m2m += (price - entry) * lot
            elif pos == -1:
                m2m += (entry - price) * lot
            equity.append(m2m)
        # 收尾强平（回测结束时若仍有持仓，按最后收盘价+滑点强制平仓）
        if pos != 0 and cur:
            price = bars[-1].close
            close_side = "SELL_CLOSE" if pos == 1 else "COVER"
            exec_price = self._apply_slippage(price, close_side)
            fee = lot * exec_price * self.friction.fee_rate
            if pos == 1:
                pnl = (exec_price - entry) * lot - fee
                slip_cost = (price - exec_price) * lot
            else:
                pnl = (entry - exec_price) * lot - fee
                slip_cost = (exec_price - price) * lot
            self._total_fees += fee
            self._total_slippage += slip_cost
            cash += pnl
            cur.close_t = bars[-1].t; cur.close_px = exec_price; cur.pnl = pnl
            trades.append(cur)
            equity[-1] = cash
        # 构建摩擦成本汇总
        total_friction = self._total_fees + self._total_slippage + self._total_deferred_fees
        friction_pct = (total_friction / self.capital0 * 100) if self.capital0 > 0 else 0
        friction_summary = {
            "总手续费": round(self._total_fees, 2),
            "总滑点损失": round(self._total_slippage, 2),
            "总延期费": round(self._total_deferred_fees, 2),
            "摩擦成本合计": round(total_friction, 2),
            "摩擦占本金%": round(friction_pct, 2),
        }
        res = SimResult(signals=signals, trades=trades, equity=equity, final_equity=cash,
                        friction_summary=friction_summary)
        res.stats = self._stats(trades, equity)
        return res

    def _stats(self, trades, equity):
        wins = [t for t in trades if t.pnl > 0]
        loses = [t for t in trades if t.pnl <= 0]
        total_ret = (equity[-1] / self.capital0 - 1) * 100 if equity else 0
        gp = sum(t.pnl for t in wins); gl = -sum(t.pnl for t in loses)
        pf = (gp / gl) if gl > 0 else float("inf")
        peak = equity[0] if equity else 0
        mdd = 0.0
        for e in equity:
            peak = max(peak, e)
            mdd = max(mdd, (peak - e) / peak * 100 if peak else 0)
        base_stats = {
            "笔数": len(trades),
            "胜率%": round(len(wins) / len(trades) * 100, 1) if trades else 0,
            "总收益率%": round(total_ret, 2),
            "盈亏比": round(pf, 2) if pf != float("inf") else "∞",
            "最大回撤%": round(mdd, 2),
            "期末权益": round(equity[-1], 2) if equity else 0,
        }
        # 合并专业风险指标
        risk_metrics = calc_risk_metrics(equity, trades)
        base_stats.update(risk_metrics)
        return base_stats


def calc_risk_metrics(equity, trades, risk_free_rate=0.02):
    """计算专业风险指标（纯Python实现，无第三方依赖）。
    返回: 夏普比率、索提诺比率、卡玛比率、VaR、CVaR、年化收益/波动率、回撤持续天数等。
    数据不足时返回空字典，避免误导性数值。"""
    if not equity or len(equity) < 10:
        return {}
    # 1. 日收益率序列
    returns = [(equity[i] - equity[i-1]) / equity[i-1] for i in range(1, len(equity))]
    n = len(returns)
    if n < 5:
        return {}
    # 2. 基础统计量
    mean_ret = sum(returns) / n
    variance = sum((r - mean_ret) ** 2 for r in returns) / (n - 1) if n > 1 else 0
    std_ret = math.sqrt(variance) if variance > 0 else 0
    # 3. 年化收益与波动率（按252个交易日年化）
    annualized_return = mean_ret * 252 * 100  # 转百分比
    annualized_vol = std_ret * math.sqrt(252) * 100 if std_ret > 0 else 0
    # 4. 最大回撤及持续天数
    peak = equity[0]
    peak_idx = 0
    mdd = 0.0
    mdd_start = 0
    mdd_end = 0
    for i, e in enumerate(equity):
        if e > peak:
            peak = e
            peak_idx = i
        dd = (peak - e) / peak * 100 if peak else 0
        if dd > mdd:
            mdd = dd
            mdd_start = peak_idx
            mdd_end = i
    mdd_duration = mdd_end - mdd_start
    # 5. 夏普比率（Sharpe Ratio）
    sharpe = (mean_ret * 252 - risk_free_rate) / (std_ret * math.sqrt(252)) if std_ret > 0 else 0.0
    # 6. 索提诺比率（Sortino Ratio）：仅计算下行波动率
    downside = [r for r in returns if r < 0]
    downside_std = 0.0
    if len(downside) >= 2:
        downside_var = sum(r**2 for r in downside) / len(downside)
        downside_std = math.sqrt(downside_var)
    sortino = (mean_ret * 252 - risk_free_rate) / (downside_std * math.sqrt(252)) if downside_std > 0 else 0.0
    # 7. VaR(95%) 历史模拟法：第5百分位的损失（VaR非负，若第5百分位为正则记为0）
    sorted_ret = sorted(returns)
    var_pos = max(0, int(n * 0.05))
    var_raw = -sorted_ret[var_pos] if var_pos < n else 0.0
    var_95 = max(0.0, var_raw) * 100  # 转百分比，VaR不可能为负
    # 8. CVaR(95%)：尾部平均损失（尾部中取亏损部分的均值）
    if var_pos + 1 > 0:
        tail = sorted_ret[:var_pos + 1]
        tail_losses = [-r for r in tail if r < 0]
        cvar_95 = (sum(tail_losses) / len(tail_losses) * 100) if tail_losses else 0.0
    else:
        cvar_95 = 0.0
    # 9. 卡玛比率（Calmar Ratio）：年化收益 / 最大回撤
    calmar = annualized_return / mdd if mdd > 0 else 0.0
    # 10. 平均持仓天数
    avg_hold_days = 0
    if trades:
        total_days = 0
        count = 0
        for t in trades:
            if t.close_t and t.open_t:
                delta = t.close_t - t.open_t
                total_days += delta.days
                count += 1
        if count > 0:
            avg_hold_days = round(total_days / count, 1)
    # 11. 盈利因子（Profit Factor）：总盈利 / 总亏损
    wins_pnl = [t.pnl for t in trades if t.pnl > 0]
    loses_pnl = [-t.pnl for t in trades if t.pnl <= 0]
    total_win = sum(wins_pnl)
    total_loss = sum(loses_pnl)
    profit_factor = round(total_win / total_loss, 2) if total_loss > 0 else "∞"
    return {
        "夏普比率": round(sharpe, 2),
        "索提诺比率": round(sortino, 2),
        "卡玛比率": round(calmar, 2),
        "VaR_95%": round(var_95, 2),
        "CVaR_95%": round(cvar_95, 2),
        "年化收益率%": round(annualized_return, 2),
        "年化波动率%": round(annualized_vol, 2),
        "最大回撤持续天数": mdd_duration,
        "平均持仓天数": avg_hold_days,
        "盈利因子": profit_factor,
    }


# ============================================================
# 5. 多轮验证：walk-forward + 参数扫描
# ============================================================
def walk_forward(bars, strat_params: Params, win=250, step=250):
    rows = []
    n = len(bars)
    k = 0
    for start in range(0, n - win, step):
        seg = bars[start:start + win]
        res = Simulator().run(seg, Strategy(strat_params))
        rows.append((f"第{k+1}轮[{start}-{start+win}]", res.stats))
        k += 1
    return rows


def walk_forward_analysis(bars, params: Params,
                          train_window=250, test_window=60, step=60,
                          friction=None) -> dict:
    """滚动验证（Walk-Forward Analysis）：样本外验证的黄金标准。
    将历史数据切分为「训练窗口+测试窗口」的多段组合，固定参数滚动检验时间稳健性。
    返回每轮训练期/测试期表现 + 汇总统计 + 过拟合风险评估。"""
    rounds = []
    n = len(bars)
    min_required = train_window + test_window
    if n < min_required:
        return {"error": f"数据不足（需至少{min_required}根，当前{n}根）"}
    k = 0
    for start in range(0, n - train_window - test_window + 1, step):
        train_end = start + train_window
        test_end = train_end + test_window
        if test_end > n:
            break
        train_bars = bars[start:train_end]
        test_bars = bars[train_end:test_end]
        train_res = Simulator(friction=friction).run(train_bars, Strategy(params))
        test_res = Simulator(friction=friction).run(test_bars, Strategy(params))
        train_ret = train_res.stats.get("总收益率%", 0)
        test_ret = test_res.stats.get("总收益率%", 0)
        # 收益衰减率：样本外相对样本内的衰减幅度
        if train_ret != 0:
            degradation = (1 - test_ret / train_ret) * 100 if train_ret > 0 else (test_ret - train_ret)
        else:
            degradation = 0
        rounds.append({
            "round": k + 1,
            "train_range": f"{start}-{train_end}",
            "test_range": f"{train_end}-{test_end}",
            "train_stats": train_res.stats,
            "test_stats": test_res.stats,
            "train_return": train_ret,
            "test_return": test_ret,
            "degradation_pct": round(degradation, 1),
            "test_win": test_ret > 0,
        })
        k += 1
    if not rounds:
        return {"error": "数据不足以完成至少一轮滚动验证"}
    # 汇总统计
    test_returns = [r["test_return"] for r in rounds]
    train_returns = [r["train_return"] for r in rounds]
    win_windows = sum(1 for r in rounds if r["test_win"])
    avg_test = sum(test_returns) / len(test_returns) if test_returns else 0
    worst_test = min(test_returns) if test_returns else 0
    avg_deg = sum(r["degradation_pct"] for r in rounds) / len(rounds) if rounds else 0
    # 过拟合风险评估
    overfit_risk = "低"
    if avg_deg > 50 and avg_test < 0:
        overfit_risk = "高"
    elif avg_deg > 30 or win_windows < len(rounds) * 0.6:
        overfit_risk = "中"
    summary = {
        "total_rounds": len(rounds),
        "avg_train_return": round(sum(train_returns) / len(train_returns), 2) if train_returns else 0,
        "avg_test_return": round(avg_test, 2),
        "worst_test_return": round(worst_test, 2),
        "win_windows": win_windows,
        "win_rate_of_windows": round(win_windows / len(rounds) * 100, 1) if rounds else 0,
        "avg_degradation_pct": round(avg_deg, 1),
        "overfit_risk": overfit_risk,
    }
    return {"rounds": rounds, "summary": summary}


def param_sweep(bars, base: Params, grid: dict):
    rows = []
    keys = list(grid.keys())
    for vals in itertools.product(*[grid[k] for k in keys]):
        p = Params(**{**base.__dict__, **dict(zip(keys, vals))})
        res = Simulator().run(bars, Strategy(p))
        rows.append((dict(zip(keys, vals)), res.stats))
    return rows


def param_sweep_2d(bars, base: Params, param_x: str, range_x: list,
                   param_y: str, range_y: list,
                   metric: str = "总收益率%", friction=None) -> dict:
    """二维参数扫描，返回热力图矩阵数据 + 稳健性评分（参数高原识别）。
    用于识别"参数高原"——一片稳定盈利的连续区域，而非孤立的最优值。"""
    matrix = []
    best_val = float("-inf")
    best_params = None
    for y_val in range_y:
        row = []
        for x_val in range_x:
            p_dict = {**base.__dict__, param_x: x_val, param_y: y_val}
            p = Params(**p_dict)
            res = Simulator(friction=friction).run(bars, Strategy(p))
            val = res.stats.get(metric, 0)
            if isinstance(val, (int, float)):
                row.append(round(val, 2))
                if val > best_val:
                    best_val = val
                    best_params = {param_x: x_val, param_y: y_val}
            else:
                row.append(0)
        matrix.append(row)
    # 计算参数高原比例：收益 >= 最优值 80% 的格子占比
    if best_val > 0 and matrix:
        plateau_count = sum(
            1 for row in matrix for v in row
            if v >= best_val * 0.8
        )
        total = len(matrix) * len(matrix[0]) if matrix else 1
        robustness_score = round(plateau_count / total * 100, 1)
    else:
        robustness_score = 0
    # 稳健性评级
    if robustness_score >= 60:
        robustness_level = "A（非常稳健）"
    elif robustness_score >= 40:
        robustness_level = "B（较稳健）"
    elif robustness_score >= 20:
        robustness_level = "C（一般）"
    else:
        robustness_level = "D（过拟合风险高）"
    return {
        "matrix": matrix,
        "x_labels": [str(x) for x in range_x],
        "y_labels": [str(y) for y in range_y],
        "param_x": param_x,
        "param_y": param_y,
        "metric": metric,
        "best_value": round(best_val, 2) if best_val != float("-inf") else 0,
        "best_params": best_params or {},
        "robustness_score": robustness_score,
        "robustness_level": robustness_level,
        "plateau_threshold_pct": 80,
    }


def monte_carlo_simulation(bars, params: Params, n_simulations=500,
                           method="bootstrap", friction=None) -> dict:
    """蒙特卡洛模拟：检验策略稳健性。
    通过随机重排收益率序列生成大量"可能的历史路径"，
    如果策略在大多数路径下都能盈利，说明稳健性好；
    如果只有少数路径盈利但收益极高，说明靠运气。
    method:
      - "bootstrap": 对日收益率有放回重抽样（保留收益分布特征，推荐）
      - "shuffle": 简单随机打乱（更极端的压力测试）
    """
    import random
    # 1. 先跑一次原始回测，获取日收益率序列
    base_res = Simulator(friction=friction).run(bars, Strategy(params))
    equity = base_res.equity
    if len(equity) < 2:
        return {"error": "数据不足"}
    # 计算日收益率
    daily_returns = [(equity[i] - equity[i-1]) / equity[i-1] for i in range(1, len(equity))]
    n_days = len(daily_returns)
    if n_days < 5:
        return {"error": "交易日不足，无法进行蒙特卡洛模拟"}
    # 2. 模拟生成多条权益路径
    simulation_results = []
    initial_cap = equity[0]
    for _ in range(n_simulations):
        if method == "bootstrap":
            sim_returns = [random.choice(daily_returns) for _ in range(n_days)]
        else:  # shuffle
            sim_returns = daily_returns.copy()
            random.shuffle(sim_returns)
        # 重建权益曲线
        sim_equity = [initial_cap]
        for r in sim_returns:
            sim_equity.append(sim_equity[-1] * (1 + r))
        final_ret = (sim_equity[-1] / initial_cap - 1) * 100
        # 计算最大回撤
        peak = sim_equity[0]
        mdd = 0.0
        for e in sim_equity:
            peak = max(peak, e)
            dd = (peak - e) / peak * 100 if peak else 0
            mdd = max(mdd, dd)
        simulation_results.append({
            "final_return_pct": final_ret,
            "max_drawdown_pct": mdd,
            "win": final_ret > 0,
        })
    # 3. 统计分布特征
    returns_sorted = sorted([r["final_return_pct"] for r in simulation_results])
    n = len(returns_sorted)
    mean_ret = sum(returns_sorted) / n
    median_ret = returns_sorted[n // 2]
    pct_5 = returns_sorted[int(n * 0.05)]
    pct_95 = returns_sorted[int(n * 0.95)]
    variance = sum((r - mean_ret) ** 2 for r in returns_sorted) / (n - 1) if n > 1 else 0
    std_ret = math.sqrt(variance) if variance > 0 else 0
    win_count = sum(1 for r in simulation_results if r["win"])
    win_rate = win_count / n * 100
    # 4. 生成直方图数据（20个区间）
    if returns_sorted:
        hist_min = returns_sorted[0]
        hist_max = returns_sorted[-1]
        hist_range = hist_max - hist_min if hist_max > hist_min else 1
        bin_width = hist_range / 20
        histogram = []
        for i in range(20):
            bin_start = hist_min + i * bin_width
            bin_end = bin_start + bin_width
            count = sum(1 for r in returns_sorted if bin_start <= r < bin_end)
            if i == 19:  # 最后一个区间包含上界
                count = sum(1 for r in returns_sorted if bin_start <= r <= bin_end)
            histogram.append({
                "bin": i + 1,
                "start": round(bin_start, 2),
                "end": round(bin_end, 2),
                "count": count,
            })
    else:
        histogram = []
    # 5. 稳健性评级
    original_return = (equity[-1] / initial_cap - 1) * 100
    if win_rate >= 80 and pct_5 >= -5:
        robustness = "A（稳健）"
    elif win_rate >= 60:
        robustness = "B（较稳健）"
    elif win_rate >= 40:
        robustness = "C（一般）"
    else:
        robustness = "D（接近随机）"
    return {
        "n_simulations": n_simulations,
        "method": method,
        "original_return": round(original_return, 2),
        "distribution": {
            "mean_return": round(mean_ret, 2),
            "median_return": round(median_ret, 2),
            "pct_5": round(pct_5, 2),
            "pct_95": round(pct_95, 2),
            "std_return": round(std_ret, 2),
            "win_rate": round(win_rate, 1),
        },
        "histogram": histogram,
        "robustness": robustness,
        "interpretation": _mc_interpretation(win_rate, pct_5, original_return, mean_ret),
    }


def _mc_interpretation(win_rate, pct_5, original, mean):
    """生成蒙特卡洛结果的中文解读"""
    parts = []
    if win_rate >= 80:
        parts.append(f"模拟胜率 {win_rate:.1f}%，策略在绝大多数路径下盈利，稳健性较好。")
    elif win_rate >= 60:
        parts.append(f"模拟胜率 {win_rate:.1f}%，多数路径盈利，但仍有相当比例亏损，需关注风险控制。")
    elif win_rate >= 40:
        parts.append(f"模拟胜率 {win_rate:.1f}%，盈利路径不足半数，策略一致性一般。")
    else:
        parts.append(f"模拟胜率 {win_rate:.1f}%，盈利路径不足四成，策略可能接近随机或依赖特定行情。")
    if pct_5 < -10:
        parts.append(f"5%分位损失 {pct_5:.1f}%，极端情况下回撤较大，需警惕尾部风险。")
    elif pct_5 < -5:
        parts.append(f"5%分位损失 {pct_5:.1f}%，尾部风险可控但不可忽视。")
    else:
        parts.append(f"5%分位损失仅 {pct_5:.1f}%，下行风险较小。")
    if original > mean * 1.5:
        parts.append("原始回测收益显著高于模拟均值，可能存在一定运气成分，不宜过度乐观。")
    return "".join(parts)


# ============================================================
# 6. 报告输出
# ============================================================
def sparkline(equity):
    if not equity:
        return ""
    lo, hi = min(equity), max(equity)
    chars = " ▁▂▃▄▅▆▇█"
    out = []
    for e in equity[::max(1, len(equity)//40)]:
        idx = int((e - lo) / (hi - lo) * 8) if hi > lo else 4
        out.append(chars[idx])
    return "".join(out)

def print_report(title, bars, res, live_info=None):
    print("\n" + "=" * 64)
    print(f"  {title}")
    print("=" * 64)
    if live_info:
        print(f"  [真实行情锚点] {live_info['name']} 当前价={live_info['last']} "
              f"昨收={live_info['prev_close']} 时点={live_info['ts']}")
    print(f"  数据点数={len(bars)}  期末权益={res.stats['期末权益']}")
    print(f"  权益曲线: {sparkline(res.equity)}")
    print("-" * 64)
    print("  关键信号（前 12 条 + 末 3 条）:")
    sig = res.signals
    for t, px, act, reason, pos, cash in sig[:12]:
        print(f"   {t:%m-%d %H:%M} 价{px:8.2f} {act:4s} | {reason} | 仓{pos}")
    if len(sig) > 15:
        print("   ... (略)")
        for t, px, act, reason, pos, cash in sig[-3:]:
            print(f"   {t:%m-%d %H:%M} 价{px:8.2f} {act:4s} | {reason} | 仓{pos}")
    print("-" * 64)
    print("  成交明细（前 8 笔）:")
    for tr in res.trades[:8]:
        print(f"   {tr.side} 开{tr.open_px:.2f}→平{tr.close_px:.2f} 盈亏={tr.pnl:+.2f} | {tr.reason}")
    print("-" * 64)
    print("  盈亏统计:")
    for k, v in res.stats.items():
        print(f"   {k}: {v}")
    print("=" * 64)


def run_backtest(bars, params: Params = None, capital=1_000_000.0,
                 fee=0.0004, leverage=1.0, size_pct=0.3, friction=None) -> dict:
    """一键回测，返回 JSON 友好的结果字典（供网页 /api/backtest 用）。"""
    p = params or Params()
    # 构建摩擦配置：若传入friction字典则转为FrictionConfig对象
    if friction is None:
        fric_cfg = FrictionConfig(fee_rate=fee)
    elif isinstance(friction, FrictionConfig):
        fric_cfg = friction
    else:
        # 从字典构建
        fric_cfg = FrictionConfig(
            fee_rate=float(friction.get("fee_rate", fee)),
            slippage_bps=float(friction.get("slippage_bps", 0)),
            slippage_mode=friction.get("slippage_mode", "fixed"),
            slippage_random_range=tuple(friction.get("slippage_random_range", [])),
            deferred_fee_rate=float(friction.get("deferred_fee_rate", 0.00015)),
            include_deferred_fee=bool(friction.get("include_deferred_fee", False)),
        )
    res = Simulator(capital=capital, fee=fee, leverage=leverage, size_pct=size_pct,
                    friction=fric_cfg).run(bars, Strategy(p))
    return {
        "points": len(bars),
        "stats": res.stats,
        "friction": res.friction_summary,
        "equity": [round(e, 2) for e in res.equity],
        "sparkline": sparkline(res.equity),
        "signals": [
            {"date": t.strftime("%Y-%m-%d"), "price": round(px, 2),
             "action": act, "reason": reason, "pos": pos}
            for (t, px, act, reason, pos, cash) in res.signals
        ],
        "trades": [
            {"side": tr.side, "open": round(tr.open_px, 2),
             "close": round(tr.close_px, 2), "pnl": round(tr.pnl, 2),
             "reason": tr.reason}
            for tr in res.trades
        ],
    }


if __name__ == "__main__":
    # 本地自测：用一段模拟行情数据跑通
    from gold_simulator import SyntheticProvider
    bars = SyntheticProvider.gen(600, seed=7)
    r = run_backtest(bars)
    print("自测 stats:", r["stats"])
    print("信号数:", len(r["signals"]), "成交数:", len(r["trades"]))
