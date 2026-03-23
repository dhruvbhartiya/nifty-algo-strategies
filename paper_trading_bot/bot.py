"""
NIFTY Paper Trading Bot — Main Engine (v2: WebSocket Real-Time)
Connects to ICICI Direct Breeze API with WebSocket streaming for
zero-latency price updates. Builds 5-min candles from live ticks.

Usage:
    python3 bot.py <session_token>

Architecture:
  - WebSocket thread: receives live NIFTY ticks (sub-second latency)
  - Candle builder: aggregates ticks into precise 5-min OHLCV candles
  - Strategy engines: process each completed candle immediately
  - Fallback: if WebSocket drops, auto-switches to polling historical API
  - Reconnect: auto-reconnects WebSocket on disconnect (max 5 retries)

Execution Timing:
  - Entry/exit signals use candle close prices (end of 5-min bar)
  - Actual execution at next candle's open (no lookahead)
  - For paper trading: "open" = first tick of next 5-min window
  - Max latency: < 1 second from candle close to strategy decision
"""
import sys
import os
import json
import time as time_mod
import logging
import threading
from collections import deque
from datetime import datetime, time, timedelta
from pathlib import Path

# IST offset — EC2 runs UTC, market runs IST (UTC+5:30)
IST_OFFSET = timedelta(hours=5, minutes=30)

def now_ist():
    """Current time in IST (Indian Standard Time)."""
    return datetime.utcnow() + IST_OFFSET
import numpy as np
import traceback

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from breeze_connect import BreezeConnect
from bot_config import *
from bot_pricing import (
    bs_call_price, bs_put_price, get_atm_strike,
    days_to_nearest_expiry, estimate_iv_from_candles,
    compute_straddle_costs, compute_put_costs,
)
from bot_notifier import (
    notify_trade_entry, notify_trade_exit,
    notify_daily_summary, notify_bot_start, notify_error,
)

# ──────────────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────────────
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("{}/bot_{}.log".format(LOG_DIR, datetime.now().strftime('%Y%m%d'))),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("bot")


# ──────────────────────────────────────────────────────────────────────
# REAL-TIME CANDLE BUILDER (from WebSocket ticks)
# ──────────────────────────────────────────────────────────────────────
class LiveCandleBuilder:
    """
    Builds 5-minute OHLCV candles from real-time tick data.
    Emits a completed candle at each 5-min boundary with zero delay.
    """
    def __init__(self, interval_min=5):
        self.interval = interval_min
        self.current_candle = None
        self.completed_candles = deque(maxlen=50)  # buffer of recent completed candles
        self.last_tick_price = None
        self.tick_count = 0
        self._lock = threading.Lock()

    def _candle_start(self, ts):
        """Get the 5-min boundary start time for a given timestamp."""
        minute = (ts.minute // self.interval) * self.interval
        return ts.replace(minute=minute, second=0, microsecond=0)

    def on_tick(self, price, volume, tick_time):
        """
        Process a single tick. Returns a completed candle dict if
        a 5-min boundary was crossed, otherwise None.
        """
        with self._lock:
            self.last_tick_price = price
            self.tick_count += 1
            candle_start = self._candle_start(tick_time)

            # First tick ever or new candle period
            if self.current_candle is None or candle_start > self.current_candle["timestamp"]:
                completed = None
                if self.current_candle is not None:
                    # Close the previous candle
                    completed = dict(self.current_candle)
                    self.completed_candles.append(completed)

                # Start new candle
                self.current_candle = {
                    "timestamp": candle_start,
                    "open": price,
                    "high": price,
                    "low": price,
                    "close": price,
                    "volume": volume,
                    "ticks": 1,
                }
                return completed

            # Update current candle
            self.current_candle["high"] = max(self.current_candle["high"], price)
            self.current_candle["low"] = min(self.current_candle["low"], price)
            self.current_candle["close"] = price
            self.current_candle["volume"] += volume
            self.current_candle["ticks"] += 1
            return None

    def get_current_price(self):
        """Get the most recent tick price (for real-time P&L monitoring)."""
        return self.last_tick_price

    def get_forming_candle(self):
        """Get the currently forming (incomplete) candle."""
        with self._lock:
            return dict(self.current_candle) if self.current_candle else None


# ──────────────────────────────────────────────────────────────────────
# TRADE CLASSES
# ──────────────────────────────────────────────────────────────────────
class StraddlePaperTrade:
    def __init__(self, entry_time, entry_spot, strike, call_prem, put_prem,
                 iv, dte, lots, signal):
        self.strategy = "STRADDLE"
        self.entry_time = entry_time
        self.entry_spot = entry_spot
        self.strike = strike
        self.call_entry = call_prem
        self.put_entry = put_prem
        self.combined_entry = call_prem + put_prem
        self.iv = iv
        self.dte = dte
        self.lots = lots
        self.signal = signal
        self.candles_held = 0
        self.peak_pnl_pct = 0.0
        self._exit_pending = False
        self._exit_reason = None

        self.exit_time = None
        self.exit_spot = None
        self.call_exit = None
        self.put_exit = None
        self.combined_exit = None
        self.exit_reason = None
        self.gross_pnl = 0.0
        self.txn_costs = 0.0
        self.net_pnl = 0.0

    def close(self, exit_time, exit_spot, call_ex, put_ex, reason):
        self.exit_time = exit_time
        self.exit_spot = exit_spot
        self.call_exit = call_ex
        self.put_exit = put_ex
        self.combined_exit = call_ex + put_ex
        self.exit_reason = reason

        qty = LOT_SIZE * self.lots
        self.gross_pnl = (self.combined_exit - self.combined_entry) * qty
        self.txn_costs = compute_straddle_costs(
            self.call_entry, self.call_exit,
            self.put_entry, self.put_exit, qty
        )
        self.net_pnl = self.gross_pnl - self.txn_costs

    def to_dict(self):
        return {
            "strategy": self.strategy,
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time),
            "entry_spot": self.entry_spot,
            "exit_spot": self.exit_spot,
            "strike": self.strike,
            "direction": "STRADDLE (C+P)",
            "call_entry": round(self.call_entry, 2),
            "put_entry": round(self.put_entry, 2),
            "call_exit": round(self.call_exit, 2) if self.call_exit else None,
            "put_exit": round(self.put_exit, 2) if self.put_exit else None,
            "combined_entry": round(self.combined_entry, 2),
            "combined_exit": round(self.combined_exit, 2) if self.combined_exit else None,
            "lots": self.lots,
            "qty": self.lots * LOT_SIZE,
            "iv": round(self.iv, 4),
            "spot_move": round(abs(self.exit_spot - self.entry_spot), 1) if self.exit_spot else 0,
            "hold_min": self.candles_held * CANDLE_INTERVAL_MIN,
            "gross_pnl": round(self.gross_pnl, 2),
            "txn_costs": round(self.txn_costs, 2),
            "net_pnl": round(self.net_pnl, 2),
            "exit_reason": self.exit_reason,
            "signal": self.signal,
        }


