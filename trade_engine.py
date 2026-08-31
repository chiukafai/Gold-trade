import sqlite3
import random
import os
from datetime import datetime, time as dtime

COMMISSION_RATE = 0.0004  # 0.04% SGE Au(T+D) 佣金率
MIN_COMMISSION = 1.0      # 最低 1.0 元手续费
MARGIN_RATE = 0.10        # 10% 保证金率（10倍杠杆）
LIQUIDATION_LEVEL = 80.0  # 风险率低于 80% 触发强平
PRE_WARNING_LEVEL = 120.0 # 风险率低于 120% 预警
DEFERRED_FEE_RATE = 0.000175  # Au(T+D) 延期补偿费 0.0175%/日（按持仓市值每日计收）


def is_market_open(now=None):
    """
    判断当前是否为 Au(T+D) 交易时段（上海黄金交易所官方交易时间）：
      - 日盘：周一至周五 09:00-11:30、13:30-15:30
      - 夜盘：周一至周四 20:00 - 次日 02:30（周五及法定节假日前夜无夜盘）
      - 周六、周日及法定节假日休市
    返回 True=可交易，False=休市。
    模拟实操时可设置环境变量 SIM_ALWAYS_OPEN=1 强制 24 小时可交易（默认按真实时段）。
    """
    if os.environ.get("SIM_ALWAYS_OPEN") == "1":
        return True

    now = now or datetime.now()
    if now.weekday() >= 5:  # 周六(5)、周日(6) 休市
        return False

    t = now.time()
    # 日盘
    if dtime(9, 0) <= t <= dtime(11, 30):
        return True
    if dtime(13, 30) <= t <= dtime(15, 30):
        return True
    # 夜盘：周一至周四 20:00-24:00，以及周二至周五 00:00-02:30（跨午夜）
    if now.weekday() <= 3 and t >= dtime(20, 0):
        return True
    if now.weekday() >= 1 and t <= dtime(2, 30):
        return True
    return False


