import requests
import time
import re
import random
import math
from datetime import datetime, timedelta
import os
import json

# 行情缓存与模拟状态字典
_price_cache = {
    "data": None,
    "last_updated": 0.0,
    "is_simulated": False
}

# 缓存有效期为 5 秒
CACHE_EXPIRY_SECONDS = 5.0

# 历史走势缓存字典与过期策略 (10分钟)
_history_cache = {}
HIST_CACHE_EXPIRY = 600.0

def _load_cache_from_file(cache_name):
    """从数据库所在目录加载持久化缓存文件，保证多进程/多线程下数据一致"""
    try:
        from db import DB_PATH
        cache_dir = os.path.dirname(os.path.abspath(DB_PATH))
        cache_file = os.path.join(cache_dir, f"{cache_name}.json")
        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[Cache Load Warning] Failed to load {cache_name} from file: {e}")
    return None

def _save_cache_to_file(cache_name, data):
    """将缓存写入数据库目录下的持久化文件，保证数据一致性"""
    try:
        from db import DB_PATH
        cache_dir = os.path.dirname(os.path.abspath(DB_PATH))
        os.makedirs(cache_dir, exist_ok=True)
        cache_file = os.path.join(cache_dir, f"{cache_name}.json")
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Cache Save Warning] Failed to save {cache_name} to file: {e}")


def _get_db_config():
    """从数据库中读取最新的配置信息，失败时则返回默认设置"""
    try:
        from db import get_db_connection
        conn = get_db_connection()
        row = conn.execute("SELECT commission_rate, margin_rate, domestic_premium FROM accounts WHERE id = 1").fetchone()
        conn.close()
        if row:
            return float(row["commission_rate"]), float(row["margin_rate"]), float(row["domestic_premium"])
    except Exception:
        pass
    return 0.0004, 0.10, 5.00

# API 地址配置 — 全面切换到东方财富国内源
EASTMONEY_QUOTE_URL = "http://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_KLINE_URL = "http://push2his.eastmoney.com/api/qt/stock/kline/get"
SINA_API_URL = "http://hq.sinajs.cn/list=gds_AUTD"
FOREX_API_URL = "https://open.er-api.com/v6/latest/USD"

# 东方财富 secid 配置
EM_SECID_AUTD = "118.AUTD"      # 上海黄金交易所 Au(T+D)
EM_SECID_DXY = "100.UDI"         # 美元指数
EM_SECID_US10Y = "171.US10Y"     # 美国10年期国债收益率

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
}


def _generate_simulated_price():
    """生成模拟黄金价格（基于随机漫步），用于所有 API 均失效时的兜底"""
    global _price_cache
    
    # 尝试从文件加载最新的 price_cache，确保跨进程的随机漫步依然连续
    file_cache = _load_cache_from_file("price_cache")
    if file_cache:
        _price_cache = file_cache
        
    current_time = time.time()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    # 如果没有历史缓存，初始化基准价（2026年实际金价约在 870 元/克左右）
    if not _price_cache["data"]:
        base_price = 870.0
        bid = base_price - 0.05
        ask = base_price + 0.05
        
        sim_data = {
            "symbol": "gds_AUTD_SIM",
            "name": "Au(T+D)[模拟]",
            "contract": "Au(T+D)",
            "latest": base_price,
            "bid": bid,
            "ask": ask,
            "open": base_price - 1.0,
            "high": base_price + 2.0,
            "low": base_price - 1.5,
            "prev_close": base_price - 0.5,
            "timestamp": now_str,
            "from_cache": False,
            "is_simulated": True,
            "source": "simulated"
        }
    else:
        # 基于上次报价进行随机变动
        prev = _price_cache["data"]
        change = round(random.uniform(-0.3, 0.3), 2)
        new_latest = round(prev["latest"] + change, 2)
        new_bid = round(new_latest - 0.05, 2)
        new_ask = round(new_latest + 0.05, 2)
        new_high = max(prev["high"], new_latest)
        new_low = min(prev["low"], new_latest)
        
        sim_data = {
            "symbol": "gds_AUTD_SIM",
            "name": "Au(T+D)[模拟]",
            "contract": "Au(T+D)",
            "latest": new_latest,
            "bid": new_bid,
            "ask": new_ask,
            "open": prev["open"],
            "high": new_high,
            "low": new_low,
            "prev_close": prev["prev_close"],
            "timestamp": now_str,
            "from_cache": False,
            "is_simulated": True,
            "source": "simulated"
        }
        
    _price_cache["data"] = sim_data
    _price_cache["last_updated"] = current_time
    _price_cache["is_simulated"] = True
    _save_cache_to_file("price_cache", _price_cache)
    return sim_data


def _fetch_sina_sge_price():
    """直接从新浪财经获取上海黄金交易所 SGE Au(T+D) 实时行情数据"""
    # 强制使用 HTTPS 和新浪要求的 Referer 头部
    url = "https://hq.sinajs.cn/list=gds_AUTD"
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    r = requests.get(url, headers=headers, timeout=3.0, verify=False)
    if r.status_code == 200:
        content = r.text
        # 解析数据格式: var hq_str_gds_AUTD="886.61,0,885.58,886.61,889.33,881.81,02:00:04,883.69,884.00,1606,9.00,4.00,2026-07-25,黄金延期";
        match = re.search(r'="([^"]+)"', content)
        if match:
            fields = match.group(1).split(',')
            if len(fields) >= 14:
                latest = float(fields[0])
                bid = float(fields[2])
                ask = float(fields[3])
                high = float(fields[4])
                low = float(fields[5])
                time_str = fields[6]
                prev_close = float(fields[7])
                open_price = float(fields[8])
                volume = int(fields[9])
                date_str = fields[12]
                name = fields[13]
                
                return {
                    "symbol": "gds_AUTD",
                    "name": name,
                    "contract": "Au(T+D)",
                    "latest": latest,
                    "bid": bid,
                    "ask": ask,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "prev_close": prev_close,
                    "timestamp": f"{date_str} {time_str}",
                    "from_cache": False,
                    "is_simulated": False,
                    "source": "sina_sge"
                }
    raise RuntimeError("Failed to parse Sina SGE price response")


def _fetch_eastmoney_price():
    """从东方财富获取 Au(T+D) 实时行情（国内直连，低延迟，数据与 SGE 官方一致）"""
    params = {
        "secid": EM_SECID_AUTD,
        "fields": "f43,f44,f45,f46,f47,f48,f57,f58,f60",
        "fltt": "2"
    }
    r = requests.get(EASTMONEY_QUOTE_URL, params=params, headers=HEADERS, timeout=3.0)
    if r.status_code == 200:
        data = r.json().get("data")
        if data and data.get("f43"):
            latest = float(data["f43"])
            high = float(data["f44"])
            low = float(data["f45"])
            open_price = float(data["f46"])
            volume = int(data["f47"]) if data.get("f47") else 0
            prev_close = float(data["f60"])
            code = data.get("f57", "AUTD")
            name = data.get("f58", "黄金T+D")

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

            return {
                "symbol": code,
                "name": name,
                "contract": "Au(T+D)",
                "latest": latest,
                "bid": round(latest - 0.05, 2),
                "ask": round(latest + 0.05, 2),
                "open": open_price,
                "high": high,
                "low": low,
                "prev_close": prev_close,
                "volume": volume,
                "timestamp": now_str,
                "from_cache": False,
                "is_simulated": False,
                "source": "eastmoney"
            }
    raise RuntimeError("Failed to parse EastMoney price response")

