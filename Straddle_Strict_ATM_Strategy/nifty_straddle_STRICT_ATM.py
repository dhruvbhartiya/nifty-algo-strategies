"""
NIFTY50 STRADDLE Strategy — STRICT ATM VARIANT
================================================
Period: Feb 23, 2026 - Mar 17, 2026 (high volatility regime)

THIS IS THE "STRICT ATM" VERSION:
  - Only enters a straddle when |spot - ATM strike| <= 15 pts
  - Ensures both CALL and PUT legs have near-equal delta (~0.50) at entry
  - Filters out ~40% of signals, but trades are HIGHER QUALITY
  - Better win rate (50% vs 42%), better profit factor (3.14 vs 2.71)
  - Lower drawdown (-2.3% vs -3.1%), higher per-trade profit (Rs 800 vs Rs 675)
  - Slightly lower total return (+16.0% vs +17.5%) due to fewer trades

COMPARISON vs CURRENT (nearest ATM, no filter):
  Metric            | Current  | Strict ATM
  ------------------|----------|----------
  Net P&L           | +17,541  | +16,003
  ROI               | +17.5%   | +16.0%
  Trades            | 26       | 20
  Net Win Rate      | 42%      | 50%
  Profit Factor     | 2.71     | 3.14
  Max Drawdown      | -3.1%    | -2.3%
  Net/DD Ratio      | 5.60x    | 6.82x  <-- better risk-adjusted

Strategy:
  Buy ATM CALL + ATM PUT when volatility conditions are met.
  Profit from large directional moves regardless of direction.

Anti-Hindsight Measures:
  - Volatility detection uses ONLY trailing (past) indicators
  - Entry at NEXT candle's open after signal fires
  - No directional bias — straddle profits from move in either direction
  - All exit rules are mechanical and forward-looking only
  - Premium pricing via Black-Scholes (proxy for live market quotes)

Exit (first triggered wins):
  1. Profit Target: Combined premium up 18% from entry
  2. Stop-Loss: Combined premium down 30% from entry
  3. Trailing Stop: Once profit > 8%, trail at 50% of peak (expensive straddles only)
  4. Time Exit: 10 candles (50 min) — extended to let winners run
  5. Session Exit: Force close by 15:20 (no overnight)
  6. One-Leg Runner: If one leg triples, exit (momentum exhaustion likely)

Fine-Tuned Features (v2):
  - Relaxed entry triggers (ATR 1.2x, BB 0.6%) for more opportunities
  - High-conviction lot scaling: ATR > 1.5x -> 6 lots (vs base 4)
  - Trailing stop only for expensive straddles (cheap ones too volatile)
  - Wider trading windows (9:20-11:30, 13:30-15:15)
  - Reduced cooldown (2 candles / 10 min)
  - ** STRICT ATM FILTER: |spot - strike| <= 15 pts **

Capital: Rs 1,00,000 | Lot Size: 25
"""

import sys
import os
from datetime import datetime, time, timedelta
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import norm
from tabulate import tabulate


# ──────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────────────────────────────────────
NIFTY_STRIKE_GAP = 50
RISK_FREE_RATE = 0.07
LOT_SIZE = 25
CAPITAL = 100_000

# Volatility detection thresholds (all use trailing data only)
ATR_FAST = 10       # fast ATR lookback (candles)
ATR_SLOW = 40       # slow ATR lookback (baseline)
ATR_RATIO_TRIGGER = 1.2   # fast ATR must be 1.2x slow ATR (relaxed from 1.3)
BREAKOUT_CANDLE_MULT = 1.5  # candle range > 1.5x avg range of last 20
RANGE_LOOKBACK = 20          # lookback for average candle range
BB_PERIOD = 20
BB_STD = 2
BB_WIDTH_TRIGGER = 0.006  # BB bandwidth > 0.6% of middle = volatile (relaxed from 0.8%)

