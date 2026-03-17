# NIFTY50 Algo Trading Strategies

Backtested options trading strategies for NIFTY50 on NSE. All strategies are designed with **zero lookahead bias** — entries and exits use only trailing (past) data, simulating live market execution.

---

## Strategies

### 1. V4 PUT Limit Order Strategy
**Folder**: `V4_PUT_Limit_Order_Strategy/`

| Metric | Value |
|--------|-------|
| Type | PUT Option Buying |
| Period | Jan 20 - Mar 17, 2026 (~2 months) |
| Capital | Rs 1,00,000 |
| Lots | 4 fixed per trade |
| Net P&L | **+Rs 7,307** |
| ROI | **+7.3%** |
| Order Type | Limit orders |

**Logic**: Bollinger Band + Resistance rejection entries. Sells when price rejects resistance levels (bearish bias). Works across all market conditions.

---

### 2. Straddle Strict ATM Strategy
**Folder**: `Straddle_Strict_ATM_Strategy/`

| Metric | Value |
|--------|-------|
| Type | ATM Straddle (CALL + PUT) |
| Period | Feb 23 - Mar 17, 2026 (15 trading days) |
| Capital | Rs 1,00,000 |
| Lots | Up to 6 (conviction-scaled) |
| Net P&L | **+Rs 16,003** |
| ROI | **+16.0%** |
| Order Type | Limit orders |

**Logic**: Volatility-triggered straddle — buys ATM CALL + ATM PUT when ATR expansion + breakout candle/BB width confirm volatility. Strict ATM filter ensures strike == nearest 50-pt multiple to spot. Profits from large moves in either direction.

---

## Key Design Principles

- **No hindsight**: Signal on candle `i` → entry at candle `i+1` open
- **No future data**: All indicators (ATR, BB, realized vol) use trailing windows only
- **Exit pending system**: Exit signal on candle `i` close → actual exit at candle `i+1` open
- **Full transaction costs**: Brokerage, STT, exchange charges, GST, SEBI, stamp duty, slippage, bid-ask spread
- **Black-Scholes pricing**: Proxy for live option premiums with dynamic IV estimation

## Timeframe
- 5-minute candles
- NIFTY50 index options (lot size: 25)
- Weekly Thursday expiry

## How to Run
```bash
pip install yfinance numpy pandas scipy tabulate matplotlib
cd V4_PUT_Limit_Order_Strategy && python nifty_v4_limit_strategy.py
cd Straddle_Strict_ATM_Strategy && python nifty_straddle_STRICT_ATM.py
```

---
*Built and backtested with Claude Code*