def fetch_sge_price(force_refresh=False):
    """
    多层容灾获取黄金行情：
    1. 首选国内源：东方财富 Au(T+D) 实时行情 (国内直连，低延迟，数据与 SGE 官方一致)
    2. 次选国内源：新浪财经 SGE 官方实盘行情 (HTTPS直连)
    3. 离线缓存：网络断开时沿用上次最后的实盘价格，停止随机变动
    4. 终极兜底：当无网络且无任何缓存时，自适应生成模拟价格随机走势
    """
    global _price_cache
 
    current_time = time.time()
    
    # 优先从文件同步缓存以实现多进程/多线程之间共享最新的报价数据
    file_cache = _load_cache_from_file("price_cache")
    if file_cache:
        _price_cache = file_cache
    
    # 5秒缓存策略
    if not force_refresh and _price_cache["data"] and (current_time - _price_cache["last_updated"] < CACHE_EXPIRY_SECONDS):
        data = _price_cache["data"].copy()
        data["from_cache"] = True
        return data

    # 1. 尝试第一优先源：东方财富 Au(T+D) 实时行情 (国内直连，低延迟)
    try:
        em_data = _fetch_eastmoney_price()
        _price_cache["data"] = em_data
        _price_cache["last_updated"] = current_time
        _price_cache["is_simulated"] = False
        _save_cache_to_file("price_cache", _price_cache)
        return em_data
    except Exception as e_em:
        print(f"[Warning] Failed to fetch EastMoney price: {e_em}. Trying Sina SGE...")
        pass

    # 2. 尝试第二优先源：新浪财经 SGE 实盘接口 (国内直连备用)
    try:
        sina_data = _fetch_sina_sge_price()
        _price_cache["data"] = sina_data
        _price_cache["last_updated"] = current_time
        _price_cache["is_simulated"] = False
        _save_cache_to_file("price_cache", _price_cache)
        return sina_data
    except Exception as e_sina:
        print(f"[Warning] Failed to fetch Sina SGE price: {e_sina}. Using offline cache...")
        pass

    # 3. 离线兜底方案：如果获取失败，但先前保存过真实的缓存数据，直接沿用之前最后一次使用的价格与日期
    if _price_cache["data"]:
        offline_data = _price_cache["data"].copy()
        offline_data["is_simulated"] = True
        offline_data["source"] = "offline_cached"
        return offline_data

    # 4. 兜底方案的兜底：只有当完全没有任何历史缓存时，才使用本地模拟价格进行随机漫步
    return _generate_simulated_price()

def fetch_gold_history(range_str="30d", interval="1d"):
    """
    获取历史黄金价格历史（支持 "7d", "30d", "90d", "1y" 范围），支持 "1d", "1h", "15m" 周期。
    采用东方财富K线接口获取真实数据 + 自适应模拟随机走势双重容灾。
    """
    global _history_cache, _price_cache
    cache_key = f"{range_str}_{interval}"
    current_time = time.time()
    
    # 优先从文件加载缓存
    file_cache = _load_cache_from_file("history_cache")
    if file_cache:
        _history_cache = file_cache
    
    # 如果缓存存在且未过期，直接返回缓存数据
    if cache_key in _history_cache:
        cache_item = _history_cache[cache_key]
        if current_time - cache_item["last_updated"] < HIST_CACHE_EXPIRY:
            return cache_item["data"]

    # 1. 尝试从东方财富获取历史K线数据
    try:
        # 映射 interval 到东方财富 klt 参数
        # 【跨平台备注】4H 周期东方财富无原生支持，用 1H 数据聚合成 4H
        klt_map = {"1d": "101", "1h": "60", "15m": "15", "4h": "60"}
        klt = klt_map.get(interval, "101")
        need_4h_aggregate = (interval == "4h")

        # 映射 range_str 到起止日期
        days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
        days = days_map.get(range_str, 30)
        end_date = datetime.now().strftime("%Y%m%d")
        beg_date = (datetime.now() - timedelta(days=days + 5)).strftime("%Y%m%d")

        params = {
            "secid": EM_SECID_AUTD,
            "klt": klt,
            "fqt": "0",
            "beg": beg_date,
            "end": end_date,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57"
        }
        r_hist = requests.get(EASTMONEY_KLINE_URL, params=params, headers=HEADERS, timeout=3.5)
        if r_hist.status_code == 200:
            data = r_hist.json()
            klines = data.get("data", {}).get("klines", [])
            
            history_points = []
            for kline in klines:
                parts = kline.split(",")
                if len(parts) < 7:
                    continue
                
                raw_date = parts[0]   # 日K: "2026-07-24", 分钟K: "2026-07-25 02:00"
                open_price = float(parts[1])
                close_price = float(parts[2])
                high_price = float(parts[3])   # f54 当日最高
                low_price = float(parts[4])    # f55 当日最低
                vol = int(float(parts[5])) if parts[5] else 0

                # 根据周期和范围格式化日期标签
                if interval in ("15m", "1h"):
                    # 分钟K: "2026-07-25 02:00" -> "07-25 02:00"
                    dt = datetime.strptime(raw_date, "%Y-%m-%d %H:%M")
                    date_str = dt.strftime("%m-%d %H:%M")
                elif range_str == "1y":
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                    date_str = dt.strftime("%y-%m-%d")
                else:
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                    date_str = dt.strftime("%m-%d")
                    
                history_points.append({
                    "date": date_str,
                    "price": close_price,
                    "open": open_price,
                    "high": high_price,
                    "low": low_price,
                    "volume": vol,
                    # 完整日期（%Y-%m-%d），供跨年窗口的日期对齐使用，避免 %m-%d 无年份导致错配
                    "full_date": dt.strftime("%Y-%m-%d")
                })
            
            if history_points:
                # 4H 聚合：每 4 根 1H K 线合并为 1 根 4H K 线
                if need_4h_aggregate:
                    aggregated = []
                    for i in range(0, len(history_points), 4):
                        chunk = history_points[i:i+4]
                        if not chunk:
                            break
                        first = chunk[0]
                        last = chunk[-1]
                        merged = {
                            "date": first["date"],
                            "price": last["price"],
                            "open": first.get("open", first["price"]),
                            "high": max(p.get("high", p["price"]) for p in chunk),
                            "low": min(p.get("low", p["price"]) for p in chunk),
                            "volume": sum(p.get("volume", 0) for p in chunk),
                            "full_date": first.get("full_date", first["date"])
                        }
                        aggregated.append(merged)
                    history_points = aggregated
                _history_cache[cache_key] = {
                    "last_updated": current_time,
                    "data": history_points
                }
                _save_cache_to_file("history_cache", _history_cache)
                return history_points
    except Exception as e:
        print(f"[Warning] Failed to fetch EastMoney history for {range_str} ({interval}): {e}. Generating simulated history...")
        pass

    # 2. 兜底策略：根据时间周期自主模拟历史随机漫步数据
    # 如果接口报错但缓存中有旧数据，即便过期也优先使用旧缓存（防止刷新价格突然跳变）
    if cache_key in _history_cache:
        return _history_cache[cache_key]["data"]

    history_points = []
    now = datetime.now()
    base_price = 870.0
    
    # 确保加载最新的价格缓存以作为历史随机漫步的基准点
    _price_cache_file = _load_cache_from_file("price_cache")
    if _price_cache_file:
        _price_cache = _price_cache_file
        
    if _price_cache["data"]:
        base_price = _price_cache["data"]["latest"]

    # 确定模拟点数
    if interval == "15m":
        points_count = 100
        delta = timedelta(minutes=15)
        fmt = "%m-%d %H:%M"
    elif interval == "1h":
        points_count = 120
        delta = timedelta(hours=1)
        fmt = "%m-%d %H:%M"
    elif interval == "4h":
        points_count = 120
        delta = timedelta(hours=4)
        fmt = "%m-%d %H:%M"
    else:
        days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
        points_count = days_map.get(range_str, 30)
        delta = timedelta(days=1)
        fmt = "%y-%m-%d" if range_str == "1y" else "%m-%d"

    # 我们从当前的最新价格（今天）开始，向过去反向递减模拟，确保最后一天的价格和实时基准价完全一致！
    current_sim_price = base_price
    for i in range(points_count):
        if interval == "15m":
            change = round(random.uniform(-0.6, 0.6), 2)
        elif interval in ("1h", "4h"):
            change = round(random.uniform(-1.2, 1.2), 2)
        else:
            change = round(random.uniform(-2.5, 3.0), 2)

        date_obj = now - delta * i
        date_str = date_obj.strftime(fmt)

        # 倒序生成：由于是倒序，我们把点插入到列表最前面
        history_points.insert(0, {
            "date": date_str,
            "price": round(current_sim_price, 2),
            "open": round(current_sim_price - change * 0.5, 2),
            "high": round(current_sim_price + abs(change), 2),
            "low": round(current_sim_price - abs(change), 2),
            "volume": int(random.uniform(500, 3500)),
            "full_date": date_obj.strftime("%Y-%m-%d")
        })
        
        # 向过去递退时，减去波幅
        current_sim_price -= change
    
    # 模拟数据也保存到缓存中，使得之后刷新完全稳定不变
    _history_cache[cache_key] = {
        "last_updated": current_time,
        "data": history_points
    }
    _save_cache_to_file("history_cache", _history_cache)
    return history_points

