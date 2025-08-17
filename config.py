# Telegram机器人Token
TOKEN = "TOKEN"

# 授权用户ID列表
CHAT_IDS = [2252551511]  # 替换为实际用户ID

# Binance API密钥
BINANCE_API_KEY = "api_key"
BINANCE_API_SECRET = "key"

# K线间隔设置
DEFAULT_INTERVAL = "60m"  # MA/MACD自动交易使用的K线间隔（5m/15m/60m/240m）

# --- 交易设置 ---
DEFAULT_LEVERAGE = 10  # 默认杠杆倍数 (1-125)
DEFAULT_AMOUNT = 100  # 默认下单金额 (USDT)
DEFAULT_TP = 5.0  # 默认止盈百分比
DEFAULT_SL = 2.0  # 默认止损百分比
DEFAULT_TRADE_MODE = "MA"  # 默认交易模式 (MA/MACD/MA-MACD)

# --- 高级设置 ---
MAX_ENTRY_PER_SYMBOL = 3  # 每对交易的最大开仓次数
TRADE_COOLDOWN_MINUTES = 30  # 相同交易对连续交易冷却时间（分钟）

# --- 系统配置 ---
ALLOWED_INTERVALS = ['5m', '15m', '60m', '240m']  # 允许的K线周期
ENABLE_POSITION_TRACKING = True  # 启用持仓跟踪
ENABLE_STOPLOSS = True  # 启用止损功能
ENABLE_TAKEPROFIT = True  # 启用止盈功能
ENABLE_AUTO_TRADE = True  # 启用自动交易

# --- 日志配置 ---
LOG_LEVEL = "INFO"  # DEBUG/INFO/WARNING/ERROR/CRITICAL
SAVE_EVENT_LOGS = True  # 保存事件日志
LOG_TO_FILE = True  # 输出到日志文件
LOG_FILE = "bot.log"  # 日志文件名

# --- 风险控制 ---
MAX_EXPOSURE_PER_ASSET = 0.1  # 单个资产最大仓位比例 (0-1)
MAX_MARGIN_USAGE = 0.5  # 最大保证金使用比例 (0-1)
RISK_FREE_MODE = False  # 风险规避模式（限制高风险操作）
