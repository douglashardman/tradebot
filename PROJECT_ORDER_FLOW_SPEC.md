# Order Flow Analysis System - Project Specification

## Overview

Build a real-time order flow analysis system that processes tick-by-tick market data to identify high-probability trading signals based on volume imbalances, exhaustion patterns, absorption, and delta divergence.

**Target Markets:** ES (E-mini S&P 500), NQ (E-mini Nasdaq), CL (Crude Oil), GC (Gold), or any futures contract with sufficient liquidity.

---

## Table of Contents

1. [Data Requirements](#1-data-requirements)
2. [Core Data Structures](#2-core-data-structures)
3. [Footprint Chart Construction](#3-footprint-chart-construction)
4. [Core Calculations](#4-core-calculations)
5. [Signal Detection Patterns](#5-signal-detection-patterns)
6. [System Architecture](#6-system-architecture)
7. [Implementation Phases](#7-implementation-phases)
8. [Configuration & Tuning](#8-configuration--tuning)
9. [Testing Strategy](#9-testing-strategy)
10. [Future Enhancements](#10-future-enhancements)

---

## 1. Data Requirements

### 1.1 Tick Data Fields

Each incoming tick must contain:

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | datetime (ms precision) | When the trade occurred |
| `price` | float | Execution price |
| `volume` | int | Number of contracts traded |
| `side` | enum | `BID` (sell market order) or `ASK` (buy market order) |
| `symbol` | string | Contract identifier (e.g., "ESZ24") |

**Critical:** The `side` field must indicate whether the trade was initiated by a buyer (lifting the offer/ask) or seller (hitting the bid). This is called "trade aggressor" or "trade initiator" attribution.

### 1.2 Data Sources

| Provider | Protocol | Notes |
|----------|----------|-------|
| **Rithmic** | R|Protocol API | Popular with retail, good ES/NQ data |
| **CQG** | CQG API | Professional grade, used by many platforms |
| **dxFeed** | dxFeed API | Aggregated feed, good coverage |
| **IQFeed** | DTN IQFeed | Cost-effective, good for development |
| **Sierra Chart** | Denali Feed | If using Sierra as frontend |
| **NinjaTrader** | Built-in | Can export or access via addon |
| **Interactive Brokers** | TWS API | Limited tick data, not ideal |

### 1.3 Data Volume Estimates

For ES during regular trading hours (RTH):
- ~50,000 - 200,000 ticks per day
- Peak: ~500 ticks/second during high volatility
- Average: ~20-50 ticks/second

### 1.4 Historical Data

For backtesting and development:
- Minimum: 20 trading days of tick data
- Recommended: 3-6 months
- Storage: ~50-200 MB per day per symbol (compressed)

---

## 2. Core Data Structures

### 2.1 Tick

```python
@dataclass
class Tick:
    timestamp: datetime
    price: float
    volume: int
    side: Literal["BID", "ASK"]
    symbol: str
```

### 2.2 Price Level

Aggregated volume at a single price within a bar:

```python
@dataclass
class PriceLevel:
    price: float
    bid_volume: int    # Volume from sell market orders (hitting bid)
    ask_volume: int    # Volume from buy market orders (lifting ask)

    @property
    def total_volume(self) -> int:
        return self.bid_volume + self.ask_volume

    @property
    def delta(self) -> int:
        """Delta at this price level."""
        return self.ask_volume - self.bid_volume
```

### 2.3 Footprint Bar

A single time-based bar containing all price levels:

```python
@dataclass
class FootprintBar:
    symbol: str
    start_time: datetime
    end_time: datetime
    timeframe: int              # Bar duration in seconds

    open_price: float
    high_price: float
    low_price: float
    close_price: float

    levels: Dict[float, PriceLevel]  # Keyed by price

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
```

### 2.4 Signal

Output from pattern detection:

```python
@dataclass
class Signal:
    timestamp: datetime
    symbol: str
    pattern: str                # e.g., "EXHAUSTION", "ABSORPTION", "IMBALANCE"
    direction: Literal["LONG", "SHORT"]
    strength: float             # 0.0 - 1.0 confidence score
    price: float                # Reference price
    details: Dict[str, Any]     # Pattern-specific data
```

---

## 3. Footprint Chart Construction

### 3.1 Bar Aggregation

Ticks are aggregated into bars based on time intervals:

```python
class FootprintAggregator:
    def __init__(self, timeframe_seconds: int = 300):  # Default 5-min bars
        self.timeframe = timeframe_seconds
        self.current_bar: Optional[FootprintBar] = None
        self.completed_bars: List[FootprintBar] = []

    def process_tick(self, tick: Tick) -> Optional[FootprintBar]:
        """
        Process incoming tick. Returns completed bar if bar closed, else None.
        """
        bar_start = self._get_bar_start(tick.timestamp)

        # Check if we need to close current bar and start new one
        if self.current_bar is None:
            self.current_bar = self._create_new_bar(tick, bar_start)
        elif bar_start > self.current_bar.start_time:
            # Close current bar, start new one
            completed = self.current_bar
            self.completed_bars.append(completed)
            self.current_bar = self._create_new_bar(tick, bar_start)
            self._add_tick_to_bar(tick)
            return completed

        self._add_tick_to_bar(tick)
        return None

    def _add_tick_to_bar(self, tick: Tick):
        """Add tick volume to appropriate price level."""
        bar = self.current_bar
        price = tick.price

        # Update OHLC
        bar.high_price = max(bar.high_price, price)
        bar.low_price = min(bar.low_price, price)
        bar.close_price = price

        # Get or create price level
        if price not in bar.levels:
            bar.levels[price] = PriceLevel(price=price, bid_volume=0, ask_volume=0)

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
        return FootprintBar(
            symbol=tick.symbol,
            start_time=bar_start,
            end_time=bar_start + timedelta(seconds=self.timeframe),
            timeframe=self.timeframe,
            open_price=tick.price,
            high_price=tick.price,
            low_price=tick.price,
            close_price=tick.price,
            levels={}
        )
```

### 3.2 Tick Size Handling

Futures have defined tick sizes. Prices should be normalized:

| Symbol | Tick Size | Tick Value |
|--------|-----------|------------|
| ES | 0.25 | $12.50 |
| NQ | 0.25 | $5.00 |
| CL | 0.01 | $10.00 |
| GC | 0.10 | $10.00 |
| MES | 0.25 | $1.25 |
| MNQ | 0.25 | $0.50 |

```python
TICK_SIZES = {
    "ES": 0.25, "MES": 0.25,
    "NQ": 0.25, "MNQ": 0.25,
    "CL": 0.01,
    "GC": 0.10,
}

def normalize_price(price: float, symbol: str) -> float:
    """Round price to valid tick increment."""
    tick_size = TICK_SIZES.get(symbol[:2], 0.25)  # Default to 0.25
    return round(price / tick_size) * tick_size
```

---

## 4. Core Calculations

### 4.1 Bar Delta

The net difference between aggressive buying and selling for an entire bar:

```python
def calculate_bar_delta(bar: FootprintBar) -> int:
    """
    Delta = Total Ask Volume - Total Bid Volume

    Positive delta = Net buying pressure (bullish)
    Negative delta = Net selling pressure (bearish)
    """
    return bar.delta  # Property defined in FootprintBar
```

### 4.2 Delta at Price

Delta calculated for each individual price level:

```python
def calculate_delta_at_price(bar: FootprintBar) -> Dict[float, int]:
    """
    Returns delta for each price level in the bar.
    """
    return {price: level.delta for price, level in bar.levels.items()}
```

### 4.3 Cumulative Delta

Running total of delta across multiple bars:

```python
class CumulativeDelta:
    def __init__(self):
        self.value = 0
        self.history: List[Tuple[datetime, int]] = []

    def update(self, bar: FootprintBar) -> int:
        """Add bar's delta to cumulative total."""
        self.value += bar.delta
        self.history.append((bar.end_time, self.value))
        return self.value

    def reset(self):
        """Reset cumulative delta (e.g., at session start)."""
        self.value = 0
        self.history.clear()
```

### 4.4 Volume Profile

Aggregate volume across multiple bars at each price level:

```python
class VolumeProfile:
    def __init__(self):
        self.levels: Dict[float, PriceLevel] = {}

    def add_bar(self, bar: FootprintBar):
        """Merge bar's volume into profile."""
        for price, level in bar.levels.items():
            if price not in self.levels:
                self.levels[price] = PriceLevel(price=price, bid_volume=0, ask_volume=0)
            self.levels[price].bid_volume += level.bid_volume
            self.levels[price].ask_volume += level.ask_volume

    def get_poc(self) -> float:
        """Point of Control: price with highest total volume."""
        return max(self.levels.values(), key=lambda x: x.total_volume).price

    def get_value_area(self, percentage: float = 0.70) -> Tuple[float, float]:
        """
        Value Area: price range containing X% of volume.
        Returns (VAL, VAH) - Value Area Low and High.
        """
        total = sum(level.total_volume for level in self.levels.values())
        target = total * percentage

        sorted_levels = sorted(self.levels.values(), key=lambda x: x.total_volume, reverse=True)

        accumulated = 0
        prices = []
        for level in sorted_levels:
            accumulated += level.total_volume
            prices.append(level.price)
            if accumulated >= target:
                break

        return min(prices), max(prices)
```

---

## 5. Signal Detection Patterns

### 5.1 Aggression / Imbalance Detection

Identifies one-sided aggressive activity using diagonal comparison:

```python
class ImbalanceDetector:
    def __init__(self, threshold: float = 3.0, min_volume: int = 10):
        """
        threshold: Ratio required to flag imbalance (3.0 = 300%)
        min_volume: Minimum volume on dominant side to consider
        """
        self.threshold = threshold
        self.min_volume = min_volume

    def detect(self, bar: FootprintBar) -> List[Signal]:
        signals = []
        levels = bar.get_sorted_levels(ascending=True)

        for i in range(1, len(levels)):
            current = levels[i]
            below = levels[i - 1]

            # Buy imbalance: Ask volume at current vs Bid volume at level below
            # This shows buyers aggressively lifting offers
            if below.bid_volume > 0 and current.ask_volume >= self.min_volume:
                ratio = current.ask_volume / below.bid_volume
                if ratio >= self.threshold:
                    signals.append(Signal(
                        timestamp=bar.end_time,
                        symbol=bar.symbol,
                        pattern="BUY_IMBALANCE",
                        direction="LONG",
                        strength=min(ratio / 10, 1.0),  # Normalize to 0-1
                        price=current.price,
                        details={
                            "ratio": ratio,
                            "ask_volume": current.ask_volume,
                            "bid_volume_below": below.bid_volume
                        }
                    ))

            # Sell imbalance: Bid volume at current vs Ask volume at level above
            if i < len(levels) - 1:
                above = levels[i + 1]
                if above.ask_volume > 0 and current.bid_volume >= self.min_volume:
                    ratio = current.bid_volume / above.ask_volume
                    if ratio >= self.threshold:
                        signals.append(Signal(
                            timestamp=bar.end_time,
                            symbol=bar.symbol,
                            pattern="SELL_IMBALANCE",
                            direction="SHORT",
                            strength=min(ratio / 10, 1.0),
                            price=current.price,
                            details={
                                "ratio": ratio,
                                "bid_volume": current.bid_volume,
                                "ask_volume_above": above.ask_volume
                            }
                        ))

        return signals

    def detect_stacked_imbalances(self, bar: FootprintBar, min_stack: int = 3) -> List[Signal]:
        """
        Detect consecutive imbalances stacked vertically.
        More significant when 3+ imbalances align.
        """
        imbalances = self.detect(bar)

        # Group by direction and check for consecutive prices
        buy_imbalances = sorted(
            [s for s in imbalances if s.direction == "LONG"],
            key=lambda x: x.price
        )
        sell_imbalances = sorted(
            [s for s in imbalances if s.direction == "SHORT"],
            key=lambda x: x.price
        )

        signals = []

        # Check for stacked buy imbalances
        stacks = self._find_stacks(buy_imbalances, bar.symbol)
        for stack in stacks:
            if len(stack) >= min_stack:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=bar.symbol,
                    pattern="STACKED_BUY_IMBALANCE",
                    direction="LONG",
                    strength=min(len(stack) / 5, 1.0),
                    price=stack[-1].price,  # Top of stack
                    details={"stack_size": len(stack), "prices": [s.price for s in stack]}
                ))

        # Check for stacked sell imbalances
        stacks = self._find_stacks(sell_imbalances, bar.symbol)
        for stack in stacks:
            if len(stack) >= min_stack:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=bar.symbol,
                    pattern="STACKED_SELL_IMBALANCE",
                    direction="SHORT",
                    strength=min(len(stack) / 5, 1.0),
                    price=stack[0].price,  # Bottom of stack
                    details={"stack_size": len(stack), "prices": [s.price for s in stack]}
                ))

        return signals

    def _find_stacks(self, imbalances: List[Signal], symbol: str) -> List[List[Signal]]:
        """Find groups of consecutive price levels with imbalances."""
        if not imbalances:
            return []

        tick_size = TICK_SIZES.get(symbol[:2], 0.25)
        stacks = []
        current_stack = [imbalances[0]]

        for i in range(1, len(imbalances)):
            prev_price = imbalances[i - 1].price
            curr_price = imbalances[i].price

            if abs(curr_price - prev_price - tick_size) < 0.001:  # Consecutive
                current_stack.append(imbalances[i])
            else:
                if len(current_stack) > 1:
                    stacks.append(current_stack)
                current_stack = [imbalances[i]]

        if len(current_stack) > 1:
            stacks.append(current_stack)

        return stacks
```

### 5.2 Exhaustion Detection

Identifies declining volume at bar extremes:

```python
class ExhaustionDetector:
    def __init__(self, min_levels: int = 3, min_decline_pct: float = 0.30):
        """
        min_levels: Minimum consecutive levels showing decline
        min_decline_pct: Minimum overall decline percentage (30% = volume dropped by 30%+)
        """
        self.min_levels = min_levels
        self.min_decline_pct = min_decline_pct

    def detect(self, bar: FootprintBar) -> List[Signal]:
        signals = []
        levels = bar.get_sorted_levels(ascending=True)

        if len(levels) < self.min_levels:
            return signals

        # Check for buying exhaustion at bar top
        # Look for declining ask volume as price increases
        top_levels = levels[-self.min_levels - 2:]  # Top N+2 levels
        buy_exhaustion = self._check_exhaustion(
            [level.ask_volume for level in top_levels],
            ascending_price=True
        )

        if buy_exhaustion:
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern="BUYING_EXHAUSTION",
                direction="SHORT",  # Exhausted buyers = potential short
                strength=buy_exhaustion["strength"],
                price=bar.high_price,
                details=buy_exhaustion
            ))

        # Check for selling exhaustion at bar bottom
        # Look for declining bid volume as price decreases
        bottom_levels = levels[:self.min_levels + 2]  # Bottom N+2 levels
        sell_exhaustion = self._check_exhaustion(
            [level.bid_volume for level in reversed(bottom_levels)],
            ascending_price=False
        )

        if sell_exhaustion:
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern="SELLING_EXHAUSTION",
                direction="LONG",  # Exhausted sellers = potential long
                strength=sell_exhaustion["strength"],
                price=bar.low_price,
                details=sell_exhaustion
            ))

        return signals

    def _check_exhaustion(self, volumes: List[int], ascending_price: bool) -> Optional[Dict]:
        """
        Check if volumes show sequential decline.

        For buying exhaustion (ascending price): volumes should decrease
        For selling exhaustion (descending price): volumes should decrease
        """
        if len(volumes) < self.min_levels:
            return None

        # Count consecutive declines
        declines = 0
        for i in range(1, len(volumes)):
            if volumes[i] < volumes[i - 1]:
                declines += 1
            else:
                break  # Sequence broken

        if declines < self.min_levels - 1:
            return None

        # Calculate overall decline percentage
        if volumes[0] == 0:
            return None

        decline_pct = (volumes[0] - volumes[declines]) / volumes[0]

        if decline_pct < self.min_decline_pct:
            return None

        return {
            "consecutive_declines": declines,
            "decline_percentage": decline_pct,
            "volumes": volumes[:declines + 1],
            "strength": min(decline_pct, 1.0)
        }
```

### 5.3 Delta Divergence Detection

Identifies price/delta divergence:

```python
class DeltaDivergenceDetector:
    def __init__(self, lookback: int = 5):
        """
        lookback: Number of bars to analyze for divergence
        """
        self.lookback = lookback
        self.bar_history: List[FootprintBar] = []

    def add_bar(self, bar: FootprintBar) -> List[Signal]:
        """Add new bar and check for divergence patterns."""
        self.bar_history.append(bar)

        # Keep only lookback + some buffer
        if len(self.bar_history) > self.lookback * 2:
            self.bar_history = self.bar_history[-self.lookback * 2:]

        if len(self.bar_history) < self.lookback:
            return []

        return self._detect_divergence()

    def _detect_divergence(self) -> List[Signal]:
        signals = []
        recent_bars = self.bar_history[-self.lookback:]

        # Get highs, lows, and deltas
        highs = [bar.high_price for bar in recent_bars]
        lows = [bar.low_price for bar in recent_bars]
        deltas = [bar.delta for bar in recent_bars]

        current_bar = recent_bars[-1]

        # Bearish divergence: Price making higher highs, delta making lower highs
        if self._is_higher_high(highs) and self._is_lower_high(deltas):
            signals.append(Signal(
                timestamp=current_bar.end_time,
                symbol=current_bar.symbol,
                pattern="BEARISH_DELTA_DIVERGENCE",
                direction="SHORT",
                strength=0.7,
                price=current_bar.close_price,
                details={
                    "price_high": max(highs),
                    "current_delta": deltas[-1],
                    "delta_trend": "declining"
                }
            ))

        # Bullish divergence: Price making lower lows, delta making higher lows
        if self._is_lower_low(lows) and self._is_higher_low(deltas):
            signals.append(Signal(
                timestamp=current_bar.end_time,
                symbol=current_bar.symbol,
                pattern="BULLISH_DELTA_DIVERGENCE",
                direction="LONG",
                strength=0.7,
                price=current_bar.close_price,
                details={
                    "price_low": min(lows),
                    "current_delta": deltas[-1],
                    "delta_trend": "rising"
                }
            ))

        return signals

    def _is_higher_high(self, values: List[float]) -> bool:
        """Check if recent values show higher high pattern."""
        if len(values) < 3:
            return False
        return values[-1] > max(values[:-1])

    def _is_lower_high(self, values: List[int]) -> bool:
        """Check if recent highs are declining."""
        if len(values) < 3:
            return False
        # Find local maxima and check if declining
        peaks = self._find_peaks(values)
        if len(peaks) < 2:
            return False
        return peaks[-1][1] < peaks[-2][1]

    def _is_lower_low(self, values: List[float]) -> bool:
        """Check if recent values show lower low pattern."""
        if len(values) < 3:
            return False
        return values[-1] < min(values[:-1])

    def _is_higher_low(self, values: List[int]) -> bool:
        """Check if recent lows are rising."""
        if len(values) < 3:
            return False
        troughs = self._find_troughs(values)
        if len(troughs) < 2:
            return False
        return troughs[-1][1] > troughs[-2][1]

    def _find_peaks(self, values: List) -> List[Tuple[int, Any]]:
        """Find local maxima."""
        peaks = []
        for i in range(1, len(values) - 1):
            if values[i] > values[i-1] and values[i] > values[i+1]:
                peaks.append((i, values[i]))
        return peaks

    def _find_troughs(self, values: List) -> List[Tuple[int, Any]]:
        """Find local minima."""
        troughs = []
        for i in range(1, len(values) - 1):
            if values[i] < values[i-1] and values[i] < values[i+1]:
                troughs.append((i, values[i]))
        return troughs
```

### 5.4 Absorption Detection

Identifies passive limit orders absorbing aggressive market orders:

```python
class AbsorptionDetector:
    def __init__(self, min_volume: int = 100, delta_threshold: float = 0.3):
        """
        min_volume: Minimum total volume at price level to consider
        delta_threshold: Max delta/volume ratio to indicate absorption (absorbed = low net movement)
        """
        self.min_volume = min_volume
        self.delta_threshold = delta_threshold

    def detect(self, bar: FootprintBar) -> List[Signal]:
        signals = []

        # Check for absorption at bar high (sellers absorbing buyers)
        high_absorption = self._check_high_absorption(bar)
        if high_absorption:
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern="SELLING_ABSORPTION",
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
                pattern="BUYING_ABSORPTION",
                direction="LONG",
                strength=low_absorption["strength"],
                price=bar.low_price,
                details=low_absorption
            ))

        return signals

    def _check_high_absorption(self, bar: FootprintBar) -> Optional[Dict]:
        """
        Check if aggressive buying was absorbed at the high.
        Signs: High ask volume at top prices, but price didn't continue higher.
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

        # Absorption = high buying volume but price failed to advance
        # Indicated by: significant ask volume, but close is not at high
        if total_ask_volume < total_volume * 0.6:  # Need at least 60% buying
            return None

        # Price rejection: close is in lower half of bar
        bar_range = bar.high_price - bar.low_price
        if bar_range == 0:
            return None

        close_position = (bar.close_price - bar.low_price) / bar_range
        if close_position > 0.5:  # Close in upper half = no rejection
            return None

        return {
            "ask_volume": total_ask_volume,
            "bid_volume": total_bid_volume,
            "close_position": close_position,
            "strength": min((1 - close_position) * total_ask_volume / self.min_volume, 1.0)
        }

    def _check_low_absorption(self, bar: FootprintBar) -> Optional[Dict]:
        """
        Check if aggressive selling was absorbed at the low.
        Signs: High bid volume at bottom prices, but price didn't continue lower.
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

        # Absorption = high selling volume but price failed to decline
        if total_bid_volume < total_volume * 0.6:  # Need at least 60% selling
            return None

        # Price rejection: close is in upper half of bar
        bar_range = bar.high_price - bar.low_price
        if bar_range == 0:
            return None

        close_position = (bar.close_price - bar.low_price) / bar_range
        if close_position < 0.5:  # Close in lower half = no rejection
            return None

        return {
            "ask_volume": total_ask_volume,
            "bid_volume": total_bid_volume,
            "close_position": close_position,
            "strength": min(close_position * total_bid_volume / self.min_volume, 1.0)
        }
```

### 5.5 Unfinished Business Detection

Identifies incomplete auctions at price extremes:

```python
class UnfinishedBusinessDetector:
    def __init__(self, max_volume_threshold: int = 5):
        """
        max_volume_threshold: Maximum volume on one side to consider "unfinished"
        """
        self.threshold = max_volume_threshold
        self.unfinished_levels: Dict[str, List[Tuple[float, datetime, str]]] = {}  # symbol -> [(price, time, direction)]

    def detect(self, bar: FootprintBar) -> List[Signal]:
        signals = []
        levels = bar.get_sorted_levels(ascending=True)

        if not levels:
            return signals

        # Check bar high for unfinished business (no/low ask volume)
        high_level = levels[-1]
        if high_level.ask_volume <= self.threshold and high_level.bid_volume > self.threshold:
            # Buyers hit this level but couldn't lift the offer
            self._add_unfinished(bar.symbol, bar.high_price, bar.end_time, "HIGH")
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern="UNFINISHED_HIGH",
                direction="LONG",  # Price may revisit this level
                strength=0.6,
                price=bar.high_price,
                details={
                    "ask_volume": high_level.ask_volume,
                    "bid_volume": high_level.bid_volume,
                    "implication": "Incomplete auction - potential magnet level"
                }
            ))

        # Check bar low for unfinished business (no/low bid volume)
        low_level = levels[0]
        if low_level.bid_volume <= self.threshold and low_level.ask_volume > self.threshold:
            # Sellers hit this level but couldn't break the bid
            self._add_unfinished(bar.symbol, bar.low_price, bar.end_time, "LOW")
            signals.append(Signal(
                timestamp=bar.end_time,
                symbol=bar.symbol,
                pattern="UNFINISHED_LOW",
                direction="SHORT",  # Price may revisit this level
                strength=0.6,
                price=bar.low_price,
                details={
                    "ask_volume": low_level.ask_volume,
                    "bid_volume": low_level.bid_volume,
                    "implication": "Incomplete auction - potential magnet level"
                }
            ))

        return signals

    def _add_unfinished(self, symbol: str, price: float, time: datetime, direction: str):
        """Track unfinished business levels."""
        if symbol not in self.unfinished_levels:
            self.unfinished_levels[symbol] = []
        self.unfinished_levels[symbol].append((price, time, direction))

        # Keep only recent levels (last 50)
        self.unfinished_levels[symbol] = self.unfinished_levels[symbol][-50:]

    def check_revisit(self, bar: FootprintBar) -> List[Signal]:
        """Check if current bar revisited any unfinished business levels."""
        signals = []
        symbol = bar.symbol

        if symbol not in self.unfinished_levels:
            return signals

        revisited = []
        for price, time, direction in self.unfinished_levels[symbol]:
            if bar.low_price <= price <= bar.high_price:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=symbol,
                    pattern="UNFINISHED_REVISITED",
                    direction="LONG" if direction == "HIGH" else "SHORT",
                    strength=0.5,
                    price=price,
                    details={
                        "original_time": time,
                        "type": direction,
                        "implication": "Auction completing"
                    }
                ))
                revisited.append((price, time, direction))

        # Remove revisited levels
        for item in revisited:
            self.unfinished_levels[symbol].remove(item)

        return signals
```

---

## 6. System Architecture

### 6.1 High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        DATA LAYER                                │
├─────────────────────────────────────────────────────────────────┤
│  Data Feed Adapter                                               │
│  (Rithmic / CQG / IQFeed / etc.)                                │
│  └── Tick Normalizer (price rounding, field mapping)            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     AGGREGATION LAYER                            │
├─────────────────────────────────────────────────────────────────┤
│  Footprint Aggregator                                            │
│  ├── Time-based bars (1m, 5m, 15m, etc.)                        │
│  ├── Volume-based bars (optional)                                │
│  └── Session management (RTH/ETH)                                │
│                                                                  │
│  Cumulative Delta Tracker                                        │
│  Volume Profile Builder                                          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     ANALYSIS LAYER                               │
├─────────────────────────────────────────────────────────────────┤
│  Pattern Detectors (run on each completed bar)                   │
│  ├── ImbalanceDetector                                           │
│  ├── ExhaustionDetector                                          │
│  ├── DeltaDivergenceDetector                                     │
│  ├── AbsorptionDetector                                          │
│  └── UnfinishedBusinessDetector                                  │
│                                                                  │
│  Signal Aggregator                                               │
│  └── Combines signals, filters noise, ranks by strength          │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      OUTPUT LAYER                                │
├─────────────────────────────────────────────────────────────────┤
│  Alert Manager                                                   │
│  ├── Console output                                              │
│  ├── Desktop notifications                                       │
│  ├── Webhook (Discord, Telegram, etc.)                          │
│  └── Audio alerts                                                │
│                                                                  │
│  Signal Logger (CSV/JSON/Database)                               │
│  Performance Tracker (backtest mode)                             │
└─────────────────────────────────────────────────────────────────┘
```

### 6.2 Core Components

```python
# Main engine orchestrating all components

class OrderFlowEngine:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.symbol = config["symbol"]
        self.timeframe = config.get("timeframe", 300)  # 5 min default

        # Aggregation
        self.aggregator = FootprintAggregator(self.timeframe)
        self.cumulative_delta = CumulativeDelta()
        self.volume_profile = VolumeProfile()

        # Detectors
        self.imbalance_detector = ImbalanceDetector(
            threshold=config.get("imbalance_threshold", 3.0),
            min_volume=config.get("imbalance_min_volume", 10)
        )
        self.exhaustion_detector = ExhaustionDetector(
            min_levels=config.get("exhaustion_min_levels", 3)
        )
        self.divergence_detector = DeltaDivergenceDetector(
            lookback=config.get("divergence_lookback", 5)
        )
        self.absorption_detector = AbsorptionDetector(
            min_volume=config.get("absorption_min_volume", 100)
        )
        self.unfinished_detector = UnfinishedBusinessDetector()

        # Output
        self.alert_manager = AlertManager(config.get("alerts", {}))
        self.signal_log: List[Signal] = []

    def process_tick(self, tick: Tick):
        """Process incoming tick data."""
        completed_bar = self.aggregator.process_tick(tick)

        if completed_bar:
            self._analyze_bar(completed_bar)

    def _analyze_bar(self, bar: FootprintBar):
        """Run all pattern detectors on completed bar."""
        signals = []

        # Update cumulative tracking
        self.cumulative_delta.update(bar)
        self.volume_profile.add_bar(bar)

        # Run detectors
        signals.extend(self.imbalance_detector.detect(bar))
        signals.extend(self.imbalance_detector.detect_stacked_imbalances(bar))
        signals.extend(self.exhaustion_detector.detect(bar))
        signals.extend(self.divergence_detector.add_bar(bar))
        signals.extend(self.absorption_detector.detect(bar))
        signals.extend(self.unfinished_detector.detect(bar))
        signals.extend(self.unfinished_detector.check_revisit(bar))

        # Filter and process signals
        for signal in signals:
            self.signal_log.append(signal)
            self.alert_manager.send(signal)

    def get_current_state(self) -> Dict:
        """Get current analysis state for display."""
        current_bar = self.aggregator.current_bar
        return {
            "symbol": self.symbol,
            "cumulative_delta": self.cumulative_delta.value,
            "current_bar": {
                "delta": current_bar.delta if current_bar else 0,
                "volume": current_bar.total_volume if current_bar else 0,
                "levels": len(current_bar.levels) if current_bar else 0,
            },
            "poc": self.volume_profile.get_poc() if self.volume_profile.levels else None,
            "recent_signals": self.signal_log[-10:],
        }
```

### 6.3 Alert Manager

```python
class AlertManager:
    def __init__(self, config: Dict):
        self.config = config
        self.min_strength = config.get("min_strength", 0.5)
        self.enabled_patterns = config.get("patterns", None)  # None = all

        # Notification channels
        self.console_enabled = config.get("console", True)
        self.webhook_url = config.get("webhook_url")
        self.sound_enabled = config.get("sound", False)

    def send(self, signal: Signal):
        """Send signal through configured channels."""
        # Filter by strength
        if signal.strength < self.min_strength:
            return

        # Filter by pattern type
        if self.enabled_patterns and signal.pattern not in self.enabled_patterns:
            return

        message = self._format_signal(signal)

        if self.console_enabled:
            self._console_alert(signal, message)

        if self.webhook_url:
            self._webhook_alert(message)

        if self.sound_enabled:
            self._play_sound(signal)

    def _format_signal(self, signal: Signal) -> str:
        direction_emoji = "🟢" if signal.direction == "LONG" else "🔴"
        return (
            f"{direction_emoji} {signal.pattern} | {signal.symbol} @ {signal.price:.2f} | "
            f"Strength: {signal.strength:.0%} | {signal.timestamp.strftime('%H:%M:%S')}"
        )

    def _console_alert(self, signal: Signal, message: str):
        # Color coding for terminal
        color = "\033[92m" if signal.direction == "LONG" else "\033[91m"
        reset = "\033[0m"
        print(f"{color}{message}{reset}")

    def _webhook_alert(self, message: str):
        try:
            import requests
            requests.post(self.webhook_url, json={"content": message}, timeout=5)
        except Exception as e:
            print(f"Webhook error: {e}")

    def _play_sound(self, signal: Signal):
        # Platform-specific sound
        try:
            import os
            if signal.direction == "LONG":
                os.system("afplay /System/Library/Sounds/Glass.aiff 2>/dev/null || paplay /usr/share/sounds/freedesktop/stereo/complete.oga 2>/dev/null &")
            else:
                os.system("afplay /System/Library/Sounds/Basso.aiff 2>/dev/null || paplay /usr/share/sounds/freedesktop/stereo/bell.oga 2>/dev/null &")
        except:
            pass
```

---

## 7. Implementation Phases

### Phase 1: Foundation (Week 1)

**Goal:** Process historical tick data and build footprint bars.

- [ ] Set up project structure
- [ ] Implement `Tick` and `PriceLevel` data classes
- [ ] Implement `FootprintBar` with delta calculations
- [ ] Implement `FootprintAggregator`
- [ ] Build tick data parser for historical files (CSV/JSON)
- [ ] Create basic visualization (console print of footprint)
- [ ] Test with sample data

**Deliverable:** Can load historical tick data and output footprint bars.

### Phase 2: Pattern Detection (Week 2)

**Goal:** Implement all five signal detection patterns.

- [ ] Implement `ImbalanceDetector` with stacked detection
- [ ] Implement `ExhaustionDetector`
- [ ] Implement `DeltaDivergenceDetector`
- [ ] Implement `AbsorptionDetector`
- [ ] Implement `UnfinishedBusinessDetector`
- [ ] Create `Signal` data class
- [ ] Unit tests for each detector

**Deliverable:** Can process historical bars and output signals.

### Phase 3: Real-Time Integration (Week 3)

**Goal:** Connect to live data feed.

- [ ] Build data feed adapter for chosen provider
- [ ] Implement tick stream processing
- [ ] Add session management (RTH vs ETH)
- [ ] Build `OrderFlowEngine` to orchestrate components
- [ ] Implement `AlertManager` with console output
- [ ] Test with paper trading account

**Deliverable:** Real-time signal generation from live data.

### Phase 4: Alerts & Logging (Week 4)

**Goal:** Production-ready output system.

- [ ] Add webhook support (Discord/Telegram)
- [ ] Add desktop notifications
- [ ] Implement signal logging (SQLite or CSV)
- [ ] Add cumulative delta display
- [ ] Add volume profile tracking
- [ ] Create simple dashboard (optional: terminal UI with `rich`)

**Deliverable:** Full alerting system with signal history.

### Phase 5: Backtesting & Optimization (Ongoing)

**Goal:** Validate and tune signals.

- [ ] Build backtesting framework
- [ ] Track signal outcomes (did price move as predicted?)
- [ ] Calculate win rates by pattern type
- [ ] Optimize thresholds based on historical performance
- [ ] A/B test parameter variations

**Deliverable:** Performance metrics and tuned parameters.

---

## 8. Configuration & Tuning

### 8.1 Default Configuration

```python
DEFAULT_CONFIG = {
    "symbol": "ES",
    "timeframe": 300,  # 5-minute bars

    # Imbalance settings
    "imbalance_threshold": 3.0,      # 300% ratio
    "imbalance_min_volume": 10,       # Minimum contracts
    "stacked_imbalance_min": 3,       # Minimum stack size

    # Exhaustion settings
    "exhaustion_min_levels": 3,       # Consecutive declining levels
    "exhaustion_min_decline": 0.30,   # 30% volume decline

    # Divergence settings
    "divergence_lookback": 5,         # Bars to analyze

    # Absorption settings
    "absorption_min_volume": 100,     # Volume threshold
    "absorption_delta_threshold": 0.3,

    # Unfinished business
    "unfinished_max_volume": 5,       # Max volume to consider unfinished

    # Alert settings
    "alerts": {
        "console": True,
        "sound": False,
        "webhook_url": None,
        "min_strength": 0.5,
        "patterns": None,  # None = all patterns
    }
}
```

### 8.2 Tuning Guidelines

| Parameter | Lower Value Effect | Higher Value Effect |
|-----------|-------------------|---------------------|
| `imbalance_threshold` | More signals, lower quality | Fewer signals, higher quality |
| `imbalance_min_volume` | More noise in low-volume periods | May miss signals in quiet markets |
| `exhaustion_min_levels` | Faster detection, more false positives | Slower detection, fewer false positives |
| `divergence_lookback` | Shorter-term divergences | Longer-term divergences |
| `absorption_min_volume` | More absorption signals | Only major absorption events |
| `min_strength` (alerts) | More alerts | Only strongest signals |

### 8.3 Symbol-Specific Tuning

Different contracts have different volume profiles:

```python
SYMBOL_PROFILES = {
    "ES": {
        "imbalance_min_volume": 20,
        "absorption_min_volume": 150,
        "typical_bar_volume": 5000,
    },
    "NQ": {
        "imbalance_min_volume": 15,
        "absorption_min_volume": 100,
        "typical_bar_volume": 3000,
    },
    "CL": {
        "imbalance_min_volume": 30,
        "absorption_min_volume": 200,
        "typical_bar_volume": 8000,
    },
    "MES": {  # Micro ES - lower volume
        "imbalance_min_volume": 5,
        "absorption_min_volume": 30,
        "typical_bar_volume": 500,
    },
}
```

---

## 9. Testing Strategy

### 9.1 Unit Tests

```python
# tests/test_imbalance.py

def test_buy_imbalance_detection():
    """Test that buy imbalances are correctly identified."""
    bar = FootprintBar(
        symbol="ES",
        start_time=datetime.now(),
        end_time=datetime.now(),
        timeframe=300,
        open_price=5000.00,
        high_price=5002.00,
        low_price=5000.00,
        close_price=5002.00,
        levels={
            5000.00: PriceLevel(5000.00, bid_volume=10, ask_volume=5),
            5000.25: PriceLevel(5000.25, bid_volume=8, ask_volume=50),  # 50/10 = 500% imbalance
            5000.50: PriceLevel(5000.50, bid_volume=12, ask_volume=60),
        }
    )

    detector = ImbalanceDetector(threshold=3.0, min_volume=10)
    signals = detector.detect(bar)

    assert len(signals) >= 1
    assert signals[0].pattern == "BUY_IMBALANCE"
    assert signals[0].direction == "LONG"
```

### 9.2 Integration Tests

```python
# tests/test_engine.py

def test_full_pipeline():
    """Test tick -> bar -> signal pipeline."""
    engine = OrderFlowEngine({"symbol": "ES", "timeframe": 60})

    # Simulate 1 minute of ticks
    ticks = generate_test_ticks(count=100, duration_seconds=60)

    for tick in ticks:
        engine.process_tick(tick)

    state = engine.get_current_state()
    assert state["cumulative_delta"] != 0
    assert len(engine.signal_log) >= 0  # May or may not have signals
```

### 9.3 Backtesting Validation

```python
# tests/test_backtest.py

def test_signal_profitability():
    """Validate signals against historical outcomes."""
    # Load historical data
    bars = load_historical_bars("ES", days=30)

    engine = OrderFlowEngine({"symbol": "ES"})
    results = []

    for i, bar in enumerate(bars[:-10]):  # Leave 10 bars for outcome check
        signals = engine._analyze_bar(bar)

        for signal in signals:
            # Check if price moved in predicted direction within next 10 bars
            future_bars = bars[i+1:i+11]
            outcome = evaluate_signal_outcome(signal, future_bars)
            results.append(outcome)

    win_rate = sum(1 for r in results if r["win"]) / len(results)
    print(f"Win rate: {win_rate:.1%}")
    assert win_rate > 0.45  # Better than random
```

---

## 10. Future Enhancements

### 10.1 Additional Patterns

- **POC Migration:** Track Point of Control movement between sessions
- **Single Prints:** Identify low-volume areas that may act as support/resistance
- **Poor Highs/Lows:** Weak extremes with no rejection (likely to be revisited)
- **Excess:** High volume at extremes indicating potential reversal

### 10.2 Multi-Timeframe Analysis

- Aggregate signals across multiple timeframes (1m, 5m, 15m)
- Higher timeframe confluence increases signal strength
- Lower timeframe for entry timing

### 10.3 Machine Learning Integration

- Train classifier on historical signal outcomes
- Feature engineering from order flow metrics
- Predict signal strength more accurately

### 10.4 Order Execution

- Paper trading integration
- Bracket order generation (entry, stop, target)
- Position sizing based on signal strength

### 10.5 Visualization Dashboard

- Real-time footprint chart rendering
- Cumulative delta chart
- Signal overlay on price chart
- Performance metrics display

---

## Appendix A: Glossary

| Term | Definition |
|------|------------|
| **Ask/Offer** | The price at which sellers are willing to sell |
| **Bid** | The price at which buyers are willing to buy |
| **Delta** | Difference between buy and sell market order volume |
| **DOM** | Depth of Market - shows pending limit orders |
| **Footprint** | Chart showing volume at each price level |
| **Iceberg** | Large order hidden behind smaller displayed quantity |
| **Imbalance** | Significant volume disparity between buyers/sellers |
| **Lifting the Offer** | Buying with a market order at the ask price |
| **Hitting the Bid** | Selling with a market order at the bid price |
| **POC** | Point of Control - price with highest volume |
| **RTH** | Regular Trading Hours (9:30 AM - 4:00 PM ET for ES) |
| **ETH** | Extended Trading Hours (overnight session) |
| **Tick** | Minimum price movement (0.25 for ES) |
| **Value Area** | Price range containing 70% of volume |

---

## Appendix B: Data Feed Setup Examples

### B.1 Rithmic (via rithmic-api-python)

```python
# Example Rithmic connection (pseudo-code - actual API varies)
from rithmic import RithmicClient

client = RithmicClient(
    user="your_user",
    password="your_password",
    system="Rithmic Paper Trading"
)

def on_tick(tick_data):
    tick = Tick(
        timestamp=tick_data.timestamp,
        price=tick_data.price,
        volume=tick_data.size,
        side="ASK" if tick_data.aggressor == "BUY" else "BID",
        symbol=tick_data.symbol
    )
    engine.process_tick(tick)

client.subscribe_trades("ESZ24", callback=on_tick)
client.run()
```

### B.2 IQFeed (via pyiqfeed)

```python
from pyiqfeed import IQFeedClient

client = IQFeedClient()
client.connect()

def on_trade(trade):
    tick = Tick(
        timestamp=trade.timestamp,
        price=trade.last_price,
        volume=trade.last_size,
        side="ASK" if trade.tick_direction > 0 else "BID",
        symbol=trade.symbol
    )
    engine.process_tick(tick)

client.watch("@ESZ24", on_trade)
```

---

*Document Version: 1.0*
*Created: 2024*
*Author: Order Flow Analysis Project*