def fetch_macro_history(symbol_type, range_str="30d"):
    """
    抓取宏观经济因子历史数据 (dxy: 美元指数 100.UDI, tnx: 10年美债收益率 171.US10Y)
    采用东方财富K线接口获取真实数据，并提供模拟数据作为降级兜底。
    """
    global _history_cache
    cache_key = f"macro_{symbol_type.lower()}_{range_str}"
    current_time = time.time()
    
    # 若缓存存在且未过期，直接返回缓存数据
    if cache_key in _history_cache:
        cache_item = _history_cache[cache_key]
        if current_time - cache_item["last_updated"] < HIST_CACHE_EXPIRY:
            return cache_item["data"]

    # 东方财富 secid 映射
    secid_map = {
        "dxy": EM_SECID_DXY,
        "tnx": EM_SECID_US10Y
    }
    secid = secid_map.get(symbol_type.lower())
    if not secid:
        return []

    # 1. 尝试从东方财富获取
    try:
        days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
        days = days_map.get(range_str, 30)
        end_date = datetime.now().strftime("%Y%m%d")
        beg_date = (datetime.now() - timedelta(days=days + 5)).strftime("%Y%m%d")

        params = {
            "secid": secid,
            "klt": "101",
            "fqt": "0",
            "beg": beg_date,
            "end": end_date,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57"
        }
        r = requests.get(EASTMONEY_KLINE_URL, params=params, headers=HEADERS, timeout=3.5)
        if r.status_code == 200:
            data = r.json()
            klines = data.get("data", {}).get("klines", [])
            
            points = []
            for kline in klines:
                parts = kline.split(",")
                if len(parts) < 3:
                    continue
                
                raw_date = parts[0]   # "2026-07-24"
                close_val = float(parts[2])

                if range_str == "1y":
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                    date_str = dt.strftime("%y-%m-%d")
                else:
                    dt = datetime.strptime(raw_date, "%Y-%m-%d")
                    date_str = dt.strftime("%m-%d")
                    
                points.append({
                    "date": date_str,
                    "value": round(close_val, 4),
                    "full_date": dt.strftime("%Y-%m-%d")
                })
            
            if points:
                _history_cache[cache_key] = {
                    "last_updated": current_time,
                    "data": points
                }
                return points
    except Exception as e:
        print(f"[Warning] Failed to fetch EastMoney macro history for {secid}: {e}. Generating simulated macro history...")
        pass

    # 2. 兜底策略：如果接口报错但缓存中有旧数据，即便过期也优先使用旧缓存（防止价格突然跳变）
    if cache_key in _history_cache:
        return _history_cache[cache_key]["data"]

    # 3. 产生新的模拟走势
    days_map = {"7d": 7, "30d": 30, "90d": 90, "1y": 365}
    days = days_map.get(range_str, 30)
    
    if symbol_type.lower() == "dxy":
        base_val = 103.5
        vol_min, vol_max = -0.3, 0.35
    else:
        base_val = 4.25
        vol_min, vol_max = -0.05, 0.06
        
    points = []
    now = datetime.now()
    current_val = base_val
    for i in range(days):
        date_obj = now - timedelta(days=i)
        change = round(random.uniform(vol_min, vol_max), 2 if symbol_type.lower() == "dxy" else 4)
        
        if range_str == "1y":
            date_str = date_obj.strftime("%y-%m-%d")
        else:
            date_str = date_obj.strftime("%m-%d")
            
        points.insert(0, {
            "date": date_str,
            "value": round(current_val, 2 if symbol_type.lower() == "dxy" else 4),
            "full_date": date_obj.strftime("%Y-%m-%d")
        })
        
        current_val -= change
        if current_val <= 0:
            current_val = 1.0
        
    # 模拟数据也保存到缓存中，使得之后刷新完全稳定不变
    _history_cache[cache_key] = {
        "last_updated": current_time,
        "data": points
    }
    return points