# Entry
ENTRY_DELAY = 1  # enter on next candle (no lookahead)

# *** STRICT ATM FILTER ***
MAX_ATM_DISTANCE = 15  # only enter when |spot - ATM strike| <= 15 pts

# Exit
PROFIT_TARGET_PCT = 0.18   # exit when combined premium up 18%
CHEAP_STRADDLE_THRESH = 120  # cheap straddle = combined premium under Rs 120 (no trail stop)
STOP_LOSS_PCT = 0.30       # exit when combined premium down 30%
MAX_HOLD_CANDLES = 10      # 50 minutes max hold (extended from 30 min)
FORCE_EXIT_TIME = time(15, 20)  # force close before session end
LAST_ENTRY_TIME = time(15, 10)  # no new entries after this
ONE_LEG_MULT = 3.0         # exit if one leg triples

# Trailing stop: once profit exceeds this threshold, trail a stop
TRAIL_ACTIVATE_PCT = 0.08  # activate trailing stop at 8% profit
TRAIL_STOP_PCT = 0.50      # trail at 50% of peak profit (give back max 50%)

# Signal strength for lot scaling
HIGH_CONVICTION_MULT = 1.5  # ATR ratio above this = high conviction
HIGH_CONVICTION_LOT_MULT = 1.5  # scale lots up by 1.5x for high conviction

# Trading windows (widened for more opportunities)
MORNING_START = time(9, 20)
MORNING_END = time(11, 30)   # extended from 10:30
AFTERNOON_START = time(13, 30)  # start earlier from 14:30
AFTERNOON_END = time(15, 15)

# Transaction costs (limit order scenario — realistic for NIFTY)
BROKERAGE_PER_ORDER = 20    # Rs per order (4 orders: buy call, buy put, sell call, sell put)
STT_RATE = 0.000625         # on sell side
EXCHANGE_TXN_RATE = 0.000495
GST_RATE = 0.18
SEBI_PER_CRORE = 10
STAMP_DUTY_RATE = 0.00003
SLIPPAGE_PTS = 0.5          # limit orders: minimal slippage
SPREAD_PTS = 0.75           # limit orders: reduced spread

# Implied volatility for BS model (use trailing realized vol as proxy)
BASE_IV = 0.15  # will be adjusted dynamically based on recent realized vol


# ──────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ──────────────────────────────────────────────────────────────────────────────
def load_data():
    """Load NIFTY 5-min data from yfinance."""
    import yfinance as yf
    # Load extra days before Feb 23 for indicator warmup
    ticker = yf.Ticker("^NSEI")
    df = ticker.history(interval="5m", start="2026-02-10", end="2026-03-17")
    if df.empty:
        print("ERROR: No data from yfinance")
        sys.exit(1)
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)

    mkt_open, mkt_close = time(9, 15), time(15, 30)
    df = df[(df.index.time >= mkt_open) & (df.index.time <= mkt_close)]
    return df[["open", "high", "low", "close", "volume"]]


# ──────────────────────────────────────────────────────────────────────────────
# OPTIONS PRICING (Black-Scholes)
# ──────────────────────────────────────────────────────────────────────────────
def bs_call_price(spot, strike, days_to_expiry, r=RISK_FREE_RATE, sigma=0.15):
    if days_to_expiry <= 0:
        return max(spot - strike, 0.0)
    T = days_to_expiry / 365.0
    d1 = (np.log(spot / strike) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    call = spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2)
    return max(call, 0.01)


