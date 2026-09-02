"""
自动交易引擎（模拟盘专用）— 专业版 v2
================================
信号源：共识评分（MA/MACD/DMI/布林/RSI/OBV 六项加权，-1.0 ~ +1.0）

交易规则（趋势跟随 + 中性回撤 + 硬止损 + 心理博弈 + 消息事件）：
  1. 无持仓 且 评分 >= +阈值  → 自动买入开仓（做多）
  2. 无持仓 且 评分 <= -阈值  → 自动卖出开仓（做空）
  3. 持有多仓 且 评分 <= 0    → 自动卖出平仓（多单离场）
  4. 持有空仓 且 评分 >= 0    → 自动买入平仓（空单离场）
  5. 评级包含"谨慎"（极端乖离，布林超1.2倍带宽且ADX>45）
     → 禁止开新仓，仅允许平仓，防止追涨杀跌
  6. 硬止损：浮亏达 -3% 强制平仓（10倍杠杆下=本金亏损30%）
  7. 移动止盈：持仓最高点回撤 2% 锁定利润
  8. 心理博弈：恐惧/贪婪指数影响开仓倾向与持仓耐心
  9. 消息事件：非农/CPI/美联储/地缘冲突模拟，影响评分与波动

风控：完全复用现有交易引擎（10%保证金、0.04%手续费、80%风险率强平、
      递延费），自动交易下单一律走 execute_trade，与手动单同等约束。
      新增 ATR 动态仓位（每笔风险固定为账户权益的 2%）。

多进程防重：gunicorn 多 worker 下用 fcntl 文件锁保证全局只有一个
  自动交易线程在运行（锁文件放在数据目录）。
"""

from __future__ import annotations

import os
import sys
import time
import math
import random
import threading
from datetime import datetime, timedelta

# 多指标共振策略引擎（项目内部共享模块，纯 Python、可进 Docker）
try:
    from strategy_engine import Strategy, Params, bars_from_history_points
except Exception:  # 本地独立测试或模块缺失时降级
    Strategy = Params = bars_from_history_points = None

# 锁文件默认放在数据库同目录，跨进程互斥
try:
    from db import DB_PATH
    LOCK_FILE = os.path.join(os.path.dirname(os.path.abspath(DB_PATH)), "auto_trader.lock")
except Exception:
    LOCK_FILE = "/tmp/auto_trader.lock"

# 【跨平台备注】fcntl 是 Unix/Linux/macOS 特有的文件锁模块
# - macOS / Linux / Docker：fcntl 可用，多 worker 进程间互斥锁生效
# - Windows：无 fcntl 模块，自动降级为 threading.Lock（仅进程内有效，单 worker 可接受）
# 如需在 Windows 上实现跨进程互斥，可用 msvcrt.locking() 替代（需自行实现）
try:
    import fcntl  # Linux/macOS 可用（Docker 部署环境）
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # Windows 本地测试无 fcntl，用进程内锁

# 进程内全局锁（仅本进程有效，用于 Windows 本地测试兜底）
_local_lock = threading.Lock()
_worker_owns_global_lock = False
_lock_file_obj = None  # 持锁文件句柄引用，防止被 GC 释放导致锁失效

# 自动交易决策日志（每小时汇总落盘）
DECISION_RECORDS = []  # 内存环形缓冲，每轮决策一条 dict
try:
    _DATA_DIR = os.path.dirname(os.path.abspath(DB_PATH))
except Exception:
    _DATA_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(_DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)