def calculate_correlation(x, y):
    """
    计算两组等长数据序列 x 和 y 的皮尔逊相关系数 r，并做显著性检验。
    返回 dict:
      r          相关系数 [-1, 1]
      n          样本量
      t_stat     t 统计量（r*sqrt((n-2)/(1-r^2))）
      p_value    双侧 p 值（基于 t 分布近似，df=n-2）
      significant 是否在 5% 水平显著（p < 0.05）
    小样本（n<30）时 r 的显著性很弱，须结合 n 与 p 值判断，不能只看 |r| 大小。
    """
    n = len(x)
    if n <= 2:
        return {"r": 0.0, "n": n, "t_stat": 0.0, "p_value": 1.0, "significant": False}
        
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    
    num = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    den_x = sum((x[i] - mean_x) ** 2 for i in range(n))
    den_y = sum((y[i] - mean_y) ** 2 for i in range(n))
    
    if den_x == 0 or den_y == 0:
        return {"r": 0.0, "n": n, "t_stat": 0.0, "p_value": 1.0, "significant": False}
        
    r = num / ((den_x * den_y) ** 0.5)
    r = max(-1.0, min(1.0, r))
    
    # t 检验：t = r * sqrt((n-2)/(1-r^2))，df = n-2
    if abs(r) >= 1.0:
        t_stat = float('inf')
        p_value = 0.0
    else:
        t_stat = r * math.sqrt((n - 2) / (1.0 - r * r))
        # 双侧 p 值：用正态近似（erf）估计 t 分布尾部概率，df 较大时足够准确
        z = abs(t_stat)
        p_tail = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))
        p_tail = min(max(p_tail, 1e-15), 1.0 - 1e-15)  # 防下溢/上溢
        p_value = 2.0 * (1.0 - p_tail)
    
    return {
        "r": round(r, 4),
        "n": n,
        "t_stat": round(t_stat, 4) if t_stat != float('inf') else None,
        "p_value": round(p_value, 6),
        "significant": p_value < 0.05
    }

def calculate_indicators(history_points):
    """
    计算技术指标 (MA5, MA20, 布林带, MACD) 并检测金叉/死叉买卖信号。
    """
    prices = [pt["price"] for pt in history_points]
    n = len(prices)
    
    # 1. 计算 MA5、MA20、MA60 和 MA120 均线
    ma5 = []
    for i in range(n):
        if i < 4:
            ma5.append(None)
        else:
            ma5.append(round(sum(prices[i-4:i+1]) / 5.0, 2))
            
    ma20 = []
    for i in range(n):
        if i < 19:
            ma20.append(None)
        else:
            ma20.append(round(sum(prices[i-19:i+1]) / 20.0, 2))

    ma60 = []
    for i in range(n):
        if i < 59:
            ma60.append(None)
        else:
            ma60.append(round(sum(prices[i-59:i+1]) / 60.0, 2))

    ma120 = []
    for i in range(n):
        if i < 119:
            ma120.append(None)
        else:
            ma120.append(round(sum(prices[i-119:i+1]) / 120.0, 2))
            
    # 2. 计算布林带 (Bollinger Bands)
    bollinger_upper = []
    bollinger_lower = []
    for i in range(n):
        if i < 19:
            bollinger_upper.append(None)
            bollinger_lower.append(None)
        else:
            mid = ma20[i]
            window = prices[i-19:i+1]
            mean_val = sum(window) / 20.0
            variance = sum((x - mean_val) ** 2 for x in window) / 20.0
            std_dev = variance ** 0.5
            bollinger_upper.append(round(mid + 2.0 * std_dev, 2))
            bollinger_lower.append(round(mid - 2.0 * std_dev, 2))
            
    # 3. 计算 MACD (DIF, DEA, Hist)
    ema12 = []
    ema26 = []
    dif = []
    
    alpha12 = 2.0 / 13.0
    alpha26 = 2.0 / 27.0
    
    curr_ema12 = 0.0
    curr_ema26 = 0.0
    for i in range(n):
        p = prices[i]
        if i == 0:
            curr_ema12 = p
            curr_ema26 = p
        else:
            curr_ema12 = p * alpha12 + curr_ema12 * (1 - alpha12)
            curr_ema26 = p * alpha26 + curr_ema26 * (1 - alpha26)
        ema12.append(curr_ema12)
        ema26.append(curr_ema26)
        dif.append(curr_ema12 - curr_ema26)
        
    alpha9 = 2.0 / 10.0
    dea = []
    curr_dea = 0.0
    for i in range(n):
        d = dif[i]
        if i == 0:
            curr_dea = d
        else:
            curr_dea = d * alpha9 + curr_dea * (1 - alpha9)
        dea.append(curr_dea)
        
    macd_hist = []
    for i in range(n):
        macd_hist.append(2.0 * (dif[i] - dea[i]))
        
    dif = [round(x, 4) for x in dif]
    dea = [round(x, 4) for x in dea]
    macd_hist = [round(x, 4) for x in macd_hist]
    
    # 4. 计算已有技术指标 (RSI, KDJ, CCI) 用于信号生成
    rsi = calculate_rsi(prices)
    # 真实最高/最低价（模拟数据已回填近似值，不存在 None）
    highs = [pt.get("high") or pt["price"] for pt in history_points]
    lows = [pt.get("low") or pt["price"] for pt in history_points]
    k_vals, d_vals, j_vals = calculate_kdj(prices, highs, lows)
    cci = calculate_cci(prices, highs, lows)

    # 5. 计算新增技术指标 (W&R, DMI, BIAS, OBV, ROC, CR)
    wr = calculate_wr(prices, highs, lows)
    plus_di, minus_di, adx = calculate_dmi(prices, highs, lows)
    bias6, bias12, bias24 = calculate_bias(prices)
    obv = calculate_obv(prices, [pt.get("volume", 0) or 0 for pt in history_points])
    roc = calculate_roc(prices)
    cr = calculate_cr(prices)

    # 6. 获取成交量列表用于波动突破过滤器
    volumes = [pt.get("volume", 0) or 0 for pt in history_points]
    
    signals = []
    for i in range(1, n):
        # 计算当天价格变动百分比
        pct_change = 0.0
        if prices[i-1] > 0:
            pct_change = (prices[i] - prices[i-1]) / prices[i-1]
            
        # --- 方案 1: 趋势顺势模型 (Trend Follow) ---
        if ma5[i-1] is not None and ma20[i-1] is not None and ma5[i] is not None and ma20[i] is not None:
            prev_diff = ma5[i-1] - ma20[i-1]
            curr_diff = ma5[i] - ma20[i]
            
            # 多头趋势顺势 (B)
            if prev_diff <= 0 and curr_diff > 0:
                # RSI 处于健康的非超买区间，且当天涨幅 >= 1.0% (代表强力金叉突破)
                if rsi[i] is not None and rsi[i] < 60 and pct_change >= 0.010:
                    signals.append({
                        "date": history_points[i]["date"],
                        "index": i,
                        "type": "buy",
                        "scheme": "trend",
                        "label": "顺",
                        "price": prices[i],
                        "desc": f"【均线顺势金叉】当天金价拉升上涨 {pct_change*100:.2f}%，引发 MA5 向上金叉突破 MA20。RSI 为 {rsi[i]} 处于蓄势区，且 MACD 柱状呈红柱发散，确认强劲上涨趋势形成。"
                    })
            # 空头趋势顺势 (S)
            elif prev_diff >= 0 and curr_diff < 0:
                # RSI 处于健康的非超卖区间，且当天跌幅 <= -1.0% (代表强力死叉向下)
                if rsi[i] is not None and rsi[i] > 40 and pct_change <= -0.010:
                    signals.append({
                        "date": history_points[i]["date"],
                        "index": i,
                        "type": "sell",
                        "scheme": "trend",
                        "label": "顺",
                        "price": prices[i],
                        "desc": f"【均线顺势死叉】当天金价重挫下跌 {abs(pct_change)*100:.2f}%，引发 MA5 向下跌破 MA20。RSI 为 {rsi[i]} 尚有下行空间，且 MACD 柱状呈绿柱发散，确认下行趋势确立。"
                    })

        # --- 方案 2: 波动突破模型 (Volatility Breakout) ---
        if bollinger_upper[i-1] is not None and bollinger_upper[i] is not None and bollinger_lower[i-1] is not None and bollinger_lower[i] is not None:
            # 10 日均量过滤器
            avg_vol = 0
            if i >= 9:
                avg_vol = sum(volumes[i-9:i+1]) / 10.0
            
            # 突破上轨 (B) - 极罕见大暴动事件 (涨幅 >= 1.5%，且大放量)
            if prices[i] > bollinger_upper[i] and pct_change >= 0.015:
                # 放量或预测阶段
                if avg_vol == 0 or volumes[i] > avg_vol * 1.4:
                    signals.append({
                        "date": history_points[i]["date"],
                        "index": i,
                        "type": "buy",
                        "scheme": "breakout",
                        "label": "突",
                        "price": prices[i],
                        "desc": f"【波动异常大涨】当天金价在宏观利好刺激下大涨 {pct_change*100:.2f}% 强力穿透布林带上轨。成交量放大至 {volumes[i]} 手（达 10日均量 {avg_vol:.0f} 手的 {volumes[i]/avg_vol if avg_vol > 0 else 1.5:.1f} 倍），多头暴动突破确认。"
                    })
            # 跌破下轨 (S) - 极罕见暴跌破位 (跌幅 <= -1.5%，且大放量)
            elif prices[i] < bollinger_lower[i] and pct_change <= -0.015:
                # 放量或预测阶段
                if avg_vol == 0 or volumes[i] > avg_vol * 1.4:
                    signals.append({
                        "date": history_points[i]["date"],
                        "index": i,
                        "type": "sell",
                        "scheme": "breakout",
                        "label": "突",
                        "price": prices[i],
                        "desc": f"【波动异常暴跌】当天金价重挫下跌 {abs(pct_change)*100:.2f}% 跌破布林带下轨。成交量放大至 {volumes[i]} 手，属于黑天鹅抛售破位，空头恐慌盘涌出信号。"
                    })

        # --- 方案 3: 均值回归模型 (Mean Reversion) ---
        if bollinger_upper[i] is not None and bollinger_lower[i] is not None and rsi[i] is not None:
            # 价格处于下轨附近或以下，RSI 处于极度超卖区（<25），KDJ 金叉，代表空头力量衰竭，适合左侧抄底买回
            if prices[i] <= bollinger_lower[i] * 1.01 and rsi[i] < 25:
                if j_vals[i-1] is not None and k_vals[i-1] is not None and d_vals[i-1] is not None:
                    # KDJ 金叉 (J 穿过 D)
                    if j_vals[i] > d_vals[i] and j_vals[i-1] <= d_vals[i-1]:
                        signals.append({
                            "date": history_points[i]["date"],
                            "index": i,
                            "type": "buy",
                            "scheme": "reversion",
                            "label": "回",
                            "price": prices[i],
                            "desc": f"【均值超跌回归】金价在下轨盘整筑底，RSI 指标达到极度超卖极值 {rsi[i]}。KDJ 指标在超卖区低位形成金叉，表明空头动能彻底衰竭，有强烈的向上回归反弹趋势。"
                        })
            # 价格处于上轨附近或以上，RSI 处于极度超买区（>75），KDJ 死叉，代表多头买盘耗尽，适合左侧止盈做空
            elif prices[i] >= bollinger_upper[i] * 0.99 and rsi[i] > 75:
                if j_vals[i-1] is not None and k_vals[i-1] is not None and d_vals[i-1] is not None:
                    # KDJ 死叉
                    if j_vals[i] < d_vals[i] and j_vals[i-1] >= d_vals[i-1]:
                        signals.append({
                            "date": history_points[i]["date"],
                            "index": i,
                            "type": "sell",
                            "scheme": "reversion",
                            "label": "回",
                            "price": prices[i],
                            "desc": f"【均值超买回归】金价冲高至上轨外侧，RSI 指标触及极度超买极值 {rsi[i]}。KDJ 指标在高位形成死叉，表明买方后续力量衰弱，回调均值下限需求极为强烈。"
                        })

    enriched_points = []
    for i in range(n):
        enriched_points.append({
            "date": history_points[i]["date"],
            "price": prices[i],
            "ma5": ma5[i],
            "ma20": ma20[i],
            "ma60": ma60[i],
            "ma120": ma120[i],
            "volume": volumes[i],
            "bollinger_upper": bollinger_upper[i],
            "bollinger_lower": bollinger_lower[i],
            "macd_dif": dif[i],
            "macd_dea": dea[i],
            "macd_hist": macd_hist[i],
            "rsi": rsi[i],
            "kdj_k": k_vals[i],
            "kdj_d": d_vals[i],
            "kdj_j": j_vals[i],
            "cci": cci[i],
            "wr": wr[i],
            "dmi_plus_di": plus_di[i],
            "dmi_minus_di": minus_di[i],
            "dmi_adx": adx[i],
            "bias6": bias6[i],
            "bias12": bias12[i],
            "bias24": bias24[i],
            "obv": obv[i] if i < len(obv) else 0.0,
            "roc": roc[i],
            "cr": cr[i]
        })
        
    return enriched_points, signals

