"""Footprint bar aggregation and volume tracking."""

from datetime import datetime, timedelta
from typing import Callable, Dict, List, Optional, Tuple

from src.core.types import Tick, PriceLevel, FootprintBar
from src.core.constants import normalize_price


class FootprintAggregator:
    """Aggregates ticks into time-based footprint bars."""

    def __init__(self, timeframe_seconds: int = 300):
        """
        Initialize the aggregator.

        Args:
            timeframe_seconds: Bar duration in seconds (default 300 = 5 minutes)
        """
        self.timeframe = timeframe_seconds
        self.current_bar: Optional[FootprintBar] = None
        self.completed_bars: List[FootprintBar] = []
        self.bar_callbacks: List[Callable[[FootprintBar], None]] = []

    def process_tick(self, tick: Tick) -> Optional[FootprintBar]:
        """
        Process incoming tick.

        Returns completed bar if bar closed, else None.
        """
        bar_start = self._get_bar_start(tick.timestamp)

        if self.current_bar is None:
            self.current_bar = self._create_new_bar(tick, bar_start)
        elif bar_start > self.current_bar.start_time:
            # Close current bar, start new one
            completed = self.current_bar
            self.completed_bars.append(completed)
            self.current_bar = self._create_new_bar(tick, bar_start)
            self._add_tick_to_bar(tick)
            self._notify_bar_complete(completed)
            return completed

        self._add_tick_to_bar(tick)
        return None

    def _add_tick_to_bar(self, tick: Tick) -> None:
        """Add tick volume to appropriate price level."""
        bar = self.current_bar
        price = normalize_price(tick.price, tick.symbol)

        # Update OHLC
        bar.high_price = max(bar.high_price, price)
        bar.low_price = min(bar.low_price, price)
        bar.close_price = price

        # Get or create price level
        if price not in bar.levels:
            bar.levels[price] = PriceLevel(price=price)

        level = bar.levels[price]

        # Add volume to correct side
        if tick.side == "ASK":
            level.ask_volume += tick.volume
        else:
            level.bid_volume += tick.volume

    def _get_bar_start(self, timestamp: datetime) -> datetime:
        """Round timestamp down to bar boundary."""
        seconds = int(timestamp.timestamp())
        bar_seconds = (seconds // self.timeframe) * self.timeframe
        return datetime.fromtimestamp(bar_seconds, tz=timestamp.tzinfo)

    def _create_new_bar(self, tick: Tick, bar_start: datetime) -> FootprintBar:
        """Create a new footprint bar."""
        price = normalize_price(tick.price, tick.symbol)
        return FootprintBar(
            symbol=tick.symbol,
            start_time=bar_start,
            end_time=bar_start + timedelta(seconds=self.timeframe),
            timeframe=self.timeframe,
            open_price=price,
            high_price=price,
            low_price=price,
            close_price=price,
            levels={}
        )

    def _notify_bar_complete(self, bar: FootprintBar) -> None:
        """Notify all registered callbacks that a bar completed."""
        for callback in self.bar_callbacks:
            callback(bar)

    def on_bar_complete(self, callback: Callable[[FootprintBar], None]) -> None:
        """Register a callback for bar completion events."""
        self.bar_callbacks.append(callback)

    def get_recent_bars(self, count: int = 10) -> List[FootprintBar]:
        """Get the most recent completed bars."""
        return self.completed_bars[-count:]

    def reset(self) -> None:
        """Reset the aggregator state."""
        self.current_bar = None
        self.completed_bars.clear()


class CumulativeDelta:
    """Tracks cumulative delta across bars."""

    def __init__(self):
        self.value: int = 0
        self.history: List[Tuple[datetime, int]] = []

    def update(self, bar: FootprintBar) -> int:
        """Add bar's delta to cumulative total."""
        self.value += bar.delta
        self.history.append((bar.end_time, self.value))

        # Keep history bounded
        if len(self.history) > 1000:
            self.history = self.history[-1000:]

        return self.value

    def reset(self) -> None:
        """Reset cumulative delta (e.g., at session start)."""
        self.value = 0
        self.history.clear()

    def get_slope(self, bars: int = 5) -> float:
        """Calculate slope of cumulative delta over recent bars."""
        if len(self.history) < 2:
            return 0.0

        recent = self.history[-bars:]
        if len(recent) < 2:
            return 0.0

        # Simple linear regression slope
        n = len(recent)
        sum_x = sum(range(n))
        sum_y = sum(v for _, v in recent)
        sum_xy = sum(i * v for i, (_, v) in enumerate(recent))
        sum_xx = sum(i * i for i in range(n))

        denominator = n * sum_xx - sum_x * sum_x
        if denominator == 0:
            return 0.0

        return (n * sum_xy - sum_x * sum_y) / denominator


class VolumeProfile:
    """Builds a volume profile across multiple bars."""

    def __init__(self):
        self.levels: Dict[float, PriceLevel] = {}
        self.bar_count: int = 0

    def add_bar(self, bar: FootprintBar) -> None:
        """Merge bar's volume into profile."""
        for price, level in bar.levels.items():
            if price not in self.levels:
                self.levels[price] = PriceLevel(price=price)
            self.levels[price].bid_volume += level.bid_volume
            self.levels[price].ask_volume += level.ask_volume

        self.bar_count += 1

    def get_poc(self) -> Optional[float]:
        """
        Get Point of Control: price with highest total volume.

        Returns None if no levels exist.
        """
        if not self.levels:
            return None
        return max(self.levels.values(), key=lambda x: x.total_volume).price

    def get_value_area(self, percentage: float = 0.70) -> Optional[Tuple[float, float]]:
        """
        Get Value Area: price range containing X% of volume.

        Returns (VAL, VAH) - Value Area Low and High.
        Returns None if no levels exist.
        """
        if not self.levels:
            return None

        total = sum(level.total_volume for level in self.levels.values())
        if total == 0:
            return None

        target = total * percentage

        # Sort levels by volume descending
        sorted_levels = sorted(
            self.levels.values(),
            key=lambda x: x.total_volume,
            reverse=True
        )

        accumulated = 0
        prices = []
        for level in sorted_levels:
            accumulated += level.total_volume
            prices.append(level.price)
            if accumulated >= target:
                break

        if not prices:
            return None

        return min(prices), max(prices)

    def get_high_volume_nodes(self, threshold_pct: float = 0.10) -> List[float]:
        """
        Get prices with volume above threshold percentage of POC volume.

        Args:
            threshold_pct: Minimum volume as percentage of POC (default 10%)

        Returns:
            List of high-volume price levels.
        """
        if not self.levels:
            return []

        poc = self.get_poc()
        if poc is None:
            return []

        poc_volume = self.levels[poc].total_volume
        threshold = poc_volume * threshold_pct

        return [
            level.price
            for level in self.levels.values()
            if level.total_volume >= threshold
        ]

    def get_low_volume_nodes(self, threshold_pct: float = 0.05) -> List[float]:
        """
        Get prices with volume below threshold percentage of total.

        These "single prints" or low volume areas often act as
        support/resistance or fast-travel zones.

        Args:
            threshold_pct: Maximum volume as percentage of total (default 5%)

        Returns:
            List of low-volume price levels.
        """
        if not self.levels:
            return []

        total = sum(level.total_volume for level in self.levels.values())
        if total == 0:
            return []

        threshold = total * threshold_pct

        return [
            level.price
            for level in self.levels.values()
            if level.total_volume <= threshold
        ]

    def reset(self) -> None:
        """Reset the volume profile."""
        self.levels.clear()
        self.bar_count = 0
