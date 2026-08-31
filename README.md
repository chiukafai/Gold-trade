# KingBot 黄金模拟交易系统 — 说明书

## 一、项目概述

KingBot 是一套**黄金（Au）模拟交易与策略回测系统**，面向财务/投资决策场景，提供从行情获取、策略回测、自动模拟交易到可视化分析的全流程支持。系统**不涉及任何真实资金**，所有交易均为虚拟模拟，用于策略验证和投资决策辅助。

### 核心功能模块

| 模块 | 说明 |
|------|------|
| 📊 策略回测实验室 | 专业级回测引擎，支持风险指标、摩擦成本、参数敏感性、滚动验证、蒙特卡洛模拟 |
| 🤖 自动模拟交易引擎 | 信号驱动（Signal-Driven），自动按策略产生多空信号并执行模拟成交 |
| 📈 实时行情获取 | 对接新浪财经等数据源，获取黄金实时/历史行情 |
| 🌐 Web 控制台 | 交易面板、回测实验室、历史记录、日志查看、系统设置 |

---

## 二、技术栈

| 类别 | 技术 | 说明 |
|------|------|------|
| 编程语言 | Python 3.10+ | 后端全部使用 Python |
| Web 框架 | Flask 3+ | 轻量级 Web 框架，提供 API 与页面渲染 |
| 数据库 | SQLite 3 | 内置于 Python，无需额外安装（`gold_trader.db`） |
| 前端 | HTML5 + CSS3 + JavaScript | 原生实现，无前端框架依赖 |
| 图表库 | Chart.js 4.4.0 | CDN 引入，用于权益曲线、柱状图、直方图 |
| 图标库 | Lucide Icons | CDN 引入，SVG 图标 |
| 容器化 | Docker + docker-compose | 可选，用于生产部署 |
| WSGI 服务器 | Gunicorn | Linux 生产环境使用 |
| 数据处理 | NumPy + Pandas | 行情数据处理与指标计算 |
| HTTP 请求 | requests | 调用第三方行情 API |

---

## 三、前后端设计说明

### 后端架构

```
                    ┌─────────────┐
                    │   app.py    │  Flask 主入口，路由 + API
                    │  (38KB)     │
                    └──────┬──────┘
                           │
          ┌────────────────┼────────────────┐
          │                │                │
   ┌──────▼──────┐  ┌─────▼──────┐  ┌──────▼──────┐
   │strategy_engine│ │auto_trader │  │ trade_engine│
   │   .py (37KB) │  │  .py(17KB) │  │  .py(17KB)  │
   │  回测引擎    │  │ 自动交易   │  │  交易执行   │
   └──────┬──────┘  └─────┬──────┘  └──────┬──────┘
          │                │                │
   ┌──────▼──────┐  ┌─────▼──────┐  ┌──────▼──────┐
   │gold_simulator│ │ gold_price │  │   db.py     │
   │  .py(8.5KB) │  │ .py(58KB)  │  │  (7.8KB)    │
   │ 模拟数据生成 │  │ 行情获取   │  │  数据库层   │
   └─────────────┘  └────────────┘  └─────────────┘
```

#### 关键模块说明

**`app.py`** — Flask 主应用入口
- 定义所有路由：`/`（首页）、`/trade`（交易）、`/backtest`（回测）、`/history`（历史）、`/settings`（设置）、`/logs`（日志）
- 定义所有 API 端点：
  - `POST /api/backtest` — 基础回测（支持摩擦成本参数）
  - `POST /api/backtest/sensitivity` — 二维参数敏感性扫描（热力图）
  - `POST /api/backtest/walkforward` — 滚动验证（Walk-Forward Analysis）
  - `POST /api/backtest/montecarlo` — 蒙特卡洛模拟
  - `GET /api/gold/price` — 实时金价
  - `GET /api/gold/history` — 历史行情
  - `GET /api/auto/status` — 自动交易状态
  - `POST /api/auto/toggle` — 启停自动交易
- 自动交易引擎在模块级启动（gunicorn 多 worker 通过文件锁保证唯一实例）

