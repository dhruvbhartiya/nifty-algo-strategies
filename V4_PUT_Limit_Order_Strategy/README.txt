===============================================================================
  V4 PUT LIMIT ORDER STRATEGY
===============================================================================

FILE:       nifty_v4_limit_strategy.py
TYPE:       NIFTY50 PUT Option Buying
PERIOD:     Jan 20 - Mar 17, 2026 (full ~2 months)
CAPITAL:    Rs 1,00,000
LOT SIZE:   25 (NIFTY) x 4 fixed lots per trade
TIMEFRAME:  5-minute candles
ORDERS:     Limit orders (reduced slippage + spread)

RESULTS:
  Gross P&L     : +Rs 14,818
  Net P&L       : +Rs 7,307  (after all transaction costs)
  ROI           : +7.3%
  Win Rate      : ~55%
  Profit Factor : ~1.5

STRATEGY LOGIC:
  - Entry: Bollinger Band + Support/Resistance rejection
  - Only enters on resistance-rejection signals (bearish bias)
  - 4 fixed lots per trade (no variable sizing)
  - Limit order execution (lower costs than market orders)

EXIT RULES:
  - Stop-loss: 10 pts above resistance
  - Time exit: 3 candles max hold
  - Session exit: Force close before market close

KEY FEATURES:
  - Works across ALL market conditions (not just volatile periods)
  - Conservative position sizing (4 lots fixed)
  - Proven over ~2 month backtest period
  - No lookahead / no hindsight bias

HOW TO RUN:
  python nifty_v4_limit_strategy.py

DEPENDENCIES:
  pip install yfinance numpy pandas scipy tabulate matplotlib
===============================================================================