def _acquire_global_lock() -> bool:
    """尝试获取全局互斥锁，成功返回 True；失败说明其他 worker 已在运行。

    注意：必须用普通 open 并保持文件句柄常驻全局变量，绝不能包在 with 里——
    with 退出时会关闭文件描述符，flock 随 fd 关闭而释放，导致多 worker 同时抢到锁、
    自动交易线程被启动多份（双开仓隐患）。锁只在进程整个生命周期持有。
    """
    global _worker_owns_global_lock, _lock_file_obj
    if _HAS_FCNTL:
        try:
            # 手动 open 且不关闭：靠全局 _lock_file_obj 持有句柄，锁在进程生命周期内有效
            _lock_file_obj = open(LOCK_FILE, "w")
            fcntl.flock(_lock_file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            _worker_owns_global_lock = True
            return True
        except (OSError, IOError):
            if _lock_file_obj is not None:
                try:
                    _lock_file_obj.close()
                except Exception:
                    pass
            _lock_file_obj = None
            _worker_owns_global_lock = False
            return False
    else:
        # Windows：进程内锁（单 worker 场景可接受）
        return _local_lock.acquire(blocking=False)


def calc_atr(history_points, period=14):
    """计算 ATR（平均真实波幅），用于动态仓位。
    ATR 反映近期波动幅度，波动大→仓位小，波动小→仓位大。"""
    if not history_points or len(history_points) < period + 1:
        return 5.0  # 默认值：5元/克
    trs = []
    for i in range(1, min(len(history_points), period + 1)):
        pt = history_points[-i]
        prev_close = history_points[-i - 1].get("price", 0) if i < len(history_points) else pt.get("price", 0)
        high = pt.get("high") or pt.get("price", 0)
        low = pt.get("low") or pt.get("price", 0)
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 5.0


def calc_dynamic_grams(equity, atr, risk_pct=0.02, leverage=10.0, max_grams=500.0):
    """ATR 动态仓位：每笔风险 = 账户权益 × risk_pct。
    克数 = (权益 × 风险比例) / (ATR × 杠杆)
    示例：权益100万、ATR=5、杠杆10 → 克数 = 1000000×0.02/(5×10) = 400克"""
    if atr <= 0 or equity <= 0:
        return 10.0  # 兜底
    grams = (equity * risk_pct) / (atr * leverage)
    return max(10.0, min(grams, max_grams))


# ============================================================
# 心理博弈模型：恐惧/贪婪指数（0~100，50=中性）
# ============================================================
def calc_fear_greed_index(history_points, score, rsi_val=None):
    """计算恐惧/贪婪指数，模拟市场参与者的情绪状态。
    <20: 极度恐惧（恐慌抛售，底部区域）
    20-40: 恐惧偏谨慎
    40-60: 中性（多空均衡）
    60-80: 贪婪偏乐观
    >80: 极度贪婪（追涨杀跌，顶部区域）
    """
    if not history_points or len(history_points) < 5:
        return 50.0
    prices = [pt.get("price", 0) for pt in history_points[-10:]]
    if len(prices) < 2 or prices[0] == 0:
        return 50.0

    # 因子1：近5日涨跌幅（贪婪=涨，恐惧=跌）
    recent_ret = (prices[-1] - prices[-5]) / prices[-5] * 100 if len(prices) >= 5 and prices[-5] > 0 else 0
    f1 = max(-100, min(100, recent_ret * 10))  # 放大10倍映射到-100~100

    # 因子2：波动率（高波动→恐惧）
    if len(prices) >= 5:
        mean_p = sum(prices) / len(prices)
        vol = math.sqrt(sum((p - mean_p) ** 2 for p in prices) / len(prices)) if mean_p > 0 else 0
        vol_pct = vol / mean_p * 100 if mean_p > 0 else 0
    else:
        vol_pct = 0
    f2 = max(-100, min(100, -vol_pct * 5))  # 波动越大→越恐惧（负分）

    # 因子3：RSI（>70 贪婪，<30 恐惧）
    f3 = 0
    if rsi_val is not None:
        f3 = (rsi_val - 50) * 2  # 50→0，70→40，30→-40

    # 因子4：共识评分（正=贪婪，负=恐惧）
    f4 = score * 60 if score is not None else 0

    # 加权综合
    raw = f1 * 0.25 + f2 * 0.20 + f3 * 0.25 + f4 * 0.30
    fg_index = 50 + raw * 0.5  # 映射到0~100
    return max(0, min(100, round(fg_index, 1)))


def fear_greed_label(fg):
    """恐惧贪婪指数→中文标签"""
    if fg < 20: return "极度恐惧"
    if fg < 40: return "恐惧偏谨慎"
    if fg < 60: return "中性均衡"
    if fg < 80: return "贪婪偏乐观"
    return "极度贪婪"


# ============================================================
# 消息事件模拟器
# ============================================================
NEWS_EVENTS = [
    {"name": "非农就业数据", "type": "economic", "weight": 0.35, "desc": "美国非农就业报告公布，黄金剧烈波动"},
    {"name": "CPI通胀数据", "type": "economic", "weight": 0.30, "desc": "CPI高于预期，加息预期升温利空黄金"},
    {"name": "美联储利率决议", "type": "monetary", "weight": 0.40, "desc": "美联储鹰派/鸽派表态，黄金趋势反转"},
    {"name": "地缘冲突升级", "type": "geopolitical", "weight": 0.35, "desc": "地缘紧张局势升级，避险资金涌入黄金"},
    {"name": "美元指数走强", "type": "forex", "weight": 0.25, "desc": "美元走强压制金价，黄金承压"},
    {"name": "央行购金消息", "type": "supply", "weight": 0.20, "desc": "新兴市场央行增持黄金储备，利好金价"},
    {"name": "技术性破位", "type": "technical", "weight": 0.15, "desc": "关键技术位突破引发程序化交易连锁反应"},
]

_last_news_time = None
_active_news = None

def maybe_trigger_news():
    """按概率触发消息事件（约每3-8小时触发一次）。
    返回 (news_dict, score_shift) 或 None。
    score_shift: 对共识评分的偏移量（-0.3~+0.3）"""
    global _last_news_time, _active_news
    now = datetime.now()

    # 如果有活跃消息且未过期（持续2小时），返回其影响
    if _active_news and _last_news_time:
        elapsed = (now - _last_news_time).total_seconds()
        if elapsed < 7200:  # 2小时内消息持续影响
            # 衰减因子：消息影响随时间递减
            decay = max(0.3, 1.0 - elapsed / 7200)
            return _active_news, _active_news["score_shift"] * decay
        else:
            _active_news = None

    # 约12%概率触发新消息
    if random.random() < 0.12:
        news = random.choice(NEWS_EVENTS).copy()
        # 消息方向随机：利好(+)/利空(-)
        direction = random.choice([-1, 1])
        score_shift = direction * news["weight"] * random.uniform(0.5, 1.0)
        news["score_shift"] = round(score_shift, 2)
        news["direction"] = "利好" if direction > 0 else "利空"
        _active_news = news
        _last_news_time = now
        return news, score_shift

    return None, 0


def decide_action(score, rating, long_grams, short_grams, threshold, grams, have_funds=True,
                  rsi_val=None, entry_price=None, current_price=None,
                  peak_price=None, fear_greed=50.0):
    """
    根据共识评分与当前持仓决定下一步动作。独立函数便于测试。
    返回 (action, grams, reason)：
      action 为 None 表示不操作；grams 为建议克数（开仓用固定值，平仓用持仓量）。

    新增参数：
      entry_price: 开仓均价（用于计算浮盈/浮亏）
      current_price: 当前最新价
      peak_price: 持仓期间最高/最低价（用于移动止盈）
      fear_greed: 恐惧贪婪指数（0-100，影响开仓倾向）
    """
    rating_extra = rating or ""
    extreme = "谨慎" in rating_extra  # 极端乖离，禁止开新仓

    # ========== 0. 硬止损 + 移动止盈（最高优先级）==========
    if long_grams > 0 and entry_price and entry_price > 0 and current_price:
        pnl_pct = (current_price - entry_price) / entry_price
        # 硬止损：浮亏 -3%
        if pnl_pct <= -0.03:
            return "sell_close", long_grams, f"触发硬止损：浮亏 {pnl_pct*100:.1f}% ≤ -3%"
        # 移动止盈：从最高点回撤 2%
        if peak_price and peak_price > entry_price:
            drawdown = (peak_price - current_price) / peak_price
            if drawdown >= 0.02 and current_price > entry_price:
                return "sell_close", long_grams, f"移动止盈：从最高 {peak_price:.2f} 回撤 {drawdown*100:.1f}%"

    if short_grams > 0 and entry_price and entry_price > 0 and current_price:
        pnl_pct = (entry_price - current_price) / entry_price
        # 硬止损：浮亏 -3%（空仓价格涨了3%）
        if pnl_pct <= -0.03:
            return "buy_close", short_grams, f"触发硬止损：浮亏 {pnl_pct*100:.1f}% ≤ -3%"
        # 移动止盈：从最低点反弹 2%
        if peak_price and peak_price < entry_price:
            drawup = (current_price - peak_price) / peak_price if peak_price > 0 else 0
            if drawup >= 0.02 and current_price < entry_price:
                return "buy_close", short_grams, f"移动止盈：从最低 {peak_price:.2f} 反弹 {drawup*100:.1f}%"

    # ========== 1. 心理博弈调整：恐惧时提高开仓阈值，贪婪时降低 ==========
    effective_threshold = threshold
    if long_grams <= 0 and short_grams <= 0:
        if fear_greed < 30:
            # 极度恐惧：提高开仓阈值（更谨慎），但反向机会可能更大
            effective_threshold = min(0.9, threshold + 0.15)
        elif fear_greed > 70:
            # 极度贪婪：降低阈值（追涨情绪），但同时增加风险提示
            effective_threshold = max(0.1, threshold - 0.10)

    # ========== 2. 评分转向中性平仓 ==========
    if long_grams > 0 and score <= 0.0:
        return "sell_close", long_grams, f"自动平多：评分 {score:+.2f} 转中性/偏空"
    if short_grams > 0 and score >= 0.0:
        return "buy_close", short_grams, f"自动平空：评分 {score:+.2f} 转中性/偏多"

    # ========== 3. 无持仓时按阈值开仓 ==========
    if long_grams <= 0 and short_grams <= 0:
        if extreme:
            return None, 0, f"极端乖离（{rating_extra}），禁止自动开新仓"
        if not have_funds:
            return None, 0, "可用资金不足，等待下轮"
        # 超买保护：RSI > 75 时禁止开新多仓，避免在超买区域追高
        if rsi_val is not None and rsi_val > 75 and score > 0:
            return None, 0, f"超买保护：RSI={rsi_val:.1f} > 75，禁止追高开多"
        # 超卖保护：RSI < 25 时禁止开新空仓，避免在超卖区域追空
        if rsi_val is not None and rsi_val < 25 and score < 0:
            return None, 0, f"超卖保护：RSI={rsi_val:.1f} < 25，禁止追空开空"
        if score >= effective_threshold:
            fg_tag = f"[{fear_greed_label(fear_greed)}]" if fear_greed != 50 else ""
            return "buy_open", grams, f"自动开多：评分 {score:+.2f} ≥ {effective_threshold} {fg_tag}"
        if score <= -effective_threshold:
            fg_tag = f"[{fear_greed_label(fear_greed)}]" if fear_greed != 50 else ""
            return "sell_open", grams, f"自动开空：评分 {score:+.2f} ≤ {-effective_threshold} {fg_tag}"

    # ========== 4. 已有持仓且评分未触发平仓线：继续持有 ==========
    fg_note = ""
    if fear_greed < 30 and (long_grams > 0 or short_grams > 0):
        fg_note = f" [{fear_greed_label(fear_greed)}，市场恐慌但策略信号未变]"
    elif fear_greed > 70 and (long_grams > 0 or short_grams > 0):
        fg_note = f" [{fear_greed_label(fear_greed)}，注意止盈保护]"
    return None, 0, f"持有观望{fg_note}"


def decide_via_resonance(hist, long_grams, short_grams, grams, have_funds=True):
    """
    多指标共振策略（模拟器 Strategy）：逢低吸纳(RSI超卖/触下轨)+高位套现(RSI超买/触上轨
    且MACD拐点)+量能确认+趋势过滤。独立函数便于测试。
    返回 (action, grams, reason)，action∈{buy_open,sell_open,sell_close,buy_close} 或 None。
    """
    if Strategy is None or bars_from_history_points is None:
        return None, 0, "共振策略模块不可用"
    try:
        bars = bars_from_history_points(hist)
        if len(bars) < 30:
            return None, 0, "共振策略：历史数据不足(需≥30根)"
        strat = Strategy(Params())
        ind = strat.indicators(bars)
        pos = 1 if long_grams > 0 else (-1 if short_grams > 0 else 0)
        i = len(bars) - 1  # 以最新一根做决策
        act, reason = strat.decide(i, ind, pos, 0.0)
        if act == "BUY" and pos == 0:
            if not have_funds:
                return None, 0, "可用资金不足，等待下轮"
            return "buy_open", grams, f"共振开多：{reason}"
        if act == "SHORT" and pos == 0:
            if not have_funds:
                return None, 0, "可用资金不足，等待下轮"
            return "sell_open", grams, f"共振开空：{reason}"
        if act == "SELL_CLOSE" and pos == 1:
            return "sell_close", long_grams, f"共振平多：{reason}"
        if act == "COVER" and pos == -1:
            return "buy_close", short_grams, f"共振平空：{reason}"
        return None, 0, reason
    except Exception as e:
        return None, 0, f"共振策略异常：{e}"


def run_once(db_conn, price_info=None, hist=None):
    """
    执行一轮自动交易判断（供线程循环与测试共用）。
    返回 (action, grams, reason, score, rating) 或 None（出错）。
    """
    from gold_price import fetch_sge_price, fetch_gold_history, calculate_consensus_score

    cfg = db_conn.execute(
        "SELECT auto_trade_enabled, auto_trade_grams, auto_trade_interval, auto_trade_threshold, "
        "strategy_mode, margin_rate FROM accounts WHERE id = 1"
    ).fetchone()
    if cfg is None:
        return None
    if not cfg["auto_trade_enabled"]:
        return None

    base_grams = float(cfg["auto_trade_grams"])
    threshold = float(cfg["auto_trade_threshold"])
    try:
        strategy_mode = cfg["strategy_mode"] or "consensus"
    except (IndexError, KeyError):
        strategy_mode = "consensus"
    try:
        leverage = 1.0 / float(cfg["margin_rate"]) if cfg["margin_rate"] else 10.0
    except (IndexError, KeyError):
        leverage = 10.0

    # 1. 拿最新行情与历史
    if price_info is None:
        price_info = fetch_sge_price()
    if hist is None:
        hist = fetch_gold_history("60d", "1d")

    # 2. 读当前持仓（含均价）与可用资金
    pos_rows = db_conn.execute("SELECT direction, grams, avg_cost FROM positions").fetchall()
    long_pos = next((p for p in pos_rows if p["direction"] == "long"), None)
    short_pos = next((p for p in pos_rows if p["direction"] == "short"), None)
    long_grams = long_pos["grams"] if long_pos else 0
    short_grams = short_pos["grams"] if short_pos else 0
    long_entry = float(long_pos["avg_cost"]) if long_pos and long_pos["avg_cost"] else 0
    short_entry = float(short_pos["avg_cost"]) if short_pos and short_pos["avg_cost"] else 0

    summary = None
    from trade_engine import get_account_summary
    try:
        summary = get_account_summary(db_conn, price_info)
    except Exception:
        summary = None
    have_funds = summary is not None and summary["available_cash"] > 0
    equity = summary["equity"] if summary else 1_000_000.0

    # 3. ATR 动态仓位计算
    atr = calc_atr(hist)
    dynamic_grams = calc_dynamic_grams(equity, atr, risk_pct=0.02, leverage=leverage)
    # 如果用户配置了固定克数>0，优先用动态仓位；否则兜底用固定值
    grams = dynamic_grams if dynamic_grams > 0 else base_grams

    # 4. 消息事件模拟（影响评分）
    news, score_shift = maybe_trigger_news()
    if news:
        print(f"[AutoTrader] 消息事件：{news['name']}({news['direction']}) 评分偏移 {score_shift:+.2f} | {news['desc']}", flush=True)

    # 5. 当前价格与持仓峰值（用于止损/止盈）
    current_price = price_info.get("price") or (price_info.get("bid", 0) + price_info.get("ask", 0)) / 2 if price_info else 0
    # 计算持仓期间最高/最低价（用历史数据近似）
    recent_prices = [pt.get("price", 0) for pt in hist[-20:]] if hist else [current_price]
    peak_long = max(recent_prices) if recent_prices else current_price
    peak_short = min(recent_prices) if recent_prices else current_price

    # 6. 按策略模式决策
    score = rating = None
    fear_greed = 50.0
    if strategy_mode == "resonance":
        # 多指标共振（模拟器 Strategy）：趋势过滤 + 逢低吸纳/高位套现 + 量能确认
        action, act_grams, reason = decide_via_resonance(
            hist, long_grams, short_grams, grams, have_funds)
    else:
        # 默认：共识评分（MA/MACD/DMI/布林/RSI/OBV 六项加权）
        score, rating, details, _ = calculate_consensus_score(hist)
        rsi_val = details.get("rsi_raw") if details else None
        # 从历史数据中提取最新 RSI 原始值（details 中只有评分映射后的值）
        if hist and len(hist) > 0:
            rsi_val = hist[-1].get("rsi")

        # 消息事件影响评分
        if score_shift != 0:
            original_score = score
            score = max(-1.0, min(1.0, score + score_shift))
            if news:
                reason_news = f" | 消息影响：{news['name']}({news['direction']}) 评分 {original_score:+.2f}→{score:+.2f}"

        # 心理博弈：计算恐惧贪婪指数
        fear_greed = calc_fear_greed_index(hist, score, rsi_val)

        # 决策（传入止损/止盈/心理参数）
        entry_price = long_entry if long_grams > 0 else (short_entry if short_grams > 0 else None)
        peak_price = peak_long if long_grams > 0 else (peak_short if short_grams > 0 else None)
        action, act_grams, reason = decide_action(
            score, rating, long_grams, short_grams, threshold, grams, have_funds,
            rsi_val=rsi_val, entry_price=entry_price, current_price=current_price,
            peak_price=peak_price, fear_greed=fear_greed)

        # 附加心理/消息信息到决策记录
        if fear_greed != 50 and action is None:
            reason += f" | 恐惧贪婪指数={fear_greed:.0f}({fear_greed_label(fear_greed)})"
        elif fear_greed != 50 and action is not None:
            reason += f" | 恐惧贪婪指数={fear_greed:.0f}({fear_greed_label(fear_greed)})"

    # 4. 执行并记录本轮决策（无论是否操作都记录判定依据）
    traded = False
    if action is not None:
        from trade_engine import execute_trade
        try:
            note = f"自动交易：{reason}"
            # 关键修复：execute_trade 内含多步写库（持仓/流水/资金），必须在事务块内提交；
            # 否则连接关闭时未提交事务被回滚，成交永远写不进数据库（表现为页面无自动交易）。
            with db_conn:
                detail = execute_trade(db_conn, action, act_grams, price_info, note=note)
            reason = f"{reason}（成交价 {detail['price']}，手续费 {detail['fee']}）"
            traded = True
        except ValueError as e:
            reason = f"自动交易被拒：{e}"

    DECISION_RECORDS.append({
        "ts": datetime.now(),
        "mode": strategy_mode,
        "score": score,
        "rating": rating,
        "long_g": long_grams,
        "short_g": short_grams,
        "action": action,
        "reason": reason,
        "traded": traded,
        "atr": round(atr, 2),
        "dynamic_grams": round(dynamic_grams, 1),
        "fear_greed": round(fear_greed, 1) if strategy_mode != "resonance" else None,
        "news": news["name"] if news else None,
        "news_dir": news["direction"] if news else None,
        "score_shift": round(score_shift, 2) if score_shift else None,
    })
    return action, (act_grams if action else 0), reason, score, rating


def auto_trade_loop():
    """后台线程主循环：按配置间隔轮询。"""
    import db as db_mod
    print("[AutoTrader] 自动交易引擎已启动（信号驱动，仅模拟盘）", flush=True)
    last_log = ""
    interval = 300  # 默认 5 分钟，读取失败时兜底
    while True:
        try:
            conn = db_mod.get_db_connection()
            try:
                cfg = conn.execute(
                    "SELECT auto_trade_enabled, auto_trade_interval FROM accounts WHERE id = 1"
                ).fetchone()
                interval = int(cfg["auto_trade_interval"]) if cfg else 300
                enabled = bool(cfg["auto_trade_enabled"]) if cfg else False

                if enabled:
                    result = run_once(conn)
                    if result and result[0] is not None:
                        action, grams, reason, score, rating = result
                        score_s = f"{score:+.2f}" if isinstance(score, (int, float)) else "-"
                        line = (f"[AutoTrader] {datetime.now().strftime('%H:%M:%S')} "
                                f"动作={action} 克数={grams} 评分={score_s} 评级={rating} | {reason}")
                        print(line, flush=True)
                        last_log = line
                    else:
                        # 仅在无操作且原因变化时输出，避免刷屏
                        reason = result[2] if result else "未启用"
                        key = f"{reason[:40]}"
                        if key != last_log[:40]:
                            print(f"[AutoTrader] {datetime.now().strftime('%H:%M:%S')} {reason}", flush=True)
                            last_log = key
            finally:
                conn.close()
        except Exception as e:
            print(f"[AutoTrader] 循环异常: {e}", flush=True)
        time.sleep(interval)


def _trim_records(keep_hours=2):
    """清理超过 keep_hours 小时的决策记录，避免内存无限增长。"""
    global DECISION_RECORDS
    cutoff = datetime.now().timestamp() - keep_hours * 3600
    DECISION_RECORDS = [r for r in DECISION_RECORDS if r["ts"].timestamp() >= cutoff]


def write_hourly_report(hour_dt: datetime):
    """生成上一小时决策日志文件，覆盖时间窗 [hour_dt-1h, hour_dt)。

    hour_dt 取整点（如 12:00 表示 11:00~12:00 这一小时）。
    文件：<DATA_DIR>/logs/auto_trade_decision_YYYYMMDDHH.log
    """
    window_start = hour_dt - timedelta(hours=1)
    recs = [r for r in DECISION_RECORDS if window_start <= r["ts"] < hour_dt]
    cnt = {"buy_open": 0, "sell_open": 0, "sell_close": 0, "buy_close": 0}
    traded_cnt = 0
    for r in recs:
        if r["action"] in cnt:
            cnt[r["action"]] += 1
        if r["traded"]:
            traded_cnt += 1

    fname = hour_dt.strftime("%Y%m%d%H") + ".log"
    fpath = os.path.join(LOG_DIR, "auto_trade_decision_" + fname)
    lines = []
    lines.append("=" * 60)
    lines.append("自动交易决策日志  %s ~ %s" % (
        window_start.strftime("%Y-%m-%d %H:%M"), hour_dt.strftime("%H:%M")))
    lines.append("生成时间：%s" % datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    lines.append("策略模式：%s" % (recs[0]["mode"] if recs else "(无记录)"))
    lines.append("本小时决策轮次：%d" % len(recs))
    lines.append("买卖成交笔数：%d（开多 %d / 开空 %d / 平多 %d / 平空 %d）" % (
        traded_cnt, cnt["buy_open"], cnt["sell_open"], cnt["sell_close"], cnt["buy_close"]))
    lines.append("-" * 60)
    if not recs:
        lines.append("本小时无自动交易决策（未启用或无可执行轮次）。")
    else:
        for r in recs:
            ts = r["ts"].strftime("%H:%M:%S")
            act_label = {"buy_open": "开多", "sell_open": "开空",
                         "sell_close": "平多", "buy_close": "平空"}.get(r["action"], "持有观望")
            score_s = ("%+.2f" % r["score"]) if isinstance(r["score"], (int, float)) else "-"
            lines.append("[%s] 模式=%s 评分=%s 评级=%s 持仓(多%.0f/空%.0f) 动作=%s 判定：%s | 成交：%s" % (
                ts, r["mode"], score_s, r["rating"] or "-",
                r["long_g"], r["short_g"], act_label, r["reason"],
                "是" if r["traded"] else "否"))
    lines.append("=" * 60)
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("[AutoTrader] 已生成小时决策日志：%s（%d 轮，成交 %d 笔）" % (
            fpath, len(recs), traded_cnt), flush=True)
    except Exception as e:
        print("[AutoTrader] 写小时决策日志失败：%s" % e, flush=True)


def hourly_report_loop():
    """后台线程：每小时整点后生成上一小时决策日志。"""
    print("[AutoTrader] 小时决策日志线程已启动", flush=True)
    last_hour = datetime.now().hour
    while True:
        try:
            now = datetime.now()
            if now.hour != last_hour:
                write_hourly_report(now.replace(minute=0, second=0, microsecond=0))
                last_hour = now.hour
            _trim_records()
        except Exception as e:
            print("[AutoTrader] 小时日志异常: %s" % e, flush=True)
        time.sleep(30)


def start_auto_trader_if_leader():
    """
    应用启动时调用：抢到全局锁的进程启动后台线程，其他 worker 跳过。
    返回是否由本进程启动。
    """
    if not _acquire_global_lock():
        print("[AutoTrader] 其他 worker 已持有自动交易锁，本进程跳过", flush=True)
        return False
    threading.Thread(target=auto_trade_loop, daemon=True, name="auto-trader").start()
    threading.Thread(target=hourly_report_loop, daemon=True, name="auto-trader-report").start()
    return True


if __name__ == "__main__":
    print("auto_trader.py 独立测试模式（不启动线程）")
    sys.exit(0)