**`strategy_engine.py`** — 策略回测引擎（核心模块）
- `Strategy` 类：基于 RSI（相对强弱指标）+ 量能确认的多空策略
- `Params` 数据类：策略参数（RSI 超卖线、超买线、量能确认开关）
- `Simulator` 类：回测模拟器
  - 支持摩擦成本：手续费、滑点（Slippage，按基点 bp 计算）、延期补偿费（Au(T+D) 特有）
  - 滑点模式：固定滑点 / 随机滑点
  - 摩擦成本汇总：总手续费、总滑点损失、总延期费
- `calc_risk_metrics()` — 专业风险指标计算（纯 Python 实现，无第三方依赖）
  - 夏普比率（Sharpe Ratio）
  - 索提诺比率（Sortino Ratio，仅计算下行波动）
  - 卡玛比率（Calmar Ratio，年化收益/最大回撤）
  - VaR(95%)（Value at Risk，历史模拟法）
  - CVaR(95%)（Conditional VaR，尾部平均损失）
  - 年化收益率 / 年化波动率（按 252 交易日年化）
  - 最大回撤持续天数
  - 盈利因子（Profit Factor）
  - 平均持仓天数
- `walk_forward_analysis()` — 滚动验证
  - 将数据切分为多段「训练窗口 + 测试窗口」
  - 固定参数滚动检验时间稳健性
  - 输出每轮收益对比、窗口胜率、收益衰减率、过拟合风险评估
- `param_sweep_2d()` — 二维参数扫描
  - 扫描两个参数的所有组合
  - 返回热力图矩阵数据
  - 计算"参数高原"比例（Parameter Plateau，收益 ≥ 最优值 80% 的格子占比）
  - 稳健性评级 A/B/C/D
- `monte_carlo_simulation()` — 蒙特卡洛模拟
  - Bootstrap（有放回重抽样）或 Shuffle（随机打乱）两种方法
  - 生成大量"可能的历史路径"
  - 收益分布直方图（20 个区间）
  - 模拟胜率、5%/95% 分位、稳健性评级
  - 自动生成中文自然语言解读

**`auto_trader.py`** — 自动模拟交易引擎
- 信号驱动模式：定期检查策略信号，自动执行模拟买卖
- 文件锁（`auto_trader.lock`）保证全局唯一实例
- 小时级决策日志线程

**`trade_engine.py`** — 交易执行引擎
- 模拟下单、持仓管理、盈亏计算
- 支持多空双向交易

**`gold_price.py`** — 行情数据获取
- 对接新浪财经等数据源
- 支持实时金价、历史日线数据
- 内置缓存机制（`price_cache.json`、`history_cache.json`）

**`gold_simulator.py`** — 模拟行情数据
- `SyntheticProvider`：生成模拟 K 线数据（用于演示和测试）
- `CSVProvider`：从 CSV 文件导入真实日线数据
- 支持自定义数据点数量和随机种子

**`db.py`** — 数据库层
- SQLite 数据库操作封装
- 交易记录、持仓记录、系统配置的增删改查

### 前端架构

#### 页面结构（基于 `base.html` 模板继承）

| 页面 | 文件 | 功能 |
|------|------|------|
| 首页 | `index.html` | 实时金价、持仓概览、自动交易状态 |
| 交易面板 | `trade.html` | 手动模拟下单、持仓管理 |
| 回测实验室 | `backtest.html` | 专业级回测（Tab 布局） |
| 历史记录 | `history.html` | 交易历史查询 |
| 日志 | `logs.html` | 系统运行日志 |
| 设置 | `settings.html` | 策略参数配置 |

#### 回测实验室前端（`backtest.html`）— 本次优化重点

采用 **Tab 布局 + Chart.js** 的专业级可视化界面：

- **Tab 1 - 回测概览**：核心收益指标 + 专业风险指标 + 权益曲线 + 摩擦成本摘要 + 成交明细 + 关键信号
- **Tab 2 - 参数敏感性**：二维参数扫描 + 热力图（Canvas 绘制，红→黄→绿渐变）+ 稳健性评级
- **Tab 3 - 滚动验证**：训练期 vs 测试期收益对比柱状图 + 各轮明细表格 + 过拟合风险评级
- **Tab 4 - 蒙特卡洛**：收益分布直方图（盈利绿/亏损红/原始金色标注）+ 模拟胜率 + 中文解读

