import sqlite3
from trade_engine import execute_trade, get_account_summary
from db import init_db

def run_tests():
    # 使用内存数据库进行测试，隔离真实文件
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    
    # 建立与 db.py 相同的结构
    conn.execute("""
        CREATE TABLE accounts (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            initial_capital REAL NOT NULL DEFAULT 100000.0,
            cash REAL NOT NULL DEFAULT 100000.0,
            commission_rate REAL NOT NULL DEFAULT 0.0004,
            margin_rate REAL NOT NULL DEFAULT 0.10,
            domestic_premium REAL NOT NULL DEFAULT 5.00,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE positions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            direction TEXT NOT NULL UNIQUE CHECK (direction IN ('long', 'short')),
            grams REAL NOT NULL DEFAULT 0.0 CHECK (grams >= 0.0),
            avg_cost REAL NOT NULL DEFAULT 0.0 CHECK (avg_cost >= 0.0),
            margin REAL NOT NULL DEFAULT 0.0 CHECK (margin >= 0.0),
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trade_no TEXT UNIQUE NOT NULL,
            type TEXT NOT NULL CHECK (type IN ('buy_open', 'sell_close', 'sell_open', 'buy_close')),
            price REAL NOT NULL,
            grams REAL NOT NULL,
            amount REAL NOT NULL,
            fee REAL NOT NULL,
            realized_pnl REAL NOT NULL DEFAULT 0.0,
            note TEXT,
            created_at TEXT NOT NULL
        )
    """)
    
    # 初始化数据
    conn.execute("INSERT INTO accounts (id, initial_capital, cash, updated_at) VALUES (1, 100000.0, 100000.0, 'test')")
    conn.execute("INSERT INTO positions (direction, grams, avg_cost, margin, updated_at) VALUES ('long', 0.0, 0.0, 0.0, 'test')")
    conn.execute("INSERT INTO positions (direction, grams, avg_cost, margin, updated_at) VALUES ('short', 0.0, 0.0, 0.0, 'test')")
    conn.commit()

    print("Executing Trade Engine Mathematical Verification...")

    # 1. 模拟多头（做多）完整循环验证
    # 假定最新价为 800 元/克，买开 100 克。因为点差，开仓买入成交价通常是 Ask 价（假设为 800.05）
    price_open_long = {
        "latest": 800.00,
        "bid": 799.95,
        "ask": 800.05
    }
    
    print("\n[Test 1] 多头开仓：买入开仓 100克 (成交价: 800.05)")
    with conn:
        detail_open = execute_trade(conn, 'buy_open', 100, price_open_long)
    
    # 开仓后，校验 cash, positions 状态
    sum1 = get_account_summary(conn, price_open_long)
    # 成交金额 = 800.05 * 100 = 80005
    # 手续费 = 80005 * 0.0004 = 32.002 -> 32.00 元
    # 多头持仓均价 = (80005 + 32.0) / 100 = 800.37 元/克
    # 占用保证金 = 100 * 800 * 0.10 = 8000.0 元
    # 浮动盈亏 = (799.95 - 800.37) * 100 = -42.0 元 (已包含开仓点差损失和手续费损失)
    # 权益 = 100000.0 + (-42.0) = 99958.0
    # 可用资金 = 99958.0 - 8000.0 = 91958.0
    
    print(f"  均价(含手续费): {sum1['long_pos']['avg_cost']} 元/克 (期望: 800.37)")
    print(f"  占用保证金: {sum1['long_pos']['margin']} 元 (期望: 8000.00)")
    print(f"  浮动盈亏: {sum1['total_floating_pnl']} 元 (期望: -42.00)")
    print(f"  账户权益: {sum1['equity']} 元 (期望: 99958.00)")
    print(f"  可用资金: {sum1['available_cash']} 元 (期望: 91958.00)")
    
    assert abs(sum1['long_pos']['avg_cost'] - 800.37) < 0.01
    assert abs(sum1['total_floating_pnl'] - (-42.00)) < 0.01
    assert abs(sum1['equity'] - 99958.00) < 0.01

    # 假定价格上涨到 810 元/克，多头卖出平仓。平仓成交以 Bid 价（假设为 809.95）
    price_close_long = {
        "latest": 810.00,
        "bid": 809.95,
        "ask": 810.05
    }
    
    print("[Test 1] 多头平仓：卖出平仓 100克 (成交价: 809.95)")
    with conn:
        detail_close = execute_trade(conn, 'sell_close', 100, price_close_long)

    sum2 = get_account_summary(conn, price_close_long)
    # 平仓金额 = 809.95 * 100 = 80995
    # 平仓手续费 = 80995 * 0.0004 = 32.398 -> 32.40 元
    # 已实现盈亏 = (809.95 - 800.37) * 100 - 32.40 = 958.0 - 32.40 = 925.60 元
    # 期末现金 cash = 100000.0 + 925.60 = 100925.60 元
    # 期望资金变动：
    # 总支出 = 100000.0 - 32.0 (开仓费) - 32.40 (平仓费) = 99935.60
    # 总毛收入差额 = (809.95 - 800.05) * 100 = 990.00 元
    # 最终净利 = 990.00 - 32.0 - 32.40 = 925.60 元
    # 账户权益应为 = 100925.60 元
    
    print(f"  已实现盈亏: {detail_close['realized_pnl']} 元 (期望: 925.60)")
    print(f"  最终期末现金: {sum2['cash']} 元 (期望: 100925.60)")
    
    assert abs(detail_close['realized_pnl'] - 925.60) < 0.01
    assert abs(sum2['cash'] - 100925.60) < 0.01
    print("  -> [Test 1 (做多)] 财务数据流计算完全一致，无财务黑洞！")

    # 2. 模拟空头（做空）完整循环验证
    # 假定此时资金余额为 100,925.60。最新价为 800 元/克，卖出开仓 100 克。成交价为 Bid 价（假设为 799.95）
    price_open_short = {
        "latest": 800.00,
        "bid": 799.95,
        "ask": 800.05
    }
    
    print("\n[Test 2] 空头开仓：卖出开仓 100克 (成交价: 799.95)")
    with conn:
        execute_trade(conn, 'sell_open', 100, price_open_short)

    sum3 = get_account_summary(conn, price_open_short)
    # 成交金额 = 799.95 * 100 = 79995
    # 手续费 = 79995 * 0.0004 = 31.998 -> 32.00 元
    # 空头持仓成本均价 = (79995 - 32.00) / 100 = 799.63 元/克 (开仓费直接削减成本)
    # 占用保证金 = 100 * 800 * 0.10 = 8000.0 元
    # 浮动盈亏 = (799.63 - 800.05) * 100 = -42.0 元 (已包含开仓点差损失和手续费损失)
    # 账户权益 = 100925.60 - 42.00 = 100883.60 元
    # 可用资金 = 100883.60 - 8000.0 = 92883.60 元
    
    print(f"  均价(含手续费扣减): {sum3['short_pos']['avg_cost']} 元/克 (期望: 799.63)")
    print(f"  浮动盈亏: {sum3['total_floating_pnl']} 元 (期望: -42.00)")
    print(f"  账户权益: {sum3['equity']} 元 (期望: 100883.60)")
    
    assert abs(sum3['short_pos']['avg_cost'] - 799.63) < 0.01
    assert abs(sum3['equity'] - 100883.60) < 0.01

    # 假定价格下跌到 790 元/克，多头获利，买入平仓。成交以 Ask 价（假设为 790.05）
    price_close_short = {
        "latest": 790.00,
        "bid": 789.95,
        "ask": 790.05
    }
    
    print("[Test 2] 空头平仓：买入平仓 100克 (成交价: 790.05)")
    with conn:
        detail_close_short = execute_trade(conn, 'buy_close', 100, price_close_short)

    sum4 = get_account_summary(conn, price_close_short)
    # 平仓金额 = 790.05 * 100 = 79005
    # 平仓手续费 = 79005 * 0.0004 = 31.602 -> 31.60 元
    # 已实现盈亏 = (799.63 - 790.05) * 100 - 31.60 = 958.00 - 31.60 = 926.40 元
    # 期末现金 cash = 100925.60 + 926.40 = 101852.00 元
    # 期望资金变动：
    # 纯价格变动收益 = (799.95 - 790.05) * 100 = 990.00
    # 总手续费 = 32.00 + 31.60 = 63.60 元
    # 最终净利 = 990.00 - 63.60 = 926.40 元
    # 最终 cash 应为 = 100925.60 + 926.40 = 101852.00
    
    print(f"  已实现盈亏: {detail_close_short['realized_pnl']} 元 (期望: 926.40)")
    print(f"  最终期末现金: {sum4['cash']} 元 (期望: 101852.00)")
    
    assert abs(detail_close_short['realized_pnl'] - 926.40) < 0.01
    assert abs(sum4['cash'] - 101852.00) < 0.01
    print("  -> [Test 2 (做空)] 财务数据流计算完全一致，无财务黑洞！")

    print("\nCongrats! All Trade Engine Math Verification tests passed successfully!")
    conn.close()

if __name__ == '__main__':
    run_tests()