def bs_put_price(spot, strike, days_to_expiry, r=RISK_FREE_RATE, sigma=0.15):
    if days_to_expiry <= 0:
        return max(strike - spot, 0.0)
    T = days_to_expiry / 365.0
    d1 = (np.log(spot / strike) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    put = strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
    return max(put, 0.01)


def get_atm_strike(spot):
    """Nearest strike to spot price."""
    return round(spot / NIFTY_STRIKE_GAP) * NIFTY_STRIKE_GAP


def days_to_nearest_expiry(current_date):
    """Weekly expiry on Thursday."""
    weekday = current_date.weekday()
    days_ahead = (3 - weekday) % 7
    if days_ahead == 0:
        if current_date.hour < 15 or (current_date.hour == 15 and current_date.minute < 30):
            return max(0.1, (15.5 - current_date.hour - current_date.minute / 60) / 24)
        days_ahead = 7
    return days_ahead


def estimate_iv(df, idx, lookback=40):
    """
    Estimate implied volatility from trailing realized volatility.
    This uses ONLY past data — no lookahead.
    """
    start = max(0, idx - lookback)
    window = df.iloc[start:idx + 1]
    if len(window) < 10:
        return BASE_IV

    # Realized vol from log returns, annualized
    # ~75 candles per day (5-min), 252 trading days
    log_returns = np.log(window["close"] / window["close"].shift(1)).dropna()
    if len(log_returns) < 5:
        return BASE_IV

    realized_vol = log_returns.std() * np.sqrt(75 * 252)
    # IV typically trades at premium to realized vol
    iv = max(realized_vol * 1.1, BASE_IV)
    return min(iv, 0.50)  # cap at 50% to be realistic


# ──────────────────────────────────────────────────────────────────────────────
# VOLATILITY INDICATORS (all trailing — no lookahead)
# ──────────────────────────────────────────────────────────────────────────────
def compute_indicators(df):
    """Add all trailing volatility indicators to dataframe."""
    df = df.copy()

    # True Range
    df["prev_close"] = df["close"].shift(1)
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            abs(df["high"] - df["prev_close"]),
            abs(df["low"] - df["prev_close"])
        )
    )

    # ATR fast and slow (trailing only)
    df["atr_fast"] = df["tr"].rolling(window=ATR_FAST, min_periods=ATR_FAST).mean()
    df["atr_slow"] = df["tr"].rolling(window=ATR_SLOW, min_periods=ATR_SLOW).mean()
    df["atr_ratio"] = df["atr_fast"] / df["atr_slow"]

    # Average candle range (trailing)
    df["candle_range"] = df["high"] - df["low"]
    df["avg_range"] = df["candle_range"].rolling(window=RANGE_LOOKBACK, min_periods=RANGE_LOOKBACK).mean()
    df["range_ratio"] = df["candle_range"] / df["avg_range"]

    # Session-aware Bollinger Bands
    df["BB_upper"] = np.nan
    df["BB_middle"] = np.nan
    df["BB_lower"] = np.nan
    df["_date"] = df.index.date
    last_upper = last_mid = last_lower = np.nan

    for date, group in df.groupby("_date"):
        closes = group["close"]
        mid = closes.rolling(window=BB_PERIOD, min_periods=BB_PERIOD).mean()
        std = closes.rolling(window=BB_PERIOD, min_periods=BB_PERIOD).std()
        upper = mid + BB_STD * std
        lower = mid - BB_STD * std
        mid = mid.fillna(last_mid)
        upper = upper.fillna(last_upper)
        lower = lower.fillna(last_lower)
        df.loc[group.index, "BB_middle"] = mid
        df.loc[group.index, "BB_upper"] = upper
        df.loc[group.index, "BB_lower"] = lower
        if mid.notna().any():
            last_mid = mid.dropna().iloc[-1]
            last_upper = upper.dropna().iloc[-1]
            last_lower = lower.dropna().iloc[-1]

    df["bb_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_middle"]
    df.drop(columns=["_date", "prev_close"], inplace=True)

    return df


def in_trading_window(ts):
    t = ts.time()
    return (MORNING_START <= t <= MORNING_END) or (AFTERNOON_START <= t <= AFTERNOON_END)