前端特性：
- 可折叠面板（策略参数、摩擦成本）
- 风险等级颜色标识（A=绿/B=蓝/C=黄/D=红）
- 所有 Tab 共用同一份数据源（先运行回测，再切换 Tab 分析）
- 支持 CSV 文件上传回测

---

## 四、目录结构

```
gold-trade（home)/
├── app.py                          # Flask 主应用入口（路由 + API）
├── auto_trader.py                  # 自动模拟交易引擎
├── trade_engine.py                 # 交易执行引擎
├── strategy_engine.py              # 策略回测引擎（风险指标/摩擦/参数扫描/蒙特卡洛）
├── gold_price.py                   # 行情数据获取（新浪等数据源）
├── gold_simulator.py               # 模拟行情数据生成 / CSV 导入
├── db.py                           # 数据库操作层（SQLite）
├── diagnose.py                     # 系统诊断工具
├── requirements.txt                # Python 依赖清单
├── Dockerfile                      # Docker 镜像构建文件
├── docker-compose.yml              # Docker Compose 编排
├── .gitignore                      # Git 忽略规则
├── task.md                         # 项目任务说明
├── README.md                       # 本说明书
│
├── templates/                      # 前端 HTML 模板
│   ├── base.html                   # 基础模板（导航栏/侧边栏/样式）
│   ├── index.html                  # 首页（实时金价/持仓概览）
│   ├── trade.html                   # 交易面板
│   ├── backtest.html               # 回测实验室（Tab 布局 + Chart.js）
│   ├── history.html                # 历史记录
│   ├── logs.html                   # 系统日志
│   └── settings.html               # 系统设置
│
├── test_curl_cffi.py               # 测试：HTTP 请求库
├── test_k780.py                    # 测试：K780 API 接口
├── test_trade_math.py              # 测试：交易数学计算
│
├── gold_trader.db                  # SQLite 数据库（运行时生成，已 gitignore）
├── auto_trader.lock                # 文件锁（运行时生成，已 gitignore）
├── price_cache.json                # 价格缓存（运行时生成，已 gitignore）
├── history_cache.json              # 历史缓存（运行时生成，已 gitignore）
├── logs/                           # 日志目录（运行时生成，已 gitignore）
└── __pycache__/                    # Python 编译缓存（自动生成，已 gitignore）
```

---

## 五、运行方式

### 方式一：本地运行（开发/测试）

#### 前置条件
- Python 3.10+
- pip 包管理器

#### 步骤

```bash
# 1. 进入项目目录
cd "e:\AI\trae solo\gold-trade（home)"

# 2. 创建虚拟环境（推荐）
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/Mac:
source venv/bin/activate

# 3. 安装依赖
pip install -r requirements.txt

# 4. 启动应用
python app.py

# 5. 浏览器访问
# http://127.0.0.1:5002
```

启动后终端会显示：
```
[AutoTrader] 自动交易引擎已启动（信号驱动，仅模拟盘）
[AutoTrader] 小时决策日志线程已启动
 * Running on http://127.0.0.1:5002
```

### 方式二：Docker 部署（生产环境）

```bash
# 构建并启动
docker-compose up -d

# 访问
# http://localhost:5002
```

### 回测引擎独立测试

```bash
# 测试所有后端功能（风险指标/摩擦/滚动验证/参数扫描/蒙特卡洛）
python -c "from strategy_engine import run_backtest, Params; from gold_simulator import SyntheticProvider; bars = SyntheticProvider.gen(500, seed=7); r = run_backtest(bars); print(r['stats'])"
```

---

## 六、环境配置

### Python 依赖（`requirements.txt`）

| 依赖 | 版本要求 | 用途 |
|------|----------|------|
| flask | >=3 | Web 框架 |
| requests | >=2 | HTTP 请求（行情 API） |
| python-dotenv | - | 环境变量管理 |
| numpy | ==2.2.6 | 数值计算 |
| pandas | ==2.3.3 | 数据处理 |
| gunicorn | - | WSGI 生产服务器 |

### 端口配置

- 默认端口：`5002`（在 `app.py` 中通过 `app.run(port=5002)` 设置）
- Docker 映射：`5002:5002`（在 `docker-compose.yml` 中配置）

### 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `SECRET_KEY` | Flask 会话密钥 | 硬编码（建议改为环境变量） |
| `PORT` | 服务端口 | 5002 |