def predict_next_7days(prices):
    """
    使用双重指数平滑（Holt's Linear Trend）模型预测未来 7 天的价格走势。
    返回:
      预测均值列表 predictions
      置信区间上轨 upper_bounds
      置信区间下轨 lower_bounds
    """
    n = len(prices)
    if n < 5:
        last_price = prices[-1] if n > 0 else 870.0
        return [last_price] * 7, [last_price * 1.02] * 7, [last_price * 0.98] * 7
        
    alpha = 0.5
    beta = 0.2
    
    L = [0.0] * n
    T = [0.0] * n
    
    L[4] = sum(prices[:5]) / 5.0
    T[4] = (prices[4] - prices[0]) / 4.0
    
    residuals = []
    for t in range(5, n):
        pred_t = L[t-1] + T[t-1]
        residuals.append(prices[t] - pred_t)
        
        L[t] = alpha * prices[t] + (1 - alpha) * (L[t-1] + T[t-1])
        T[t] = beta * (L[t] - L[t-1]) + (1 - beta) * T[t-1]
        
    if residuals:
        std_err = (sum(r**2 for r in residuals) / len(residuals)) ** 0.5
    else:
        std_err = 2.0
        
    predictions = []
    upper_bounds = []
    lower_bounds = []
    
    z_score = 1.282
    last_L = L[-1]
    last_T = T[-1]
    damp = 0.9
    
    for h in range(1, 8):
        pred_val = last_L + sum(damp ** i for i in range(1, h + 1)) * last_T
        se = std_err * (h ** 0.5)
        
        predictions.append(round(pred_val, 2))
        upper_bounds.append(round(pred_val + z_score * se, 2))
        lower_bounds.append(round(pred_val - z_score * se, 2))
        
    return predictions, upper_bounds, lower_bounds