class PutPaperTrade:
    def __init__(self, entry_time, entry_spot, strike, put_prem,
                 iv, dte, lots, signal, resistance_level):
        self.strategy = "V4_PUT"
        self.entry_time = entry_time
        self.entry_spot = entry_spot
        self.strike = strike
        self.put_entry = put_prem
        self.iv = iv
        self.dte = dte
        self.lots = lots
        self.signal = signal
        self.resistance_level = resistance_level
        self.candles_held = 0
        self._exit_pending = False
        self._exit_reason = None

        self.exit_time = None
        self.exit_spot = None
        self.put_exit = None
        self.exit_reason = None
        self.gross_pnl = 0.0
        self.txn_costs = 0.0
        self.net_pnl = 0.0

    def close(self, exit_time, exit_spot, put_ex, reason):
        self.exit_time = exit_time
        self.exit_spot = exit_spot
        self.put_exit = put_ex
        self.exit_reason = reason

        qty = LOT_SIZE * self.lots
        self.gross_pnl = (self.put_exit - self.put_entry) * qty
        self.txn_costs = compute_put_costs(self.put_entry, self.put_exit, qty)
        self.net_pnl = self.gross_pnl - self.txn_costs

    def to_dict(self):
        return {
            "strategy": self.strategy,
            "entry_time": str(self.entry_time),
            "exit_time": str(self.exit_time),
            "entry_spot": self.entry_spot,
            "exit_spot": self.exit_spot,
            "strike": self.strike,
            "direction": "PUT BUY",
            "put_entry": round(self.put_entry, 2),
            "put_exit": round(self.put_exit, 2) if self.put_exit else None,
            "combined_entry": round(self.put_entry, 2),
            "combined_exit": round(self.put_exit, 2) if self.put_exit else None,
            "lots": self.lots,
            "qty": self.lots * LOT_SIZE,
            "iv": round(self.iv, 4),
            "spot_move": round(abs(self.exit_spot - self.entry_spot), 1) if self.exit_spot else 0,
            "hold_min": self.candles_held * CANDLE_INTERVAL_MIN,
            "gross_pnl": round(self.gross_pnl, 2),
            "txn_costs": round(self.txn_costs, 2),
            "net_pnl": round(self.net_pnl, 2),
            "exit_reason": self.exit_reason,
            "signal": self.signal,
            "resistance": self.resistance_level,
        }


