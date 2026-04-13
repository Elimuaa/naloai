"""
Technical indicators module for CryptoBot.
Provides RSI, EMA, SMA, ADX, Bollinger Bands, ATR, and volume analysis.
All functions operate on plain Python lists for zero-copy integration with bot_engine.
"""

import numpy as np
from typing import Optional


def ema(prices: list[float], period: int) -> Optional[float]:
    """Exponential Moving Average. Returns the latest EMA value."""
    if len(prices) < period:
        return None
    multiplier = 2.0 / (period + 1)
    ema_val = float(np.mean(prices[:period]))
    for p in prices[period:]:
        ema_val = (p - ema_val) * multiplier + ema_val
    return ema_val


def sma(prices: list[float], period: int) -> Optional[float]:
    """Simple Moving Average over the last `period` values."""
    if len(prices) < period:
        return None
    return float(np.mean(prices[-period:]))


def rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """Relative Strength Index (Wilder's smoothing). Returns 0-100."""
    if len(prices) < period + 1:
        return None
    deltas = np.diff(prices[-(period + 1):])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(np.mean(gains))
    avg_loss = float(np.mean(losses))
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def bollinger_bands(prices: list[float], period: int = 20, num_std: float = 2.0) -> Optional[dict]:
    """Returns {upper, middle, lower, pct_b} or None if not enough data."""
    if len(prices) < period:
        return None
    window = prices[-period:]
    middle = float(np.mean(window))
    std = float(np.std(window))
    upper = middle + num_std * std
    lower = middle - num_std * std
    current = prices[-1]
    pct_b = (current - lower) / (upper - lower) if (upper - lower) > 0 else 0.5
    return {"upper": upper, "middle": middle, "lower": lower, "pct_b": pct_b}


def atr(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> Optional[float]:
    """Average True Range. For single-price feeds, pass price as high/low/close."""
    if len(closes) < period + 1:
        return None
    true_ranges = []
    for i in range(-period, 0):
        h = highs[i] if i < len(highs) else closes[i]
        l = lows[i] if i < len(lows) else closes[i]
        prev_c = closes[i - 1]
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        true_ranges.append(tr)
    return float(np.mean(true_ranges))


def atr_from_prices(prices: list[float], period: int = 14) -> Optional[float]:
    """Simplified ATR when only close prices are available.
    Uses consecutive price differences as a proxy for true range."""
    if len(prices) < period + 1:
        return None
    ranges = [abs(prices[i] - prices[i - 1]) for i in range(-period, 0)]
    return float(np.mean(ranges))


def adx(prices: list[float], period: int = 14) -> Optional[float]:
    """Average Directional Index approximation from close prices.
    Returns 0-100. Values > 25 indicate a strong trend."""
    if len(prices) < period * 2 + 1:
        return None
    # Compute +DM, -DM from price differences
    plus_dm = []
    minus_dm = []
    tr_list = []
    data = prices[-(period * 2 + 1):]
    for i in range(1, len(data)):
        up_move = data[i] - data[i - 1]
        down_move = data[i - 1] - data[i]
        plus_dm.append(max(up_move, 0) if up_move > down_move else 0)
        minus_dm.append(max(down_move, 0) if down_move > up_move else 0)
        tr_list.append(abs(data[i] - data[i - 1]))

    if len(tr_list) < period:
        return None

    # Smooth with Wilder's method (simple average for first period, then EMA)
    def wilder_smooth(values: list[float], p: int) -> list[float]:
        result = [float(np.mean(values[:p]))]
        for v in values[p:]:
            result.append(result[-1] - result[-1] / p + v)
        return result

    smooth_tr = wilder_smooth(tr_list, period)
    smooth_plus = wilder_smooth(plus_dm, period)
    smooth_minus = wilder_smooth(minus_dm, period)

    dx_values = []
    for i in range(len(smooth_tr)):
        if smooth_tr[i] == 0:
            dx_values.append(0.0)
            continue
        plus_di = 100 * smooth_plus[i] / smooth_tr[i]
        minus_di = 100 * smooth_minus[i] / smooth_tr[i]
        di_sum = plus_di + minus_di
        if di_sum == 0:
            dx_values.append(0.0)
        else:
            dx_values.append(100.0 * abs(plus_di - minus_di) / di_sum)

    if not dx_values:
        return None
    # ADX = smoothed DX
    adx_val = float(np.mean(dx_values[-period:])) if len(dx_values) >= period else float(np.mean(dx_values))
    return min(100.0, max(0.0, adx_val))


def macd(prices: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Optional[dict]:
    """MACD line, signal line, and histogram."""
    if len(prices) < slow + signal:
        return None
    fast_ema = ema(prices, fast)
    slow_ema = ema(prices, slow)
    if fast_ema is None or slow_ema is None:
        return None
    macd_line = fast_ema - slow_ema
    # Compute MACD values incrementally using EMA update formula (O(N) instead of O(N²))
    multiplier_f = 2.0 / (fast + 1)
    multiplier_s = 2.0 / (slow + 1)
    ema_f = float(np.mean(prices[:fast]))
    ema_s = float(np.mean(prices[:slow]))
    macd_values = []
    for i in range(slow, len(prices)):
        # Update fast EMA incrementally
        if i < fast:
            ema_f = float(np.mean(prices[:i + 1]))
        else:
            ema_f = (prices[i] - ema_f) * multiplier_f + ema_f
        # Update slow EMA incrementally
        ema_s = (prices[i] - ema_s) * multiplier_s + ema_s
        macd_values.append(ema_f - ema_s)
    if len(macd_values) < signal:
        return None
    signal_ema = ema(macd_values, signal)
    if signal_ema is None:
        return None
    histogram = macd_line - signal_ema
    return {"macd": macd_line, "signal": signal_ema, "histogram": histogram}


def volume_confirmation(volumes: list[float], period: int = 20, threshold: float = 1.5) -> Optional[dict]:
    """Check if current volume exceeds threshold * average volume.
    Returns {confirmed: bool, ratio: float, avg_volume: float}."""
    if len(volumes) < period + 1:
        return None
    avg_vol = float(np.mean(volumes[-period - 1:-1]))
    if avg_vol == 0:
        return {"confirmed": False, "ratio": 0.0, "avg_volume": 0.0}
    current_vol = volumes[-1]
    ratio = current_vol / avg_vol
    return {"confirmed": ratio >= threshold, "ratio": ratio, "avg_volume": avg_vol}


def compute_all_indicators(prices: list[float], lookback: int = 20) -> dict:
    """Compute all indicators at once and return a summary dict."""
    result = {
        "ema_50": ema(prices, 50),
        "sma_20": sma(prices, lookback),
        "rsi_14": rsi(prices, 14),
        "adx_14": adx(prices, 14),
        "bollinger": bollinger_bands(prices, lookback),
        "macd": macd(prices),
        "atr": atr_from_prices(prices, 14),
    }
    return result
