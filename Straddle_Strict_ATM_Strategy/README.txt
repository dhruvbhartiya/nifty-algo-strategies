===============================================================================
  STRADDLE STRICT ATM STRATEGY
===============================================================================

FILE:       nifty_straddle_STRICT_ATM.py
TYPE:       NIFTY50 ATM Straddle (Buy CALL + PUT)
PERIOD:     Feb 23 - Mar 16, 2026 (15 trading days, high volatility)
CAPITAL:    Rs 1,00,000
LOT SIZE:   25 (NIFTY) x 4-6 lots per trade (scales with conviction)
TIMEFRAME:  5-minute candles
ORDERS:     Limit orders (reduced slippage + spread)

RESULTS:
  Trades        : 20 (12 signals skipped — spot too far from ATM)
  Gross P&L     : +Rs 31,097
  Net P&L       : +Rs 16,003  (after all transaction costs)
  ROI           : +16.0%
  Net Win Rate  : 50%
  Profit Factor : 3.14
  Max Drawdown  : -2.3% (Rs -2,348)

STRATEGY LOGIC:
  - Entry: Volatility-triggered (ATR ratio + Breakout candle + BB width)
  - Buy ATM CALL + ATM PUT simultaneously (straddle)
  - STRICT ATM FILTER: Only enters when |spot - strike| <= 15 pts
    This ensures both legs have near-equal delta (~0.50)
  - Signal on candle N -> Entry at candle N+1 open (no lookahead)
  - High-conviction scaling: ATR > 1.5x -> 6 lots (vs base 4)

EXIT RULES (first triggered wins):
  1. Profit Target: Combined premium up 18%
  2. Stop-Loss: Combined premium down 30%
  3. Trailing Stop: Once profit > 8%, trail at 50% of peak (expensive straddles only)
  4. Time Exit: 10 candles (50 min)
  5. Session Exit: Force close by 15:20
  6. Leg Runner: If one leg triples

VOLATILITY DETECTION (all trailing, no future data):
  - ATR Ratio >= 1.2x (fast 10-candle vs slow 40-candle)
  - Breakout Candle >= 1.5x average range  OR  BB Width >= 0.6%
  - Trading windows: 9:20-11:30 AM and 1:30-3:15 PM

KEY FEATURES:
  - ONLY trades in volatile markets (stays flat in calm conditions)
  - Strict ATM filter = higher quality trades, lower drawdown
  - No directional bias — profits from big moves in either direction
  - No lookahead / no hindsight bias (audited and verified)
  - Exit-pending system: exit signals at close, execution at next open

IMPORTANT CAVEATS:
  - This was tested during an EXTREME volatility period (NIFTY fell ~10%)
  - In calm markets this strategy generates ZERO trades
  - Returns are NOT annualizable — they depend on volatility events
  - 16% ROI in 15 days is exceptional and not typical

HOW TO RUN:
  python nifty_straddle_STRICT_ATM.py

DEPENDENCIES:
  pip install yfinance numpy pandas scipy tabulate matplotlib
===============================================================================