def calculate_rsi(prices, period=14):
    """
    计算 RSI 相对强弱指标 (Wilder's smoothing)
    """
    n = len(prices)
    rsi_values = [None] * n
    if n <= period:
        return rsi_values
        
    avg_gain = 0.0
    avg_loss = 0.0
    
    # 初始化第一个 period 周期
    for i in range(1, period + 1):
        diff = prices[i] - prices[i-1]
        if diff > 0:
            avg_gain += diff
        else:
            avg_loss += abs(diff)
            
    avg_gain /= period
    avg_loss /= period
    
    if avg_gain + avg_loss > 0:
        rsi_values[period] = round(100.0 * avg_gain / (avg_gain + avg_loss), 2)
    else:
        rsi_values[period] = 50.0
        
    # 递推计算后面的 RSI
    for i in range(period + 1, n):
        diff = prices[i] - prices[i-1]
        gain = diff if diff > 0 else 0.0
        loss = abs(diff) if diff < 0 else 0.0
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_gain + avg_loss > 0:
            rsi_values[i] = round(100.0 * avg_gain / (avg_gain + avg_loss), 2)
        else:
            rsi_values[i] = 50.0
            
    return rsi_values

def calculate_kdj(prices, highs=None, lows=None, period=9, m1=3, m2=3):
    """
    计算 KDJ 指标。
    传入真实最高价/最低价序列（highs/lows）时按标准 KDJ 计算；
    未传入（旧模拟数据兼容）时退化为以收盘价近似极值。
    """
    n = len(prices)
    k_values = [None] * n
    d_values = [None] * n
    j_values = [None] * n
    
    if n < period:
        return k_values, d_values, j_values
        
    # 真实高低价缺失时用收盘价近似（保持向后兼容）
    if highs is None or lows is None or len(highs) != n or len(lows) != n:
        highs = prices
        lows = prices
        
    # 初始化第一个常数值
    curr_k = 50.0
    curr_d = 50.0
    
    for i in range(n):
        if i < period - 1:
            continue
            
        window_h = highs[i - period + 1 : i + 1]
        window_l = lows[i - period + 1 : i + 1]
        low_val = min(window_l)
        high_val = max(window_h)
        
        if high_val == low_val:
            rsv = 50.0
        else:
            rsv = 100.0 * (prices[i] - low_val) / (high_val - low_val)
            
        curr_k = (1.0 / m1) * rsv + ((m1 - 1.0) / m1) * curr_k
        curr_d = (1.0 / m2) * curr_k + ((m2 - 1.0) / m2) * curr_d
        curr_j = 3.0 * curr_k - 2.0 * curr_d
        
        k_values[i] = round(curr_k, 2)
        d_values[i] = round(curr_d, 2)
        j_values[i] = round(curr_j, 2)
        
    return k_values, d_values, j_values

def calculate_cci(prices, highs=None, lows=None, period=14):
    """
    计算 CCI 顺势通道指标（标准口径：典型价 TP=(H+L+C)/3）。
    未传入高低价时退化为以收盘价近似（兼容旧模拟数据）。
    """
    n = len(prices)
    cci_values = [None] * n
    if n < period:
        return cci_values
    
    if highs is None or lows is None or len(highs) != n or len(lows) != n:
        highs = prices
        lows = prices

    # 典型价序列
    typical = [(highs[i] + lows[i] + prices[i]) / 3.0 for i in range(n)]
    
    for i in range(period - 1, n):
        window = typical[i - period + 1 : i + 1]
        ma = sum(window) / period
        
        # 计算平均绝对偏差 (MD)
        md = sum(abs(p - ma) for p in window) / period
        
        if md == 0:
            cci_values[i] = 0.0
        else:
            cci_values[i] = round((typical[i] - ma) / (0.015 * md), 2)
            
    return cci_values

def calculate_wr(prices, highs=None, lows=None, period=14):
    """
    计算 W&R 威廉指标 (Williams %R)。
    衡量收盘价在 N 日真实最高最低区间内的相对位置。
    值域 [0, 100]：0-20=超买（价格接近N日最高），80-100=超卖（价格接近N日最低）。
    未传入高低价时退化为以收盘价近似（兼容旧模拟数据）。
    """
    n = len(prices)
    wr = [None] * n
    
    if highs is None or lows is None or len(highs) != n or len(lows) != n:
        highs = prices
        lows = prices
        
    for i in range(period - 1, n):
        highest = max(highs[i - period + 1 : i + 1])
        lowest = min(lows[i - period + 1 : i + 1])
        wr[i] = round((highest - prices[i]) / (highest - lowest) * 100, 2) if highest != lowest else 50.0
    return wr


def calculate_dmi(prices, highs=None, lows=None, period=14):
    """
    计算 DMI 趋向指标 (Directional Movement Index)。
    返回 +DI（多头方向线）、-DI（空头方向线）、ADX（平均趋向指数）。
    ADX>25=强趋势，ADX<20=无趋势/震荡。
    使用真实高低价计算 TR/+DM/-DM（标准 Wilder 口径）；
    未传入高低价时退化为以收盘价近似（兼容旧模拟数据）。
    """
    n = len(prices)
    tr = [0.0] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n

    if highs is None or lows is None or len(highs) != n or len(lows) != n:
        highs = prices
        lows = prices

    for i in range(1, n):
        prev_c = prices[i - 1]
        curr_h = highs[i]
        curr_l = lows[i]
        # 真实波幅 TR = max(H-L, |H-前收|, |L-前收|)
        tr[i] = max(curr_h - curr_l, abs(curr_h - prev_c), abs(curr_l - prev_c))
        # 方向变动：与前一周期真实高低比较
        up_move = max(0, curr_h - highs[i - 1])
        down_move = max(0, lows[i - 1] - curr_l)
        plus_dm[i] = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm[i] = down_move if down_move > up_move and down_move > 0 else 0

    plus_di = [None] * n
    minus_di = [None] * n
    adx = [None] * n

    for i in range(period, n):
        sum_tr = sum(tr[i - period + 1 : i + 1])
        sum_plus = sum(plus_dm[i - period + 1 : i + 1])
        sum_minus = sum(minus_dm[i - period + 1 : i + 1])

        pdi = round(sum_plus / sum_tr * 100, 2) if sum_tr > 0 else 0
        mdi = round(sum_minus / sum_tr * 100, 2) if sum_tr > 0 else 0
        plus_di[i] = pdi
        minus_di[i] = mdi

        dx = abs(pdi - mdi) / (pdi + mdi) * 100 if (pdi + mdi) > 0 else 0
        if i == period:
            adx[i] = round(dx, 2)
        elif i > period and adx[i - 1] is not None:
            adx[i] = round((adx[i - 1] * (period - 1) + dx) / period, 2)

    return plus_di, minus_di, adx


def calculate_bias(prices):
    """
    计算 BIAS 乖离率，衡量价格偏离移动均线的百分比程度。
    返回 BIAS(6)、BIAS(12)、BIAS(24) 三线。
    正值=价格在均线上方（超涨），负值=价格在均线下方（超跌）。
    """
    n = len(prices)
    bias6 = [None] * n
    bias12 = [None] * n
    bias24 = [None] * n

    for i in range(n):
        for period, bias_list in [(6, bias6), (12, bias12), (24, bias24)]:
            if i >= period - 1:
                ma = sum(prices[i - period + 1 : i + 1]) / period
                bias_list[i] = round((prices[i] - ma) / ma * 100, 2)
    return bias6, bias12, bias24