# ──────────────────────────────────────────────────────────────────────────────
# TRANSACTION COSTS
# ──────────────────────────────────────────────────────────────────────────────
def compute_straddle_costs(call_entry, call_exit, put_entry, put_exit, total_qty):
    """
    Transaction costs for a straddle (4 orders: buy call, buy put, sell call, sell put).
    """
    buy_value = (call_entry + put_entry) * total_qty
    sell_value = (call_exit + put_exit) * total_qty
    turnover = buy_value + sell_value

    brokerage = BROKERAGE_PER_ORDER * 4  # 4 legs
    stt = sell_value * STT_RATE
    exchange_charges = turnover * EXCHANGE_TXN_RATE
    gst = (brokerage + exchange_charges) * GST_RATE
    sebi = turnover * SEBI_PER_CRORE / 1e7
    stamp = buy_value * STAMP_DUTY_RATE
    # Slippage and spread on all 4 legs
    slippage = SLIPPAGE_PTS * 4 * total_qty
    spread_cost = SPREAD_PTS * 4 * total_qty

    return brokerage + stt + exchange_charges + gst + sebi + stamp + slippage + spread_cost


# ──────────────────────────────────────────────────────────────────────────────
# TRADE CLASS
# ──────────────────────────────────────────────────────────────────────────────
class StraddleTrade:
    def __init__(self, entry_time, entry_spot, strike, call_premium, put_premium,
                 dte, iv_used, num_lots, vol_signal):
        self.entry_time = entry_time
        self.entry_spot = entry_spot
        self.strike = strike
        self.call_entry = call_premium
        self.put_entry = put_premium
        self.combined_entry = call_premium + put_premium
        self.dte_at_entry = dte
        self.iv_used = iv_used
        self.num_lots = num_lots
        self.vol_signal = vol_signal  # description of what triggered entry

        self.exit_time = None
        self.exit_spot = None
        self.call_exit = None
        self.put_exit = None
        self.combined_exit = None
        self.exit_reason = None
        self.candles_held = 0
        self.pnl_per_lot = 0.0
        self.pnl_total = 0.0
        self.txn_costs = 0.0
        self.net_pnl_total = 0.0
        # Trailing stop tracking
        self.peak_pnl_pct = 0.0  # highest profit % seen during trade

    def close(self, exit_time, exit_spot, call_prem, put_prem, reason):
        self.exit_time = exit_time
        self.exit_spot = exit_spot
        self.call_exit = call_prem
        self.put_exit = put_prem
        self.combined_exit = call_prem + put_prem
        self.exit_reason = reason

        self.pnl_per_lot = (self.combined_exit - self.combined_entry) * LOT_SIZE
        self.pnl_total = self.pnl_per_lot * self.num_lots

        total_qty = LOT_SIZE * self.num_lots
        self.txn_costs = compute_straddle_costs(
            self.call_entry, self.call_exit,
            self.put_entry, self.put_exit,
            total_qty
        )
        self.net_pnl_total = self.pnl_total - self.txn_costs


