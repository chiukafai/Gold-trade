import sqlite3
import os
from datetime import datetime

# 使用动态路径确保在云端/不同部署环境下的兼容性
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'gold_trader.db'))

# 确保数据库所在的父目录存在（在 Docker 挂载全新数据卷时非常有用）
_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)


def get_db_connection():
    """获取数据库连接，配置 WAL 模式和外键约束"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # 开启 WAL 模式提高写入并发性能
    conn.execute("PRAGMA journal_mode=WAL;")
    # 开启外键约束限制
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    """初始化数据库表结构，并创建初始账户数据"""
    conn = get_db_connection()
    try:
        with conn:
            # 1. 创建账户表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initial_capital REAL NOT NULL DEFAULT 100000.0,
                    cash REAL NOT NULL DEFAULT 100000.0,
                    commission_rate REAL NOT NULL DEFAULT 0.0004,
                    margin_rate REAL NOT NULL DEFAULT 0.10,
                    domestic_premium REAL NOT NULL DEFAULT 5.00,
                    updated_at TEXT NOT NULL
                )
            """)

            # 自动模式升级：检测如果缺少配置字段则自动 ALTER TABLE
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(accounts)")
            cols = [row[1] for row in cursor.fetchall()]
            if "commission_rate" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN commission_rate REAL NOT NULL DEFAULT 0.0004")
                print("Migration: Added commission_rate to accounts table.")
            if "margin_rate" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN margin_rate REAL NOT NULL DEFAULT 0.10")
                print("Migration: Added margin_rate to accounts table.")
            if "domestic_premium" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN domestic_premium REAL NOT NULL DEFAULT 5.00")
                print("Migration: Added domestic_premium to accounts table.")
            if "last_deferred_date" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN last_deferred_date TEXT")
                print("Migration: Added last_deferred_date to accounts table.")
            if "total_deferred_fee" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN total_deferred_fee REAL NOT NULL DEFAULT 0.0")
                print("Migration: Added total_deferred_fee to accounts table.")
            # 自动交易配置（信号驱动的自动模拟交易）
            if "auto_trade_enabled" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN auto_trade_enabled INTEGER NOT NULL DEFAULT 0")
                print("Migration: Added auto_trade_enabled to accounts table.")
            if "auto_trade_grams" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN auto_trade_grams REAL NOT NULL DEFAULT 10.0")
                print("Migration: Added auto_trade_grams to accounts table.")
            if "auto_trade_interval" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN auto_trade_interval INTEGER NOT NULL DEFAULT 300")
                print("Migration: Added auto_trade_interval to accounts table.")
            if "auto_trade_threshold" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN auto_trade_threshold REAL NOT NULL DEFAULT 0.5")
                print("Migration: Added auto_trade_threshold to accounts table.")
            # 自动交易策略模式：consensus=现有共识评分；resonance=模拟器多指标共振
            if "strategy_mode" not in cols:
                conn.execute("ALTER TABLE accounts ADD COLUMN strategy_mode TEXT NOT NULL DEFAULT 'consensus'")
                print("Migration: Added strategy_mode to accounts table.")

            # 2. 创建持仓表 (多/空双向持仓)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    direction TEXT NOT NULL UNIQUE CHECK (direction IN ('long', 'short')),
                    grams REAL NOT NULL DEFAULT 0.0 CHECK (grams >= 0.0),
                    avg_cost REAL NOT NULL DEFAULT 0.0 CHECK (avg_cost >= 0.0),
                    margin REAL NOT NULL DEFAULT 0.0 CHECK (margin >= 0.0),
                    updated_at TEXT NOT NULL
                )
            """)

            # 3. 创建交易流水表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_no TEXT UNIQUE NOT NULL,
                    type TEXT NOT NULL CHECK (type IN ('buy_open', 'sell_close', 'sell_open', 'buy_close')),
                    price REAL NOT NULL CHECK (price > 0.0),
                    grams REAL NOT NULL CHECK (grams > 0.0),
                    amount REAL NOT NULL CHECK (amount > 0.0),
                    fee REAL NOT NULL CHECK (fee >= 0.0),
                    realized_pnl REAL NOT NULL DEFAULT 0.0,
                    note TEXT,
                    created_at TEXT NOT NULL
                )
            """)

            # 4. 创建挂单表
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pending_orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_no TEXT UNIQUE NOT NULL,
                    order_type TEXT NOT NULL CHECK (order_type IN ('limit', 'stop')),
                    direction TEXT NOT NULL CHECK (direction IN ('buy_open', 'sell_close', 'sell_open', 'buy_close')),
                    trigger_price REAL NOT NULL CHECK (trigger_price > 0.0),
                    grams REAL NOT NULL CHECK (grams > 0.0),
                    frozen_margin REAL NOT NULL DEFAULT 0.0 CHECK (frozen_margin >= 0.0),
                    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'filled', 'cancelled')),
                    created_at TEXT NOT NULL
                )
            """)

            # 5. 初始化单账户记录 (如果不存在)
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM accounts WHERE id = 1")
            if cursor.fetchone()[0] == 0:
                now_str = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO accounts (id, initial_capital, cash, updated_at) VALUES (1, 100000.0, 100000.0, ?)",
                    (now_str,)
                )

            # 6. 初始化空头和多头的零持仓记录 (方便后续使用 UPDATE 统一操作)
            cursor.execute("SELECT COUNT(*) FROM positions")
            if cursor.fetchone()[0] == 0:
                now_str = datetime.now().isoformat()
                conn.execute(
                    "INSERT INTO positions (direction, grams, avg_cost, margin, updated_at) VALUES ('long', 0.0, 0.0, 0.0, ?)",
                    (now_str,)
                )
                conn.execute(
                    "INSERT INTO positions (direction, grams, avg_cost, margin, updated_at) VALUES ('short', 0.0, 0.0, 0.0, ?)",
                    (now_str,)
                )
        print("Database initialized successfully.")
    except sqlite3.Error as e:
        print(f"Database initialization failed: {e}")
    finally:
        conn.close()

if __name__ == '__main__':
    init_db()