> ⚠️ **安全建议**：`app.py` 第 11 行 `SECRET_KEY` 当前为硬编码值，建议改为 `os.environ.get('SECRET_KEY', 'fallback')` 并通过 `.env` 文件配置。

---

## 七、第三方依赖

### 外部 API

| 数据源 | 用途 | 调用方式 |
|--------|------|----------|
| 新浪财经 API | 黄金实时/历史行情 | `gold_price.py` 中 `fetch_gold_history()` |
| K780 API | 备用行情数据 | `test_k780.py` 中测试 |

### CDN 资源（前端）

| 资源 | 版本 | 用途 |
|------|------|------|
| Chart.js | 4.4.0 | 图表绘制（权益曲线/柱状图/直方图） |
| Lucide Icons | latest | SVG 图标库 |

> 以上 CDN 资源在 `base.html` 和 `backtest.html` 中通过 `<script>` / `<link>` 标签引入，无需本地安装。

---

## 八、已知限制

1. **策略单一性**：当前仅内置 RSI + 量能确认策略，不支持自定义策略插件
2. **数据源限制**：真实行情模式依赖新浪财经 API，接口不稳定时可能获取失败
3. **并发限制**：开发模式（`app.run()`）不支持高并发，生产环境需用 Gunicorn
4. **滑点模型简化**：滑点为固定值或均匀随机，未建模订单簿深度影响
5. **蒙特卡洛方法**：仅支持 Bootstrap 和 Shuffle 两种重抽样方法，未支持 Block Bootstrap（保留时序结构）
6. **数据库容量**：SQLite 单文件数据库，不适合高并发写入场景
7. **安全性**：Flask `SECRET_KEY` 为硬编码，生产环境需通过环境变量配置
8. **前端兼容性**：使用现代 CSS（Grid/Flexbox），不支持 IE 浏览器
9. **时区**：系统按本地时区运行，未做 UTC 统一处理

---

## 九、交接说明（供其他 AI Agent 接手参考）

### 项目状态

- **当前分支**：`main`
- **远程仓库**：`https://github.com/chiukafai/Gold-trade.git`
- **最新提交**：初始化黄金模拟交易系统（24 文件，11,730 行）

### 核心代码路径

1. **修改回测逻辑** → `strategy_engine.py`
   - `Simulator` 类（第 251 行起）：回测主循环
   - `calc_risk_metrics()`（第 425 行起）：风险指标计算
   - `walk_forward_analysis()`（第 530 行起）：滚动验证
   - `param_sweep_2d()`（第 608 行起）：参数扫描
   - `monte_carlo_simulation()`（第 665 行起）：蒙特卡洛模拟

2. **修改 API 端点** → `app.py`
   - 回测 API：第 660 行起
   - 参数敏感性 API：搜索 `handle_backtest_sensitivity`
   - 滚动验证 API：搜索 `handle_backtest_walkforward`
   - 蒙特卡洛 API：搜索 `handle_backtest_montecarlo`

3. **修改前端界面** → `templates/backtest.html`
   - Tab 布局：搜索 `switchTab`
   - 图表绘制：搜索 `drawEquityChart` / `drawHeatmap` / `mcChart`
   - 摩擦成本面板：搜索 `friction-params`

### 待办事项（按优先级）

| 优先级 | 事项 | 文件 |
|--------|------|------|
| P3 | 黄金专属特性（季节性分析/美元相关性分析） | `strategy_engine.py` |
| P3 | 回测→自动交易参数同步机制 | `app.py` + `auto_trader.py` |
| 安全 | SECRET_KEY 改为环境变量 | `app.py` 第 11 行 |
| 增强 | 添加 Block Bootstrap 蒙特卡洛方法 | `strategy_engine.py` |
| 增强 | 支持自定义策略插件 | `strategy_engine.py` |

### 注意事项

- `gold_trader.db`、`*.json` 缓存文件已加入 `.gitignore`，首次运行会自动生成
- 修改 `strategy_engine.py` 后无需重启服务器即可在回测页面看到效果（每次 API 调用重新导入）
- 修改 `app.py` 或 `auto_trader.py` 需要重启服务器
- 前端 `templates/*.html` 修改后刷新浏览器即可生效（Flask 默认开启模板自动重载）
