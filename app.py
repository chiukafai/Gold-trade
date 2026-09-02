from flask import Flask, jsonify, render_template, g, request
import sqlite3
import os
from datetime import datetime
from gold_price import fetch_sge_price
from db import init_db, DB_PATH
import trade_engine


app = Flask(__name__)
app.config['SECRET_KEY'] = 'gold_simulation_secret_key'

db_initialized = False

@app.before_request
def setup():
    global db_initialized
    if not db_initialized:
        # 每次进程启动都执行 init_db（幂等）：
        # 数据库不存在时建表，已存在时自动补充缺失列（迁移新配置字段，如自动交易参数）
        init_db()
        db_initialized = True

def get_db():
    """获取当前请求的数据库连接，保存在 Flask g 对象中"""
    if 'db' not in g:
        g.db = sqlite3.connect(DB_PATH, check_same_thread=False)
        g.db.row_factory = sqlite3.Row
        # 启用 WAL 模式和外键约束
        g.db.execute("PRAGMA journal_mode=WAL;")
        g.db.execute("PRAGMA foreign_keys=ON;")
    return g.db

@app.teardown_appcontext
def close_db(exception):
    """请求结束时自动释放数据库连接"""
    db = g.pop('db', None)
    if db is not None:
        db.close()

@app.route('/')
def index():
    """渲染仪表盘首页"""
    return render_template('index.html', active_page='index')

@app.route('/trade')
def trade_page():
    """渲染模拟交易页面"""
    return render_template('trade.html', active_page='trade')

