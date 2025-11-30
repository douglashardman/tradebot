"""Delta divergence detection - identifies price/delta discrepancies."""

from datetime import datetime
from typing import Any, Dict, List, Tuple

from src.core.types import FootprintBar, Signal, SignalPattern


class DeltaDivergenceDetector:
    """
    Detects divergence between price action and delta.

    Divergence occurs when price makes new highs/lows but delta
    doesn't confirm, suggesting the move may not be sustainable.
    """

    def __init__(self, lookback: int = 5):
        """
        Initialize the detector.

        Args:
            lookback: Number of bars to analyze for divergence
        """
        self.lookback = lookback
        self.bar_history: List[FootprintBar] = []

    def add_bar(self, bar: FootprintBar) -> List[Signal]:
        """
        Add new bar and check for divergence patterns.

        Args:
            bar: Completed footprint bar

        Returns:
            List of divergence signals (if any)
        """
        self.bar_history.append(bar)

        # Keep history bounded
        if len(self.bar_history) > self.lookback * 2:
            self.bar_history = self.bar_history[-self.lookback * 2:]

        if len(self.bar_history) < self.lookback:
            return []

        return self._detect_divergence()

    def _detect_divergence(self) -> List[Signal]:
        """Detect price/delta divergence patterns."""
        signals = []
        recent_bars = self.bar_history[-self.lookback:]

        # Extract data series
        highs = [bar.high_price for bar in recent_bars]
        lows = [bar.low_price for bar in recent_bars]
        deltas = [bar.delta for bar in recent_bars]

        current_bar = recent_bars[-1]

        # Bearish divergence: Price making higher highs, delta making lower highs
        if self._is_higher_high(highs) and self._is_lower_high(deltas):
            # Additional confirmation: current delta is negative
            if deltas[-1] < 0:
                signals.append(Signal(
                    timestamp=current_bar.end_time,
                    symbol=current_bar.symbol,
                    pattern=SignalPattern.BEARISH_DELTA_DIVERGENCE,
                    direction="SHORT",
                    strength=0.7,
                    price=current_bar.close_price,
                    details={
                        "price_high": max(highs),
                        "current_delta": deltas[-1],
                        "delta_trend": "declining",
                        "highs": highs,
                        "deltas": deltas,
                    }
                ))

        # Bullish divergence: Price making lower lows, delta making higher lows
        if self._is_lower_low(lows) and self._is_higher_low(deltas):
            # Additional confirmation: current delta is positive
            if deltas[-1] > 0:
                signals.append(Signal(
                    timestamp=current_bar.end_time,
                    symbol=current_bar.symbol,
                    pattern=SignalPattern.BULLISH_DELTA_DIVERGENCE,
                    direction="LONG",
                    strength=0.7,
                    price=current_bar.close_price,
                    details={
                        "price_low": min(lows),
                        "current_delta": deltas[-1],
                        "delta_trend": "rising",
                        "lows": lows,
                        "deltas": deltas,
                    }
                ))

        return signals

    def _is_higher_high(self, values: List[float]) -> bool:
        """Check if most recent value is a higher high."""
        if len(values) < 3:
            return False
        return values[-1] > max(values[:-1])

    def _is_lower_high(self, values: List[int]) -> bool:
        """Check if recent highs (peaks) are declining."""
        if len(values) < 3:
            return False

        peaks = self._find_peaks(values)
        if len(peaks) < 2:
            return False

        return peaks[-1][1] < peaks[-2][1]

    def _is_lower_low(self, values: List[float]) -> bool:
        """Check if most recent value is a lower low."""
        if len(values) < 3:
            return False
        return values[-1] < min(values[:-1])

    def _is_higher_low(self, values: List[int]) -> bool:
        """Check if recent lows (troughs) are rising."""
        if len(values) < 3:
            return False

        troughs = self._find_troughs(values)
        if len(troughs) < 2:
            return False

        return troughs[-1][1] > troughs[-2][1]

    def _find_peaks(self, values: List) -> List[Tuple[int, Any]]:
        """Find local maxima in a series."""
        peaks = []
        for i in range(1, len(values) - 1):
            if values[i] > values[i-1] and values[i] > values[i+1]:
                peaks.append((i, values[i]))

        # Include last value if it's higher than previous
        if len(values) >= 2 and values[-1] > values[-2]:
            peaks.append((len(values) - 1, values[-1]))

        return peaks

    def _find_troughs(self, values: List) -> List[Tuple[int, Any]]:
        """Find local minima in a series."""
        troughs = []
        for i in range(1, len(values) - 1):
            if values[i] < values[i-1] and values[i] < values[i+1]:
                troughs.append((i, values[i]))

        # Include last value if it's lower than previous
        if len(values) >= 2 and values[-1] < values[-2]:
            troughs.append((len(values) - 1, values[-1]))

        return troughs

    def reset(self) -> None:
        """Reset the detector's bar history."""
        self.bar_history.clear()
