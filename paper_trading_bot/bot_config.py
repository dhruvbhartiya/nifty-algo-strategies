"""
Configuration for NIFTY Paper Trading Bot
All parameters for both strategies + system settings
"""
from datetime import time

# ──────────────────────────────────────────────────────────────────────
# SYSTEM
# ──────────────────────────────────────────────────────────────────────
ENV_PATH = "/home/ec2-user/nifty-algo-strategies/.env"
LOG_DIR = "/home/ec2-user/nifty-algo-strategies/paper_trading_bot/logs"
TRADE_LOG_JSON = "/home/ec2-user/nifty-algo-strategies/paper_trading_bot/trades.json"

# ──────────────────────────────────────────────────────────────────────
# MARKET
# ──────────────────────────────────────────────────────────────────────
NIFTY_STRIKE_GAP = 50
LOT_SIZE = 25
RISK_FREE_RATE = 0.07
MARKET_OPEN = time(9, 15)
MARKET_CLOSE = time(15, 30)
CANDLE_INTERVAL_MIN = 5

# ──────────────────────────────────────────────────────────────────────
# CAPITAL & POSITION SIZING
# ──────────────────────────────────────────────────────────────────────
CAPITAL = 200_000
LOTS_PER_TRADE = 2  # fixed 2 lots per trade

# ──────────────────────────────────────────────────────────────────────
# STRADDLE STRICT ATM STRATEGY
# ──────────────────────────────────────────────────────────────────────
STRADDLE = {
    # Volatility detection
    "ATR_FAST": 10,
    "ATR_SLOW": 40,
    "ATR_RATIO_TRIGGER": 1.2,
    "BREAKOUT_CANDLE_MULT": 1.5,
    "RANGE_LOOKBACK": 20,
    "BB_PERIOD": 20,
    "BB_STD": 2,
    "BB_WIDTH_TRIGGER": 0.006,

    # Strict ATM filter
    "ATM_MAX_DISTANCE": 15,  # |spot - strike| must be <= 15 pts

    # Exit rules
    "PROFIT_TARGET_PCT": 0.18,
    "STOP_LOSS_PCT": 0.30,
    "MAX_HOLD_CANDLES": 10,
    "FORCE_EXIT_TIME": time(15, 20),
    "LAST_ENTRY_TIME": time(15, 10),
    "ONE_LEG_MULT": 3.0,

    # Trailing stop
    "TRAIL_ACTIVATE_PCT": 0.08,
    "TRAIL_STOP_PCT": 0.50,
    "CHEAP_STRADDLE_THRESH": 120,

    # Trading windows
    "MORNING_START": time(9, 20),
    "MORNING_END": time(11, 30),
    "AFTERNOON_START": time(13, 30),
    "AFTERNOON_END": time(15, 15),

    # Cooldown
    "COOLDOWN_CANDLES": 2,

    # High conviction
    "HIGH_CONVICTION_ATR": 1.5,
}

# ──────────────────────────────────────────────────────────────────────
# V4 PUT LIMIT ORDER STRATEGY
# ──────────────────────────────────────────────────────────────────────
V4_PUT = {
    "BB_PERIOD": 20,
    "BB_STD": 2,
    "SR_BIN_SIZE_PCT": 0.001,
    "SR_MIN_TOUCHES": 5,
    "SR_LOOKBACK": 200,
    "STOP_LOSS_ABOVE_RESISTANCE": 10,
    "EXIT_CANDLES": 3,
    "MAX_ENTRY_PREMIUM": 150,

    # Trading windows
    "MORNING_START": time(9, 30),
    "MORNING_END": time(11, 30),
    "AFTERNOON_START": time(13, 30),
    "AFTERNOON_END": time(15, 15),

    "FORCE_EXIT_TIME": time(15, 20),
    "LAST_ENTRY_TIME": time(15, 10),
    "COOLDOWN_CANDLES": 3,
}

# ──────────────────────────────────────────────────────────────────────
# TRANSACTION COSTS (for P&L tracking — paper trades)
# ──────────────────────────────────────────────────────────────────────
BROKERAGE_PER_ORDER = 20
STT_RATE = 0.000625
EXCHANGE_TXN_RATE = 0.000495
GST_RATE = 0.18
SEBI_PER_CRORE = 10
STAMP_DUTY_RATE = 0.00003
SLIPPAGE_PTS = 0.5
SPREAD_PTS = 0.75

# ──────────────────────────────────────────────────────────────────────
# IV ESTIMATION
# ──────────────────────────────────────────────────────────────────────
BASE_IV = 0.15
IV_LOOKBACK = 40
IV_CAP = 0.50
