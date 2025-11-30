"""Regime inputs calculation from footprint bars."""

from datetime import datetime, time
from typing import List, Optional

from src.core.types import FootprintBar, RegimeInputs
from src.analysis.indicators import (
    OHLC, ema, adx, atr, vwap, calculate_slope, percentile,
    check_higher_highs, check_higher_lows,
    check_lower_highs, check_lower_lows,
    count_range_bound_bars, avg_bar_range
)


# Default trading hours - can be overridden in config
DEFAULT_SESSION_OPEN = time(9, 30)
DEFAULT_SESSION_CLOSE = time(16, 0)


class RegimeInputsCalculator:
    """Calculates all inputs needed for regime detection from footprint bars."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.bars: List[FootprintBar] = []
        self.ohlc_cache: List[OHLC] = []

        # For ATR percentile calculation
        self.daily_atr_values: List[float] = []

        # News windows (time ranges to avoid trading)
        self.news_windows = self.config.get("news_windows", [])

        # Trading session hours (can be overridden for extended hours/demo)
        self.session_open = self.config.get("session_open", DEFAULT_SESSION_OPEN)
        self.session_close = self.config.get("session_close", DEFAULT_SESSION_CLOSE)

    def add_bar(self, bar: FootprintBar) -> None:
        """Add a new bar to the history."""
        self.bars.append(bar)
        self.ohlc_cache.append(OHLC(
            open=bar.open_price,
            high=bar.high_price,
            low=bar.low_price,
            close=bar.close_price,
            volume=bar.total_volume
        ))

        # Keep bounded
        max_bars = 200
        if len(self.bars) > max_bars:
            self.bars = self.bars[-max_bars:]
            self.ohlc_cache = self.ohlc_cache[-max_bars:]

    def calculate(self) -> RegimeInputs:
        """Calculate all regime inputs from current bar history."""
        if len(self.bars) < 21:
            return self._default_inputs()

        ohlc = self.ohlc_cache
        closes = [bar.close for bar in ohlc]
        highs = [bar.high for bar in ohlc]
        lows = [bar.low for bar in ohlc]
        deltas = [bar.delta for bar in self.bars]
        volumes = [bar.total_volume for bar in self.bars]

        # Calculate indicators
        ema_9 = ema(closes, 9)
        ema_21 = ema(closes, 21)
        adx_values = adx(ohlc, 14)
        atr_values = atr(ohlc, 14)
        vwap_values = vwap(ohlc)

        # Current values
        current_adx = adx_values[-1] if adx_values else 0.0
        current_atr = atr_values[-1] if atr_values else 0.0
        current_ema_fast = ema_9[-1] if ema_9 else closes[-1]
        current_ema_slow = ema_21[-1] if ema_21 else closes[-1]
        current_vwap = vwap_values[-1] if vwap_values else closes[-1]

        # Slopes
        adx_slope = calculate_slope(adx_values, 5)
        delta_slope = calculate_slope([float(d) for d in deltas], 10)

        # Volume comparison
        avg_volume = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / len(volumes)
        volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 1.0

        # ATR percentile (vs recent history)
        atr_pct = percentile(current_atr, atr_values[-50:]) if len(atr_values) >= 10 else 50.0

        # Market structure
        higher_highs = check_higher_highs(highs, 5)
        higher_lows = check_higher_lows(lows, 5)
        lower_highs = check_lower_highs(highs, 5)
        lower_lows = check_lower_lows(lows, 5)
        range_bars = count_range_bound_bars(highs, lows, 10)

        # Time context
        now = datetime.now()
        current_time = now.time()
        mins_since_open = self._minutes_since(self.session_open, current_time)
        mins_to_close = self._minutes_until(current_time, self.session_close)

        return RegimeInputs(
            adx_14=current_adx,
            adx_slope=adx_slope,
            ema_fast=current_ema_fast,
            ema_slow=current_ema_slow,
            ema_trend=current_ema_fast - current_ema_slow,
            price_vs_vwap=closes[-1] - current_vwap,
            atr_14=current_atr,
            atr_percentile=atr_pct,
            bar_range_avg=avg_bar_range(ohlc, 5),
            volume_vs_average=volume_ratio,
            cumulative_delta=sum(deltas),
            delta_slope=delta_slope,
            higher_highs=higher_highs,
            higher_lows=higher_lows,
            lower_highs=lower_highs,
            lower_lows=lower_lows,
            range_bound_bars=range_bars,
            minutes_since_open=mins_since_open,
            minutes_to_close=mins_to_close,
            is_news_window=self._is_news_window(current_time),
        )

    def _default_inputs(self) -> RegimeInputs:
        """Return default inputs when not enough bar history."""
        return RegimeInputs()  # Uses dataclass defaults

    def _minutes_since(self, start: time, current: time) -> int:
        """Calculate minutes elapsed since a start time."""
        start_mins = start.hour * 60 + start.minute
        current_mins = current.hour * 60 + current.minute
        diff = current_mins - start_mins
        return max(0, diff)

    def _minutes_until(self, current: time, end: time) -> int:
        """Calculate minutes remaining until end time."""
        current_mins = current.hour * 60 + current.minute
        end_mins = end.hour * 60 + end.minute
        diff = end_mins - current_mins
        return max(0, diff)

    def _is_news_window(self, current: time) -> bool:
        """Check if current time is within a news window."""
        for window in self.news_windows:
            start, end = window
            if start <= current <= end:
                return True
        return False

    def reset(self) -> None:
        """Reset the calculator state."""
        self.bars.clear()
        self.ohlc_cache.clear()
