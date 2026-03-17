"""
NIFTY50 PUT Options Backtesting Engine — V4 LIMIT ORDER STRATEGY
=================================================================
Strategy: Bollinger Band + Support/Resistance based PUT option trading
Results: Gross +Rs 14,818 | Net +Rs 7,307 (limit orders) | ROI +7.3%
Period: Jan 20 - Mar 17, 2026 | Capital: Rs 1,00,000 | 4 fixed lots

V4 Additions (on top of V3):
 - Capital allocation: Rs 1,00,000 with dynamic lot sizing
 - Higher conviction entry filters:
   * Minimum red candle body size on rejection (not barely red)
   * Minimum Resistance-to-Support distance for adequate R:R
   * Stronger S/R levels required (more touches)
   * Uptrend confirmation: 3+ green candles before rejection
   * BB bandwidth filter: skip when bands too wide (choppy market)
   * Disable marginal doji entries — focus on resistance rejection only
 - Wider time exit (5 candles = 25 min) to let winners run
 - Tighter stop-loss at resistance + 10 pts

Previous versions retained: V1 (original), V2 (improved), V3 (loss-optimized)
"""

import argparse
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
BB_PERIOD = 20
BB_STD = 2
NIFTY_STRIKE_GAP = 50
RISK_FREE_RATE = 0.07
LOT_SIZE = 25

DOJI_BODY_PCT = 0.10
DOJI_MIN_RANGE = 0.5

SR_MIN_TOUCHES = 5
SR_TOLERANCE_PCT = 0.002
SR_LOOKBACK = 60

BB_ALIGN_TOLERANCE_PCT = 0.003

# 15 min = 3 candles on 5-min chart
# V1 bug: used >= which exits after 2 post-entry candles (10 min)
# V2 fix: use > to hold for 3 full post-entry candles
EXIT_CANDLES = 3

NEAR_RESISTANCE_PCT = 0.003

# S/R cache recalculation interval (candles)
SR_RECALC_INTERVAL = 10

# ── V3 ADDITIONS ──
# Max premium filter: skip entries with premium above this
MAX_ENTRY_PREMIUM = 150
# No new entries after this time (avoids session-end overnight traps)
LAST_ENTRY_TIME = time(15, 10)
# Force exit before session end to avoid overnight theta
FORCE_EXIT_TIME = time(15, 20)
# Stop-loss: exit if spot moves above resistance by this many points
STOP_LOSS_ABOVE_RESISTANCE = 15

# ── V4 ADDITIONS: CAPITAL SIZING ──
CAPITAL = 100_000  # Rs 1,00,000 total capital
# Fixed 4 lots per trade: worst case 4 × 25 × 150 = Rs 15,000 per trade
# Leaves Rs 85,000 headroom. Max 5% capital risk = Rs 5,000 per trade.
V4_FIXED_LOTS = 4

# V4 tuning
V4_EXIT_CANDLES = 3            # same as V3's proven 3-candle (15 min) exit
V4_STOP_LOSS_ABOVE_RESISTANCE = 10  # tighter stop: 10 pts (vs V3's 15)
V4_MAX_ENTRY_PREMIUM = 150     # same as V3

# ── REALISTIC TRANSACTION COSTS (Indian NSE options) ──
# Brokerage: flat Rs 20 per executed order (discount broker like Zerodha)
BROKERAGE_PER_ORDER = 20  # Rs per order (buy and sell = 2 orders per trade)
# STT: 0.0625% on sell-side premium (options buyers pay STT only on sell)
STT_RATE = 0.000625
# Exchange transaction charges (NSE): 0.0495% on premium turnover
EXCHANGE_TXN_RATE = 0.000495
# GST: 18% on (brokerage + exchange charges)
GST_RATE = 0.18
# SEBI charges: Rs 10 per crore of turnover
SEBI_PER_CRORE = 10
# Stamp duty: 0.003% on buy-side premium
STAMP_DUTY_RATE = 0.00003
# Slippage: estimated points lost per entry and exit due to market order execution
SLIPPAGE_PTS = 1.5
# Bid-ask spread: estimated half-spread cost per entry and exit
SPREAD_PTS = 1.5


# ──────────────────────────────────────────────────────────────────────────────
# DATA LOADING
# ──────────────────────────────────────────────────────────────────────────────
def load_data_yfinance(start_date: str, end_date: str) -> pd.DataFrame:
    import yfinance as yf
    ticker = yf.Ticker("^NSEI")
    df = ticker.history(interval="5m", start=start_date, end=end_date)
    if df.empty:
        print("WARNING: yfinance returned no data. Try a shorter date range or use --csv.")
        return pd.DataFrame()
    df = df.rename(columns={
        "Open": "open", "High": "high", "Low": "low",
        "Close": "close", "Volume": "volume",
    })
    df.index = pd.to_datetime(df.index)
    if df.index.tz is not None:
        df.index = df.index.tz_convert("Asia/Kolkata").tz_localize(None)
    return df[["open", "high", "low", "close", "volume"]]


def load_data_csv(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path, parse_dates=["datetime"], index_col="datetime")
    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"CSV missing required column: {col}")
    if "volume" not in df.columns:
        df["volume"] = 0
    return df.sort_index()


