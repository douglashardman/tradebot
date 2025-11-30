"""Technical indicators for regime detection."""

from typing import List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class OHLC:
    """Simple OHLC bar data."""
    open: float
    high: float
    low: float
    close: float
    volume: int = 0


def ema(values: List[float], period: int) -> List[float]:
    """
    Calculate Exponential Moving Average.

    Args:
        values: List of values (e.g., closing prices)
        period: EMA period

    Returns:
        List of EMA values (same length as input, first period-1 values are SMA-based)
    """
    if len(values) < period:
        return [sum(values) / len(values)] * len(values) if values else []

    result = []
    multiplier = 2 / (period + 1)

    # First value is SMA
    sma = sum(values[:period]) / period
    result.extend([sma] * period)

    # Calculate EMA for remaining values
    for i in range(period, len(values)):
        ema_val = (values[i] - result[-1]) * multiplier + result[-1]
        result.append(ema_val)

    return result


def sma(values: List[float], period: int) -> List[float]:
    """
    Calculate Simple Moving Average.

    Args:
        values: List of values
        period: SMA period

    Returns:
        List of SMA values
    """
    if len(values) < period:
        return [sum(values) / len(values)] * len(values) if values else []

    result = []
    for i in range(len(values)):
        if i < period - 1:
            result.append(sum(values[:i+1]) / (i + 1))
        else:
            result.append(sum(values[i-period+1:i+1]) / period)

    return result


def true_range(bars: List[OHLC]) -> List[float]:
    """
    Calculate True Range for each bar.

    TR = max(high - low, abs(high - prev_close), abs(low - prev_close))
    """
    if not bars:
        return []

    result = [bars[0].high - bars[0].low]  # First bar: just high - low

    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        current = bars[i]

        tr = max(
            current.high - current.low,
            abs(current.high - prev_close),
            abs(current.low - prev_close)
        )
        result.append(tr)

    return result


def atr(bars: List[OHLC], period: int = 14) -> List[float]:
    """
    Calculate Average True Range.

    Args:
        bars: List of OHLC bars
        period: ATR period (default 14)

    Returns:
        List of ATR values
    """
    tr_values = true_range(bars)
    return ema(tr_values, period)


def directional_movement(bars: List[OHLC]) -> Tuple[List[float], List[float]]:
    """
    Calculate Directional Movement (+DM and -DM).

    Returns:
        Tuple of (+DM values, -DM values)
    """
    if len(bars) < 2:
        return [0.0], [0.0]

    plus_dm = [0.0]
    minus_dm = [0.0]

    for i in range(1, len(bars)):
        up_move = bars[i].high - bars[i - 1].high
        down_move = bars[i - 1].low - bars[i].low

        if up_move > down_move and up_move > 0:
            plus_dm.append(up_move)
        else:
            plus_dm.append(0.0)

        if down_move > up_move and down_move > 0:
            minus_dm.append(down_move)
        else:
            minus_dm.append(0.0)

    return plus_dm, minus_dm


def adx(bars: List[OHLC], period: int = 14) -> List[float]:
    """
    Calculate Average Directional Index (ADX).

    ADX measures trend strength regardless of direction.
    - ADX > 25: Strong trend
    - ADX < 20: Weak trend / ranging
    - ADX 20-25: Uncertain

    Args:
        bars: List of OHLC bars
        period: ADX period (default 14)

    Returns:
        List of ADX values
    """
    if len(bars) < period * 2:
        return [0.0] * len(bars)

    # Calculate True Range and Directional Movement
    tr_values = true_range(bars)
    plus_dm, minus_dm = directional_movement(bars)

    # Smooth TR, +DM, -DM
    smoothed_tr = ema(tr_values, period)
    smoothed_plus_dm = ema(plus_dm, period)
    smoothed_minus_dm = ema(minus_dm, period)

    # Calculate +DI and -DI
    plus_di = []
    minus_di = []
    dx = []

    for i in range(len(bars)):
        if smoothed_tr[i] > 0:
            pdi = 100 * smoothed_plus_dm[i] / smoothed_tr[i]
            mdi = 100 * smoothed_minus_dm[i] / smoothed_tr[i]
        else:
            pdi = 0.0
            mdi = 0.0

        plus_di.append(pdi)
        minus_di.append(mdi)

        # DX = 100 * |+DI - -DI| / (+DI + -DI)
        di_sum = pdi + mdi
        if di_sum > 0:
            dx.append(100 * abs(pdi - mdi) / di_sum)
        else:
            dx.append(0.0)

    # ADX is smoothed DX
    return ema(dx, period)