@app.route('/api/price')
def get_price():
    """获取 Au(T+D) 实时/模拟价格的 API，并在后台顺带触发风险率检查与强平机制"""
    db = get_db()
    try:
        price_info = fetch_sge_price()
        
        # 行情更新时，顺带运行强平风控逻辑
        liquidated_trades = []
        try:
            with db:  # ACID 事务包含强平逻辑
                liquidated_trades = trade_engine.check_and_trigger_liquidation(db, price_info)
        except Exception as liq_err:
            print(f"[Liquidation Hook Error] {liq_err}")

        # 缺陷6修复：每日计收 Au(T+D) 延期补偿费（当天只扣一次，不重复扣费）
        try:
            with db:
                trade_engine.charge_daily_deferred_fee(db, price_info)
        except Exception as fee_err:
            print(f"[Deferred Fee Hook Error] {fee_err}")
            
        return jsonify({
            "status": "success",
            "data": price_info,
            "liquidated": liquidated_trades  # 若发生强平，将通知前端弹窗警告
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/account')
def get_account():
    """获取账户与持仓的动态汇总信息（由交易引擎统一计算）"""
    db = get_db()
    try:
        price_info = fetch_sge_price()
        # 引擎统一计算实时折旧、保证金与可用资金
        summary = trade_engine.get_account_summary(db, price_info)
        return jsonify({
            "status": "success",
            "account": {
                "cash": summary["cash"],
                "initial_capital": summary["initial_capital"],
                "commission_rate": summary["commission_rate"],
                "margin_rate": summary["margin_rate"],
                "equity": summary["equity"],
                "available_cash": summary["available_cash"],
                "total_margin": summary["total_margin"],
                "total_floating_pnl": summary["total_floating_pnl"],
                "risk_ratio": summary["risk_ratio"],
                "total_deferred_fee": summary["total_deferred_fee"],
                "last_deferred_date": summary["last_deferred_date"]
            },
            "positions": [
                summary["long_pos"],
                summary["short_pos"]
            ]
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/trade', methods=['POST'])
def execute_trade_api():
    """提交模拟交易订单 (买开/卖平/卖开/买平)"""
    db = get_db()
    req_data = request.get_json() or {}
    
    action = req_data.get('action')      # 'buy_open', 'sell_close', 'sell_open', 'buy_close'
    grams_val = req_data.get('grams')
    
    if not action or grams_val is None:
        return jsonify({"status": "error", "message": "缺失必要参数 action 或 grams"}), 400
        
    try:
        grams = float(grams_val)
    except ValueError:
        return jsonify({"status": "error", "message": "克数必须为数值类型"}), 400

    try:
        # 缺陷6修复：交易时段检查（Au(T+D) 日盘/夜盘），休市时拒绝下单
        if not trade_engine.is_market_open():
            return jsonify({
                "status": "error",
                "message": "当前为 Au(T+D) 休市时段，无法下单。交易时段：周一至周五 09:00-11:30、13:30-15:30；夜盘 20:00-次日02:30（周五无夜盘，周六日休市）。"
            }), 400

        price_info = fetch_sge_price()
        
        # 开启事务执行核心交易
        with db:
            trade_detail = trade_engine.execute_trade(db, action, grams, price_info)
            
        return jsonify({
            "status": "success",
            "message": "交易执行成功",
            "trade": trade_detail
        })
    except ValueError as val_err:
        # 可用资金不足或持仓不足等业务逻辑错误
        return jsonify({
            "status": "error",
            "message": str(val_err)
        }), 400
    except Exception as e:
        # 数据库或其他底气层崩溃
        return jsonify({
            "status": "error",
            "message": f"交易写入失败: {str(e)}"
        }), 500

@app.route('/api/chart_data')
def get_chart_data():
    """获取指定范围（7d/30d/90d/1y）及周期（1d/1h/15m）黄金价格与指标的 API"""
    try:
        range_str = request.args.get('range', '30d')
        interval = request.args.get('interval', '1d')
        from gold_price import (
            fetch_gold_history, 
            calculate_indicators, 
            calculate_rsi, 
            calculate_kdj, 
            calculate_cci
        )
        history = fetch_gold_history(range_str, interval)
        
        # 计算各种趋势分析指标
        enriched_history, signals = calculate_indicators(history)
        
        labels = [pt["date"] for pt in enriched_history]
        prices = [pt["price"] for pt in enriched_history]
        opens = [pt.get("open", pt["price"]) for pt in enriched_history]
        highs = [pt.get("high", pt["price"]) for pt in enriched_history]
        lows = [pt.get("low", pt["price"]) for pt in enriched_history]
        ma5 = [pt["ma5"] for pt in enriched_history]
        ma20 = [pt["ma20"] for pt in enriched_history]
        ma60 = [pt["ma60"] for pt in enriched_history]
        ma120 = [pt["ma120"] for pt in enriched_history]
        volumes = [pt.get("volume", 0) for pt in enriched_history]
        bollinger_upper = [pt["bollinger_upper"] for pt in enriched_history]
        bollinger_lower = [pt["bollinger_lower"] for pt in enriched_history]
        macd_dif = [pt["macd_dif"] for pt in enriched_history]
        macd_dea = [pt["macd_dea"] for pt in enriched_history]
        macd_hist = [pt["macd_hist"] for pt in enriched_history]
        
        # 提取技术指标 (KDJ, RSI, CCI)
        rsi = [pt["rsi"] for pt in enriched_history]
        k = [pt["kdj_k"] for pt in enriched_history]
        d = [pt["kdj_d"] for pt in enriched_history]
        j = [pt["kdj_j"] for pt in enriched_history]
        cci = [pt["cci"] for pt in enriched_history]

        # 提取新增技术指标 (W&R, DMI, BIAS, OBV, ROC, CR)
        wr = [pt["wr"] for pt in enriched_history]
        dmi_plus_di = [pt["dmi_plus_di"] for pt in enriched_history]
        dmi_minus_di = [pt["dmi_minus_di"] for pt in enriched_history]
        dmi_adx = [pt["dmi_adx"] for pt in enriched_history]
        bias6 = [pt["bias6"] for pt in enriched_history]
        bias12 = [pt["bias12"] for pt in enriched_history]
        bias24 = [pt["bias24"] for pt in enriched_history]
        obv = [pt["obv"] for pt in enriched_history]
        roc = [pt["roc"] for pt in enriched_history]
        cr = [pt["cr"] for pt in enriched_history]

        return jsonify({
            "status": "success",
            "labels": labels,
            "prices": prices,
            "opens": opens,
            "highs": highs,
            "lows": lows,
            "ma5": ma5,
            "ma20": ma20,
            "ma60": ma60,
            "ma120": ma120,
            "volumes": volumes,
            "bollinger_upper": bollinger_upper,
            "bollinger_lower": bollinger_lower,
            "macd_dif": macd_dif,
            "macd_dea": macd_dea,
            "macd_hist": macd_hist,
            "rsi": rsi,
            "kdj_k": k,
            "kdj_d": d,
            "kdj_j": j,
            "cci": cci,
            "wr": wr,
            "dmi_plus_di": dmi_plus_di,
            "dmi_minus_di": dmi_minus_di,
            "dmi_adx": dmi_adx,
            "bias6": bias6,
            "bias12": bias12,
            "bias24": bias24,
            "obv": obv,
            "roc": roc,
            "cr": cr,
            "signals": signals
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/consensus_decision')
def get_consensus_decision():
    """获取多技术指标在特定周期（1d/1h/15m）综合多空决策的 API"""
    try:
        interval = request.args.get('interval', '1d')
        # 根据分析周期的敏感度选择最合适的回溯数据范围，保证有足够样本算指标
        if interval == "15m":
            range_str = "5d"
        elif interval == "1h":
            range_str = "7d"
        else:
            range_str = "30d"
            
        from gold_price import fetch_gold_history, calculate_consensus_score
        history = fetch_gold_history(range_str, interval)
        score, rating, details, advice = calculate_consensus_score(history)
        
        return jsonify({
            "status": "success",
            "score": score,
            "rating": rating,
            "details": details,
            "advice": advice
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/macro_data')
def get_macro_data():
    """获取指定范围黄金价格与宏观经济因子（DXY或TNX）对照及相关系数的 API"""
    try:
        range_str = request.args.get('range', '30d')
        symbol_type = request.args.get('symbol', 'dxy')
        
        from gold_price import fetch_gold_history, fetch_macro_history, calculate_correlation
        
        gold_hist = fetch_gold_history(range_str)
        macro_hist = fetch_macro_history(symbol_type, range_str)
        
        # 用完整日期（%Y-%m-%d）对齐，避免 %m-%d 无年份导致跨年窗口错配
        gold_lookup = {pt.get("full_date") or pt["date"]: pt for pt in gold_hist}
        macro_lookup = {pt.get("full_date") or pt["date"]: pt for pt in macro_hist}
        
        common_dates = sorted(list(set(gold_lookup.keys()) & set(macro_lookup.keys())))
        
        aligned_dates = []
        aligned_gold = []
        aligned_macro = []
        
        for d in common_dates:
            aligned_dates.append(gold_lookup[d]["date"])
            aligned_gold.append(gold_lookup[d]["price"])
            aligned_macro.append(macro_lookup[d]["value"])
            
        corr_result = calculate_correlation(aligned_gold, aligned_macro)
        
        return jsonify({
            "status": "success",
            "labels": aligned_dates,
            "gold_prices": aligned_gold,
            "macro_values": aligned_macro,
            "correlation": corr_result["r"],
            "correlation_n": corr_result["n"],
            "correlation_p": corr_result["p_value"],
            "correlation_t": corr_result["t_stat"],
            "correlation_significant": corr_result["significant"]
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/api/forecast')
def get_price_forecast():
    """获取黄金未来 7 天走势预测的 API"""
    try:
        from gold_price import fetch_gold_history, predict_next_7days, calculate_indicators
        from datetime import timedelta
        # 默认取最近 30 天的数据来训练预测模型
        history = fetch_gold_history("30d")
        prices = [pt["price"] for pt in history]
        
        predictions, upper_bounds, lower_bounds = predict_next_7days(prices)
        
        # 提取最后一天日期，生成后续7天的标签
        last_date_str = history[-1]["date"]
        
        # 自动识别日期格式并递增
        if '-' in last_date_str:
            parts = last_date_str.split('-')
            if len(parts) == 3:  # %y-%m-%d
                last_date = datetime.strptime(last_date_str, "%y-%m-%d")
                out_fmt = "%y-%m-%d"
            else:  # %m-%d
                this_year = datetime.now().year
                last_date = datetime.strptime(f"{this_year}-{last_date_str}", "%Y-%m-%d")
                out_fmt = "%m-%d"
        else:
            last_date = datetime.now()
            out_fmt = "%m-%d"
            
        pred_dates = []
        for h in range(1, 8):
            future_date = last_date + timedelta(days=h)
            pred_dates.append(future_date.strftime(out_fmt))
            
        # 合并历史与预测未来数据，计算未来7天的交易信号
        combined_history = history.copy()
        for d, p in zip(pred_dates, predictions):
            combined_history.append({"date": d, "price": p, "volume": 0})
            
        enriched_res, combined_signals = calculate_indicators(combined_history)
        forecast_signals = [sig for sig in combined_signals if sig["date"] in pred_dates]
        forecast_points = enriched_res[-7:]
            
        return jsonify({
            "status": "success",
            "labels": pred_dates,
            "predictions": predictions,
            "upper_bounds": upper_bounds,
            "lower_bounds": lower_bounds,
            "signals": forecast_signals,
            "forecast_points": forecast_points,
            # 缺陷4修复：明确预测模型性质，避免用户将统计外推当作确定性预测
            "model": "holt_linear_exponential_smoothing",
            "confidence_level": 0.8,
            "is_forecast": True,
            "disclaimer": "以上为基于近30日历史价格的统计外推（Holt双指数平滑），非基本面预测、不构成投资建议；"
                          "黄金受宏观事件驱动，外推误差随天数快速扩大，请以区间而非均值线作参考。"
        })
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# ----------------- 系统设置页面及配置 API -----------------

@app.route('/settings')
def settings_page():
    """渲染系统设置页面"""
    return render_template('settings.html', active_page='settings')

@app.route('/api/config', methods=['GET', 'POST'])
def handle_config():
    """读取或修改数据库系统参数配置的 API"""
    db = get_db()
    if request.method == 'GET':
        try:
            row = db.execute("SELECT initial_capital, commission_rate, margin_rate, domestic_premium FROM accounts WHERE id = 1").fetchone()
            return jsonify({
                "status": "success",
                "config": dict(row)
            })
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        req_data = request.get_json() or {}
        # 支持修改参数
        commission_rate = req_data.get('commission_rate')
        margin_rate = req_data.get('margin_rate')
        domestic_premium = req_data.get('domestic_premium')
        initial_capital = req_data.get('initial_capital')

        if None in (commission_rate, margin_rate, domestic_premium, initial_capital):
            return jsonify({"status": "error", "message": "缺失配置参数"}), 400

        try:
            now_str = datetime.now().isoformat()
            with db:
                db.execute(
                    "UPDATE accounts SET commission_rate = ?, margin_rate = ?, domestic_premium = ?, initial_capital = ?, updated_at = ? WHERE id = 1",
                    (float(commission_rate), float(margin_rate), float(domestic_premium), float(initial_capital), now_str)
                )
            return jsonify({"status": "success", "message": "配置更新成功"})
        except Exception as e:
            return jsonify({"status": "error", "message": f"写入配置失败: {str(e)}"}), 500

@app.route('/api/reset', methods=['POST'])
def reset_account():
    """重置模拟盘：清空所有持仓和交易记录，恢复初始本金现金余额"""
    db = get_db()
    try:
        now_str = datetime.now().isoformat()
        with db:
            # 1. 读取账户初始资金配置
            row = db.execute("SELECT initial_capital FROM accounts WHERE id = 1").fetchone()
            init_cap = row["initial_capital"] if row else 100000.0
            
            # 2. 回滚现金余额
            db.execute("UPDATE accounts SET cash = ?, updated_at = ? WHERE id = 1", (init_cap, now_str))
            
            # 3. 清空持仓表并初始化为零持仓状态
            db.execute("DELETE FROM positions")
            db.execute("INSERT INTO positions (direction, grams, avg_cost, margin, updated_at) VALUES ('long', 0.0, 0.0, 0.0, ?)", (now_str,))
            db.execute("INSERT INTO positions (direction, grams, avg_cost, margin, updated_at) VALUES ('short', 0.0, 0.0, 0.0, ?)", (now_str,))
            
            # 4. 清空交易流水表和挂单表
            db.execute("DELETE FROM trades")
            db.execute("DELETE FROM pending_orders")
            
        return jsonify({"status": "success", "message": "模拟盘已重置，本金已回滚"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"重置账户失败: {str(e)}"}), 500

# ----------------- 交易历史报表 API -----------------

@app.route('/history')
def history_page():
    """渲染交易历史报表页面"""
    return render_template('history.html', active_page='history')

@app.route('/api/history')
def get_trade_history():
    """获取所有交易流水列表及统计指标（已实现总盈亏、交易总数与平仓胜率）"""
    db = get_db()
    try:
        # 1. 查询所有流水明细 (created_at 倒序)
        trades_rows = db.execute("SELECT * FROM trades ORDER BY created_at DESC").fetchall()
        trades = [dict(row) for row in trades_rows]
        
        # 2. 统计指标计算
        total_count = len(trades)
        
        # 已实现总盈亏 = 所有平仓单 realized_pnl 的总和
        pnl_row = db.execute("SELECT SUM(realized_pnl) FROM trades WHERE type IN ('sell_close', 'buy_close')").fetchone()
        total_realized_pnl = round(pnl_row[0] if pnl_row[0] is not None else 0.0, 2)
        
        # 平仓单总数
        close_row = db.execute("SELECT COUNT(*) FROM trades WHERE type IN ('sell_close', 'buy_close')").fetchone()
        close_count = close_row[0]
        
        # 盈利平仓单数 (已实现盈亏 > 0 的平仓单)
        win_row = db.execute("SELECT COUNT(*) FROM trades WHERE type IN ('sell_close', 'buy_close') AND realized_pnl > 0").fetchone()
        win_count = win_row[0]
        
        # 胜率计算
        win_rate = round((win_count / close_count * 100), 2) if close_count > 0 else 0.0
        
        return jsonify({
            "status": "success",
            "trades": trades,
            "stats": {
                "total_trades": total_count,
                "total_realized_pnl": total_realized_pnl,
                "close_count": close_count,
                "win_count": win_count,
                "win_rate": win_rate
            }
        })
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/auto_trade', methods=['GET', 'POST'])
def handle_auto_trade():
    """读取或修改自动交易配置（开关、单笔克数、轮询间隔、开仓阈值）"""
    db = get_db()
    if request.method == 'GET':
        try:
            row = db.execute(
                "SELECT auto_trade_enabled, auto_trade_grams, auto_trade_interval, auto_trade_threshold, strategy_mode "
                "FROM accounts WHERE id = 1"
            ).fetchone()
            return jsonify({
                "status": "success",
                "config": {
                    "auto_trade_enabled": bool(row["auto_trade_enabled"]),
                    "auto_trade_grams": row["auto_trade_grams"],
                    "auto_trade_interval": row["auto_trade_interval"],
                    "auto_trade_threshold": row["auto_trade_threshold"],
                    "strategy_mode": row["strategy_mode"] or "consensus"
                }
            })
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500
    else:
        req_data = request.get_json() or {}
        try:
            current = db.execute(
                "SELECT auto_trade_enabled, auto_trade_grams, auto_trade_interval, auto_trade_threshold, strategy_mode "
                "FROM accounts WHERE id = 1"
            ).fetchone()
            enabled = int(bool(req_data.get('auto_trade_enabled', current["auto_trade_enabled"])))
            grams = float(req_data.get('auto_trade_grams', current["auto_trade_grams"]))
            interval = int(req_data.get('auto_trade_interval', current["auto_trade_interval"]))
            threshold = float(req_data.get('auto_trade_threshold', current["auto_trade_threshold"]))
            mode = req_data.get('strategy_mode', current["strategy_mode"] or "consensus")
            if mode not in ("consensus", "resonance"):
                return jsonify({"status": "error", "message": "策略模式须为 consensus 或 resonance"}), 400
            # 参数合理性校验
            if grams <= 0 or grams > 1000:
                return jsonify({"status": "error", "message": "单笔克数须在 0~1000 之间"}), 400
            if interval < 10:
                return jsonify({"status": "error", "message": "轮询间隔不能小于 10 秒"}), 400
            if not (0.1 <= threshold <= 0.9):
                return jsonify({"status": "error", "message": "开仓阈值须在 0.1~0.9 之间"}), 400
            now_str = datetime.now().isoformat()
            with db:
                db.execute(
                    "UPDATE accounts SET auto_trade_enabled = ?, auto_trade_grams = ?, "
                    "auto_trade_interval = ?, auto_trade_threshold = ?, strategy_mode = ?, updated_at = ? WHERE id = 1",
                    (enabled, grams, interval, threshold, mode, now_str)
                )
            return jsonify({
                "status": "success",
                "message": ("自动交易已开启" if enabled else "自动交易已关闭"),
                "config": {
                    "auto_trade_enabled": bool(enabled),
                    "auto_trade_grams": grams,
                    "auto_trade_interval": interval,
                    "auto_trade_threshold": threshold,
                    "strategy_mode": mode
                }
            })
        except ValueError:
            return jsonify({"status": "error", "message": "参数格式错误"}), 400
        except Exception as e:
            return jsonify({"status": "error", "message": f"写入配置失败: {str(e)}"}), 500


# ----------------- 策略回测实验室 API -----------------

@app.route('/backtest')
def backtest_page():
    """渲染策略回测实验室页面"""
    return render_template('backtest.html', active_page='backtest')


# ----------------- 自动交易决策日志查看 API -----------------

def query_logs(log_dir, req_file=None):
    """列出小时决策日志文件或读取指定文件内容（纯逻辑，不依赖 Flask，便于测试）。
    日志文件名 auto_trade_decision_YYYYMMDDHH.log：时间戳为结束整点，覆盖其前 1 小时。"""
    prefix = 'auto_trade_decision_'
    files = []
    try:
        for fn in os.listdir(log_dir):
            if fn.startswith(prefix) and fn.endswith('.log'):
                fpath = os.path.join(log_dir, fn)
                try:
                    size = os.path.getsize(fpath)
                except OSError:
                    size = 0
                stamp = fn[len(prefix):-4]
                try:
                    dt = datetime.strptime(stamp, '%Y%m%d%H')
                    label = '%s %02d:00 ~ %02d:00' % (
                        dt.strftime('%Y-%m-%d'), dt.hour - 1, dt.hour)
                except ValueError:
                    label = stamp
                files.append({'name': fn, 'size': size, 'label': label})
    except FileNotFoundError:
        files = []
    files.sort(key=lambda x: x['name'], reverse=True)  # 最新（整点最大）在前

    content = None
    if req_file:
        # 防目录穿越：只允许 logs/ 下的指定文件名
        if not req_file.startswith(prefix) or not req_file.endswith('.log') or '/' in req_file or os.path.sep in req_file:
            return {'status': 'error', 'message': '非法文件名'}, 400
        fpath = os.path.join(log_dir, req_file)
        if os.path.isfile(fpath):
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
            except Exception as e:
                content = '读取失败：%s' % e
    return {
        'status': 'success',
        'files': files,
        'selected': req_file or (files[0]['name'] if files else None),
        'content': content
    }


@app.route('/logs')
def logs_page():
    """渲染自动交易决策日志查看页面"""
    return render_template('logs.html', active_page='logs')


@app.route('/api/logs')
def handle_logs():
    """列出小时决策日志文件，或读取指定文件内容。
    日志落在数据卷 logs/ 目录（auto_trader.LOG_DIR），无需 SSH 进容器即可查看。"""
    try:
        from auto_trader import LOG_DIR
    except Exception:
        LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'logs')
    req_file = request.args.get('file', '').strip()
    result = query_logs(LOG_DIR, req_file)
    if result.get('status') == 'error':
        return jsonify(result), 400
    return jsonify(result)


@app.route('/api/auto_trade/status')
def auto_trade_status():
    """返回自动交易的实时状态信息（ATR、恐惧贪婪指数、活跃消息、最近决策）。
    供前端实时展示自动交易引擎的"思维过程"。"""
    from auto_trader import (DECISION_RECORDS, calc_atr, calc_fear_greed_index,
                             fear_greed_label, _active_news)
    from gold_price import fetch_sge_price, fetch_gold_history, calculate_consensus_score

    try:
        price_info = fetch_sge_price()
    except Exception:
        price_info = {"price": 0, "bid": 0, "ask": 0}
    try:
        hist = fetch_gold_history("30d", "1d")
    except Exception:
        hist = []

    atr = calc_atr(hist)
    score, rating, details, _ = calculate_consensus_score(hist)
    rsi_val = hist[-1].get("rsi") if hist else None
    fg = calc_fear_greed_index(hist, score, rsi_val)

    recent = DECISION_RECORDS[-5:] if DECISION_RECORDS else []
    recent_list = [{
        "time": r["ts"].strftime("%H:%M:%S"),
        "mode": r["mode"],
        "score": r["score"],
        "rating": r["rating"],
        "action": r["action"],
        "reason": r["reason"],
        "atr": r.get("atr"),
        "dynamic_grams": r.get("dynamic_grams"),
        "fear_greed": r.get("fear_greed"),
        "fear_greed_label": fear_greed_label(r["fear_greed"]) if r.get("fear_greed") else None,
        "news": r.get("news"),
        "news_dir": r.get("news_dir"),
    } for r in recent]

    news_info = None
    if _active_news:
        news_info = {
            "name": _active_news.get("name"),
            "direction": _active_news.get("direction"),
            "desc": _active_news.get("desc"),
            "score_shift": _active_news.get("score_shift"),
        }

    return jsonify({
        "atr": round(atr, 2),
        "score": score,
        "rating": rating,
        "fear_greed": round(fg, 1),
        "fear_greed_label": fear_greed_label(fg),
        "active_news": news_info,
        "recent_decisions": recent_list,
    })


@app.route('/api/backtest', methods=['POST'])
def handle_backtest():
    """运行策略回测，返回信号/盈亏/权益曲线（JSON）。
    三种数据源：
      - real      : 项目行情管道 fetch_gold_history（与线上自动交易同源）
      - synthetic : 演示用模拟行情数据（非真实行情）
      - csv       : 上传真实日线 CSV（data.csv_text 为文件文本）
    不涉及任何真实资金，纯策略验证。"""
    db = get_db()
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'synthetic')
    try:
        from strategy_engine import Params, run_backtest, bars_from_history_points
        params = Params()
        if 'rsi_oversold' in data:
            params.rsi_oversold = float(data['rsi_oversold'])
        if 'rsi_overbought' in data:
            params.rsi_overbought = float(data['rsi_overbought'])
        if 'vol_confirm' in data:
            params.vol_confirm = bool(data['vol_confirm'])

        if mode == 'real':
            from gold_price import fetch_gold_history
            hp = fetch_gold_history('90d', '1d')
            if not hp:
                return jsonify({"status": "error", "message": "真实行情获取为空"}), 500
            bars = bars_from_history_points(hp)
            note = f"真实行情管道(90d)：{len(bars)} 根（与线上自动交易同源）"
        elif mode == 'synthetic':
            from gold_simulator import SyntheticProvider
            points = int(data.get('points', 600))
            seed = int(data.get('seed', 7))
            bars = SyntheticProvider.gen(points, seed)
            note = f"模拟行情数据（演示用，非真实行情）：{points} 根"
        elif mode == 'csv':
            csv_text = data.get('csv_text', '')
            if not csv_text.strip():
                return jsonify({"status": "error", "message": "请提供 CSV 文本"}), 400
            from gold_simulator import CSVProvider
            import tempfile, os
            tf = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8')
            tf.write(csv_text); tf.close()
            try:
                bars = CSVProvider.load(tf.name)
            finally:
                try:
                    os.unlink(tf.name)
                except Exception:
                    pass
            if not bars:
                return jsonify({"status": "error", "message": "CSV 解析失败，需含 date,close 列"}), 400
            note = f"上传 CSV 真实日线：{len(bars)} 根"
        else:
            return jsonify({"status": "error", "message": "mode 须为 real/synthetic/csv"}), 400

        result = run_backtest(bars, params, friction=data.get('friction'))
        result['note'] = note
        result['mode'] = mode
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/backtest/sensitivity', methods=['POST'])
def handle_backtest_sensitivity():
    """参数敏感性分析：二维扫描返回热力图矩阵 + 稳健性评分。"""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'synthetic')
    try:
        from strategy_engine import Params, param_sweep_2d, bars_from_history_points
        # 构建基础参数
        params = Params()
        if 'rsi_oversold' in data:
            params.rsi_oversold = float(data['rsi_oversold'])
        if 'rsi_overbought' in data:
            params.rsi_overbought = float(data['rsi_overbought'])
        if 'vol_confirm' in data:
            params.vol_confirm = bool(data['vol_confirm'])

        param_x = data.get('param_x', 'rsi_oversold')
        param_y = data.get('param_y', 'rsi_overbought')
        range_x = [float(v) for v in data.get('range_x', [20, 25, 30, 35, 40])]
        range_y = [float(v) for v in data.get('range_y', [60, 65, 70, 75, 80])]
        metric = data.get('metric', '总收益率%')
        friction = data.get('friction')

        # 获取行情数据
        if mode == 'real':
            from gold_price import fetch_gold_history
            hp = fetch_gold_history('90d', '1d')
            if not hp:
                return jsonify({"status": "error", "message": "真实行情获取为空"}), 500
            bars = bars_from_history_points(hp)
        elif mode == 'synthetic':
            from gold_simulator import SyntheticProvider
            points = int(data.get('points', 600))
            seed = int(data.get('seed', 7))
            bars = SyntheticProvider.gen(points, seed)
        elif mode == 'csv':
            csv_text = data.get('csv_text', '')
            if not csv_text.strip():
                return jsonify({"status": "error", "message": "请提供 CSV 文本"}), 400
            from gold_simulator import CSVProvider
            import tempfile, os
            tf = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8')
            tf.write(csv_text); tf.close()
            try:
                bars = CSVProvider.load(tf.name)
            finally:
                try:
                    os.unlink(tf.name)
                except Exception:
                    pass
            if not bars:
                return jsonify({"status": "error", "message": "CSV 解析失败"}), 400
        else:
            return jsonify({"status": "error", "message": "mode 须为 real/synthetic/csv"}), 400

        result = param_sweep_2d(bars, params, param_x, range_x, param_y, range_y, metric, friction)
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/backtest/walkforward', methods=['POST'])
def handle_backtest_walkforward():
    """滚动验证（Walk-Forward）：固定参数滚动检验时间稳健性。"""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'synthetic')
    try:
        from strategy_engine import Params, walk_forward_analysis, bars_from_history_points
        params = Params()
        if 'rsi_oversold' in data:
            params.rsi_oversold = float(data['rsi_oversold'])
        if 'rsi_overbought' in data:
            params.rsi_overbought = float(data['rsi_overbought'])
        if 'vol_confirm' in data:
            params.vol_confirm = bool(data['vol_confirm'])

        train_window = int(data.get('train_window', 200))
        test_window = int(data.get('test_window', 50))
        step = int(data.get('step', 50))
        friction = data.get('friction')

        if mode == 'real':
            from gold_price import fetch_gold_history
            hp = fetch_gold_history('90d', '1d')
            if not hp:
                return jsonify({"status": "error", "message": "真实行情获取为空"}), 500
            bars = bars_from_history_points(hp)
        elif mode == 'synthetic':
            from gold_simulator import SyntheticProvider
            points = int(data.get('points', 600))
            seed = int(data.get('seed', 7))
            bars = SyntheticProvider.gen(points, seed)
        elif mode == 'csv':
            csv_text = data.get('csv_text', '')
            if not csv_text.strip():
                return jsonify({"status": "error", "message": "请提供 CSV 文本"}), 400
            from gold_simulator import CSVProvider
            import tempfile, os
            tf = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8')
            tf.write(csv_text); tf.close()
            try:
                bars = CSVProvider.load(tf.name)
            finally:
                try:
                    os.unlink(tf.name)
                except Exception:
                    pass
            if not bars:
                return jsonify({"status": "error", "message": "CSV 解析失败"}), 400
        else:
            return jsonify({"status": "error", "message": "mode 须为 real/synthetic/csv"}), 400

        result = walk_forward_analysis(bars, params, train_window, test_window, step, friction)
        if "error" in result:
            return jsonify({"status": "error", "message": result["error"]}), 400
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/api/backtest/montecarlo', methods=['POST'])
def handle_backtest_montecarlo():
    """蒙特卡洛模拟：检验策略稳健性。"""
    data = request.get_json(silent=True) or {}
    mode = data.get('mode', 'synthetic')
    try:
        from strategy_engine import Params, monte_carlo_simulation, bars_from_history_points
        params = Params()
        if 'rsi_oversold' in data:
            params.rsi_oversold = float(data['rsi_oversold'])
        if 'rsi_overbought' in data:
            params.rsi_overbought = float(data['rsi_overbought'])
        if 'vol_confirm' in data:
            params.vol_confirm = bool(data['vol_confirm'])

        n_simulations = int(data.get('n_simulations', 500))
        method = data.get('method', 'bootstrap')
        friction = data.get('friction')

        if mode == 'real':
            from gold_price import fetch_gold_history
            hp = fetch_gold_history('90d', '1d')
            if not hp:
                return jsonify({"status": "error", "message": "真实行情获取为空"}), 500
            bars = bars_from_history_points(hp)
        elif mode == 'synthetic':
            from gold_simulator import SyntheticProvider
            points = int(data.get('points', 600))
            seed = int(data.get('seed', 7))
            bars = SyntheticProvider.gen(points, seed)
        elif mode == 'csv':
            csv_text = data.get('csv_text', '')
            if not csv_text.strip():
                return jsonify({"status": "error", "message": "请提供 CSV 文本"}), 400
            from gold_simulator import CSVProvider
            import tempfile, os
            tf = tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, encoding='utf-8')
            tf.write(csv_text); tf.close()
            try:
                bars = CSVProvider.load(tf.name)
            finally:
                try:
                    os.unlink(tf.name)
                except Exception:
                    pass
            if not bars:
                return jsonify({"status": "error", "message": "CSV 解析失败"}), 400
        else:
            return jsonify({"status": "error", "message": "mode 须为 real/synthetic/csv"}), 400

        result = monte_carlo_simulation(bars, params, n_simulations, method, friction)
        if "error" in result:
            return jsonify({"status": "error", "message": result["error"]}), 400
        return jsonify({"status": "success", "result": result})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 启动自动交易引擎（模块级：gunicorn 每个 worker 都会执行本段，
# fcntl 文件锁保证全局只有一个自动交易线程运行）
try:
    import auto_trader
    auto_trader.start_auto_trader_if_leader()
except Exception as e:
    print(f"[AutoTrader] 启动失败: {e}")

if __name__ == '__main__':
    init_db()
    # 优先读取环境变量 PORT，若无则默认 5002
    port = int(os.environ.get("PORT", 5002))
    # 绑定 0.0.0.0 以便局域网及飞牛外网穿透访问
    app.run(host='0.0.0.0', port=port, debug=False)