def calculate_obv(prices, volumes):
    """
    计算 OBV 能量潮 (On-Balance Volume)。
    价涨加成交量，价跌减成交量，累计值反映资金流向。
    OBV上升=资金流入支撑涨势，OBV与价格背离=趋势可能反转。
    """
    n = len(prices)
    obv = [0.0] * n
    obv[0] = float(volumes[0])
    for i in range(1, n):
        if prices[i] > prices[i - 1]:
            obv[i] = obv[i - 1] + float(volumes[i])
        elif prices[i] < prices[i - 1]:
            obv[i] = obv[i - 1] - float(volumes[i])
        else:
            obv[i] = obv[i - 1]
    return obv


def calculate_roc(prices, period=12):
    """
    计算 ROC 变动率指标 (Rate of Change)。
    (当日收盘 – N日前收盘) / N日前收盘 × 100。
    正值=价格高于N日前（上升动量），负值=低于N日前（下降动量），
    零轴突破通常预示趋势拐点。
    """
    n = len(prices)
    roc = [None] * n
    for i in range(period, n):
        roc[i] = round((prices[i] - prices[i - period]) / prices[i - period] * 100, 2)
    return roc


def calculate_cr(prices, period=26):
    """
    计算 CR 中间意愿指标。
    通过比较 N 日内上涨动能与下跌动能的比值，判断多空意愿对比。
    CR>200=极度危险（多头过热），CR<60=低估区域。
    """
    n = len(prices)
    # 数据窗口自适应：当数据点不足以计算标准 26 日 CR 时，使用可用数据量
    actual_period = min(period, max(2, n - 1))
    mid = [0.0] * n
    for i in range(1, n):
        mid[i] = (prices[i] + prices[i - 1]) / 2.0

    cr = [None] * n
    for i in range(actual_period, n):
        up_sum = sum(max(0, prices[j] - mid[j - 1]) for j in range(i - actual_period + 1, i + 1))
        down_sum = sum(max(0, mid[j - 1] - prices[j]) for j in range(i - actual_period + 1, i + 1))
        cr[i] = round(up_sum / down_sum * 100, 2) if down_sum > 0 else 0
    return cr


