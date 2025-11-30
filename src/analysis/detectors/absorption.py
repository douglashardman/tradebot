"""Absorption detection - identifies passive orders absorbing aggressive orders."""

from typing import Dict, List, Optional

from src.core.types import FootprintBar, Signal, SignalPattern


class AbsorptionDetector:
    """
    Detects absorption patterns at price extremes.

    Absorption occurs when passive limit orders absorb aggressive market
    orders without price continuing in the aggressive direction. This
    indicates strong institutional presence at that level.
    """

    def __init__(self, min_volume: int = 100, delta_threshold: float = 0.3):
        """
        Initialize the detector.

        Args:
            min_volume: Minimum total volume at price level to consider
            delta_threshold: Max delta/volume ratio for absorption (lower = more absorption)
        """
        self.min_volume = min_volume
        self.delta_threshold = delta_threshold

    def detect(self, bar: FootprintBar) -> List[Signal]:
        """Detect absorption patterns at bar extremes."""
        signals = []

        # Check for absorption at bar high (sellers absorbing buyers)
        high_absorption = self._check_high_absorption(bar)
        if high_absorption:
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern=SignalPattern.SELLING_ABSORPTION,
                direction="SHORT",
                strength=high_absorption["strength"],
                price=bar.high_price,
                details=high_absorption
            ))

        # Check for absorption at bar low (buyers absorbing sellers)
        low_absorption = self._check_low_absorption(bar)
        if low_absorption:
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern=SignalPattern.BUYING_ABSORPTION,
                direction="LONG",
                strength=low_absorption["strength"],
                price=bar.low_price,
                details=low_absorption
            ))

        return signals

    def _check_high_absorption(self, bar: FootprintBar) -> Optional[Dict]:
        """
        Check if aggressive buying was absorbed at the high.

        Signs:
        - High ask volume at top prices (aggressive buying)
        - Price didn't continue higher (close not at high)
        - Sellers absorbed the buying pressure
        """
        levels = bar.get_sorted_levels(ascending=True)
        if len(levels) < 3:
            return None

        # Look at top 3 price levels
        top_levels = levels[-3:]

        total_ask_volume = sum(level.ask_volume for level in top_levels)
        total_bid_volume = sum(level.bid_volume for level in top_levels)
        total_volume = total_ask_volume + total_bid_volume

        if total_volume < self.min_volume:
            return None

        # Absorption requires significant buying activity
        if total_ask_volume < total_volume * 0.6:
            return None

        # Price rejection: close is in lower portion of bar
        bar_range = bar.high_price - bar.low_price
        if bar_range == 0:
            return None

        close_position = (bar.close_price - bar.low_price) / bar_range

        # Close in upper half means no rejection
        if close_position > 0.5:
            return None

        return {
            "ask_volume": total_ask_volume,
            "bid_volume": total_bid_volume,
            "total_volume": total_volume,
            "close_position": round(close_position, 3),
            "absorption_ratio": round(total_ask_volume / total_volume, 3),
            "strength": min((1 - close_position) * (total_ask_volume / self.min_volume) / 2, 1.0)
        }

    def _check_low_absorption(self, bar: FootprintBar) -> Optional[Dict]:
        """
        Check if aggressive selling was absorbed at the low.

        Signs:
        - High bid volume at bottom prices (aggressive selling)
        - Price didn't continue lower (close not at low)
        - Buyers absorbed the selling pressure
        """
        levels = bar.get_sorted_levels(ascending=True)
        if len(levels) < 3:
            return None

        # Look at bottom 3 price levels
        bottom_levels = levels[:3]

        total_ask_volume = sum(level.ask_volume for level in bottom_levels)
        total_bid_volume = sum(level.bid_volume for level in bottom_levels)
        total_volume = total_ask_volume + total_bid_volume

        if total_volume < self.min_volume:
            return None

        # Absorption requires significant selling activity
        if total_bid_volume < total_volume * 0.6:
            return None

        # Price rejection: close is in upper portion of bar
        bar_range = bar.high_price - bar.low_price
        if bar_range == 0:
            return None

        close_position = (bar.close_price - bar.low_price) / bar_range

        # Close in lower half means no rejection
        if close_position < 0.5:
            return None

        return {
            "ask_volume": total_ask_volume,
            "bid_volume": total_bid_volume,
            "total_volume": total_volume,
            "close_position": round(close_position, 3),
            "absorption_ratio": round(total_bid_volume / total_volume, 3),
            "strength": min(close_position * (total_bid_volume / self.min_volume) / 2, 1.0)
        }