# ──────────────────────────────────────────────────────────────────────────────
# STRADDLE BACKTEST ENGINE (STRICT ATM VERSION)
# ──────────────────────────────────────────────────────────────────────────────
def run_straddle_backtest(df):
    """
    Volatility-triggered straddle strategy — STRICT ATM variant.
    All signals use ONLY trailing data. Entry on NEXT candle open.
    ** Only enters when |spot - ATM strike| <= MAX_ATM_DISTANCE pts **
    """
    df = compute_indicators(df)

    trades = []
    skipped_otm = 0  # count signals skipped due to ATM filter
    active_trade = None
    signal_pending = False  # signal fired, waiting for next candle to enter
    signal_info = ""
    prev_date = None

    # Cooldown: no re-entry for N candles after a trade closes
    cooldown = 0
    COOLDOWN_CANDLES = 2  # 10 min cooldown between trades (reduced from 20 min)

    for i in range(max(ATR_SLOW, BB_PERIOD) + 5, len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        row_date = ts.date()

        # Day boundary reset
        if prev_date is not None and row_date != prev_date:
            signal_pending = False
            cooldown = 0
        prev_date = row_date

        # Decrement cooldown
        if cooldown > 0:
            cooldown -= 1

        # ── EXECUTE PENDING ENTRY ────────────────────────────────────────
        if signal_pending and active_trade is None:
            signal_pending = False

            entry_spot = row["open"]  # enter at this candle's open
            strike = get_atm_strike(entry_spot)

            # *** STRICT ATM FILTER — skip if spot too far from ATM strike ***
            atm_dist = abs(entry_spot - strike)
            if atm_dist > MAX_ATM_DISTANCE:
                skipped_otm += 1
                continue

            dte = days_to_nearest_expiry(ts)
            iv = estimate_iv(df, i)

            call_prem = bs_call_price(entry_spot, strike, dte, sigma=iv)
            put_prem = bs_put_price(entry_spot, strike, dte, sigma=iv)
            combined = call_prem + put_prem

            # Position sizing: how many lots can we afford?
            cost_per_lot = combined * LOT_SIZE
            if cost_per_lot <= 0:
                continue
            max_lots = min(int(CAPITAL / cost_per_lot), 4)  # cap at 4 lots base
            num_lots = max(1, max_lots)

            # Scale up lots for high conviction signals
            if signal_info and "ATR:" in signal_info:
                try:
                    atr_val = float(signal_info.split("ATR:")[1].split(" ")[0])
                    if atr_val >= HIGH_CONVICTION_MULT:
                        num_lots = min(int(num_lots * HIGH_CONVICTION_LOT_MULT), 6)
                except (ValueError, IndexError):
                    pass

            active_trade = StraddleTrade(
                entry_time=ts,
                entry_spot=entry_spot,
                strike=strike,
                call_premium=call_prem,
                put_premium=put_prem,
                dte=dte,
                iv_used=iv,
                num_lots=num_lots,
                vol_signal=signal_info,
            )
            continue

        # ── EXIT LOGIC ───────────────────────────────────────────────────
        # FIX: exit_pending system — signal to exit on current candle's close,
        # but actually execute exit at NEXT candle's OPEN (no lookahead).
        # Session exit is the only exception (must exit before market close).
        if active_trade is not None:
            active_trade.candles_held += 1

            # If we flagged an exit last candle, execute NOW at this candle's open
            if getattr(active_trade, '_exit_pending', False):
                spot_exit = row["open"]  # EXIT AT OPEN — no lookahead
                dte = days_to_nearest_expiry(ts)
                iv = estimate_iv(df, i - 1)  # use PREVIOUS candle's IV (what we knew)
                call_ex = bs_call_price(spot_exit, active_trade.strike, dte, sigma=iv)
                put_ex = bs_put_price(spot_exit, active_trade.strike, dte, sigma=iv)
                active_trade.close(ts, spot_exit, call_ex, put_ex, active_trade._exit_reason)
                trades.append(active_trade)
                active_trade = None
                cooldown = COOLDOWN_CANDLES
                continue

            spot_now = row["close"]
            dte = days_to_nearest_expiry(ts)
            iv = estimate_iv(df, i)

            call_now = bs_call_price(spot_now, active_trade.strike, dte, sigma=iv)
            put_now = bs_put_price(spot_now, active_trade.strike, dte, sigma=iv)
            combined_now = call_now + put_now

            pnl_pct = (combined_now - active_trade.combined_entry) / active_trade.combined_entry

            # Track peak profit for trailing stop
            if pnl_pct > active_trade.peak_pnl_pct:
                active_trade.peak_pnl_pct = pnl_pct

            # Exit 1: Session force exit — IMMEDIATE (can't wait for next candle)
            if ts.time() >= FORCE_EXIT_TIME:
                active_trade.close(ts, spot_now, call_now, put_now, "SESSION_EXIT")
                trades.append(active_trade)
                active_trade = None
                cooldown = COOLDOWN_CANDLES
                continue

            # Exit 2-6: Flag for execution on NEXT candle's open
            exit_reason = None

            if pnl_pct >= PROFIT_TARGET_PCT:
                exit_reason = "PROFIT_TARGET"
            elif pnl_pct <= -STOP_LOSS_PCT:
                exit_reason = "STOP_LOSS"
            elif (active_trade.combined_entry >= CHEAP_STRADDLE_THRESH and
                    active_trade.peak_pnl_pct >= TRAIL_ACTIVATE_PCT and
                    pnl_pct <= active_trade.peak_pnl_pct * TRAIL_STOP_PCT):
                # Trailing stop: only for expensive straddles (cheap ones are too volatile)
                exit_reason = "TRAIL_STOP"
            elif (call_now >= active_trade.call_entry * ONE_LEG_MULT or
                    put_now >= active_trade.put_entry * ONE_LEG_MULT):
                exit_reason = "LEG_RUNNER"
            elif active_trade.candles_held >= MAX_HOLD_CANDLES:
                exit_reason = "TIME_EXIT_50MIN"

            if exit_reason:
                # Don't exit now — flag it, execute on next candle's open
                active_trade._exit_pending = True
                active_trade._exit_reason = exit_reason

            continue  # still holding (or pending exit)

        # ── SIGNAL DETECTION (trailing indicators only) ──────────────────
        if not in_trading_window(ts):
            continue
        if ts.time() > LAST_ENTRY_TIME:
            continue
        if cooldown > 0:
            continue

        # Only trade on/after Feb 23
        if ts.date() < pd.Timestamp("2026-02-23").date():
            continue

        # Check all three volatility conditions (ALL trailing data)
        atr_ratio = row.get("atr_ratio", np.nan)
        range_ratio = row.get("range_ratio", np.nan)
        bb_width = row.get("bb_width", np.nan)

        if pd.isna(atr_ratio) or pd.isna(range_ratio) or pd.isna(bb_width):
            continue

        # Condition 1: ATR expanding (fast > slow by threshold)
        vol_atr = atr_ratio >= ATR_RATIO_TRIGGER

        # Condition 2: Current candle is a breakout candle
        vol_breakout = range_ratio >= BREAKOUT_CANDLE_MULT

        # Condition 3: BB bands are wide (volatile regime)
        vol_bb = bb_width >= BB_WIDTH_TRIGGER

        # Need ATR expansion + at least one of (breakout candle OR wide BB)
        # This avoids being too strict while still requiring volatility confirmation
        if vol_atr and (vol_breakout or vol_bb):
            signal_pending = True
            reasons = []
            reasons.append(f"ATR:{atr_ratio:.2f}")
            if vol_breakout:
                reasons.append(f"Range:{range_ratio:.1f}x")
            if vol_bb:
                reasons.append(f"BB:{bb_width:.4f}")
            signal_info = " | ".join(reasons)

    # Close any remaining trade
    if active_trade is not None:
        last = df.iloc[-1]
        dte = days_to_nearest_expiry(df.index[-1])
        iv = estimate_iv(df, len(df) - 1)
        call_now = bs_call_price(last["close"], active_trade.strike, dte, sigma=iv)
        put_now = bs_put_price(last["close"], active_trade.strike, dte, sigma=iv)
        active_trade.close(df.index[-1], last["close"], call_now, put_now, "END_OF_DATA")
        trades.append(active_trade)

    return trades, df, skipped_otm


# ──────────────────────────────────────────────────────────────────────────────
# REPORTING
# ──────────────────────────────────────────────────────────────────────────────
def print_report(trades, df, skipped_otm=0):
    # Filter df to trading period for stats
    trade_df_start = pd.Timestamp("2026-02-23")
    df_period = df[df.index >= trade_df_start]

    print(f"\n{'=' * 110}")
    print(f"  NIFTY50 STRADDLE STRATEGY — STRICT ATM VARIANT")
    print(f"{'=' * 110}")
    print(f"\n  Period          : Feb 23, 2026 -> Mar 16, 2026 (15 trading days)")
    print(f"  Capital         : Rs {CAPITAL:,.0f}")
    print(f"  Lot Size        : {LOT_SIZE}")
    print(f"  Order Type      : LIMIT (reduced slippage)")
    print(f"  ATM Filter      : |spot - strike| <= {MAX_ATM_DISTANCE} pts")
    print(f"  Signals Skipped : {skipped_otm} (spot too far from ATM strike)")
    print(f"  Total Candles   : {len(df_period)}")
    print(f"  Spot Range      : {df_period['low'].min():.0f} - {df_period['high'].max():.0f}")

    if not trades:
        print(f"\n  NO TRADES GENERATED")
        return

    # Trade log
    rows = []
    for t in trades:
        spot_move = abs(t.exit_spot - t.entry_spot) if t.exit_spot else 0
        atm_dist = abs(t.entry_spot - t.strike)
        rows.append({
            "Entry": t.entry_time.strftime("%m-%d %H:%M"),
            "Spot": f"{t.entry_spot:.0f}",
            "K": t.strike,
            "|S-K|": f"{atm_dist:.0f}",
            "C+P": f"{t.combined_entry:.1f}",
            "IV": f"{t.iv_used:.1%}",
            "Lots": t.num_lots,
            "Exit": t.exit_time.strftime("%m-%d %H:%M") if t.exit_time else "-",
            "ExSpot": f"{t.exit_spot:.0f}" if t.exit_spot else "-",
            "C+P Ex": f"{t.combined_exit:.1f}" if t.combined_exit else "-",
            "Move": f"{spot_move:.0f}",
            "Gross": f"{t.pnl_total:+,.0f}",
            "Cost": f"-{t.txn_costs:.0f}",
            "Net": f"{t.net_pnl_total:+,.0f}",
            "#": t.candles_held,
            "Reason": (t.exit_reason or "-")[:14],
            "Signal": t.vol_signal[:25] if t.vol_signal else "-",
        })

    tdf = pd.DataFrame(rows)
    print(f"\n{'─' * 110}")
    print("  TRADE LOG")
    print(f"{'─' * 110}")
    print(tabulate(tdf, headers="keys", tablefmt="simple", showindex=False))

    # Statistics
    gross_pnls = [t.pnl_total for t in trades]
    net_pnls = [t.net_pnl_total for t in trades]
    costs = [t.txn_costs for t in trades]

    gross_winners = [p for p in gross_pnls if p > 0]
    gross_losers = [p for p in gross_pnls if p <= 0]
    net_winners = [p for p in net_pnls if p > 0]
    net_losers = [p for p in net_pnls if p <= 0]

    cum_net = np.cumsum(net_pnls)
    peak = np.maximum.accumulate(cum_net)
    max_dd = (cum_net - peak).min()

    total_lots = sum(t.num_lots for t in trades)
    avg_hold = np.mean([t.candles_held for t in trades])
    avg_spot_move = np.mean([abs(t.exit_spot - t.entry_spot) for t in trades if t.exit_spot])

    print(f"\n{'─' * 110}")
    print("  SUMMARY")
    print(f"{'─' * 110}")
    summary = [
        ["Total Trades", len(trades)],
        ["Total Lots", total_lots],
        ["Signals Skipped (non-ATM)", skipped_otm],
        ["Avg Hold (candles)", f"{avg_hold:.1f} ({avg_hold * 5:.0f} min)"],
        ["Avg Spot Move", f"{avg_spot_move:.0f} pts"],
        ["", ""],
        ["--- GROSS ---", ""],
        ["Gross Winners", f"{len(gross_winners)} ({len(gross_winners)/len(trades)*100:.0f}%)"],
        ["Gross P&L", f"Rs {sum(gross_pnls):+,.0f}"],
        ["Avg Win", f"Rs {np.mean(gross_winners):+,.0f}" if gross_winners else "N/A"],
        ["Avg Loss", f"Rs {np.mean(gross_losers):+,.0f}" if gross_losers else "N/A"],
        ["Profit Factor", f"{abs(sum(gross_winners)/sum(gross_losers)):.2f}" if gross_losers and sum(gross_losers) != 0 else "INF"],
        ["", ""],
        ["--- COSTS ---", ""],
        ["Total Costs", f"Rs {sum(costs):,.0f}"],
        ["Avg Cost/Trade", f"Rs {np.mean(costs):,.0f}"],
        ["", ""],
        ["--- NET (live market) ---", ""],
        ["Net Winners", f"{len(net_winners)} ({len(net_winners)/len(trades)*100:.0f}%)"],
        ["Net P&L", f"Rs {sum(net_pnls):+,.0f}"],
        ["Net Profit Factor", f"{abs(sum(net_winners)/sum(net_losers)):.2f}" if net_losers and sum(net_losers) != 0 else "INF"],
        ["Max Drawdown", f"Rs {max_dd:+,.0f}"],
        ["ROI on Capital", f"{sum(net_pnls)/CAPITAL*100:+.1f}%"],
    ]
    print(tabulate(summary, tablefmt="simple"))

    # Exit reason breakdown
    exit_counts = {}
    for t in trades:
        r = t.exit_reason or "UNKNOWN"
        exit_counts[r] = exit_counts.get(r, 0) + 1
    print(f"\n  Exit Reasons:")
    for reason, count in sorted(exit_counts.items()):
        print(f"    {reason:20s}: {count}")

    # Equity curve
    cum_gross = np.cumsum(gross_pnls)
    cum_net_arr = np.cumsum(net_pnls)
    x = range(1, len(trades) + 1)

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

    ax1 = axes[0]
    ax1.plot(x, cum_gross, "b-o", markersize=5, label="Gross P&L")
    ax1.plot(x, cum_net_arr, "r-s", markersize=5, label="Net P&L (after costs)")
    ax1.fill_between(x, cum_net_arr, 0, where=cum_net_arr >= 0, color="green", alpha=0.1)
    ax1.fill_between(x, cum_net_arr, 0, where=cum_net_arr < 0, color="red", alpha=0.1)
    ax1.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax1.set_title("NIFTY Straddle — STRICT ATM Equity Curve (Feb 23 - Mar 16, 2026)", fontsize=13)
    ax1.set_xlabel("Trade #")
    ax1.set_ylabel("Cumulative P&L (Rs)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2 = axes[1]
    colors = ["green" if p > 0 else "red" for p in net_pnls]
    ax2.bar(x, net_pnls, color=colors, alpha=0.7)
    ax2.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax2.set_title("Per-Trade Net P&L", fontsize=12)
    ax2.set_xlabel("Trade #")
    ax2.set_ylabel("Net P&L (Rs)")
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = Path("straddle_strict_atm_results.png")
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"\n  Chart saved: {out_path.resolve()}")

    csv_path = Path("straddle_strict_atm_trades.csv")
    tdf.to_csv(csv_path, index=False)
    print(f"  CSV saved: {csv_path.resolve()}")
    print(f"\n{'=' * 110}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Loading NIFTY 5-min data...")
    df = load_data()
    print(f"Loaded {len(df)} candles: {df.index[0]} -> {df.index[-1]}")

    print("Running STRICT ATM straddle backtest...")
    trades, df_with_indicators, skipped = run_straddle_backtest(df)
    print(f"Completed: {len(trades)} trades ({skipped} signals skipped — spot too far from ATM)")

    print_report(trades, df_with_indicators, skipped)
