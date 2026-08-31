import os
import sys
import json
import sqlite3
import time
import requests

def diagnose():
    print("=" * 60)
    print("         黄金模拟交易系统 - 容器运行状态诊断工具")
    print("=" * 60)

    # 1. 检查环境变量与路径权限
    print("\n[1] 正在检查路径与文件权限...")
    db_path_env = os.environ.get('DB_PATH')
    print(f"  - 环境变量 DB_PATH: {db_path_env}")
    
    # 获取 db.py 中解析 of DB_PATH
    try:
        from db import DB_PATH
        print(f"  - 系统解析的 DB_PATH: {DB_PATH}")
    except Exception as e:
        print(f"  - 【错误】导入 db 失败: {e}")
        return

    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    print(f"  - 数据库所在目录: {db_dir}")
    print(f"  - 目录是否存在: {os.path.exists(db_dir)}")
    
    # 测试目录写入权限
    test_file = os.path.join(db_dir, ".write_test")
    try:
        with open(test_file, 'w') as f:
            f.write("test")
        os.remove(test_file)
        print("  - 【正常】数据库目录具有写权限 (Write Permission: OK)")
    except Exception as e:
        print(f"  - 【严重错误】数据库目录无写入权限: {e}")
        print("    这会导致 SQLite 无法写入，通常是因为宿主机映射的文件夹权限不足，或者宿主机上的 data 目录所有人为其他用户。")

    # 2. 检查数据库连接与数据完整性
    print("\n[2] 正在检查数据库完整性与内容...")
    print(f"  - 数据库文件是否存在: {os.path.exists(DB_PATH)}")
    if os.path.exists(DB_PATH):
        try:
            size_kb = os.path.getsize(DB_PATH) / 1024
            print(f"  - 数据库文件大小: {size_kb:.2f} KB")
        except Exception as e:
            print(f"  - 无法获取数据库大小: {e}")
            
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=5)
        conn.row_factory = sqlite3.Row
        
        # 检查 WAL 模式
        cur = conn.cursor()
        cur.execute("PRAGMA journal_mode")
        mode = cur.fetchone()[0]
        print(f"  - 当前数据库模式 (Journal Mode): {mode}")
        
        # 检查表结构
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        print(f"  - 已存在数据库表: {tables}")
        
        expected_tables = ['accounts', 'positions', 'trades', 'pending_orders']
        missing_tables = [t for t in expected_tables if t not in tables]
        if missing_tables:
            print(f"  - 【警告】缺失表: {missing_tables}")
        else:
            print("  - 【正常】所有基础数据库表结构完整")
            
        # 查询账户资金
        if 'accounts' in tables:
            cur.execute("SELECT cash, initial_capital, domestic_premium FROM accounts WHERE id = 1")
            acc = cur.fetchone()
            if acc:
                print(f"  - 账户可用资金 (Cash): {acc['cash']} 元 (初始资金: {acc['initial_capital']} 元)")
                print(f"  - 国内溢价系数 (Premium): {acc['domestic_premium']} 元")
                if acc['cash'] == 100000.0 and acc['initial_capital'] == 100000.0:
                    print("    【提示】检测到账户资金正好是默认的 10 万。如果你之前有做过交易且有持仓，")
                    print("            这说明你的历史数据库 `gold_trader.db` 没有被正确载入，当前运行的是全新初始化的空白库。")
            else:
                print("  - 【错误】未在 accounts 表中查找到 ID=1 的数据记录")
                
        # 查询交易流水数
        if 'trades' in tables:
            cur.execute("SELECT COUNT(*) FROM trades")
            trade_count = cur.fetchone()[0]
            print(f"  - 交易流水记录数 (Trades): {trade_count} 条")
            if trade_count > 0:
                print("    已加载历史交易记录：")
                cur.execute("SELECT type, price, grams, created_at FROM trades ORDER BY id DESC LIMIT 3")
                for row in cur.fetchall():
                    print(f"      * {row['created_at']} | {row['type']} | 单价: {row['price']} 元 | 重量: {row['grams']} 克")
            else:
                print("    【提示】交易流水记录为空。")
                
    except Exception as e:
        print(f"  - 【严重错误】无法读取或查询 SQLite 数据库: {e}")
    finally:
        if conn:
            conn.close()

    # 3. 检查缓存文件状态
    print("\n[3] 正在检查缓存文件状态...")
    for cache_name in ['price_cache', 'history_cache']:
        c_file = os.path.join(db_dir, f"{cache_name}.json")
        exists = os.path.exists(c_file)
        print(f"  - 缓存 {cache_name}.json 是否存在: {exists}")
        if exists:
            try:
                with open(c_file, 'r', encoding='utf-8') as f:
                    c_data = json.load(f)
                mtime = os.path.getmtime(c_file)
                mtime_str = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(mtime))
                print(f"    * 文件修改时间: {mtime_str}")
                if cache_name == 'price_cache':
                    latest_price = c_data.get('data', {}).get('latest') if c_data else None
                    is_sim = c_data.get('is_simulated') if c_data else False
                    print(f"    * 缓存的最新金价: {latest_price} 元 (是否为模拟数据: {is_sim})")
            except Exception as ce:
                print(f"    * 【错误】无法解析缓存文件: {ce}")

    # 4. 检查网络连接与外网 API 状态
    print("\n[4] 正在检测容器外网 API 通信能力 (3秒超时)...")
    from gold_price import EASTMONEY_QUOTE_URL, EASTMONEY_KLINE_URL, FOREX_API_URL, HEADERS

    apis = {
        "EastMoney Au(T+D)": EASTMONEY_QUOTE_URL,
        "EastMoney K线": EASTMONEY_KLINE_URL,
        "Sina SGE 备用": "https://hq.sinajs.cn/list=gds_AUTD",
        "Exchange Rate API": FOREX_API_URL,
    }

    for name, url in apis.items():
        start = time.time()
        try:
            if "Sina" in name:
                req_headers = {
                    "Referer": "https://finance.sina.com.cn",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                }
            else:
                req_headers = HEADERS
            r = requests.get(url, headers=req_headers, timeout=3.0, verify=False)
            elapsed = time.time() - start
            print(f"  - {name}: 响应码={r.status_code} | 耗时={elapsed:.2f}秒 -> 【正常】")
        except requests.exceptions.Timeout:
            print(f"  - {name}: 请求超时 (Timeout) -> 【网络受阻 / 无法访问外部网络】")
        except requests.exceptions.ConnectionError as ce:
            print(f"  - {name}: 连接失败 (Connection Error: {ce}) -> 【网络受阻 / DNS解析失败 / 接口被屏蔽】")
        except Exception as ge:
            print(f"  - {name}: 其他错误 ({ge})")

    print("\n" + "=" * 60)
    print("诊断结束，请根据以上的【错误】和【提示】项目进行排查。")
    print("=" * 60)

if __name__ == '__main__':
    diagnose()