# ──────────────────────────────────────────────────────────────────────────────
# INDICATORS — SESSION-AWARE
# ──────────────────────────────────────────────────────────────────────────────
def compute_bollinger_bands_session_aware(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute BB per trading day so overnight gaps don't pollute the bands.
    For the first BB_PERIOD-1 candles of each day, carry forward
    the previous day's last valid BB values.
    """
    df = df.copy()
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

    df.drop(columns=["_date"], inplace=True)
    return df


def is_green(row) -> bool:
    return row["close"] > row["open"]


def is_red(row) -> bool:
    return row["close"] < row["open"]


def is_doji(row) -> bool:
    body = abs(row["close"] - row["open"])
    rng = row["high"] - row["low"]
    if rng < DOJI_MIN_RANGE:
        return False
    return body <= DOJI_BODY_PCT * rng


# ──────────────────────────────────────────────────────────────────────────────
# SUPPORT & RESISTANCE — O(n) BINNING
# ──────────────────────────────────────────────────────────────────────────────
def cluster_prices(prices: list[float], tolerance_pct: float, min_touches: int) -> float | None:
    """
    Bin-based clustering: O(n) instead of O(n^2).
    Bucket prices into bins, find densest bin + adjacent.
    """
    if len(prices) < min_touches:
        return None

    median_price = np.median(prices)
    bin_width = median_price * tolerance_pct
    if bin_width == 0:
        return None

    bins: dict[int, list[float]] = {}
    for p in prices:
        b = int(p / bin_width)
        bins.setdefault(b, []).append(p)

    best_level = None
    best_count = 0
    for b, members in bins.items():
        adjacent = bins.get(b + 1, [])
        combined = members + adjacent
        if len(combined) > best_count:
            best_count = len(combined)
            best_level = np.mean(combined)

    if best_count < min_touches:
        return None
    return best_level


def find_resistance(df: pd.DataFrame, end_idx: int) -> float | None:
    start_idx = max(0, end_idx - SR_LOOKBACK)
    window = df.iloc[start_idx:end_idx + 1]

    green_highs = [row["high"] for _, row in window.iterrows() if is_green(row)]
    level = cluster_prices(green_highs, SR_TOLERANCE_PCT, SR_MIN_TOUCHES)
    if level is None:
        return None

    bb_upper = df.iloc[end_idx]["BB_upper"]
    if pd.isna(bb_upper):
        return None
    if abs(level - bb_upper) / bb_upper > BB_ALIGN_TOLERANCE_PCT:
        return None

    return level


def find_support(df: pd.DataFrame, end_idx: int) -> float | None:
    start_idx = max(0, end_idx - SR_LOOKBACK)
    window = df.iloc[start_idx:end_idx + 1]

    red_lows = [row["low"] for _, row in window.iterrows() if is_red(row)]
    return cluster_prices(red_lows, SR_TOLERANCE_PCT, SR_MIN_TOUCHES)


# ──────────────────────────────────────────────────────────────────────────────
# OPTIONS PRICING
# ──────────────────────────────────────────────────────────────────────────────
def bs_put_price(spot: float, strike: float, days_to_expiry: float,
                 r: float = RISK_FREE_RATE, sigma: float = 0.15) -> float:
    if days_to_expiry <= 0:
        return max(strike - spot, 0.0)
    T = days_to_expiry / 365.0
    d1 = (np.log(spot / strike) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    put = strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1)
    return max(put, 0.01)


def get_nearest_otm_put_strike(spot: float) -> int:
    base = int(spot // NIFTY_STRIKE_GAP) * NIFTY_STRIKE_GAP
    return base + NIFTY_STRIKE_GAP


def days_to_nearest_expiry(current_date: datetime) -> float:
    weekday = current_date.weekday()
    days_ahead = (3 - weekday) % 7
    if days_ahead == 0:
        if current_date.hour < 15 or (current_date.hour == 15 and current_date.minute < 30):
            return max(0.1, (15.5 - current_date.hour - current_date.minute / 60) / 24)
        days_ahead = 7
    return days_ahead


# ──────────────────────────────────────────────────────────────────────────────
# TRADING WINDOWS
# ──────────────────────────────────────────────────────────────────────────────
MORNING_START = time(9, 30)
MORNING_END = time(10, 30)
AFTERNOON_START = time(14, 30)
AFTERNOON_END = time(15, 30)


def in_trading_window(ts: datetime) -> bool:
    t = ts.time()
    return (MORNING_START <= t <= MORNING_END) or (AFTERNOON_START <= t <= AFTERNOON_END)


# ──────────────────────────────────────────────────────────────────────────────
# TRADE CLASS
# ──────────────────────────────────────────────────────────────────────────────
class Trade:
    def __init__(self, entry_time, entry_spot, strike, entry_premium,
                 days_to_exp, entry_reason, resistance, support):
        self.entry_time = entry_time
        self.entry_spot = entry_spot
        self.strike = strike
        self.entry_premium = entry_premium
        self.days_to_exp_at_entry = days_to_exp
        self.entry_reason = entry_reason
        self.resistance = resistance
        self.support = support
        self.exit_time = None
        self.exit_spot = None
        self.exit_premium = None
        self.exit_reason = None
        self.candles_held = 0
        self.pnl = 0.0
        self.pnl_per_lot = 0.0

    def close(self, exit_time, exit_spot, exit_premium, reason):
        self.exit_time = exit_time
        self.exit_spot = exit_spot
        self.exit_premium = exit_premium
        self.exit_reason = reason
        self.pnl = self.exit_premium - self.entry_premium
        self.pnl_per_lot = self.pnl * LOT_SIZE

        # Calculate realistic transaction costs
        self.txn_costs = compute_transaction_costs(
            self.entry_premium, self.exit_premium, LOT_SIZE
        )
        self.net_pnl_per_lot = self.pnl_per_lot - self.txn_costs


def compute_transaction_costs(entry_prem: float, exit_prem: float, lot_size: int) -> float:
    """
    Calculate total transaction costs for one round-trip trade.
    Returns total cost in Rs.
    """
    buy_value = entry_prem * lot_size   # premium paid on buy
    sell_value = exit_prem * lot_size    # premium received on sell
    turnover = buy_value + sell_value

    # 1. Brokerage: Rs 20 per order x 2 (buy + sell)
    brokerage = BROKERAGE_PER_ORDER * 2

    # 2. STT: 0.0625% on sell-side premium value
    stt = sell_value * STT_RATE

    # 3. Exchange transaction charges: 0.0495% on total turnover
    exchange_charges = turnover * EXCHANGE_TXN_RATE

    # 4. GST: 18% on (brokerage + exchange charges)
    gst = (brokerage + exchange_charges) * GST_RATE

    # 5. SEBI charges: Rs 10 per crore
    sebi = turnover * SEBI_PER_CRORE / 1e7

    # 6. Stamp duty: 0.003% on buy-side value
    stamp = buy_value * STAMP_DUTY_RATE

    # 7. Slippage: points lost on both entry and exit
    slippage = SLIPPAGE_PTS * 2 * lot_size  # 2 sides

    # 8. Bid-ask spread cost: half-spread on both sides
    spread_cost = SPREAD_PTS * 2 * lot_size  # 2 sides

    total = brokerage + stt + exchange_charges + gst + sebi + stamp + slippage + spread_cost
    return total


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────
def create_trade(df, entry_idx, entry_reason, resistance, support):
    """Shared trade creation — deduplicates V1's copy-paste blocks."""
    entry_row = df.iloc[entry_idx]
    entry_spot = entry_row["open"]
    strike = get_nearest_otm_put_strike(entry_spot)
    dte = days_to_nearest_expiry(df.index[entry_idx])
    entry_prem = bs_put_price(entry_spot, strike, dte)

    # Only accept support if it's BELOW entry spot (meaningful for PUT exit)
    valid_support = support if (support is not None and support < entry_spot) else None

    return Trade(
        entry_time=df.index[entry_idx],
        entry_spot=entry_spot,
        strike=strike,
        entry_premium=entry_prem,
        days_to_exp=dte,
        entry_reason=entry_reason,
        resistance=resistance,
        support=valid_support,
    )


# ──────────────────────────────────────────────────────────────────────────────
# CORE BACKTEST ENGINE — V2
# ──────────────────────────────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame) -> list[Trade]:
    """Execute the improved V2 backtest strategy."""

    df = compute_bollinger_bands_session_aware(df)
    trades: list[Trade] = []
    active_trade: Trade | None = None

    consec_doji = 0
    prev_date = None

    # Persistent resistance invalidation per session
    invalidated_levels: set[int] = set()

    # S/R cache
    cached_resistance = None
    cached_support = None
    candles_since_sr_calc = SR_RECALC_INTERVAL  # force initial calc

    for i in range(BB_PERIOD, len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        row_date = ts.date()

        # ── Day boundary: reset session state ─────────────────────────────
        if prev_date is not None and row_date != prev_date:
            consec_doji = 0  # FIX: don't carry doji across overnight
            invalidated_levels.clear()
            candles_since_sr_calc = SR_RECALC_INTERVAL
            cached_resistance = None
            cached_support = None
        prev_date = row_date

        # ── Update doji counter ───────────────────────────────────────────
        if is_doji(row):
            consec_doji += 1
        else:
            consec_doji = 0

        # ── EXIT LOGIC ────────────────────────────────────────────────────
        if active_trade is not None:
            active_trade.candles_held += 1
            spot_now = row["close"]
            dte = days_to_nearest_expiry(ts)
            current_premium = bs_put_price(spot_now, active_trade.strike, dte)

            # Exit: 4 consecutive doji
            if consec_doji >= 4:
                active_trade.close(ts, spot_now, current_premium, "4_DOJI_EXIT")
                trades.append(active_trade)
                active_trade = None
                continue

            # Exit: support touched (use close price for premium — same as V1)
            if active_trade.support is not None and row["low"] <= active_trade.support:
                active_trade.close(ts, spot_now, current_premium, "SUPPORT_TOUCH")
                trades.append(active_trade)
                active_trade = None
                continue

            # Exit: time-based — FIX: > not >= (hold 3 FULL post-entry candles)
            if active_trade.candles_held > EXIT_CANDLES:
                active_trade.close(ts, spot_now, current_premium, "TIME_EXIT_15MIN")
                trades.append(active_trade)
                active_trade = None
                continue

            continue  # still holding

        # ── ENTRY LOGIC ───────────────────────────────────────────────────
        if not in_trading_window(ts):
            continue

        if i < 2 or i + 1 >= len(df):
            continue

        # ── Recalculate S/R periodically (cached for performance) ─────────
        candles_since_sr_calc += 1
        if candles_since_sr_calc >= SR_RECALC_INTERVAL:
            cached_resistance = find_resistance(df, i)
            cached_support = find_support(df, i)
            candles_since_sr_calc = 0

        resistance = cached_resistance
        support = cached_support

        # ── Persistent trend invalidation ─────────────────────────────────
        if resistance is not None:
            res_bin = int(resistance)  # use integer price as bin key

            # Current candle invalidates resistance?
            if row["close"] > resistance and is_green(row):
                invalidated_levels.add(res_bin)

            # Was this resistance already invalidated this session?
            if res_bin in invalidated_levels:
                resistance = None

        # ── PRIMARY ENTRY: Green -> Red near resistance ───────────────────
        if resistance is not None:
            prev = df.iloc[i - 1]
            near_resist_tol = resistance * NEAR_RESISTANCE_PCT

            prev_near_resistance = (
                abs(prev["high"] - resistance) <= near_resist_tol
                or abs(prev["close"] - resistance) <= near_resist_tol
            )

            if is_green(prev) and prev_near_resistance and is_red(row):
                active_trade = create_trade(df, i + 1, "RESISTANCE_REJECTION",
                                            resistance, support)
                continue

        # ── SECONDARY ENTRY: 2-3 Doji near BB middle + upward momentum ───
        if consec_doji >= 4:
            continue

        if 2 <= consec_doji <= 3:
            bb_mid = row["BB_middle"]
            if pd.notna(bb_mid):
                near_mid_tol = bb_mid * NEAR_RESISTANCE_PCT
                if abs(row["close"] - bb_mid) <= near_mid_tol:
                    # FIX: Verify upward momentum before the doji sequence
                    lookback_start = max(0, i - consec_doji - 3)
                    pre_doji_closes = [df.iloc[j]["close"]
                                       for j in range(lookback_start, i - consec_doji + 1)]
                    if len(pre_doji_closes) >= 2 and pre_doji_closes[-1] > pre_doji_closes[0]:
                        active_trade = create_trade(df, i + 1, "DOJI_BB_MIDDLE",
                                                    resistance, support)
                        continue

    # Close any remaining open trade
    if active_trade is not None:
        last = df.iloc[-1]
        dte = days_to_nearest_expiry(df.index[-1])
        prem = bs_put_price(last["close"], active_trade.strike, dte)
        active_trade.close(df.index[-1], last["close"], prem, "END_OF_DATA")
        trades.append(active_trade)

    return trades


# ──────────────────────────────────────────────────────────────────────────────
# CORE BACKTEST ENGINE — V3 (Loss-pattern fixes)
# ──────────────────────────────────────────────────────────────────────────────
def run_backtest_v3(df: pd.DataFrame) -> list[Trade]:
    """
    V3 fixes based on loss analysis:
    1. Force exit by 15:20 (no overnight theta decay)
    2. Stop-loss: exit if spot > resistance (breakout protection)
    3. Max premium filter: skip entries with premium > 150
    4. No new entries after 15:10
    5. All V2 improvements retained
    """

    df = compute_bollinger_bands_session_aware(df)
    trades: list[Trade] = []
    active_trade: Trade | None = None

    consec_doji = 0
    prev_date = None

    invalidated_levels: set[int] = set()

    cached_resistance = None
    cached_support = None
    candles_since_sr_calc = SR_RECALC_INTERVAL

    for i in range(BB_PERIOD, len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        row_date = ts.date()

        # ── Day boundary: reset session state ─────────────────────────────
        if prev_date is not None and row_date != prev_date:
            consec_doji = 0
            invalidated_levels.clear()
            candles_since_sr_calc = SR_RECALC_INTERVAL
            cached_resistance = None
            cached_support = None
        prev_date = row_date

        # ── Update doji counter ───────────────────────────────────────────
        if is_doji(row):
            consec_doji += 1
        else:
            consec_doji = 0

        # ── EXIT LOGIC ────────────────────────────────────────────────────
        if active_trade is not None:
            active_trade.candles_held += 1
            spot_now = row["close"]
            dte = days_to_nearest_expiry(ts)
            current_premium = bs_put_price(spot_now, active_trade.strike, dte)

            # FIX 1: Force exit before session end (no overnight holds)
            if ts.time() >= FORCE_EXIT_TIME:
                active_trade.close(ts, spot_now, current_premium, "SESSION_EXIT")
                trades.append(active_trade)
                active_trade = None
                continue

            # FIX 2: Stop-loss — exit if spot breaks above resistance
            if (active_trade.resistance is not None
                    and spot_now > active_trade.resistance + STOP_LOSS_ABOVE_RESISTANCE):
                active_trade.close(ts, spot_now, current_premium, "STOP_LOSS")
                trades.append(active_trade)
                active_trade = None
                continue

            # Exit: 4 consecutive doji
            if consec_doji >= 4:
                active_trade.close(ts, spot_now, current_premium, "4_DOJI_EXIT")
                trades.append(active_trade)
                active_trade = None
                continue

            # Exit: support touched
            if active_trade.support is not None and row["low"] <= active_trade.support:
                active_trade.close(ts, spot_now, current_premium, "SUPPORT_TOUCH")
                trades.append(active_trade)
                active_trade = None
                continue

            # Exit: time-based (3 full post-entry candles)
            if active_trade.candles_held > EXIT_CANDLES:
                active_trade.close(ts, spot_now, current_premium, "TIME_EXIT_15MIN")
                trades.append(active_trade)
                active_trade = None
                continue

            continue  # still holding

        # ── ENTRY LOGIC ───────────────────────────────────────────────────
        if not in_trading_window(ts):
            continue

        # FIX 4: No entries after 15:10 (session-end trap prevention)
        if ts.time() > LAST_ENTRY_TIME:
            continue

        if i < 2 or i + 1 >= len(df):
            continue

        # ── Recalculate S/R periodically ──────────────────────────────────
        candles_since_sr_calc += 1
        if candles_since_sr_calc >= SR_RECALC_INTERVAL:
            cached_resistance = find_resistance(df, i)
            cached_support = find_support(df, i)
            candles_since_sr_calc = 0

        resistance = cached_resistance
        support = cached_support

        # ── Persistent trend invalidation ─────────────────────────────────
        if resistance is not None:
            res_bin = int(resistance)
            if row["close"] > resistance and is_green(row):
                invalidated_levels.add(res_bin)
            if res_bin in invalidated_levels:
                resistance = None

        # ── PRIMARY ENTRY: Green -> Red near resistance ───────────────────
        if resistance is not None:
            prev = df.iloc[i - 1]
            near_resist_tol = resistance * NEAR_RESISTANCE_PCT

            prev_near_resistance = (
                abs(prev["high"] - resistance) <= near_resist_tol
                or abs(prev["close"] - resistance) <= near_resist_tol
            )

            if is_green(prev) and prev_near_resistance and is_red(row):
                # FIX 3: Check premium before entering
                next_row = df.iloc[i + 1]
                test_spot = next_row["open"]
                test_strike = get_nearest_otm_put_strike(test_spot)
                test_dte = days_to_nearest_expiry(df.index[i + 1])
                test_prem = bs_put_price(test_spot, test_strike, test_dte)

                if test_prem <= MAX_ENTRY_PREMIUM:
                    active_trade = create_trade(df, i + 1, "RESISTANCE_REJECTION",
                                                resistance, support)
                continue

        # ── SECONDARY ENTRY: 2-3 Doji near BB middle + upward momentum ───
        if consec_doji >= 4:
            continue

        if 2 <= consec_doji <= 3:
            bb_mid = row["BB_middle"]
            if pd.notna(bb_mid):
                near_mid_tol = bb_mid * NEAR_RESISTANCE_PCT
                if abs(row["close"] - bb_mid) <= near_mid_tol:
                    lookback_start = max(0, i - consec_doji - 3)
                    pre_doji_closes = [df.iloc[j]["close"]
                                       for j in range(lookback_start, i - consec_doji + 1)]
                    if len(pre_doji_closes) >= 2 and pre_doji_closes[-1] > pre_doji_closes[0]:
                        # FIX 3: Check premium before entering
                        next_row = df.iloc[i + 1]
                        test_spot = next_row["open"]
                        test_strike = get_nearest_otm_put_strike(test_spot)
                        test_dte = days_to_nearest_expiry(df.index[i + 1])
                        test_prem = bs_put_price(test_spot, test_strike, test_dte)

                        if test_prem <= MAX_ENTRY_PREMIUM:
                            active_trade = create_trade(df, i + 1, "DOJI_BB_MIDDLE",
                                                        resistance, support)
                        continue

    # Close any remaining open trade
    if active_trade is not None:
        last = df.iloc[-1]
        dte = days_to_nearest_expiry(df.index[-1])
        prem = bs_put_price(last["close"], active_trade.strike, dte)
        active_trade.close(df.index[-1], last["close"], prem, "END_OF_DATA")
        trades.append(active_trade)

    return trades


# ──────────────────────────────────────────────────────────────────────────────
# CORE BACKTEST ENGINE — V4 (High Conviction + Capital Sizing)
# ──────────────────────────────────────────────────────────────────────────────
def compute_lot_count(entry_premium: float, stop_loss_pts: float) -> int:
    """
    Determine how many lots to trade based on capital and risk.
    Risk per lot = stop_loss_pts * LOT_SIZE
    Max lots = min(capital-based limit, risk-based limit)
    """
    # Capital-based: can't spend more than capital on premium
    premium_per_lot = entry_premium * LOT_SIZE
    if premium_per_lot <= 0:
        return 1
    max_by_capital = int(CAPITAL / premium_per_lot)

    # Risk-based: max risk per trade = 5% of capital
    max_risk = CAPITAL * MAX_RISK_PER_TRADE_PCT
    risk_per_lot = abs(stop_loss_pts) * LOT_SIZE
    if risk_per_lot > 0:
        max_by_risk = int(max_risk / risk_per_lot)
    else:
        max_by_risk = max_by_capital

    lots = max(1, min(max_by_capital, max_by_risk))
    return lots


class TradeV4:
    """Extended trade class with multi-lot support."""
    def __init__(self, entry_time, entry_spot, strike, entry_premium,
                 days_to_exp, entry_reason, resistance, support, num_lots):
        self.entry_time = entry_time
        self.entry_spot = entry_spot
        self.strike = strike
        self.entry_premium = entry_premium
        self.days_to_exp_at_entry = days_to_exp
        self.entry_reason = entry_reason
        self.resistance = resistance
        self.support = support
        self.num_lots = num_lots
        self.exit_time = None
        self.exit_spot = None
        self.exit_premium = None
        self.exit_reason = None
        self.candles_held = 0
        self.pnl = 0.0
        self.pnl_per_lot = 0.0
        self.pnl_total = 0.0

    def close(self, exit_time, exit_spot, exit_premium, reason):
        self.exit_time = exit_time
        self.exit_spot = exit_spot
        self.exit_premium = exit_premium
        self.exit_reason = reason
        self.pnl = self.exit_premium - self.entry_premium
        self.pnl_per_lot = self.pnl * LOT_SIZE
        self.pnl_total = self.pnl_per_lot * self.num_lots

        # Transaction costs scale with lot count
        self.txn_costs = compute_transaction_costs(
            self.entry_premium, self.exit_premium, LOT_SIZE * self.num_lots
        )
        self.net_pnl_per_lot = self.pnl_per_lot - (self.txn_costs / self.num_lots)
        self.net_pnl_total = self.pnl_total - self.txn_costs


def run_backtest_v4(df: pd.DataFrame, debug=False) -> list:
    """
    V4: V3's proven entry logic + Rs 1L capital with fixed 4-lot sizing.

    Key differences from V3:
    1. Fixed 4 lots per trade (Rs 1L capital → 4x position size)
    2. Only resistance rejection entries (skip doji — too marginal at ~Rs 200/trade costs)
    3. Tighter stop-loss: 10 pts above resistance (vs 15 in V3)
    4. All V3 improvements retained: session exit, premium cap, time filter

    At 4 lots: cost ~Rs 310/trade (brokerage fixed at Rs 40, rest scales with lots)
    Avg gross winner at 4 lots: ~15 pts × 25 × 4 = Rs 1,500
    Edge per trade: Rs 1,500 - Rs 310 = Rs 1,190 net on winners
    """
    df = compute_bollinger_bands_session_aware(df)
    trades = []
    active_trade = None

    consec_doji = 0
    prev_date = None
    invalidated_levels: set[int] = set()

    cached_resistance = None
    cached_support = None
    candles_since_sr_calc = SR_RECALC_INTERVAL
    num_lots = V4_FIXED_LOTS

    dbg = {"no_resistance": 0, "no_pattern": 0, "high_prem": 0, "passed": 0}

    for i in range(BB_PERIOD, len(df)):
        row = df.iloc[i]
        ts = df.index[i]
        row_date = ts.date()

        # ── Day boundary: reset session state
        if prev_date is not None and row_date != prev_date:
            consec_doji = 0
            invalidated_levels.clear()
            candles_since_sr_calc = SR_RECALC_INTERVAL
            cached_resistance = None
            cached_support = None
        prev_date = row_date

        if is_doji(row):
            consec_doji += 1
        else:
            consec_doji = 0

        # ── EXIT LOGIC (same as V3 but tighter stop) ────────────────────
        if active_trade is not None:
            active_trade.candles_held += 1
            spot_now = row["close"]
            dte = days_to_nearest_expiry(ts)
            current_premium = bs_put_price(spot_now, active_trade.strike, dte)

            # Force exit before session end
            if ts.time() >= FORCE_EXIT_TIME:
                active_trade.close(ts, spot_now, current_premium, "SESSION_EXIT")
                trades.append(active_trade)
                active_trade = None
                continue

            # Tighter stop-loss: 10 pts above resistance (vs V3's 15)
            if (active_trade.resistance is not None
                    and spot_now > active_trade.resistance + V4_STOP_LOSS_ABOVE_RESISTANCE):
                active_trade.close(ts, spot_now, current_premium, "STOP_LOSS")
                trades.append(active_trade)
                active_trade = None
                continue

            # 4 consecutive doji exit
            if consec_doji >= 4:
                active_trade.close(ts, spot_now, current_premium, "4_DOJI_EXIT")
                trades.append(active_trade)
                active_trade = None
                continue

            # Support touched
            if active_trade.support is not None and row["low"] <= active_trade.support:
                active_trade.close(ts, spot_now, current_premium, "SUPPORT_TOUCH")
                trades.append(active_trade)
                active_trade = None
                continue

            # Time exit: 3 candles (15 min) — same as V3's proven timing
            if active_trade.candles_held > V4_EXIT_CANDLES:
                active_trade.close(ts, spot_now, current_premium, "TIME_EXIT_15MIN")
                trades.append(active_trade)
                active_trade = None
                continue

            continue

        # ── ENTRY LOGIC (V3's resistance rejection, skip doji) ──────────
        if not in_trading_window(ts):
            continue
        if ts.time() > LAST_ENTRY_TIME:
            continue
        if i < 2 or i + 1 >= len(df):
            continue

        # ── S/R cache (same as V3)
        candles_since_sr_calc += 1
        if candles_since_sr_calc >= SR_RECALC_INTERVAL:
            cached_resistance = find_resistance(df, i)
            cached_support = find_support(df, i)
            candles_since_sr_calc = 0

        resistance = cached_resistance
        support = cached_support

        # ── Persistent invalidation
        if resistance is not None:
            res_bin = int(resistance)
            if row["close"] > resistance and is_green(row):
                invalidated_levels.add(res_bin)
            if res_bin in invalidated_levels:
                resistance = None

        # ── RESISTANCE REJECTION ONLY ───────────────────────────────────
        if resistance is None:
            dbg["no_resistance"] += 1
            continue

        prev = df.iloc[i - 1]
        near_resist_tol = resistance * NEAR_RESISTANCE_PCT
        prev_near_resistance = (
            abs(prev["high"] - resistance) <= near_resist_tol
            or abs(prev["close"] - resistance) <= near_resist_tol
        )
        if not (is_green(prev) and prev_near_resistance and is_red(row)):
            dbg["no_pattern"] += 1
            continue

        # ── Premium check
        next_row = df.iloc[i + 1]
        test_spot = next_row["open"]
        test_strike = get_nearest_otm_put_strike(test_spot)
        test_dte = days_to_nearest_expiry(df.index[i + 1])
        test_prem = bs_put_price(test_spot, test_strike, test_dte)

        if test_prem > V4_MAX_ENTRY_PREMIUM:
            dbg["high_prem"] += 1
            continue

        # ── Validate support below entry
        valid_support = support if (support is not None and support < test_spot) else None

        dbg["passed"] += 1

        active_trade = TradeV4(
            entry_time=df.index[i + 1],
            entry_spot=test_spot,
            strike=test_strike,
            entry_premium=test_prem,
            days_to_exp=test_dte,
            entry_reason="RESISTANCE_REJECTION",
            resistance=resistance,
            support=valid_support,
            num_lots=num_lots,
        )

    # Close any remaining open trade
    if active_trade is not None:
        last = df.iloc[-1]
        dte = days_to_nearest_expiry(df.index[-1])
        prem = bs_put_price(last["close"], active_trade.strike, dte)
        active_trade.close(df.index[-1], last["close"], prem, "END_OF_DATA")
        trades.append(active_trade)

    if debug:
        print(f"\n  V4 FILTER DEBUG:")
        for k, v in dbg.items():
            print(f"    {k:20s}: {v}")

    return trades


# ──────────────────────────────────────────────────────────────────────────────
# V1 BACKTEST (original logic, for comparison)
# ──────────────────────────────────────────────────────────────────────────────
def compute_bollinger_bands_v1(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["BB_middle"] = df["close"].rolling(window=BB_PERIOD).mean()
    rolling_std = df["close"].rolling(window=BB_PERIOD).std()
    df["BB_upper"] = df["BB_middle"] + BB_STD * rolling_std
    df["BB_lower"] = df["BB_middle"] - BB_STD * rolling_std
    return df


def find_resistance_v1(df, end_idx):
    start_idx = max(0, end_idx - SR_LOOKBACK)
    window = df.iloc[start_idx:end_idx + 1]
    green_highs = []
    for ii in range(len(window)):
        r = window.iloc[ii]
        if is_green(r):
            green_highs.append(r["high"])
    if len(green_highs) < SR_MIN_TOUCHES:
        return None
    green_highs = sorted(green_highs)
    best_level = None
    best_count = 0
    for h in green_highs:
        tol = h * SR_TOLERANCE_PCT
        count = sum(1 for x in green_highs if abs(x - h) <= tol)
        if count > best_count:
            best_count = count
            best_level = h
    if best_count < SR_MIN_TOUCHES:
        return None
    bb_upper = df.iloc[end_idx]["BB_upper"]
    if pd.isna(bb_upper):
        return None
    if abs(best_level - bb_upper) / bb_upper > BB_ALIGN_TOLERANCE_PCT:
        return None
    return best_level


def find_support_v1(df, end_idx):
    start_idx = max(0, end_idx - SR_LOOKBACK)
    window = df.iloc[start_idx:end_idx + 1]
    red_lows = []
    for ii in range(len(window)):
        r = window.iloc[ii]
        if is_red(r):
            red_lows.append(r["low"])
    if len(red_lows) < SR_MIN_TOUCHES:
        return None
    red_lows = sorted(red_lows)
    best_level = None
    best_count = 0
    for lo in red_lows:
        tol = lo * SR_TOLERANCE_PCT
        count = sum(1 for x in red_lows if abs(x - lo) <= tol)
        if count > best_count:
            best_count = count
            best_level = lo
    if best_count < SR_MIN_TOUCHES:
        return None
    return best_level


def run_backtest_v1(df: pd.DataFrame) -> list[Trade]:
    """Original V1 logic for side-by-side comparison."""
    df = compute_bollinger_bands_v1(df)
    trades: list[Trade] = []
    active_trade: Trade | None = None
    consec_doji = 0

    for i in range(BB_PERIOD, len(df)):
        row = df.iloc[i]
        ts = df.index[i]

        if is_doji(row):
            consec_doji += 1
        else:
            consec_doji = 0

        if active_trade is not None:
            active_trade.candles_held += 1
            spot_now = row["close"]
            dte = days_to_nearest_expiry(ts)
            current_premium = bs_put_price(spot_now, active_trade.strike, dte)

            if consec_doji >= 4:
                active_trade.close(ts, spot_now, current_premium, "4_DOJI_EXIT")
                trades.append(active_trade)
                active_trade = None
                continue
            if active_trade.support is not None and row["low"] <= active_trade.support:
                active_trade.close(ts, spot_now, current_premium, "SUPPORT_TOUCH")
                trades.append(active_trade)
                active_trade = None
                continue
            if active_trade.candles_held >= EXIT_CANDLES:
                active_trade.close(ts, spot_now, current_premium, "TIME_EXIT_15MIN")
                trades.append(active_trade)
                active_trade = None
                continue
            continue

        if not in_trading_window(ts):
            continue
        if i < 2:
            continue

        resistance = find_resistance_v1(df, i)
        support = find_support_v1(df, i)

        if resistance is not None and row["close"] > resistance and is_green(row):
            continue

        if resistance is not None:
            prev = df.iloc[i - 1]
            near_resist_tol = resistance * 0.003
            prev_near_resistance = abs(prev["high"] - resistance) <= near_resist_tol or \
                                   abs(prev["close"] - resistance) <= near_resist_tol
            if is_green(prev) and prev_near_resistance and is_red(row):
                if i + 1 < len(df):
                    next_row = df.iloc[i + 1]
                    entry_spot = next_row["open"]
                    strike = get_nearest_otm_put_strike(entry_spot)
                    dte = days_to_nearest_expiry(df.index[i + 1])
                    entry_prem = bs_put_price(entry_spot, strike, dte)
                    active_trade = Trade(
                        entry_time=df.index[i + 1], entry_spot=entry_spot,
                        strike=strike, entry_premium=entry_prem,
                        days_to_exp=dte, entry_reason="RESISTANCE_REJECTION",
                        resistance=resistance, support=support,
                    )
                    continue

        if consec_doji >= 4:
            continue
        if 2 <= consec_doji <= 3:
            bb_mid = row["BB_middle"]
            if pd.notna(bb_mid):
                near_mid_tol = bb_mid * 0.003
                if abs(row["close"] - bb_mid) <= near_mid_tol:
                    if i + 1 < len(df):
                        next_row = df.iloc[i + 1]
                        entry_spot = next_row["open"]
                        strike = get_nearest_otm_put_strike(entry_spot)
                        dte = days_to_nearest_expiry(df.index[i + 1])
                        entry_prem = bs_put_price(entry_spot, strike, dte)
                        active_trade = Trade(
                            entry_time=df.index[i + 1], entry_spot=entry_spot,
                            strike=strike, entry_premium=entry_prem,
                            days_to_exp=dte, entry_reason="DOJI_BB_MIDDLE",
                            resistance=resistance, support=support,
                        )
                        continue

    if active_trade is not None:
        last = df.iloc[-1]
        dte = days_to_nearest_expiry(df.index[-1])
        prem = bs_put_price(last["close"], active_trade.strike, dte)
        active_trade.close(df.index[-1], last["close"], prem, "END_OF_DATA")
        trades.append(active_trade)

    return trades


# ──────────────────────────────────────────────────────────────────────────────
# REPORTING
# ──────────────────────────────────────────────────────────────────────────────
def compute_stats(trades, multi_lot=False) -> dict:
    if not trades:
        return {}
    pnls = [t.pnl for t in trades]

    if multi_lot:
        # V4: use total P&L across all lots
        lot_pnls = [t.pnl_total for t in trades]
        net_pnls = [t.net_pnl_total for t in trades]
    else:
        lot_pnls = [t.pnl_per_lot for t in trades]
        net_pnls = [t.net_pnl_per_lot for t in trades]

    txn_costs = [t.txn_costs for t in trades]

    winners = [p for p in pnls if p > 0]
    losers = [p for p in pnls if p <= 0]
    net_winners = [p for p in net_pnls if p > 0]
    net_losers = [p for p in net_pnls if p <= 0]

    cumulative_gross = np.cumsum(lot_pnls)
    cumulative_net = np.cumsum(net_pnls)
    peak_gross = np.maximum.accumulate(cumulative_gross)
    peak_net = np.maximum.accumulate(cumulative_net)
    dd_gross = (cumulative_gross - peak_gross).min()
    dd_net = (cumulative_net - peak_net).min()

    total_lots = sum(getattr(t, 'num_lots', 1) for t in trades) if multi_lot else len(trades)

    return {
        "total_trades": len(trades),
        "total_lots_traded": total_lots,
        "win_rate": len(winners) / len(pnls) * 100,
        "net_win_rate": len(net_winners) / len(net_pnls) * 100,
        "total_pnl": sum(pnls),
        "total_lot_pnl": sum(lot_pnls),
        "total_net_pnl": sum(net_pnls),
        "total_txn_costs": sum(txn_costs),
        "avg_txn_cost": np.mean(txn_costs),
        "avg_win": np.mean(winners) if winners else 0,
        "avg_loss": np.mean(losers) if losers else 0,
        "max_win": max(pnls),
        "max_loss": min(pnls),
        "profit_factor": abs(sum(winners) / sum(losers)) if losers and sum(losers) != 0 else float("inf"),
        "net_profit_factor": abs(sum(net_winners) / sum(net_losers)) if net_losers and sum(net_losers) != 0 else float("inf"),
        "max_drawdown": dd_gross,
        "max_drawdown_net": dd_net,
        "avg_candles": np.mean([t.candles_held for t in trades]),
        "resist_entries": sum(1 for t in trades if t.entry_reason == "RESISTANCE_REJECTION"),
        "doji_entries": sum(1 for t in trades if t.entry_reason == "DOJI_BB_MIDDLE"),
    }


def print_report(trades, df: pd.DataFrame, version: str = "V2", multi_lot=False):
    if not trades:
        print(f"\n  NO TRADES GENERATED DURING BACKTEST ({version})")
        print(f"  Data range: {df.index[0]} -> {df.index[-1]}")
        return {}

    rows = []
    for t in trades:
        if multi_lot:
            num_lots = getattr(t, 'num_lots', 1)
            gross_val = t.pnl_total
            net_val = t.net_pnl_total
        else:
            num_lots = 1
            gross_val = t.pnl_per_lot
            net_val = t.net_pnl_per_lot

        row_dict = {
            "Entry Time": t.entry_time.strftime("%Y-%m-%d %H:%M"),
            "Spot": f"{t.entry_spot:.1f}",
            "K": t.strike,
            "EntPrem": f"{t.entry_premium:.2f}",
        }
        if multi_lot:
            row_dict["Lots"] = num_lots
        row_dict.update({
            "Exit Time": t.exit_time.strftime("%Y-%m-%d %H:%M") if t.exit_time else "OPEN",
            "ExPrem": f"{t.exit_premium:.2f}" if t.exit_premium else "-",
            "Gross": f"{gross_val:+,.0f}",
            "Costs": f"-{t.txn_costs:.0f}",
            "Net": f"{net_val:+,.0f}",
            "#": t.candles_held,
            "Exit": (t.exit_reason or "-")[:12],
        })
        rows.append(row_dict)

    trade_df = pd.DataFrame(rows)
    stats = compute_stats(trades, multi_lot=multi_lot)

    exit_counts = {}
    for t in trades:
        r = t.exit_reason or "UNKNOWN"
        exit_counts[r] = exit_counts.get(r, 0) + 1

    print(f"\n{'=' * 90}")
    print(f"  NIFTY50 PUT OPTIONS BACKTEST REPORT ({version})")
    print(f"{'=' * 90}")
    print(f"\n  Data Range        : {df.index[0].strftime('%Y-%m-%d')} -> {df.index[-1].strftime('%Y-%m-%d')}")
    print(f"  Total Candles     : {len(df)}")
    trading_candles = sum(1 for ts in df.index if in_trading_window(ts))
    print(f"  Candles in Windows: {trading_candles}")
    print(f"  Lot Size          : {LOT_SIZE}")
    if multi_lot:
        print(f"  Capital           : Rs {CAPITAL:,.0f}")
        print(f"  Total Lots Traded : {stats.get('total_lots_traded', 'N/A')}")

    print(f"\n{'─' * 90}")
    print("  TRADE LOG")
    print(f"{'─' * 90}")
    print(tabulate(trade_df, headers="keys", tablefmt="simple", showindex=False))

    n_gross_winners = int(stats['win_rate'] * stats['total_trades'] / 100)
    n_net_winners = int(stats['net_win_rate'] * stats['total_trades'] / 100)

    print(f"\n{'─' * 90}")
    print("  SUMMARY STATISTICS")
    print(f"{'─' * 90}")
    summary = [
        ["Total Trades", stats["total_trades"]],
        ["", ""],
        ["--- GROSS (before costs) ---", ""],
        ["Winners (gross)", f"{n_gross_winners} ({stats['win_rate']:.1f}%)"],
        ["Total P&L (gross)", f"Rs{stats['total_lot_pnl']:+,.0f}"],
        ["Profit Factor (gross)", f"{stats['profit_factor']:.2f}"],
        ["Max Drawdown (gross)", f"Rs{stats['max_drawdown']:+,.0f}"],
        ["", ""],
        ["--- TRANSACTION COSTS ---", ""],
        ["Total Costs", f"Rs{stats['total_txn_costs']:,.0f}"],
        ["Avg Cost/Trade", f"Rs{stats['avg_txn_cost']:,.0f}"],
        ["Costs as % of Gross P&L", f"{abs(stats['total_txn_costs'] / stats['total_lot_pnl']) * 100:.1f}%" if stats['total_lot_pnl'] != 0 else "N/A"],
        ["", ""],
        ["--- NET (after all costs) ---", ""],
        ["Winners (net)", f"{n_net_winners} ({stats['net_win_rate']:.1f}%)"],
        ["Total P&L (NET)", f"Rs{stats['total_net_pnl']:+,.0f}"],
        ["Profit Factor (net)", f"{stats['net_profit_factor']:.2f}"],
        ["Max Drawdown (net)", f"Rs{stats['max_drawdown_net']:+,.0f}"],
        ["", ""],
        ["--- DETAILS ---", ""],
        ["Avg Win (points)", f"{stats['avg_win']:+.2f}"],
        ["Avg Loss (points)", f"{stats['avg_loss']:+.2f}"],
        ["Max Win (points)", f"{stats['max_win']:+.2f}"],
        ["Max Loss (points)", f"{stats['max_loss']:+.2f}"],
        ["Avg Holding (candles)", f"{stats['avg_candles']:.1f}"],
        ["Resistance Entries", stats["resist_entries"]],
        ["Doji + BB Mid Entries", stats["doji_entries"]],
    ]
    print(tabulate(summary, tablefmt="simple"))

    print(f"\n  Exit Reason Breakdown:")
    for reason, count in sorted(exit_counts.items()):
        print(f"    {reason:25s} : {count}")

    # Charts
    if multi_lot:
        lot_pnls = [t.pnl_total for t in trades]
        net_pnls = [t.net_pnl_total for t in trades]
    else:
        lot_pnls = [t.pnl_per_lot for t in trades]
        net_pnls = [t.net_pnl_per_lot for t in trades]
    cum_gross = np.cumsum(lot_pnls)
    cum_net = np.cumsum(net_pnls)
    x = range(1, len(cum_gross) + 1)

    fig, axes = plt.subplots(2, 1, figsize=(14, 8), gridspec_kw={"height_ratios": [3, 1]})

    ax1 = axes[0]
    ax1.plot(x, cum_gross, "b-o", markersize=4, label="Gross P&L")
    ax1.plot(x, cum_net, "r-s", markersize=4, label="Net P&L (after costs)")
    ax1.fill_between(x, cum_net, 0, where=cum_net >= 0, color="green", alpha=0.08)
    ax1.fill_between(x, cum_net, 0, where=cum_net < 0, color="red", alpha=0.08)
    ax1.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax1.set_title(f"Equity Curve - {version} (Gross vs Net P&L per Lot)", fontsize=13)
    ax1.set_xlabel("Trade #")
    ax1.set_ylabel("Cumulative P&L (Rs)")
    ax1.legend()
    ax1.grid(True, alpha=0.3)

    ax2 = axes[1]
    bar_width = 0.35
    x_arr = np.arange(1, len(lot_pnls) + 1)
    colors_gross = ["green" if p > 0 else "red" for p in lot_pnls]
    colors_net = ["darkgreen" if p > 0 else "darkred" for p in net_pnls]
    ax2.bar(x_arr - bar_width / 2, lot_pnls, bar_width, color=colors_gross, alpha=0.5, label="Gross")
    ax2.bar(x_arr + bar_width / 2, net_pnls, bar_width, color=colors_net, alpha=0.7, label="Net")
    ax2.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax2.set_title("Per-Trade P&L (Gross vs Net)", fontsize=13)
    ax2.set_xlabel("Trade #")
    ax2.set_ylabel("P&L (Rs)")
    ax2.legend(fontsize=9)
    ax2.grid(True, alpha=0.3)

    plt.tight_layout()
    out_path = Path(f"backtest_results_{version.lower()}.png")
    plt.savefig(out_path, dpi=150)
    plt.close()
    print(f"\n  Equity curve saved to: {out_path.resolve()}")

    csv_path = Path(f"backtest_trades_{version.lower()}.csv")
    trade_df.to_csv(csv_path, index=False)
    print(f"  Trade log saved to: {csv_path.resolve()}")
    print(f"\n{'=' * 90}")

    return stats


def print_comparison(v1_stats: dict, v2_stats: dict):
    if not v1_stats or not v2_stats:
        return
    print(f"\n{'=' * 90}")
    print("  V1 vs V2 COMPARISON")
    print(f"{'=' * 90}")

    metrics = [
        ("total_trades", "Total Trades", "{}"),
        ("win_rate", "Win Rate (%)", "{:.1f}"),
        ("total_pnl", "Total P&L (pts)", "{:+.2f}"),
        ("total_lot_pnl", "Total P&L/Lot (Rs)", "{:+,.0f}"),
        ("profit_factor", "Profit Factor", "{:.2f}"),
        ("max_drawdown", "Max Drawdown (Rs)", "{:+,.0f}"),
        ("avg_candles", "Avg Holding (candles)", "{:.1f}"),
    ]

    rows = []
    for key, label, fmt_str in metrics:
        v1 = v1_stats.get(key, 0)
        v2 = v2_stats.get(key, 0)
        diff = v2 - v1
        if key in ("max_drawdown",):
            # For drawdown, less negative = better
            better = diff > 0
        elif key in ("total_trades",):
            better = None  # neutral
        else:
            better = diff > 0

        if better is True:
            arrow = " [+]"
        elif better is False:
            arrow = " [-]"
        else:
            arrow = ""

        rows.append([label, fmt_str.format(v1), fmt_str.format(v2),
                      f"{diff:+.1f}{arrow}" if isinstance(diff, float) else f"{diff:+}{arrow}"])

    print(tabulate(rows, headers=["Metric", "V1", "V2", "Delta"], tablefmt="simple"))
    print(f"{'=' * 90}")


# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="NIFTY50 PUT Options Backtest V2")
    parser.add_argument("--csv", type=str, default=None,
                        help="Path to CSV file with 5-min OHLCV data")
    parser.add_argument("--start", type=str, default="2025-03-17")
    parser.add_argument("--end", type=str, default="2026-03-17")
    parser.add_argument("--compare", action="store_true",
                        help="Run both V1 and V2 and print comparison")
    args = parser.parse_args()

    print("=" * 90)
    print("  NIFTY50 PUT OPTIONS BACKTEST ENGINE -- V4 (High Conviction + Capital Sizing)")
    print(f"  Capital: Rs {CAPITAL:,.0f} | Lot Size: {LOT_SIZE}")
    print("=" * 90)

    if args.csv:
        print(f"\n  Loading data from CSV: {args.csv}")
        df = load_data_csv(args.csv)
    else:
        print(f"\n  Fetching data from yfinance: {args.start} -> {args.end}")
        print("  Note: yfinance only provides ~60 days of 5-min data.")
        print("  For full 1-year backtest, use --csv with your own data.\n")
        df = load_data_yfinance(args.start, args.end)

    if df.empty:
        print("ERROR: No data loaded. Exiting.")
        sys.exit(1)

    print(f"  Loaded {len(df)} candles from {df.index[0]} to {df.index[-1]}")

    market_open = time(9, 15)
    market_close = time(15, 30)
    df = df[(df.index.time >= market_open) & (df.index.time <= market_close)]
    print(f"  After market-hours filter: {len(df)} candles")

    if len(df) < BB_PERIOD + 10:
        print("ERROR: Not enough data.")
        sys.exit(1)

    # Run V4 (primary — high conviction + capital sizing)
    print(f"\n  Running V4 backtest (high conviction, Rs {CAPITAL:,.0f} capital)...")
    trades_v4 = run_backtest_v4(df, debug=True)
    print(f"  V4 complete: {len(trades_v4)} trades")
    v4_stats = print_report(trades_v4, df, "V4", multi_lot=True)

    # Run V4 again with limit-order costs (reduced slippage/spread)
    print(f"\n  Running V4 with LIMIT ORDER costs (reduced slippage)...")
    # Temporarily reduce slippage/spread for limit-order simulation
    saved_slip, saved_spread = SLIPPAGE_PTS, SPREAD_PTS
    import builtins
    # Monkey-patch globals for this run
    globals()['SLIPPAGE_PTS'] = 0.5   # limit orders: much less slippage
    globals()['SPREAD_PTS'] = 0.75    # limit orders: capture half the spread
    trades_v4_limit = run_backtest_v4(df, debug=False)
    # Re-calculate transaction costs with new slippage/spread values
    for t in trades_v4_limit:
        if t.exit_premium is not None:
            t.txn_costs = compute_transaction_costs(
                t.entry_premium, t.exit_premium, LOT_SIZE * t.num_lots
            )
            t.net_pnl_per_lot = t.pnl_per_lot - (t.txn_costs / t.num_lots)
            t.net_pnl_total = t.pnl_total - t.txn_costs
    print(f"  V4-Limit complete: {len(trades_v4_limit)} trades")
    v4_limit_stats = print_report(trades_v4_limit, df, "V4_LIMIT", multi_lot=True)
    # Restore original values
    globals()['SLIPPAGE_PTS'] = saved_slip
    globals()['SPREAD_PTS'] = saved_spread

    # Also run V3 for comparison
    print("\n  Running V3 backtest (loss-optimized, 1 lot)...")
    trades_v3 = run_backtest_v3(df)
    print(f"  V3 complete: {len(trades_v3)} trades")
    v3_stats = print_report(trades_v3, df, "V3")

    # V3 vs V4 vs V4-Limit comparison
    if v4_stats and v3_stats:
        print(f"\n{'=' * 90}")
        print("  V3 vs V4 vs V4-LIMIT COMPARISON")
        print(f"{'=' * 90}")
        print(f"  V3     : 1 lot, market orders")
        print(f"  V4     : {V4_FIXED_LOTS} lots (Rs {CAPITAL:,.0f} capital), market orders")
        print(f"  V4-LMT : {V4_FIXED_LOTS} lots (Rs {CAPITAL:,.0f} capital), LIMIT orders (reduced slippage)")
        print()

        metrics = [
            ("total_trades", "Total Trades", "{}"),
            ("total_lots_traded", "Total Lots Traded", "{}"),
            ("", "", ""),
            ("win_rate", "Win Rate (gross %)", "{:.1f}"),
            ("total_lot_pnl", "P&L Gross (Rs)", "{:+,.0f}"),
            ("profit_factor", "Profit Factor (gross)", "{:.2f}"),
            ("max_drawdown", "Max DD (gross Rs)", "{:+,.0f}"),
            ("", "", ""),
            ("total_txn_costs", "Total Costs (Rs)", "{:,.0f}"),
            ("avg_txn_cost", "Avg Cost/Trade (Rs)", "{:,.0f}"),
            ("", "", ""),
            ("net_win_rate", "Win Rate (net %)", "{:.1f}"),
            ("total_net_pnl", "P&L NET (Rs)", "{:+,.0f}"),
            ("net_profit_factor", "Profit Factor (net)", "{:.2f}"),
            ("max_drawdown_net", "Max DD (net Rs)", "{:+,.0f}"),
        ]

        v4l = v4_limit_stats if v4_limit_stats else {}
        rows = []
        for key, label, fmt_str in metrics:
            if key == "":
                rows.append(["", "", "", ""])
                continue
            v3_val = v3_stats.get(key, 0)
            v4_val = v4_stats.get(key, 0)
            v4l_val = v4l.get(key, 0)
            rows.append([label, fmt_str.format(v3_val), fmt_str.format(v4_val), fmt_str.format(v4l_val)])

        print(tabulate(rows, headers=["Metric", "V3 (1 lot)", "V4 (market)", "V4 (limit)"], tablefmt="simple"))

        # ROI calculation
        if v4_stats.get("total_net_pnl") and v4l.get("total_net_pnl"):
            print(f"\n  Return on Capital (Rs {CAPITAL:,.0f}):")
            print(f"    V4 Market Orders : {v4_stats['total_net_pnl']/CAPITAL*100:+.1f}%")
            print(f"    V4 Limit Orders  : {v4l['total_net_pnl']/CAPITAL*100:+.1f}%")

        print(f"{'=' * 90}")

    # Run V1 and V2 for full comparison
    if args.compare:
        print("\n  Running V2 backtest...")
        trades_v2 = run_backtest(df)
        print(f"  V2 complete: {len(trades_v2)} trades")
        v2_stats = print_report(trades_v2, df, "V2")

        print("\n  Running V1 backtest (original)...")
        trades_v1 = run_backtest_v1(df)
        print(f"  V1 complete: {len(trades_v1)} trades")
        v1_stats = print_report(trades_v1, df, "V1")

        print(f"\n{'=' * 90}")
        print("  ALL VERSIONS COMPARISON")
        print(f"{'=' * 90}")

        metrics = [
            ("total_trades", "Total Trades", "{}"),
            ("", "", ""),
            ("win_rate", "Win Rate (gross %)", "{:.1f}"),
            ("total_lot_pnl", "P&L Gross (Rs)", "{:+,.0f}"),
            ("profit_factor", "Profit Factor (gross)", "{:.2f}"),
            ("max_drawdown", "Max DD (gross Rs)", "{:+,.0f}"),
            ("", "", ""),
            ("total_txn_costs", "Total Costs (Rs)", "{:,.0f}"),
            ("", "", ""),
            ("net_win_rate", "Win Rate (net %)", "{:.1f}"),
            ("total_net_pnl", "P&L NET (Rs)", "{:+,.0f}"),
            ("net_profit_factor", "Profit Factor (net)", "{:.2f}"),
            ("max_drawdown_net", "Max DD (net Rs)", "{:+,.0f}"),
        ]

        rows = []
        for key, label, fmt_str in metrics:
            if key == "":
                rows.append(["", "", "", "", ""])
                continue
            v1 = v1_stats.get(key, 0)
            v2 = v2_stats.get(key, 0)
            v3 = v3_stats.get(key, 0)
            v4 = v4_stats.get(key, 0)
            rows.append([label, fmt_str.format(v1), fmt_str.format(v2), fmt_str.format(v3), fmt_str.format(v4)])

        print(tabulate(rows, headers=["Metric", "V1", "V2", "V3", "V4"], tablefmt="simple"))
        print(f"{'=' * 90}")


if __name__ == "__main__":
    main()