def vwap(bars: List[OHLC]) -> List[float]:
    """
    Calculate Volume Weighted Average Price (cumulative from session start).

    Args:
        bars: List of OHLC bars with volume

    Returns:
        List of VWAP values
    """
    if not bars:
        return []

    result = []
    cumulative_volume = 0
    cumulative_pv = 0.0

    for bar in bars:
        typical_price = (bar.high + bar.low + bar.close) / 3
        cumulative_pv += typical_price * bar.volume
        cumulative_volume += bar.volume

        if cumulative_volume > 0:
            result.append(cumulative_pv / cumulative_volume)
        else:
            result.append(typical_price)

    return result


def calculate_slope(values: List[float], period: int = 5) -> float:
    """
    Calculate the slope of recent values using linear regression.

    Args:
        values: List of values
        period: Number of recent values to use

    Returns:
        Slope value (positive = rising, negative = falling)
    """
    if len(values) < 2:
        return 0.0

    recent = values[-period:] if len(values) >= period else values
    n = len(recent)

    if n < 2:
        return 0.0

    # Linear regression slope
    sum_x = sum(range(n))
    sum_y = sum(recent)
    sum_xy = sum(i * v for i, v in enumerate(recent))
    sum_xx = sum(i * i for i in range(n))

    denominator = n * sum_xx - sum_x * sum_x
    if denominator == 0:
        return 0.0

    return (n * sum_xy - sum_x * sum_y) / denominator


def percentile(value: float, values: List[float]) -> float:
    """
    Calculate what percentile a value is within a distribution.

    Args:
        value: The value to find percentile for
        values: The distribution of values

    Returns:
        Percentile (0-100)
    """
    if not values:
        return 50.0

    count_below = sum(1 for v in values if v < value)
    return (count_below / len(values)) * 100


def check_higher_highs(highs: List[float], lookback: int = 5) -> bool:
    """
    Check if recent highs show higher-high pattern.

    Args:
        highs: List of high prices
        lookback: Number of bars to check

    Returns:
        True if most recent high is higher than previous highs
    """
    if len(highs) < lookback:
        return False

    recent = highs[-lookback:]
    return recent[-1] > max(recent[:-1])


def check_higher_lows(lows: List[float], lookback: int = 5) -> bool:
    """
    Check if recent lows show higher-low pattern.

    Args:
        lows: List of low prices
        lookback: Number of bars to check

    Returns:
        True if most recent low is higher than previous lows
    """
    if len(lows) < lookback:
        return False

    recent = lows[-lookback:]
    # Find the lowest point before the last bar
    prev_min = min(recent[:-1])
    # Most recent low should be higher
    return recent[-1] > prev_min


def check_lower_highs(highs: List[float], lookback: int = 5) -> bool:
    """
    Check if recent highs show lower-high pattern.
    """
    if len(highs) < lookback:
        return False

    recent = highs[-lookback:]
    prev_max = max(recent[:-1])
    return recent[-1] < prev_max


def check_lower_lows(lows: List[float], lookback: int = 5) -> bool:
    """
    Check if recent lows show lower-low pattern.
    """
    if len(lows) < lookback:
        return False

    recent = lows[-lookback:]
    return recent[-1] < min(recent[:-1])


def count_range_bound_bars(highs: List[float], lows: List[float], lookback: int = 10) -> int:
    """
    Count how many recent bars stayed within a range.

    Args:
        highs: List of high prices
        lows: List of low prices
        lookback: Number of bars to analyze

    Returns:
        Count of bars that stayed within initial range
    """
    if len(highs) < lookback or len(lows) < lookback:
        return 0

    recent_highs = highs[-lookback:]
    recent_lows = lows[-lookback:]

    # Define range from first bar
    range_high = recent_highs[0]
    range_low = recent_lows[0]

    # Expand range slightly (10%)
    range_size = range_high - range_low
    range_high += range_size * 0.1
    range_low -= range_size * 0.1

    # Count bars within range
    count = 0
    for h, l in zip(recent_highs, recent_lows):
        if l >= range_low and h <= range_high:
            count += 1

    return count


def avg_bar_range(bars: List[OHLC], period: int = 5) -> float:
    """
    Calculate average bar range over recent bars.

    Args:
        bars: List of OHLC bars
        period: Number of bars to average

    Returns:
        Average of (high - low) for recent bars
    """
    if not bars:
        return 0.0

    recent = bars[-period:] if len(bars) >= period else bars
    ranges = [bar.high - bar.low for bar in recent]
    return sum(ranges) / len(ranges)
