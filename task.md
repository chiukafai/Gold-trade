# 黄金模拟交易平台 — Phase 1 任务清单

- [x] 环境与依赖配置
  - [x] 创建 `requirements.txt`
  - [x] 安装 Python 依赖库
- [x] 数据库层实现 (`db.py`)
  - [x] 编写表结构初始化逻辑（支持 WAL 模式、外键约束）
  - [x] 注入初始模拟资金 $100,000$ 元
  - [x] 编写 Flask 请求生命周期的数据库连接获取及关闭工具
- [x] 行情接口实现 (`gold_price.py`)
  - [x] 编写新浪财经 SGE Au(T+D) 行情抓取与解析逻辑
  - [x] 实现内存级别 5 秒的高频缓存机制
  - [x] 添加超时控制（5秒）与 3 次请求重试机制
- [x] 后端基础框架 (`app.py`)
  - [x] 初始化 Flask 服务
  - [x] 绑定数据库请求拦截器（`before_request` / `teardown_appcontext`）
  - [x] 实现 `/api/price` 行情接口
- [x] 前端基础骨架 (`templates/base.html`)
  - [x] 创建 `templates/` 目录
  - [x] 实现 Apple Design System 风格毛玻璃与暗色渐变主题 CSS
  - [x] 编写支持响应式侧边导航的基础 HTML 框架
- [x] 阶段验证
  - [x] 校验 SQLite 数据库是否自动生成且表结构完备
  - [x] 验证行情 API 能否稳定返回 SGE 买一/卖一/最新价 JSON
  - [x] 启动服务并访问，确保基础页面能正常加载并呈呈现系统样式

# 黄金模拟交易平台 — Phase 2 任务清单

- [x] 交易核心引擎开发 (`trade_engine.py`)
  - [x] 编写买开、卖平（多头）业务计算公式，实现开仓手续费资本化
  - [x] 编写卖开、买平（空头）业务计算公式，实现做空开仓手续费扣减
  - [x] 编写账户全局权益（Equity）、浮动盈亏（Floating PnL）及可用资金（Available Cash）计算函数
  - [x] 编写强平（Liquidation）触发判断及清算逻辑
- [x] 核心算法数学验证 (`test_trade_math.py`)
  - [x] 创建自动化测试脚本，验证做多完整平仓循环的资金一致性
  - [x] 验证做空完整平仓循环的资金一致性，防止“账目黑洞”
- [x] 后端 API 对接与事务绑定 (`app.py` 修改)
  - [x] 实现 `/api/trade` 交易提交 API，使用连接上下文绑定 ACID 事务
  - [x] 在行情更新 API 中植入强平检测钩子
- [x] 前端交易终端页面开发 (`templates/trade.html`)
  - [x] 编写多/空交易方向分段控件（Segmented Control）
  - [x] 实现前端克数输入时，通过最新 Bid/Ask 价格动态计算预估保证金和手续费
  - [x] 编写 Apple 风格的毛玻璃二次确认交易弹窗（防误触）
- [x] 双向交易验证测试
  - [x] 验证多头与空头的开平仓操作能正常影响数据库中的 cash 和 positions
  - [x] 测试资金不足开仓被 API 阻断
  - [x] 测试强平阈值触发时被成功托管清算

# 黄金模拟交易平台 — Phase 3 任务清单

- [x] 历史走势获取引擎开发 (`gold_price.py` 修改)
  - [x] 编写 `fetch_gold_history()` 抓取并解析 Yahoo 7 天走势历史
  - [x] 实现历史收盘价转换及国内溢价系数叠加
  - [x] 增加 timedelta 日期生成与随机走势的容灾降级逻辑
- [x] 接口扩展与对接 (`app.py` 修改)
  - [x] 注册 `/api/chart_data` 路由，整合输出日期（X轴）与价格（Y轴）的 JSON 报文
- [x] 仪表盘改版集成 ECharts (`templates/index.html` 修改)
  - [x] 引入 ECharts CDN 并构建自适应 Chart 容器
  - [x] 编写面积折线图样式配置（Apple 蓝色、透明卡片、磨砂 Tooltip）
  - [x] 增加快速下单便捷交互卡片（一键做多/做空 10g/50g 接口调用）
  - [x] 实现与后端 `/api/account` 完全对齐的实时资金与多空持仓展示卡
- [x] 可视化验证
  - [x] 验证 7 天走势折线图正常渲染，且数值符合当前的 87X/88X 元区间
  - [x] 测试首页快捷下单卡片，确认可成功触发并实时回显账户资产变动