def calculate_consensus_score(history_points):
    """
    基于多个技术指标融合成趋势强弱得分 (-1.0 到 1.0)
    返回:
      consensus_score: 浮点数 [-1.0, 1.0]
      rating: 中文评级字串 (强力做多/建议做多/震荡观望/建议做空/强力做空)
      details: 各项子得分明细字典 (用于前端展示进度条)
      advice: 针对性的决策建议文案
    """
    # 1. 计算全部指标（含真实高低价的 KDJ/DMI/W&R/CCI），直接取用保持口径一致
    enriched_points, signals = calculate_indicators(history_points)
    n = len(enriched_points)
    if n < 20:
        return 0.0, "震荡观望", {}, "数据样本不足，建议观望。"
        
    prices = [pt["price"] for pt in enriched_points]
    
    # 提取最新的数据点
    last_pt = enriched_points[-1]
    last_price = last_pt["price"]
    
    last_ma5 = last_pt["ma5"]
    last_ma20 = last_pt["ma20"]
    last_macd_hist = last_pt["macd_hist"]
    last_boll_upper = last_pt["bollinger_upper"]
    last_boll_lower = last_pt["bollinger_lower"]
    
    last_rsi = last_pt["rsi"]
    last_k = last_pt["kdj_k"]
    last_d = last_pt["kdj_d"]
    last_j = last_pt["kdj_j"]
    last_cci = last_pt["cci"]
    
    # ------------------ 子项得分计算（统一趋势一致性口径，各映射到 -100 到 +100） ------------------
    # 精简为 6 项高信噪比指标：MA/MACD/DMI（趋势，60%）、布林/RSI（位置动量，30%）、OBV（量能确认，10%）
    # 移除 CCI/KDJ/W&R/BIAS/ROC/CR 的权重打分（保留在展示层，避免噪音与逻辑冲突）
    score_ma = None
    score_macd = None
    score_dmi = None
    score_boll = None
    score_rsi = None
    score_obv = None
    
    # 1. 均线 (MA5/20) - 权重 20%
    if last_ma5 is not None and last_ma20 is not None:
        prev_ma5 = enriched_points[-2]["ma5"]
        if last_ma5 > last_ma20:
            score_ma = 100 if (prev_ma5 is not None and last_ma5 > prev_ma5) else 50
        else:
            score_ma = -100 if (prev_ma5 is not None and last_ma5 < prev_ma5) else -50
    
    # 2. MACD - 权重 20%（红柱/绿柱与动能方向一致）
    if last_macd_hist is not None:
        prev_macd_hist = enriched_points[-2]["macd_hist"]
        if last_macd_hist > 0:
            score_macd = 100 if (prev_macd_hist is not None and last_macd_hist > prev_macd_hist) else 50
        else:
            score_macd = -100 if (prev_macd_hist is not None and last_macd_hist < prev_macd_hist) else -50
    
    # 3. DMI - 权重 20%（方向 + ADX 强度过滤：弱趋势降档）
    last_plus_di = last_pt.get("dmi_plus_di")
    last_minus_di = last_pt.get("dmi_minus_di")
    last_adx = last_pt.get("dmi_adx")
    if last_plus_di is not None and last_minus_di is not None and last_adx is not None:
        if last_plus_di > last_minus_di:
            score_dmi = 100 if last_adx >= 25 else 40
        else:
            score_dmi = -100 if last_adx >= 25 else -40
    
    # 4. 布林带位置 - 权重 15%（顺轨计分，不再做边界反向惩罚）
    boll_pos_ratio = None  # 记录原始乖离倍数（用于极端乖离降级判断）
    if last_boll_upper is not None and last_boll_lower is not None:
        mid_boll = (last_boll_upper + last_boll_lower) / 2.0
        width = last_boll_upper - last_boll_lower
        if width > 0:
            pos_ratio = (last_price - mid_boll) / (width / 2.0)
            boll_pos_ratio = pos_ratio
            pos_ratio = max(-1.5, min(1.5, pos_ratio))
            score_boll = int(pos_ratio / 1.5 * 100.0)
    
    # 5. RSI 动量 - 权重 15%（50 中性，20-80 线性映射，极端区封顶；不再反向抄底/摸顶）
    if last_rsi is not None:
        score_rsi = max(-100, min(100, int((last_rsi - 50.0) / 30.0 * 100.0)))
    
    # 6. OBV 量能确认 - 权重 10%（5日变化量 ÷ 5日成交量换算成比例，避免数值过大失真；量价背离时分数减半）
    obv_vals = [pt.get("obv", 0) or 0 for pt in enriched_points[-6:]]
    vol5 = [pt.get("volume", 0) or 0 for pt in enriched_points[-5:]]
    if len(obv_vals) >= 6 and sum(vol5) > 0:
        slope = obv_vals[-1] - obv_vals[0]
        ratio = slope / sum(vol5) * 100.0
        score_obv = max(-100, min(100, int(ratio * 1.5)))
        price_up = last_price >= prices[-6]
        if (price_up and ratio < 0) or (not price_up and ratio > 0):
            score_obv = int(score_obv * 0.5)  # 量价背离：分数减半警示

    # ------------------ 综合加权计算（按可用指标重新分配权重，避免指标缺失时总分失真） ------------------
    items = [
        ("ma", score_ma, 0.20),
        ("macd", score_macd, 0.20),
        ("dmi", score_dmi, 0.20),
        ("boll", score_boll, 0.15),
        ("rsi", score_rsi, 0.15),
        ("obv", score_obv, 0.10),
    ]

    # 额外子评分（仅用于前端展示，不参与加权总分计算）
    last_k = last_pt.get("kdj_k")
    last_d = last_pt.get("kdj_d")
    last_cci = last_pt.get("cci")
    last_wr = last_pt.get("wr")
    last_bias6 = last_pt.get("bias6")
    last_roc = last_pt.get("roc")
    last_cr = last_pt.get("cr")

    score_kdj = None
    if last_k is not None and last_d is not None:
        if last_k > last_d:
            score_kdj = 60 if last_k < 80 else 30  # 金叉但高位降分
        else:
            score_kdj = -60 if last_k > 20 else -30  # 死叉但低位降分

    score_cci = None
    if last_cci is not None:
        score_cci = max(-100, min(100, int(last_cci / 2)))

    score_wr = None
    if last_wr is not None:
        score_wr = max(-100, min(100, int((50 - last_wr) * 2)))

    score_bias = None
    if last_bias6 is not None:
        score_bias = max(-100, min(100, int(last_bias6 * 20)))

    score_roc = None
    if last_roc is not None:
        score_roc = max(-100, min(100, int(last_roc * 20)))

    score_cr = None
    if last_cr is not None:
        if last_cr > 100:
            score_cr = min(100, int((last_cr - 100) * 2))
        else:
            score_cr = max(-100, int((last_cr - 100) * 2))
    total_weight = 0.0
    weighted_score = 0.0
    for _, s, w in items:
        if s is not None:
            weighted_score += s * w
            total_weight += w
    if total_weight <= 0:
        return 0.0, "震荡观望", {}, "指标数据不足，建议观望。"

    weighted_score = weighted_score / total_weight
    consensus_score = round(weighted_score / 100.0, 2)

    # 极端乖离降级：价格偏离布林带中轨超 1.2 倍带宽 且 ADX>45（强趋势末端）
    # 此时趋势信号可靠性下降，历史回测中易出现超卖/超买反弹，故降级为谨慎档并提示谨防反向
    extreme_divergence = False
    if boll_pos_ratio is not None and last_adx is not None and abs(boll_pos_ratio) > 1.2 and last_adx > 45:
        extreme_divergence = True
        consensus_score = round(consensus_score * 0.5, 2)

    if consensus_score >= 0.6:
        rating = "强力做多"
    elif consensus_score >= 0.2:
        rating = "建议做多"
    elif consensus_score > -0.2:
        rating = "震荡观望"
    elif consensus_score > -0.6:
        rating = "建议做空"
    else:
        rating = "强力做空"

    if extreme_divergence:
        rating = f"谨慎·{rating}（极端乖离）"
        
    # 3. 编写动态的决策解读建议
    advice_parts = []
    if consensus_score >= 0.2:
        advice_parts.append("【多头信号占优】")
    elif consensus_score <= -0.2:
        advice_parts.append("【空头信号占优】")
    else:
        advice_parts.append("【多空力量均势】")
        
    trend_desc = "均线多头排列" if (score_ma or 0) > 0 else "均线空头排列"
    macd_desc = "MACD红柱动能增强" if (score_macd or 0) > 50 else "MACD绿柱动能增强" if (score_macd or 0) < -50 else "MACD红绿柱过渡，趋势尚不显著"
    
    advice_parts.append(f"当前市场{trend_desc}，且{macd_desc}。")
    
    if last_rsi is not None:
        if last_rsi > 70:
            advice_parts.append("当前 RSI 处于 >70 强势区，短线注意追高风险（趋势未破坏前不轻易反向）。")
        elif last_rsi < 30:
            advice_parts.append("当前 RSI 处于 <30 弱势区，下跌动能仍存，暂不急于抄底，等待企稳信号。")
            
    if last_j is not None and (last_j > 100 or last_j < 0):
        advice_parts.append("KDJ 指标处于极端区间，注意波动放大。")

    if extreme_divergence:
        advice_parts.append("极端乖离警示：当前价格偏离布林带中轨超过1.2倍带宽，且趋势强度极高（ADX>45），处于强趋势末端，谨防反向波动，建议降低仓位或观望。")

    # 量价背离警示
    if score_obv is not None and score_obv < -20:
        advice_parts.append("量价背离警示：当前价格上涨但 OBV 资金指标未配合，上涨缺乏量能支撑，建议减小开仓规模或等待回调确认。")

    if consensus_score >= 0.6:
        advice_parts.append("建议策略：强劲上涨趋势中，轻仓顺势做多，可设置中轨下方为止损位。")
    elif consensus_score >= 0.2:
        advice_parts.append("建议策略：逢低分批吸纳做多，关注上方布林上轨压力。")
    elif consensus_score <= -0.6:
        advice_parts.append("建议策略：顺势反向做空，多头阻力较强，严格止损。")
    elif consensus_score <= -0.2:
        advice_parts.append("建议策略：逢高分批开空，注意仓位控制，防范快速反弹。")
    else:
        advice_parts.append("建议策略：短线以日内高抛低吸为主，或者离场观望，等待新一轮方向突破。")
        
    advice = "".join(advice_parts)
    
    details = {
        "ma": int(score_ma) if score_ma is not None else 0,
        "macd": int(score_macd) if score_macd is not None else 0,
        "dmi": int(score_dmi) if score_dmi is not None else 0,
        "boll": int(score_boll) if score_boll is not None else 0,
        "rsi": int(score_rsi) if score_rsi is not None else 0,
        "obv": int(score_obv) if score_obv is not None else 0,
        "kdj": int(score_kdj) if score_kdj is not None else 0,
        "cci": int(score_cci) if score_cci is not None else 0,
        "wr": int(score_wr) if score_wr is not None else 0,
        "bias": int(score_bias) if score_bias is not None else 0,
        "roc": int(score_roc) if score_roc is not None else 0,
        "cr": int(score_cr) if score_cr is not None else 0
    }
    
    return consensus_score, rating, details, advice

if __name__ == '__main__':
    print("Testing multi-tier gold price fetcher (EastMoney -> Sina -> Simulated)...")
    price_info = fetch_sge_price(force_refresh=True)
    print("Successfully fetched price data:")
    for k, v in price_info.items():
        print(f"  {k}: {v}")
        
    print("\nTesting fetch_gold_history()...")
    hist = fetch_gold_history()
    print("Successfully fetched 7 days history data:")
    for pt in hist:
        print(f"  Date: {pt['date']}, Price: {pt['price']} CNY/g")