# ──────────────────────────────────────────────────────────────────────
# INDICATOR ENGINE
# ──────────────────────────────────────────────────────────────────────
class IndicatorEngine:
    """
    Maintains a rolling window of 5-min candles and calculates
    all volatility indicators in real-time. All trailing — no lookahead.
    """
    def __init__(self):
        self.candles = []
        self.max_candles = 500

    def add_candle(self, candle):
        self.candles.append(candle)
        if len(self.candles) > self.max_candles:
            self.candles = self.candles[-self.max_candles:]

    def get_closes(self):
        return [c["close"] for c in self.candles]

    def compute_atr(self, period):
        if len(self.candles) < period + 1:
            return None
        trs = []
        for i in range(-period, 0):
            c = self.candles[i]
            prev_close = self.candles[i - 1]["close"]
            tr = max(
                c["high"] - c["low"],
                abs(c["high"] - prev_close),
                abs(c["low"] - prev_close),
            )
            trs.append(tr)
        return np.mean(trs)

    def get_atr_ratio(self):
        fast = self.compute_atr(STRADDLE["ATR_FAST"])
        slow = self.compute_atr(STRADDLE["ATR_SLOW"])
        if fast is None or slow is None or slow == 0:
            return None
        return fast / slow

    def get_range_ratio(self):
        lookback = STRADDLE["RANGE_LOOKBACK"]
        if len(self.candles) < lookback + 1:
            return None
        ranges = [c["high"] - c["low"] for c in self.candles[-(lookback + 1):-1]]
        avg_range = np.mean(ranges)
        if avg_range == 0:
            return None
        current_range = self.candles[-1]["high"] - self.candles[-1]["low"]
        return current_range / avg_range

    def get_bb_width(self):
        period = STRADDLE["BB_PERIOD"]
        if len(self.candles) < period:
            return None
        # Use most recent candles (cross-day OK for BB calculation)
        closes = np.array([c["close"] for c in self.candles[-period:]])
        mid = np.mean(closes)
        std = np.std(closes, ddof=1)
        if mid == 0:
            return None
        upper = mid + STRADDLE["BB_STD"] * std
        lower = mid - STRADDLE["BB_STD"] * std
        return (upper - lower) / mid

    def get_bb_values(self):
        period = V4_PUT["BB_PERIOD"]
        if len(self.candles) < period:
            return None, None, None
        closes = np.array([c["close"] for c in self.candles[-period:]])
        mid = np.mean(closes)
        std = np.std(closes, ddof=1)
        upper = mid + V4_PUT["BB_STD"] * std
        lower = mid - V4_PUT["BB_STD"] * std
        return upper, mid, lower

    def find_resistance(self):
        lookback = V4_PUT["SR_LOOKBACK"]
        if len(self.candles) < lookback:
            return None
        highs = [c["high"] for c in self.candles[-lookback:]]
        current = self.candles[-1]["close"]
        bin_size = current * V4_PUT["SR_BIN_SIZE_PCT"]
        bins = {}
        for h in highs:
            b = round(h / bin_size)
            bins[b] = bins.get(b, 0) + 1
        best_level = None
        best_touches = 0
        for b, count in bins.items():
            level = b * bin_size
            if level > current and count >= V4_PUT["SR_MIN_TOUCHES"]:
                if best_level is None or count > best_touches:
                    best_level = level
                    best_touches = count
        return best_level

    def get_latest(self):
        return self.candles[-1] if self.candles else None


# ──────────────────────────────────────────────────────────────────────
# BREEZE DATA FETCHER (fallback + warmup)
# ──────────────────────────────────────────────────────────────────────
def ist_to_breeze_utc(ist_dt):
    """Convert IST datetime to UTC ISO string for Breeze API."""
    utc_dt = ist_dt - IST_OFFSET
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")

def fetch_historical_candles(breeze, from_dt, to_dt):
    """Fetch 5-min NIFTY candles from Breeze historical data API."""
    try:
        data = breeze.get_historical_data_v2(
            interval="5minute",
            from_date=ist_to_breeze_utc(from_dt),
            to_date=ist_to_breeze_utc(to_dt),
            stock_code="NIFTY",
            exchange_code="NSE",
            product_type="cash",
        )
        if not data or "Success" not in data or not data["Success"]:
            logger.warning("No historical data returned for {} to {}".format(from_dt, to_dt))
            return []
        candles = []
        for row in data["Success"]:
            ts = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            candles.append({
                "timestamp": ts,
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": int(row.get("volume", 0)),
            })
        return sorted(candles, key=lambda x: x["timestamp"])
    except Exception as e:
        logger.error("Historical data fetch error: {}".format(e))
        return []


def get_latest_candle(breeze):
    """Fallback: Get live NIFTY price via get_quotes API."""
    try:
        quotes = breeze.get_quotes(stock_code="NIFTY", exchange_code="NSE", product_type="cash")
        if not quotes or "Success" not in quotes or not quotes["Success"]:
            return None
        q = quotes["Success"][0]
        ltp = float(q["ltp"])
        if ltp <= 0:
            return None
        return {"ltp": ltp, "open": float(q.get("open", ltp)), "high": float(q.get("high", ltp)), "low": float(q.get("low", ltp))}
    except Exception as e:
        logger.error("Quote polling error: {}".format(e))
        return None

# ──────────────────────────────────────────────────────────────────────
# TRADE PERSISTENCE
# ──────────────────────────────────────────────────────────────────────
def load_trades():
    if os.path.exists(TRADE_LOG_JSON):
        with open(TRADE_LOG_JSON) as f:
            return json.load(f)
    return {"straddle": [], "v4_put": [], "summary": {"cum_pnl": 0, "total_trades": 0}}


def save_trades(trades_data):
    with open(TRADE_LOG_JSON, "w") as f:
        json.dump(trades_data, f, indent=2, default=str)