def charge_daily_deferred_fee(conn, price_data):
    """
    按日计收 Au(T+D) 延期补偿费（模拟真实持仓成本）。
    规则：当日已计收（last_deferred_date == 今天）则跳过；否则按
    全部持仓市值 × DEFERRED_FEE_RATE 从现金中扣除，并累计到 total_deferred_fee。
    递延费不写入 trades 流水（保留 trades 表 type CHECK 约束不变），
    通过 accounts.total_deferred_fee 单独累计，便于核对。
    返回本次实扣金额；未持仓或当日已扣返回 0.0。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    row = conn.execute("SELECT last_deferred_date, cash FROM accounts WHERE id = 1").fetchone()
    if row is None:
        return 0.0
    if row["last_deferred_date"] == today:
        return 0.0

    pos_rows = conn.execute("SELECT direction, grams, avg_cost, margin FROM positions").fetchall()
    total_grams_mktval = 0.0
    latest = price_data.get("latest", 0.0)
    for p in pos_rows:
        if p["grams"] > 0:
            total_grams_mktval += p["grams"] * latest

    if total_grams_mktval <= 0:
        conn.execute("UPDATE accounts SET last_deferred_date = ? WHERE id = 1", (today,))
        return 0.0

    fee = round(total_grams_mktval * DEFERRED_FEE_RATE, 2)
    new_cash = round(row["cash"] - fee, 2)
    conn.execute(
        "UPDATE accounts SET cash = ?, last_deferred_date = ?, total_deferred_fee = total_deferred_fee + ? WHERE id = 1",
        (new_cash, today, fee)
    )
    print(f"[Deferred Fee] Charged {fee} CNY for {today} (position market value {total_grams_mktval:.2f})")
    return fee

def generate_trade_no():
    """生成唯一的交易流水号，格式为 T + 14位时间戳 + 4位随机数"""
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    rand_num = random.randint(1000, 9999)
    return f"T{timestamp}{rand_num}"

def calculate_fee(amount):
    """计算交易手续费，比例为 0.04%，最低 1 元"""
    return max(amount * COMMISSION_RATE, MIN_COMMISSION)

def get_account_summary(conn, current_price_data):
    """
    计算并获取账户资产汇总信息（包含权益、可用资金、保证金、浮动盈亏、持仓与风险率）
    在计算过程中会动态更新数据库中持仓的最新保证金。
    """
    now_str = datetime.now().isoformat()
    latest_price = current_price_data["latest"]
    bid_price = current_price_data["bid"]
    ask_price = current_price_data["ask"]

    # 1. 查询账户资金
    account_row = conn.execute("SELECT * FROM accounts WHERE id = 1").fetchone()
    if not account_row:
        raise ValueError("Account not initialized.")
    cash = account_row["cash"]
    initial_capital = account_row["initial_capital"]
    margin_rate = account_row["margin_rate"]

    # 2. 查询持仓
    positions = conn.execute("SELECT * FROM positions").fetchall()
    
    long_pos = {"grams": 0.0, "avg_cost": 0.0, "margin": 0.0}
    short_pos = {"grams": 0.0, "avg_cost": 0.0, "margin": 0.0}
    
    for pos in positions:
        if pos["direction"] == "long":
            long_pos = dict(pos)
        elif pos["direction"] == "short":
            short_pos = dict(pos)

    # 3. 动态更新并计算保证金与浮动盈亏
    # 对于多头持仓：平仓以 Bid 价（买一价）结算；保证金以最新成交价计算
    long_pnl = 0.0
    long_margin = 0.0
    if long_pos["grams"] > 0:
        long_margin = round(long_pos["grams"] * latest_price * margin_rate, 2)
        # 浮动盈亏 = (当前卖出价 - 多头均价) * 克数
        long_pnl = round((bid_price - long_pos["avg_cost"]) * long_pos["grams"], 2)
        # 更新数据库中的动态保证金
        conn.execute(
            "UPDATE positions SET margin = ?, updated_at = ? WHERE direction = 'long'",
            (long_margin, now_str)
        )
        long_pos["margin"] = long_margin

    # 对于空头持仓：平仓以 Ask 价（卖一价）结算；保证金以最新成交价计算
    short_pnl = 0.0
    short_margin = 0.0
    if short_pos["grams"] > 0:
        short_margin = round(short_pos["grams"] * latest_price * margin_rate, 2)
        # 浮动盈亏 = (空头均价 - 当前买入价) * 克数
        short_pnl = round((short_pos["avg_cost"] - ask_price) * short_pos["grams"], 2)
        # 更新数据库中的动态保证金
        conn.execute(
            "UPDATE positions SET margin = ?, updated_at = ? WHERE direction = 'short'",
            (short_margin, now_str)
        )
        short_pos["margin"] = short_margin

    # 4. 计算账户全局财务指标
    total_floating_pnl = round(long_pnl + short_pnl, 2)
    total_margin = round(long_margin + short_margin, 2)
    equity = round(cash + total_floating_pnl, 2)
    available_cash = round(equity - total_margin, 2)

    # 风险率 = 账户权益 / 占用保证金 * 100%
    if total_margin > 0:
        risk_ratio = round((equity / total_margin) * 100, 2)
    else:
        risk_ratio = 999999.0  # 无持仓时风险率设为无穷大表示极度安全

    return {
        "cash": cash,
        "initial_capital": initial_capital,
        "commission_rate": account_row["commission_rate"],
        "margin_rate": margin_rate,
        "long_pos": long_pos,
        "short_pos": short_pos,
        "total_floating_pnl": total_floating_pnl,
        "total_margin": total_margin,
        "equity": equity,
        "available_cash": available_cash,
        "risk_ratio": risk_ratio,
        "total_deferred_fee": account_row["total_deferred_fee"] if "total_deferred_fee" in account_row.keys() else 0.0,
        "last_deferred_date": account_row["last_deferred_date"] if "last_deferred_date" in account_row.keys() else None,
        "updated_at": now_str
    }

def execute_trade(conn, action, grams, price_data, note=None):
    """
    在传入的 DB 连接事务中执行交易。
    action 可选: 'buy_open' (买入开仓), 'sell_close' (卖出平仓), 'sell_open' (卖出开仓), 'buy_close' (买入平仓)
    """
    if grams <= 0:
        raise ValueError("交易数量必须大于 0 克")

    now_str = datetime.now().isoformat()
    latest_price = price_data["latest"]
    bid_price = price_data["bid"]
    ask_price = price_data["ask"]

    # 1. 确定成交价与方向
    # 买入开仓/平仓用卖一价 (Ask)；卖出开仓/平仓用买一价 (Bid)
    if action in ['buy_open', 'buy_close']:
        trade_price = ask_price
    else:
        trade_price = bid_price

    # 2. 查询账户当前配置与资金状态
    account_row = conn.execute("SELECT cash, commission_rate, margin_rate FROM accounts WHERE id = 1").fetchone()
    if not account_row:
        raise ValueError("Account config not found.")
    cash = account_row["cash"]
    commission_rate = account_row["commission_rate"]
    margin_rate = account_row["margin_rate"]

    trade_amount = trade_price * grams
    fee = max(trade_amount * commission_rate, MIN_COMMISSION)

    # 3. 动态获取汇总数据，做开平仓合法性检查
    summary = get_account_summary(conn, price_data)

    trade_detail = None
    trade_no = generate_trade_no()

    if action == 'buy_open':
        # 买入开仓（做多）
        req_margin = trade_amount * margin_rate
        # 开仓检查：可用资金必须大于 保证金 + 手续费
        if summary["available_cash"] < (req_margin + fee):
            raise ValueError(f"可用资金不足。需要预估保证金与手续费共 {req_margin + fee:.2f} 元，当前可用资金仅 {summary['available_cash']:.2f} 元")
        
        pos = summary["long_pos"]
        old_grams = pos["grams"]
        old_cost = pos["avg_cost"]
        
        new_grams = old_grams + grams
        # 资本化手续费摊入持仓均价
        new_cost = round((old_grams * old_cost + trade_amount + fee) / new_grams, 4)
        new_margin = round(new_grams * latest_price * margin_rate, 2)

        # 更新多头持仓表
        conn.execute(
            "UPDATE positions SET grams = ?, avg_cost = ?, margin = ?, updated_at = ? WHERE direction = 'long'",
            (new_grams, new_cost, new_margin, now_str)
        )

        # 写入交易流水
        conn.execute(
            "INSERT INTO trades (trade_no, type, price, grams, amount, fee, realized_pnl, note, created_at) "
            "VALUES (?, 'buy_open', ?, ?, ?, ?, 0.0, ?, ?)",
            (trade_no, trade_price, grams, trade_amount, fee, note, now_str)
        )
        
        trade_detail = {
            "trade_no": trade_no,
            "type": "buy_open",
            "price": trade_price,
            "grams": grams,
            "amount": trade_amount,
            "fee": fee,
            "realized_pnl": 0.0
        }

    elif action == 'sell_close':
        # 卖出平仓（多头平仓）
        pos = summary["long_pos"]
        if pos["grams"] < grams:
            raise ValueError(f"多头持仓不足。当前持仓 {pos['grams']:.2f} 克，请求平仓 {grams:.2f} 克")
        
        # 已实现盈亏 = (平仓价 - 多头均价) * 平仓克数 - 平仓手续费
        realized_pnl = round((trade_price - pos["avg_cost"]) * grams - fee, 2)
        new_cash = round(cash + realized_pnl, 2)
        
        new_grams = pos["grams"] - grams
        if new_grams == 0:
            new_cost = 0.0
            new_margin = 0.0
        else:
            new_cost = pos["avg_cost"]
            new_margin = round(new_grams * latest_price * margin_rate, 2)

        # 更新账户资金与持仓
        conn.execute("UPDATE accounts SET cash = ?, updated_at = ? WHERE id = 1", (new_cash, now_str))
        conn.execute(
            "UPDATE positions SET grams = ?, avg_cost = ?, margin = ?, updated_at = ? WHERE direction = 'long'",
            (new_grams, new_cost, new_margin, now_str)
        )

        # 写入交易流水
        conn.execute(
            "INSERT INTO trades (trade_no, type, price, grams, amount, fee, realized_pnl, note, created_at) "
            "VALUES (?, 'sell_close', ?, ?, ?, ?, ?, ?, ?)",
            (trade_no, trade_price, grams, trade_amount, fee, realized_pnl, note, now_str)
        )
        
        trade_detail = {
            "trade_no": trade_no,
            "type": "sell_close",
            "price": trade_price,
            "grams": grams,
            "amount": trade_amount,
            "fee": fee,
            "realized_pnl": realized_pnl
        }

    elif action == 'sell_open':
        # 卖出开仓（做空）
        req_margin = trade_amount * margin_rate
        if summary["available_cash"] < (req_margin + fee):
            raise ValueError(f"可用资金不足。做空开仓需要预估保证金与手续费共 {req_margin + fee:.2f} 元，当前可用资金仅 {summary['available_cash']:.2f} 元")
            
        pos = summary["short_pos"]
        old_grams = pos["grams"]
        old_cost = pos["avg_cost"]
        
        new_grams = old_grams + grams
        # 做空开仓资本化手续费（售价扣除开仓费作为新成本 basis）
        new_cost = round((old_grams * old_cost + trade_amount - fee) / new_grams, 4)
        new_margin = round(new_grams * latest_price * margin_rate, 2)

        # 更新空头持仓表
        conn.execute(
            "UPDATE positions SET grams = ?, avg_cost = ?, margin = ?, updated_at = ? WHERE direction = 'short'",
            (new_grams, new_cost, new_margin, now_str)
        )

        # 写入交易流水
        conn.execute(
            "INSERT INTO trades (trade_no, type, price, grams, amount, fee, realized_pnl, note, created_at) "
            "VALUES (?, 'sell_open', ?, ?, ?, ?, 0.0, ?, ?)",
            (trade_no, trade_price, grams, trade_amount, fee, note, now_str)
        )
        
        trade_detail = {
            "trade_no": trade_no,
            "type": "sell_open",
            "price": trade_price,
            "grams": grams,
            "amount": trade_amount,
            "fee": fee,
            "realized_pnl": 0.0
        }

    elif action == 'buy_close':
        # 买入平仓（空头平仓）
        pos = summary["short_pos"]
        if pos["grams"] < grams:
            raise ValueError(f"空头持仓不足。当前空仓 {pos['grams']:.2f} 克，请求平仓 {grams:.2f} 克")
            
        # 已实现盈亏 = (空头均价 - 平仓价) * 平仓克数 - 平仓手续费
        realized_pnl = round((pos["avg_cost"] - trade_price) * grams - fee, 2)
        new_cash = round(cash + realized_pnl, 2)
        
        new_grams = pos["grams"] - grams
        if new_grams == 0:
            new_cost = 0.0
            new_margin = 0.0
        else:
            new_cost = pos["avg_cost"]
            new_margin = round(new_grams * latest_price * margin_rate, 2)

        # 更新账户资金与持仓
        conn.execute("UPDATE accounts SET cash = ?, updated_at = ? WHERE id = 1", (new_cash, now_str))
        conn.execute(
            "UPDATE positions SET grams = ?, avg_cost = ?, margin = ?, updated_at = ? WHERE direction = 'short'",
            (new_grams, new_cost, new_margin, now_str)
        )

        # 写入交易流水
        conn.execute(
            "INSERT INTO trades (trade_no, type, price, grams, amount, fee, realized_pnl, note, created_at) "
            "VALUES (?, 'buy_close', ?, ?, ?, ?, ?, ?, ?)",
            (trade_no, trade_price, grams, trade_amount, fee, realized_pnl, note, now_str)
        )
        
        trade_detail = {
            "trade_no": trade_no,
            "type": "buy_close",
            "price": trade_price,
            "grams": grams,
            "amount": trade_amount,
            "fee": fee,
            "realized_pnl": realized_pnl
        }

    else:
        raise ValueError(f"Unsupported action type: {action}")

    return trade_detail

def check_and_trigger_liquidation(conn, price_data):
    """
    检查账户是否满足强制平仓条件。若满足，则强制平仓全部持仓，释放保证金，返回清算的交易单明细。
    强平线：风险率 < 80%
    """
    summary = get_account_summary(conn, price_data)
    
    # 无持仓或风险率安全时直接跳过
    if summary["total_margin"] <= 0.0 or summary["risk_ratio"] >= LIQUIDATION_LEVEL:
        return []

    liquidated_trades = []
    
    # 触发强平：市价平仓所有仓位
    # 多头持仓强平 -> 卖出平仓
    long_grams = summary["long_pos"]["grams"]
    if long_grams > 0:
        try:
            detail = execute_trade(
                conn, 
                action='sell_close', 
                grams=long_grams, 
                price_data=price_data, 
                note=f"系统强制平仓：风险率 {summary['risk_ratio']}% 低于强平阈值 {LIQUIDATION_LEVEL}%"
            )
            liquidated_trades.append(detail)
        except Exception as e:
            print(f"[Forced Liquidation Error] Failed to liquidate long position: {e}")

    # 空头持仓强平 -> 买入平仓
    short_grams = summary["short_pos"]["grams"]
    if short_grams > 0:
        try:
            detail = execute_trade(
                conn, 
                action='buy_close', 
                grams=short_grams, 
                price_data=price_data, 
                note=f"系统强制平仓：风险率 {summary['risk_ratio']}% 低于强平阈值 {LIQUIDATION_LEVEL}%"
            )
            liquidated_trades.append(detail)
        except Exception as e:
            print(f"[Forced Liquidation Error] Failed to liquidate short position: {e}")

    return liquidated_trades
