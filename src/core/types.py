"""Core data types for the order flow trading system."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Literal, Optional, Any
from enum import Enum


@dataclass
class Tick:
    """Single trade execution from the exchange."""
    timestamp: datetime
    price: float
    volume: int
    side: Literal["BID", "ASK"]  # Trade aggressor
    symbol: str


@dataclass
class PriceLevel:
    """Aggregated volume at a single price within a bar."""
    price: float
    bid_volume: int = 0   # Sell market orders (hitting bid)
    ask_volume: int = 0   # Buy market orders (lifting ask)

    @property
    def total_volume(self) -> int:
        return self.bid_volume + self.ask_volume

    @property
    def delta(self) -> int:
        """Delta at this price level: buy volume - sell volume."""
        return self.ask_volume - self.bid_volume


@dataclass
class FootprintBar:
    """A time-based bar containing volume at each price level."""
    symbol: str
    start_time: datetime
    end_time: datetime
    timeframe: int  # Seconds

    open_price: float
    high_price: float
    low_price: float
    close_price: float

    levels: Dict[float, PriceLevel] = field(default_factory=dict)

    @property
    def total_volume(self) -> int:
        return sum(level.total_volume for level in self.levels.values())

    @property
    def delta(self) -> int:
        """Bar delta: total buy volume - total sell volume."""
        return sum(level.delta for level in self.levels.values())

    @property
    def buy_volume(self) -> int:
        return sum(level.ask_volume for level in self.levels.values())

    @property
    def sell_volume(self) -> int:
        return sum(level.bid_volume for level in self.levels.values())

    def get_sorted_levels(self, ascending: bool = True) -> List[PriceLevel]:
        """Get price levels sorted by price."""
        return sorted(self.levels.values(), key=lambda x: x.price, reverse=not ascending)


class SignalPattern(Enum):
    """All detectable order flow patterns."""
    BUY_IMBALANCE = "BUY_IMBALANCE"
    SELL_IMBALANCE = "SELL_IMBALANCE"
    STACKED_BUY_IMBALANCE = "STACKED_BUY_IMBALANCE"
    STACKED_SELL_IMBALANCE = "STACKED_SELL_IMBALANCE"
    BUYING_EXHAUSTION = "BUYING_EXHAUSTION"
    SELLING_EXHAUSTION = "SELLING_EXHAUSTION"
    BULLISH_DELTA_DIVERGENCE = "BULLISH_DELTA_DIVERGENCE"
    BEARISH_DELTA_DIVERGENCE = "BEARISH_DELTA_DIVERGENCE"
    BUYING_ABSORPTION = "BUYING_ABSORPTION"
    SELLING_ABSORPTION = "SELLING_ABSORPTION"
    UNFINISHED_HIGH = "UNFINISHED_HIGH"
    UNFINISHED_LOW = "UNFINISHED_LOW"
    UNFINISHED_REVISITED = "UNFINISHED_REVISITED"


@dataclass
class Signal:
    """Output from pattern detection."""
    timestamp: datetime
    symbol: str
    pattern: SignalPattern
    direction: Literal["LONG", "SHORT"]
    strength: float  # 0.0 - 1.0
    price: float
    details: Dict[str, Any] = field(default_factory=dict)

    # Added by strategy router
    regime: Optional[str] = None
    approved: bool = False
    rejection_reason: Optional[str] = None


class Regime(Enum):
    """Market regime classifications."""
    TRENDING_UP = "TRENDING_UP"
    TRENDING_DOWN = "TRENDING_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"
    NO_TRADE = "NO_TRADE"


@dataclass
class RegimeInputs:
    """All inputs for regime classification."""

    # Trend Strength
    adx_14: float = 0.0
    adx_slope: float = 0.0

    # Trend Direction
    ema_fast: float = 0.0           # EMA(9)
    ema_slow: float = 0.0           # EMA(21)
    ema_trend: float = 0.0          # Fast - Slow
    price_vs_vwap: float = 0.0

    # Volatility
    atr_14: float = 0.0
    atr_percentile: float = 50.0    # 0-100, where current ATR sits vs last 20 days
    bar_range_avg: float = 0.0

    # Volume/Delta
    volume_vs_average: float = 1.0  # 1.0 = normal
    cumulative_delta: int = 0
    delta_slope: float = 0.0

    # Market Structure
    higher_highs: bool = False
    higher_lows: bool = False
    lower_highs: bool = False
    lower_lows: bool = False
    range_bound_bars: int = 0

    # Time Context
    minutes_since_open: int = 0
    minutes_to_close: int = 390     # Full RTH session
    is_news_window: bool = False