# ──────────────────────────────────────────────────────────────────────
# STRADDLE STRATEGY ENGINE
# ──────────────────────────────────────────────────────────────────────
class StraddleEngine:
    def __init__(self, indicators):
        self.indicators = indicators
        self.active_trade = None
        self.signal_pending = False
        self.signal_info = ""
        self.cooldown = 0
        self.trades_today = []

    def in_trading_window(self, ts):
        t = ts.time()
        s = STRADDLE
        return ((s["MORNING_START"] <= t <= s["MORNING_END"]) or
                (s["AFTERNOON_START"] <= t <= s["AFTERNOON_END"]))

    def process_candle(self, candle, trades_data):
        ts = candle["timestamp"]
        s = STRADDLE

        if self.cooldown > 0:
            self.cooldown -= 1

        # ── EXECUTE PENDING ENTRY ──
        if self.signal_pending and self.active_trade is None:
            self.signal_pending = False
            entry_spot = candle["open"]
            strike = get_atm_strike(entry_spot)

            if abs(entry_spot - strike) > s["ATM_MAX_DISTANCE"]:
                logger.info("[STRADDLE] Signal skipped — spot {:.0f} too far from ATM {}".format(entry_spot, strike))
                return

            dte = days_to_nearest_expiry(ts)
            iv = estimate_iv_from_candles(self.indicators.get_closes())
            call_prem = bs_call_price(entry_spot, strike, dte, sigma=iv)
            put_prem = bs_put_price(entry_spot, strike, dte, sigma=iv)

            self.active_trade = StraddlePaperTrade(
                entry_time=ts, entry_spot=entry_spot, strike=strike,
                call_prem=call_prem, put_prem=put_prem,
                iv=iv, dte=dte, lots=LOTS_PER_TRADE, signal=self.signal_info,
            )

            logger.info("[STRADDLE] ENTRY: Strike={} Spot={:.0f} C+P={:.1f} IV={:.1%} Signal={}".format(
                strike, entry_spot, call_prem + put_prem, iv, self.signal_info))

            notify_trade_entry("STRADDLE", {
                "entry_time": str(ts), "entry_spot": "{:.0f}".format(entry_spot),
                "strike": strike, "direction": "STRADDLE (ATM CALL + PUT)",
                "combined_premium": call_prem + put_prem, "lots": LOTS_PER_TRADE,
                "iv": iv, "signal": self.signal_info,
            })
            return

        # ── EXIT LOGIC ──
        if self.active_trade is not None:
            trade = self.active_trade
            trade.candles_held += 1

            if trade._exit_pending:
                spot_exit = candle["open"]
                dte = days_to_nearest_expiry(ts)
                iv = estimate_iv_from_candles(self.indicators.get_closes()[:-1])
                call_ex = bs_call_price(spot_exit, trade.strike, dte, sigma=iv)
                put_ex = bs_put_price(spot_exit, trade.strike, dte, sigma=iv)
                trade.close(ts, spot_exit, call_ex, put_ex, trade._exit_reason)
                self._record_trade(trade, trades_data)
                self.active_trade = None
                self.cooldown = s["COOLDOWN_CANDLES"]
                return

            spot_now = candle["close"]
            dte = days_to_nearest_expiry(ts)
            iv = estimate_iv_from_candles(self.indicators.get_closes())
            call_now = bs_call_price(spot_now, trade.strike, dte, sigma=iv)
            put_now = bs_put_price(spot_now, trade.strike, dte, sigma=iv)
            combined_now = call_now + put_now
            pnl_pct = (combined_now - trade.combined_entry) / trade.combined_entry

            if pnl_pct > trade.peak_pnl_pct:
                trade.peak_pnl_pct = pnl_pct

            # Session exit — immediate
            if ts.time() >= s["FORCE_EXIT_TIME"]:
                trade.close(ts, spot_now, call_now, put_now, "SESSION_EXIT")
                self._record_trade(trade, trades_data)
                self.active_trade = None
                self.cooldown = s["COOLDOWN_CANDLES"]
                return

            exit_reason = None
            if pnl_pct >= s["PROFIT_TARGET_PCT"]:
                exit_reason = "PROFIT_TARGET"
            elif pnl_pct <= -s["STOP_LOSS_PCT"]:
                exit_reason = "STOP_LOSS"
            elif (trade.combined_entry >= s["CHEAP_STRADDLE_THRESH"] and
                    trade.peak_pnl_pct >= s["TRAIL_ACTIVATE_PCT"] and
                    pnl_pct <= trade.peak_pnl_pct * s["TRAIL_STOP_PCT"]):
                exit_reason = "TRAIL_STOP"
            elif (call_now >= trade.call_entry * s["ONE_LEG_MULT"] or
                    put_now >= trade.put_entry * s["ONE_LEG_MULT"]):
                exit_reason = "LEG_RUNNER"
            elif trade.candles_held >= s["MAX_HOLD_CANDLES"]:
                exit_reason = "TIME_EXIT_50MIN"

            if exit_reason:
                trade._exit_pending = True
                trade._exit_reason = exit_reason
            return

        # ── SIGNAL DETECTION ──
        if not self.in_trading_window(ts):
            logger.debug("[STRADDLE] ts={} NOT in trading window".format(ts))
            return
        if ts.time() > s["LAST_ENTRY_TIME"]:
            logger.debug("[STRADDLE] ts={} past LAST_ENTRY_TIME".format(ts))
            return
        if self.cooldown > 0:
            logger.debug("[STRADDLE] cooldown={}".format(self.cooldown))
            return

        atr_ratio = self.indicators.get_atr_ratio()
        range_ratio = self.indicators.get_range_ratio()
        bb_width = self.indicators.get_bb_width()
        logger.info("[STRADDLE] CHECK ts={} ATR={} Range={} BB={} (need ATR>={} Range>={} BB>={})".format(
            ts,
            "{:.2f}".format(atr_ratio) if atr_ratio else "None",
            "{:.2f}".format(range_ratio) if range_ratio else "None",
            "{:.4f}".format(bb_width) if bb_width else "None",
            s["ATR_RATIO_TRIGGER"], s["BREAKOUT_CANDLE_MULT"], s["BB_WIDTH_TRIGGER"]))

        if atr_ratio is None or range_ratio is None or bb_width is None:
            return

        vol_atr = atr_ratio >= s["ATR_RATIO_TRIGGER"]
        vol_breakout = range_ratio >= s["BREAKOUT_CANDLE_MULT"]
        vol_bb = bb_width >= s["BB_WIDTH_TRIGGER"]

        if vol_atr and (vol_breakout or vol_bb):
            self.signal_pending = True
            reasons = ["ATR:{:.2f}".format(atr_ratio)]
            if vol_breakout:
                reasons.append("Range:{:.1f}x".format(range_ratio))
            if vol_bb:
                reasons.append("BB:{:.4f}".format(bb_width))
            self.signal_info = " | ".join(reasons)
            logger.info("[STRADDLE] SIGNAL: {} — will enter on next candle".format(self.signal_info))

    def _record_trade(self, trade, trades_data):
        trade_dict = trade.to_dict()
        trades_data["straddle"].append(trade_dict)
        trades_data["summary"]["cum_pnl"] += trade.net_pnl
        trades_data["summary"]["total_trades"] += 1
        save_trades(trades_data)
        self.trades_today.append(trade)

        logger.info("[STRADDLE] EXIT: {} | Gross={:+,.0f} Net={:+,.0f} | Cum={:+,.0f}".format(
            trade.exit_reason, trade.gross_pnl, trade.net_pnl, trades_data['summary']['cum_pnl']))

        notify_trade_exit("STRADDLE", {
            **trade_dict,
            "cum_pnl": trades_data["summary"]["cum_pnl"],
            "roi": trades_data["summary"]["cum_pnl"] / CAPITAL * 100,
        })


