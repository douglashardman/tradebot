"""Unfinished business detection - identifies incomplete auctions."""

from datetime import datetime
from typing import Dict, List, Tuple

from src.core.types import FootprintBar, Signal, SignalPattern


class UnfinishedBusinessDetector:
    """
    Detects unfinished business at price extremes.

    Unfinished business occurs when an auction doesn't complete at an
    extreme - there's volume on one side but not the other. These levels
    often act as magnets that price returns to.
    """

    def __init__(self, max_volume_threshold: int = 5):
        """
        Initialize the detector.

        Args:
            max_volume_threshold: Max volume on one side to consider "unfinished"
        """
        self.threshold = max_volume_threshold
        # Track unfinished levels: symbol -> [(price, time, direction)]
        self.unfinished_levels: Dict[str, List[Tuple[float, datetime, str]]] = {}

    def detect(self, bar: FootprintBar) -> List[Signal]:
        """Detect unfinished business at bar extremes."""
        signals = []
        levels = bar.get_sorted_levels(ascending=True)

        if not levels:
            return signals

        # Check bar high for unfinished business
        # Unfinished high: buyers hit but couldn't lift offer (low/no ask volume)
        high_level = levels[-1]
        if high_level.ask_volume <= self.threshold and high_level.bid_volume > self.threshold:
            self._add_unfinished(bar.symbol, bar.high_price, bar.end_time, "HIGH")
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern=SignalPattern.UNFINISHED_HIGH,
                direction="LONG",  # Price may revisit this level
                strength=0.6,
                price=bar.high_price,
                details={
                    "ask_volume": high_level.ask_volume,
                    "bid_volume": high_level.bid_volume,
                    "implication": "Incomplete auction - potential magnet level",
                    "type": "HIGH"
                }
            ))

        # Check bar low for unfinished business
        # Unfinished low: sellers hit but couldn't break bid (low/no bid volume)
        low_level = levels[0]
        if low_level.bid_volume <= self.threshold and low_level.ask_volume > self.threshold:
            self._add_unfinished(bar.symbol, bar.low_price, bar.end_time, "LOW")
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern=SignalPattern.UNFINISHED_LOW,
                direction="SHORT",  # Price may revisit this level
                strength=0.6,
                price=bar.low_price,
                details={
                    "ask_volume": low_level.ask_volume,
                    "bid_volume": low_level.bid_volume,
                    "implication": "Incomplete auction - potential magnet level",
                    "type": "LOW"
                }
            ))

        return signals

    def check_revisit(self, bar: FootprintBar) -> List[Signal]:
        """
        Check if current bar revisited any unfinished business levels.

        When price revisits an unfinished level, the auction completes
        and the level is removed from tracking.
        """
        signals = []
        symbol = bar.symbol

        if symbol not in self.unfinished_levels:
            return signals

        revisited = []
        for price, time, direction in self.unfinished_levels[symbol]:
            # Check if current bar traded through this price
            if bar.low_price <= price <= bar.high_price:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=symbol,
                    pattern=SignalPattern.UNFINISHED_REVISITED,
                    direction="LONG" if direction == "HIGH" else "SHORT",
                    strength=0.5,
                    price=price,
                    details={
                        "original_time": time.isoformat(),
                        "type": direction,
                        "implication": "Auction completing - level fulfilled"
                    }
                ))
                revisited.append((price, time, direction))

        # Remove revisited levels
        for item in revisited:
            self.unfinished_levels[symbol].remove(item)

        return signals

    def _add_unfinished(
        self,
        symbol: str,
        price: float,
        time: datetime,
        direction: str
    ) -> None:
        """Track an unfinished business level."""
        if symbol not in self.unfinished_levels:
            self.unfinished_levels[symbol] = []

        self.unfinished_levels[symbol].append((price, time, direction))

        # Keep only recent levels (last 50)
        if len(self.unfinished_levels[symbol]) > 50:
            self.unfinished_levels[symbol] = self.unfinished_levels[symbol][-50:]

    def get_active_levels(self, symbol: str) -> List[Tuple[float, datetime, str]]:
        """Get all active unfinished levels for a symbol."""
        return self.unfinished_levels.get(symbol, [])

    def reset(self, symbol: str = None) -> None:
        """
        Reset unfinished levels.

        Args:
            symbol: If provided, reset only that symbol. Otherwise reset all.
        """
        if symbol:
            self.unfinished_levels.pop(symbol, None)
        else:
            self.unfinished_levels.clear()
