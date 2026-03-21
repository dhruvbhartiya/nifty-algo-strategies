"""
Options pricing and utility functions.
Black-Scholes pricing, ATM strike calculation, IV estimation, transaction costs.
"""
import numpy as np
from scipy.stats import norm
from bot_config import (
    NIFTY_STRIKE_GAP, RISK_FREE_RATE, LOT_SIZE, BASE_IV, IV_CAP,
    BROKERAGE_PER_ORDER, STT_RATE, EXCHANGE_TXN_RATE, GST_RATE,
    SEBI_PER_CRORE, STAMP_DUTY_RATE, SLIPPAGE_PTS, SPREAD_PTS,
)


def bs_call_price(spot, strike, days_to_expiry, r=RISK_FREE_RATE, sigma=0.15):
    if days_to_expiry <= 0:
        return max(spot - strike, 0.0)
    T = days_to_expiry / 365.0
    d1 = (np.log(spot / strike) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return max(spot * norm.cdf(d1) - strike * np.exp(-r * T) * norm.cdf(d2), 0.01)


def bs_put_price(spot, strike, days_to_expiry, r=RISK_FREE_RATE, sigma=0.15):
    if days_to_expiry <= 0:
        return max(strike - spot, 0.0)
    T = days_to_expiry / 365.0
    d1 = (np.log(spot / strike) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return max(strike * np.exp(-r * T) * norm.cdf(-d2) - spot * norm.cdf(-d1), 0.01)


def get_atm_strike(spot):
    """Nearest strike to spot price."""
    return round(spot / NIFTY_STRIKE_GAP) * NIFTY_STRIKE_GAP


def days_to_nearest_expiry(current_dt):
    """Weekly expiry on Thursday."""
    weekday = current_dt.weekday()
    days_ahead = (3 - weekday) % 7
    if days_ahead == 0:
        if current_dt.hour < 15 or (current_dt.hour == 15 and current_dt.minute < 30):
            return max(0.1, (15.5 - current_dt.hour - current_dt.minute / 60) / 24)
        days_ahead = 7
    return days_ahead


def estimate_iv_from_candles(candles, lookback=40):
    """
    Estimate IV from trailing 5-min candle closes.
    candles: list of close prices (most recent last)
    """
    if len(candles) < 10:
        return BASE_IV

    closes = np.array(candles[-lookback:] if len(candles) >= lookback else candles)
    log_returns = np.diff(np.log(closes))

    if len(log_returns) < 5:
        return BASE_IV

    realized_vol = np.std(log_returns) * np.sqrt(75 * 252)
    iv = max(realized_vol * 1.1, BASE_IV)
    return min(iv, IV_CAP)


def compute_straddle_costs(call_entry, call_exit, put_entry, put_exit, total_qty):
    """Transaction costs for a straddle (4 orders)."""
    buy_value = (call_entry + put_entry) * total_qty
    sell_value = (call_exit + put_exit) * total_qty
    turnover = buy_value + sell_value

    brokerage = BROKERAGE_PER_ORDER * 4
    stt = sell_value * STT_RATE
    exchange_charges = turnover * EXCHANGE_TXN_RATE
    gst = (brokerage + exchange_charges) * GST_RATE
    sebi = turnover * SEBI_PER_CRORE / 1e7
    stamp = buy_value * STAMP_DUTY_RATE
    slippage = SLIPPAGE_PTS * 4 * total_qty
    spread_cost = SPREAD_PTS * 4 * total_qty

    return brokerage + stt + exchange_charges + gst + sebi + stamp + slippage + spread_cost


def compute_put_costs(put_entry, put_exit, total_qty):
    """Transaction costs for a single PUT trade (2 orders: buy + sell)."""
    buy_value = put_entry * total_qty
    sell_value = put_exit * total_qty
    turnover = buy_value + sell_value

    brokerage = BROKERAGE_PER_ORDER * 2
    stt = sell_value * STT_RATE
    exchange_charges = turnover * EXCHANGE_TXN_RATE
    gst = (brokerage + exchange_charges) * GST_RATE
    sebi = turnover * SEBI_PER_CRORE / 1e7
    stamp = buy_value * STAMP_DUTY_RATE
    slippage = SLIPPAGE_PTS * 2 * total_qty
    spread_cost = SPREAD_PTS * 2 * total_qty

    return brokerage + stt + exchange_charges + gst + sebi + stamp + slippage + spread_cost