# ──────────────────────────────────────────────────────────────────────
# V4 PUT STRATEGY ENGINE
# ──────────────────────────────────────────────────────────────────────
class V4PutEngine:
    def __init__(self, indicators):
        self.indicators = indicators
        self.active_trade = None
        self.signal_pending = False
        self.signal_info = ""
        self.pending_resistance = None
        self.cooldown = 0
        self.trades_today = []

    def in_trading_window(self, ts):
        t = ts.time()
        v = V4_PUT
        return ((v["MORNING_START"] <= t <= v["MORNING_END"]) or
                (v["AFTERNOON_START"] <= t <= v["AFTERNOON_END"]))

    def process_candle(self, candle, trades_data):
        ts = candle["timestamp"]
        v = V4_PUT

        if self.cooldown > 0:
            self.cooldown -= 1

        if self.signal_pending and self.active_trade is None:
            self.signal_pending = False
            entry_spot = candle["open"]
            strike = get_atm_strike(entry_spot)
            dte = days_to_nearest_expiry(ts)
            iv = estimate_iv_from_candles(self.indicators.get_closes())
            put_prem = bs_put_price(entry_spot, strike, dte, sigma=iv)

            if put_prem > v["MAX_ENTRY_PREMIUM"]:
                logger.info("[V4_PUT] Entry skipped — premium {:.1f} > max {}".format(put_prem, v['MAX_ENTRY_PREMIUM']))
                return

            self.active_trade = PutPaperTrade(
                entry_time=ts, entry_spot=entry_spot, strike=strike,
                put_prem=put_prem, iv=iv, dte=dte,
                lots=LOTS_PER_TRADE, signal=self.signal_info,
                resistance_level=self.pending_resistance,
            )
            logger.info("[V4_PUT] ENTRY: Strike={} Spot={:.0f} Put={:.1f} Res={:.0f}".format(
                strike, entry_spot, put_prem, self.pending_resistance))
            notify_trade_entry("V4 PUT", {
                "entry_time": str(ts), "entry_spot": "{:.0f}".format(entry_spot),
                "strike": strike, "direction": "PUT BUY",
                "combined_premium": put_prem, "lots": LOTS_PER_TRADE,
                "iv": iv, "signal": self.signal_info,
            })
            return

        if self.active_trade is not None:
            trade = self.active_trade
            trade.candles_held += 1

            if trade._exit_pending:
                spot_exit = candle["open"]
                dte = days_to_nearest_expiry(ts)
                iv = estimate_iv_from_candles(self.indicators.get_closes()[:-1])
                put_ex = bs_put_price(spot_exit, trade.strike, dte, sigma=iv)
                trade.close(ts, spot_exit, put_ex, trade._exit_reason)
                self._record_trade(trade, trades_data)
                self.active_trade = None
                self.cooldown = v["COOLDOWN_CANDLES"]
                return

            spot_now = candle["close"]

            if ts.time() >= v["FORCE_EXIT_TIME"]:
                dte = days_to_nearest_expiry(ts)
                iv = estimate_iv_from_candles(self.indicators.get_closes())
                put_now = bs_put_price(spot_now, trade.strike, dte, sigma=iv)
                trade.close(ts, spot_now, put_now, "SESSION_EXIT")
                self._record_trade(trade, trades_data)
                self.active_trade = None
                self.cooldown = v["COOLDOWN_CANDLES"]
                return

            if trade.resistance_level and spot_now > trade.resistance_level + v["STOP_LOSS_ABOVE_RESISTANCE"]:
                trade._exit_pending = True
                trade._exit_reason = "STOP_LOSS"
                return

            if trade.candles_held >= v["EXIT_CANDLES"]:
                trade._exit_pending = True
                trade._exit_reason = "TIME_EXIT"
                return
            return

        if not self.in_trading_window(ts):
            return
        if ts.time() > v["LAST_ENTRY_TIME"]:
            return
        if self.cooldown > 0:
            return

        bb_upper, bb_mid, bb_lower = self.indicators.get_bb_values()
        if bb_upper is None:
            return

        spot = candle["close"]
        resistance = self.indicators.find_resistance()
        if resistance is None:
            return

        near_resistance = abs(spot - resistance) < resistance * 0.002
        near_bb_upper = spot >= bb_upper * 0.998

        if near_resistance and near_bb_upper:
            if candle["close"] < candle["open"]:
                self.signal_pending = True
                self.pending_resistance = resistance
                self.signal_info = "Res:{:.0f} | BB_Up:{:.0f} | Reject".format(resistance, bb_upper)
                logger.info("[V4_PUT] SIGNAL: {}".format(self.signal_info))

    def _record_trade(self, trade, trades_data):
        trade_dict = trade.to_dict()
        trades_data["v4_put"].append(trade_dict)
        trades_data["summary"]["cum_pnl"] += trade.net_pnl
        trades_data["summary"]["total_trades"] += 1
        save_trades(trades_data)
        self.trades_today.append(trade)

        logger.info("[V4_PUT] EXIT: {} | Gross={:+,.0f} Net={:+,.0f} | Cum={:+,.0f}".format(
            trade.exit_reason, trade.gross_pnl, trade.net_pnl, trades_data['summary']['cum_pnl']))
        notify_trade_exit("V4 PUT", {
            **trade_dict,
            "cum_pnl": trades_data["summary"]["cum_pnl"],
            "roi": trades_data["summary"]["cum_pnl"] / CAPITAL * 100,
        })


# ──────────────────────────────────────────────────────────────────────
# WEBSOCKET FEED MANAGER
# ──────────────────────────────────────────────────────────────────────
class WebSocketFeed:
    """
    Manages Breeze WebSocket connection for real-time NIFTY ticks.
    Auto-reconnects on failure. Falls back to polling if WS unavailable.
    """
    def __init__(self, breeze, candle_builder):
        self.breeze = breeze
        self.candle_builder = candle_builder
        self.connected = False
        self.tick_count = 0
        self.last_tick_time = None
        self.reconnect_count = 0
        self.max_reconnects = 10

    def _on_ticks(self, tick_data):
        """Callback for incoming WebSocket ticks."""
        try:
            if not tick_data or not isinstance(tick_data, dict):
                return

            # Extract price from Breeze tick format
            ltp = tick_data.get("last", tick_data.get("ltp", tick_data.get("LTP")))
            if ltp is None:
                return

            try:
                price = float(ltp)
            except (ValueError, TypeError):
                return
            if price <= 0:
                return
            vol_raw = tick_data.get("ltq", tick_data.get("volume", 0))
            try:
                volume = int(vol_raw) if vol_raw != "" else 0
            except (ValueError, TypeError):
                volume = 0
            tick_time = now_ist()  # IST time for tick timestamp

            self.tick_count += 1
            self.last_tick_time = tick_time

            # Feed tick to candle builder
            completed_candle = self.candle_builder.on_tick(price, volume, tick_time)

            if completed_candle is not None:
                # A 5-min candle just completed — this triggers strategy processing
                logger.info("WS CANDLE COMPLETE: {} | O={:.0f} H={:.0f} L={:.0f} C={:.0f} | {} ticks".format(
                    completed_candle["timestamp"].strftime("%H:%M"),
                    completed_candle["open"], completed_candle["high"],
                    completed_candle["low"], completed_candle["close"],
                    completed_candle.get("ticks", 0)))

            # Log tick rate every 100 ticks
            if self.tick_count % 500 == 0:
                logger.debug("WS ticks received: {} | Last: {:.0f}".format(self.tick_count, price))

        except Exception as e:
            logger.error("Tick processing error: {}".format(e))

    def connect(self):
        """Subscribe to NIFTY live feed via WebSocket."""
        try:
            self.breeze.ws_connect()
            self.breeze.on_ticks = self._on_ticks

            # Subscribe to NIFTY 50 index
            self.breeze.subscribe_feeds(
                exchange_code="NSE",
                stock_code="NIFTY",
                product_type="cash",
                expiry_date="",
                strike_price="",
                right="",
                get_exchange_quotes=True,
                get_market_depth=False,
            )
            self.connected = True
            self.reconnect_count = 0
            logger.info("WebSocket connected — receiving live NIFTY ticks")
            return True
        except Exception as e:
            logger.warning("WebSocket connection failed: {} — will use polling fallback".format(e))
            self.connected = False
            return False

    def reconnect(self):
        """Attempt to reconnect WebSocket."""
        if self.reconnect_count >= self.max_reconnects:
            logger.error("Max WebSocket reconnects ({}) reached — using polling only".format(self.max_reconnects))
            return False

        self.reconnect_count += 1
        logger.info("WebSocket reconnecting (attempt {}/{})...".format(
            self.reconnect_count, self.max_reconnects))
        time_mod.sleep(5)  # brief pause before reconnect
        return self.connect()

    def is_healthy(self):
        """Check if WebSocket is still receiving ticks."""
        if not self.connected or self.last_tick_time is None:
            return False
        # Consider unhealthy if no tick in last 30 seconds during market hours
        now = now_ist()
        if MARKET_OPEN <= now.time() <= time(15, 30):
            return (now - self.last_tick_time).total_seconds() < 30
        return True


# ──────────────────────────────────────────────────────────────────────
# MAIN BOT LOOP (v2: WebSocket + fallback polling)
# ──────────────────────────────────────────────────────────────────────
def run_bot(session_token):
    """Main bot loop with WebSocket streaming and polling fallback."""

    # Load credentials
    env = {}
    with open(ENV_PATH) as f:
        for line in f:
            if "=" in line:
                k, v = line.strip().split("=", 1)
                env[k] = v

    # Connect to Breeze
    logger.info("Connecting to Breeze API...")
    breeze = BreezeConnect(api_key=env["BREEZE_API_KEY"])
    breeze.generate_session(api_secret=env["BREEZE_API_SECRET"], session_token=session_token)
    logger.info("Breeze API connected!")

    # Initialize
    candle_builder = LiveCandleBuilder(interval_min=5)
    indicators = IndicatorEngine()
    straddle_engine = StraddleEngine(indicators)
    v4_put_engine = V4PutEngine(indicators)
    trades_data = load_trades()

    # Load warmup candles (last 5 days for indicator warmup)
    logger.info("Loading warmup candles...")
    warmup_start = now_ist() - timedelta(days=5)
    warmup_candles = fetch_historical_candles(breeze, warmup_start, now_ist())
    for c in warmup_candles:
        indicators.add_candle(c)
        # Also feed into candle builder so BB has full history at startup
        candle_builder.on_tick(c["close"], c.get("volume", 0), c["timestamp"])
    # Force-complete the last warmup candle so builder starts fresh for live data
    if warmup_candles:
        candle_builder.current_candle = None
        candle_builder.tick_count = 0
    logger.info("Loaded {} warmup candles — indicators + candle builder ready".format(len(warmup_candles)))
    logger.info("BB available at startup: {}".format(indicators.get_bb_width() is not None))

    # Connect WebSocket for live streaming
    ws_feed = WebSocketFeed(breeze, candle_builder)
    ws_connected = ws_feed.connect()

    if ws_connected:
        logger.info("MODE: WebSocket streaming (sub-second latency)")
    else:
        logger.info("MODE: Polling fallback (5-10 second latency)")

    # Notify bot start
    notify_bot_start()

    last_processed_candle_time = None
    last_poll_time = None
    daily_summary_sent = False
    poll_interval = 5  # seconds between polls (fallback mode)

    logger.info("Bot running — monitoring NIFTY...")

    while True:
        now = now_ist()

        # ── PRE-MARKET ──
        if now.time() < MARKET_OPEN:
            time_mod.sleep(30)
            continue

        # ── POST-MARKET ──
        if now.time() > time(15, 35):
            if not daily_summary_sent:
                _send_daily_summary(straddle_engine, v4_put_engine, trades_data, indicators)
                daily_summary_sent = True
                logger.info("Market closed — daily summary sent")

            # Sleep until next day
            tomorrow = (now + timedelta(days=1)).replace(hour=9, minute=0, second=0)
            sleep_secs = min((tomorrow - now).total_seconds(), 64800)
            logger.info("Sleeping {:.0f} hours until next session...".format(sleep_secs / 3600))
            time_mod.sleep(sleep_secs)
            daily_summary_sent = False
            straddle_engine.trades_today = []
            v4_put_engine.trades_today = []
            continue

        # ── MARKET HOURS: process candles ──

        new_candle = None

        # METHOD 1: WebSocket — DISABLED (unreliable on Breeze)
        # Check for completed candles from candle builder (fed by polling)
        if candle_builder.completed_candles:
            new_candle = candle_builder.completed_candles.popleft()

        # METHOD 2: Quote-based polling — primary data source
        if last_poll_time is None or (now - last_poll_time).total_seconds() >= poll_interval:
            try:
                quote_data = get_latest_candle(breeze)
                if quote_data is not None:
                    ltp = quote_data["ltp"]
                    # Feed into candle builder just like a WebSocket tick
                    completed = candle_builder.on_tick(ltp, 0, now)
                    if candle_builder.tick_count % 12 == 1:  # log every ~1 min
                        cur = candle_builder.current_candle
                        logger.info("POLL TICK #{}: LTP={:.0f} now={} candle_start={} O={:.0f} H={:.0f} L={:.0f} C={:.0f}".format(
                            candle_builder.tick_count, ltp, now.strftime("%H:%M:%S"),
                            cur["timestamp"].strftime("%H:%M") if cur else "None",
                            cur["open"] if cur else 0, cur["high"] if cur else 0,
                            cur["low"] if cur else 0, cur["close"] if cur else 0))
                    if completed is not None:
                        new_candle = completed
                        logger.info("POLL CANDLE COMPLETE: {} | O={:.0f} H={:.0f} L={:.0f} C={:.0f}".format(
                            completed["timestamp"].strftime("%H:%M"),
                            completed["open"], completed["high"],
                            completed["low"], completed["close"]))
                last_poll_time = now
            except Exception as e:
                logger.error("Polling error: {}".format(e))

        # ── PROCESS NEW CANDLE ──
        if new_candle is not None:
            candle_time = new_candle["timestamp"]

            # Skip if already processed
            if last_processed_candle_time and candle_time <= last_processed_candle_time:
                time_mod.sleep(1)
                continue

            last_processed_candle_time = candle_time
            indicators.add_candle(new_candle)

            source = "WS" if ws_feed.connected else "POLL"
            latency_ms = (now - candle_time).total_seconds() * 1000 if candle_time else 0
            logger.info("[{}] Candle {} | O={:.0f} H={:.0f} L={:.0f} C={:.0f} | latency={:.0f}ms".format(
                source, candle_time.strftime("%H:%M"),
                new_candle["open"], new_candle["high"],
                new_candle["low"], new_candle["close"], latency_ms))

            # Run both strategies
            try:
                straddle_engine.process_candle(new_candle, trades_data)
            except Exception as e:
                logger.error("Straddle engine error: {}".format(e))
                logger.error(traceback.format_exc())
                notify_error("Straddle: {}".format(str(e)[:200]))

            try:
                v4_put_engine.process_candle(new_candle, trades_data)
            except Exception as e:
                logger.error("V4 PUT engine error: {}".format(e))
                logger.error(traceback.format_exc())
                notify_error("V4 PUT: {}".format(str(e)[:200]))

        # Brief sleep to prevent busy-waiting
        time_mod.sleep(1)


def _send_daily_summary(straddle_engine, v4_put_engine, trades_data, indicators):
    straddle_trades = straddle_engine.trades_today
    v4_trades = v4_put_engine.trades_today
    all_trades = straddle_trades + v4_trades

    net_pnl = sum(t.net_pnl for t in all_trades)
    gross_pnl = sum(t.gross_pnl for t in all_trades)
    costs = sum(t.txn_costs for t in all_trades)
    latest = indicators.get_latest()

    notify_daily_summary({
        "date": now_ist().strftime("%d %b %Y"),
        "straddle_trades": len(straddle_trades),
        "v4_trades": len(v4_trades),
        "winners": sum(1 for t in all_trades if t.net_pnl > 0),
        "losers": sum(1 for t in all_trades if t.net_pnl <= 0),
        "gross_pnl": gross_pnl,
        "total_costs": costs,
        "net_pnl": net_pnl,
        "cum_pnl": trades_data["summary"]["cum_pnl"],
        "roi": trades_data["summary"]["cum_pnl"] / CAPITAL * 100,
        "nifty_open": "{:.0f}".format(latest['open']) if latest else "-",
        "nifty_close": "{:.0f}".format(latest['close']) if latest else "-",
    })


# ──────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 bot.py <session_token>")
        print("Get session token by logging into ICICI Direct API portal")
        sys.exit(1)

    session_token = sys.argv[1]
    logger.info("Starting NIFTY Paper Trading Bot v2 (WebSocket + Polling)...")
    logger.info("Capital: Rs {:,} | Lots/trade: {}".format(CAPITAL, LOTS_PER_TRADE))
    logger.info("Session token: {}****".format(session_token[:4]))

    try:
        run_bot(session_token)
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error("Bot crashed: {}".format(e))
        logger.error(traceback.format_exc())
        notify_error(str(e))
        raise
