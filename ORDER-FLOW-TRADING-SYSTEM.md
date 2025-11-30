# Order Flow Trading System - Complete Project Specification

## Executive Summary

Build a headless, regime-adaptive order flow trading system that:
1. Ingests real-time tick data from AMP/CQG (or Rithmic)
2. Constructs footprint charts and detects order flow patterns
3. Classifies current market regime (trending, ranging, volatile)
4. Routes signals to appropriate strategies based on regime
5. Executes trades via NautilusTrader (paper first, then live)
6. Provides a web dashboard for monitoring and control
7. Logs everything to a database for analysis

**Target Markets:** ES (E-mini S&P 500), MES (Micro E-mini S&P 500), NQ, MNQ

**Infrastructure:** Headless Linux server (Ryzen 7 mini-PC), AMP Futures brokerage, CQG data feed

---

## Table of Contents

1. [System Architecture](#1-system-architecture)
2. [Data Layer](#2-data-layer)
3. [Order Flow Engine](#3-order-flow-engine)
4. [Regime Detection](#4-regime-detection)
5. [Strategy Layer](#5-strategy-layer)
6. [Execution Layer](#6-execution-layer)
7. [Dashboard & Control Plane](#7-dashboard--control-plane)
8. [Database Schema](#8-database-schema)
9. [Configuration](#9-configuration)
10. [Implementation Phases](#10-implementation-phases)
11. [File Structure](#11-file-structure)
12. [Dependencies](#12-dependencies)

---

## 1. System Architecture

### 1.1 High-Level Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                      DASHBOARD (Browser)                         │
│  - Session controls (start/stop, paper/live, instrument)         │
│  - Risk parameters (daily P&L limits, position size)             │
│  - Real-time P&L, position status, signal feed                   │
│  - Performance charts, trade history                             │
└─────────────────────────────────────────────────────────────────┘
                              │
                         WebSocket
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     CONTROL SERVER (FastAPI)                     │
│  - REST endpoints for config changes                             │
│  - WebSocket for real-time state push                            │
│  - Session state management                                      │
│  - Persists everything to database                               │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      TRADING ENGINE                              │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                    REGIME DETECTOR                        │   │
│  │  Classifies: TRENDING_UP | TRENDING_DOWN | RANGING |      │   │
│  │              VOLATILE | NO_TRADE                          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                   STRATEGY ROUTER                         │   │
│  │  Maps regime → enabled patterns → position sizing         │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                  ORDER FLOW ENGINE                        │   │
│  │  Detects: Imbalances, Exhaustion, Absorption,             │   │
│  │           Delta Divergence, Unfinished Business           │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                  EXECUTION MANAGER                        │   │
│  │  Signal validation → Order generation → Risk checks       │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
│                              ▼                                   │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │                   NAUTILUSTRADER                          │   │
│  │  Paper trading engine / Live execution via CQG            │   │
│  └──────────────────────────────────────────────────────────┘   │
│                              │                                   │
└──────────────────────────────│───────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       DATABASE (Postgres)                        │
│  - Sessions, Trades, Signals, Regimes, Performance              │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     DATA FEED (AMP/CQG)                          │
│  - Tick-by-tick trades with aggressor side                       │
│  - Level 2 DOM data (optional, for future enhancement)          │
└─────────────────────────────────────────────────────────────────┘
```

### 1.2 Component Responsibilities

| Component | Responsibility |
|-----------|----------------|
| **Data Feed Adapter** | Connect to CQG, normalize ticks, handle reconnection |
| **Order Flow Engine** | Build footprint bars, detect patterns, emit signals |
| **Regime Detector** | Classify market state, determine trading mode |
| **Strategy Router** | Filter signals based on regime, apply position sizing |
| **Execution Manager** | Validate trades, manage risk, generate orders |
| **NautilusTrader** | Paper/live execution, position tracking, order management |
| **Control Server** | API for dashboard, session management, config |
| **Dashboard** | User interface for monitoring and control |
| **Database** | Persist all data for analysis and compliance |

---

## 2. Data Layer

### 2.1 Data Structures

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Literal, Optional, Any
from enum import Enum

# === Core Data Types ===

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
    
    levels: Dict[float, PriceLevel]
    
    @property
    def total_volume(self) -> int:
        return sum(level.total_volume for level in self.levels.values())
    
    @property
    def delta(self) -> int:
        return sum(level.delta for level in self.levels.values())
    
    @property
    def buy_volume(self) -> int:
        return sum(level.ask_volume for level in self.levels.values())
    
    @property
    def sell_volume(self) -> int:
        return sum(level.bid_volume for level in self.levels.values())
    
    def get_sorted_levels(self, ascending: bool = True) -> List[PriceLevel]:
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
    details: Dict[str, Any]
    
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
```

### 2.2 Tick Size Constants

```python
TICK_SIZES = {
    "ES": 0.25,
    "MES": 0.25,
    "NQ": 0.25,
    "MNQ": 0.25,
    "CL": 0.01,
    "GC": 0.10,
}

TICK_VALUES = {
    "ES": 12.50,
    "MES": 1.25,
    "NQ": 5.00,
    "MNQ": 0.50,
    "CL": 10.00,
    "GC": 10.00,
}

def normalize_price(price: float, symbol: str) -> float:
    """Round price to valid tick increment."""
    tick_size = TICK_SIZES.get(symbol[:3], TICK_SIZES.get(symbol[:2], 0.25))
    return round(price / tick_size) * tick_size
```

### 2.3 CQG Data Feed Adapter

```python
class CQGDataAdapter:
    """
    Adapter for CQG data feed via NautilusTrader.
    Converts CQG tick format to our Tick dataclass.
    """
    
    def __init__(self, config: dict):
        self.config = config
        self.symbol = config["symbol"]
        self.callbacks: List[Callable[[Tick], None]] = []
        
    def on_tick(self, cqg_tick) -> None:
        """
        Called by NautilusTrader when a tick arrives.
        Convert and dispatch to registered callbacks.
        """
        tick = Tick(
            timestamp=cqg_tick.ts_event,
            price=float(cqg_tick.price),
            volume=int(cqg_tick.size),
            side="ASK" if cqg_tick.aggressor_side == AggressorSide.BUYER else "BID",
            symbol=self.symbol
        )
        
        for callback in self.callbacks:
            callback(tick)
    
    def register_callback(self, callback: Callable[[Tick], None]) -> None:
        self.callbacks.append(callback)
```

---

## 3. Order Flow Engine

### 3.1 Footprint Aggregator

```python
class FootprintAggregator:
    """Aggregates ticks into time-based footprint bars."""
    
    def __init__(self, timeframe_seconds: int = 300):
        self.timeframe = timeframe_seconds
        self.current_bar: Optional[FootprintBar] = None
        self.completed_bars: List[FootprintBar] = []
        self.bar_callbacks: List[Callable[[FootprintBar], None]] = []
    
    def process_tick(self, tick: Tick) -> Optional[FootprintBar]:
        """Process tick, return completed bar if bar closed."""
        bar_start = self._get_bar_start(tick.timestamp)
        
        if self.current_bar is None:
            self.current_bar = self._create_new_bar(tick, bar_start)
        elif bar_start > self.current_bar.start_time:
            completed = self.current_bar
            self.completed_bars.append(completed)
            self.current_bar = self._create_new_bar(tick, bar_start)
            self._add_tick_to_bar(tick)
            self._notify_bar_complete(completed)
            return completed
        
        self._add_tick_to_bar(tick)
        return None
    
    def _add_tick_to_bar(self, tick: Tick) -> None:
        bar = self.current_bar
        price = normalize_price(tick.price, tick.symbol)
        
        bar.high_price = max(bar.high_price, price)
        bar.low_price = min(bar.low_price, price)
        bar.close_price = price
        
        if price not in bar.levels:
            bar.levels[price] = PriceLevel(price=price)
        
        level = bar.levels[price]
        if tick.side == "ASK":
            level.ask_volume += tick.volume
        else:
            level.bid_volume += tick.volume
    
    def _get_bar_start(self, timestamp: datetime) -> datetime:
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
    
    def _notify_bar_complete(self, bar: FootprintBar) -> None:
        for callback in self.bar_callbacks:
            callback(bar)
    
    def on_bar_complete(self, callback: Callable[[FootprintBar], None]) -> None:
        self.bar_callbacks.append(callback)
```

### 3.2 Pattern Detectors

See ORDER_FLOW_PROJECT.md for full implementations of:
- `ImbalanceDetector` — Diagonal volume comparison, stacked imbalances
- `ExhaustionDetector` — Declining volume at extremes
- `DeltaDivergenceDetector` — Price/delta divergence patterns
- `AbsorptionDetector` — Passive order absorption at extremes
- `UnfinishedBusinessDetector` — Incomplete auctions

### 3.3 Order Flow Engine (Orchestrator)

```python
class OrderFlowEngine:
    """Main engine orchestrating all order flow analysis."""
    
    def __init__(self, config: dict):
        self.config = config
        self.symbol = config["symbol"]
        self.timeframe = config.get("timeframe", 300)
        
        # Aggregation
        self.aggregator = FootprintAggregator(self.timeframe)
        self.cumulative_delta = CumulativeDelta()
        self.volume_profile = VolumeProfile()
        
        # Detectors
        self.detectors = [
            ImbalanceDetector(
                threshold=config.get("imbalance_threshold", 3.0),
                min_volume=config.get("imbalance_min_volume", 10)
            ),
            ExhaustionDetector(
                min_levels=config.get("exhaustion_min_levels", 3)
            ),
            DeltaDivergenceDetector(
                lookback=config.get("divergence_lookback", 5)
            ),
            AbsorptionDetector(
                min_volume=config.get("absorption_min_volume", 100)
            ),
            UnfinishedBusinessDetector(),
        ]
        
        # Signal output
        self.signal_callbacks: List[Callable[[Signal], None]] = []
        
        # Wire up bar completion
        self.aggregator.on_bar_complete(self._analyze_bar)
    
    def process_tick(self, tick: Tick) -> None:
        """Process incoming tick."""
        self.aggregator.process_tick(tick)
    
    def _analyze_bar(self, bar: FootprintBar) -> None:
        """Run all pattern detectors on completed bar."""
        self.cumulative_delta.update(bar)
        self.volume_profile.add_bar(bar)
        
        signals = []
        for detector in self.detectors:
            if hasattr(detector, 'detect'):
                signals.extend(detector.detect(bar))
            if hasattr(detector, 'detect_stacked_imbalances'):
                signals.extend(detector.detect_stacked_imbalances(bar))
            if hasattr(detector, 'add_bar'):
                signals.extend(detector.add_bar(bar))
            if hasattr(detector, 'check_revisit'):
                signals.extend(detector.check_revisit(bar))
        
        for signal in signals:
            self._emit_signal(signal)
    
    def _emit_signal(self, signal: Signal) -> None:
        for callback in self.signal_callbacks:
            callback(signal)
    
    def on_signal(self, callback: Callable[[Signal], None]) -> None:
        self.signal_callbacks.append(callback)
    
    def get_state(self) -> dict:
        """Get current analysis state."""
        return {
            "symbol": self.symbol,
            "cumulative_delta": self.cumulative_delta.value,
            "current_bar": {
                "delta": self.aggregator.current_bar.delta if self.aggregator.current_bar else 0,
                "volume": self.aggregator.current_bar.total_volume if self.aggregator.current_bar else 0,
            },
            "poc": self.volume_profile.get_poc() if self.volume_profile.levels else None,
        }
```

---

## 4. Regime Detection

### 4.1 Regime Inputs

```python
@dataclass
class RegimeInputs:
    """All inputs for regime classification."""
    
    # Trend Strength
    adx_14: float
    adx_slope: float
    
    # Trend Direction
    ema_fast: float           # EMA(9)
    ema_slow: float           # EMA(21)
    ema_trend: float          # Fast - Slow
    price_vs_vwap: float
    
    # Volatility
    atr_14: float
    atr_percentile: float     # 0-100, where current ATR sits vs last 20 days
    bar_range_avg: float
    
    # Volume/Delta
    volume_vs_average: float  # 1.0 = normal
    cumulative_delta: int
    delta_slope: float
    
    # Market Structure
    higher_highs: bool
    higher_lows: bool
    lower_highs: bool
    lower_lows: bool
    range_bound_bars: int
    
    # Time Context
    minutes_since_open: int
    minutes_to_close: int
    is_news_window: bool
```

### 4.2 Regime Detector

```python
class RegimeDetector:
    """Classifies current market regime."""
    
    def __init__(self, config: dict = None):
        self.config = config or DEFAULT_REGIME_CONFIG
        self.current_regime = Regime.NO_TRADE
        self.regime_confidence = 0.0
        self.regime_history: List[Tuple[datetime, Regime, float]] = []
    
    def classify(self, inputs: RegimeInputs) -> Tuple[Regime, float]:
        """Returns (regime, confidence)."""
        
        # Hard overrides
        if self._should_not_trade(inputs):
            return Regime.NO_TRADE, 1.0
        
        # Score each regime
        scores = {
            Regime.TRENDING_UP: self._score_trending_up(inputs),
            Regime.TRENDING_DOWN: self._score_trending_down(inputs),
            Regime.RANGING: self._score_ranging(inputs),
            Regime.VOLATILE: self._score_volatile(inputs),
        }
        
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winner, winner_score = sorted_scores[0]
        runner_up_score = sorted_scores[1][1]
        
        if winner_score == 0:
            return Regime.NO_TRADE, 0.5
        
        margin = (winner_score - runner_up_score) / winner_score
        confidence = min(0.5 + (margin * 0.5), 1.0)
        
        if winner_score < self.config["min_regime_score"]:
            return Regime.VOLATILE, 0.5
        
        self._update_history(winner, confidence)
        return winner, confidence
    
    def _should_not_trade(self, inputs: RegimeInputs) -> bool:
        if inputs.minutes_to_close < 15:
            return True
        if inputs.is_news_window:
            return True
        if inputs.minutes_since_open < 5:
            return True
        if inputs.volume_vs_average < 0.3:
            return True
        return False
    
    def _score_trending_up(self, inputs: RegimeInputs) -> float:
        score = 0.0
        
        if inputs.adx_14 > 25:
            score += 2.0
        elif inputs.adx_14 > 20:
            score += 1.0
        
        if inputs.ema_trend > 0:
            score += 1.5
        
        if inputs.price_vs_vwap > 0:
            score += 1.0
        
        if inputs.higher_highs and inputs.higher_lows:
            score += 2.0
        elif inputs.higher_lows:
            score += 1.0
        
        if inputs.cumulative_delta > 0 and inputs.delta_slope > 0:
            score += 1.5
        elif inputs.cumulative_delta > 0:
            score += 0.5
        
        if inputs.adx_slope > 0:
            score += 0.5
        
        return score
    
    def _score_trending_down(self, inputs: RegimeInputs) -> float:
        score = 0.0
        
        if inputs.adx_14 > 25:
            score += 2.0
        elif inputs.adx_14 > 20:
            score += 1.0
        
        if inputs.ema_trend < 0:
            score += 1.5
        
        if inputs.price_vs_vwap < 0:
            score += 1.0
        
        if inputs.lower_highs and inputs.lower_lows:
            score += 2.0
        elif inputs.lower_highs:
            score += 1.0
        
        if inputs.cumulative_delta < 0 and inputs.delta_slope < 0:
            score += 1.5
        elif inputs.cumulative_delta < 0:
            score += 0.5
        
        if inputs.adx_slope > 0:
            score += 0.5
        
        return score
    
    def _score_ranging(self, inputs: RegimeInputs) -> float:
        score = 0.0
        
        if inputs.adx_14 < 20:
            score += 2.0
        elif inputs.adx_14 < 25:
            score += 1.0
        
        if abs(inputs.price_vs_vwap) < 0.5:
            score += 1.0
        
        if not (inputs.higher_highs or inputs.lower_lows):
            score += 1.5
        
        if inputs.range_bound_bars >= 3:
            score += 2.0
        elif inputs.range_bound_bars >= 2:
            score += 1.0
        
        if abs(inputs.cumulative_delta) < 500:
            score += 1.0
        
        if inputs.atr_percentile < 50:
            score += 1.0
        
        return score
    
    def _score_volatile(self, inputs: RegimeInputs) -> float:
        score = 0.0
        
        if inputs.atr_percentile > 80:
            score += 2.5
        elif inputs.atr_percentile > 60:
            score += 1.5
        
        if inputs.bar_range_avg > inputs.atr_14 * 1.5:
            score += 1.5
        
        if inputs.volume_vs_average > 2.0:
            score += 1.0
        
        if 20 <= inputs.adx_14 <= 30 and inputs.adx_slope < 0:
            score += 1.0
        
        if abs(inputs.delta_slope) > 100:
            score += 1.0
        
        return score
    
    def _update_history(self, regime: Regime, confidence: float) -> None:
        now = datetime.now()
        if (not self.regime_history or
            self.regime_history[-1][1] != regime or
            abs(self.regime_history[-1][2] - confidence) > 0.2):
            self.regime_history.append((now, regime, confidence))
            if len(self.regime_history) > 100:
                self.regime_history = self.regime_history[-100:]
        
        self.current_regime = regime
        self.regime_confidence = confidence


DEFAULT_REGIME_CONFIG = {
    "min_regime_score": 4.0,
    "adx_trend_threshold": 25,
    "adx_weak_threshold": 20,
    "atr_high_percentile": 70,
    "atr_extreme_percentile": 85,
    "min_bars_in_regime": 2,
    "min_regime_confidence": 0.6,
    "news_buffer_minutes": 15,
    "no_trade_before_open_minutes": 5,
    "no_trade_before_close_minutes": 15,
}
```

### 4.3 Regime Inputs Calculator

```python
class RegimeInputsCalculator:
    """Calculates all inputs needed for regime detection."""
    
    def __init__(self, config: dict):
        self.config = config
        self.bars: List[FootprintBar] = []
        self.daily_bars: List[FootprintBar] = []  # For ATR percentile
        
    def add_bar(self, bar: FootprintBar) -> None:
        self.bars.append(bar)
        if len(self.bars) > 100:
            self.bars = self.bars[-100:]
    
    def calculate(self) -> RegimeInputs:
        """Calculate all regime inputs from current bar history."""
        if len(self.bars) < 21:
            return self._default_inputs()
        
        closes = [bar.close_price for bar in self.bars]
        highs = [bar.high_price for bar in self.bars]
        lows = [bar.low_price for bar in self.bars]
        deltas = [bar.delta for bar in self.bars]
        volumes = [bar.total_volume for bar in self.bars]
        
        return RegimeInputs(
            adx_14=self._calculate_adx(highs, lows, closes, 14),
            adx_slope=self._calculate_slope(self._adx_series(highs, lows, closes, 14)[-5:]),
            ema_fast=self._ema(closes, 9)[-1],
            ema_slow=self._ema(closes, 21)[-1],
            ema_trend=self._ema(closes, 9)[-1] - self._ema(closes, 21)[-1],
            price_vs_vwap=self._price_vs_vwap(),
            atr_14=self._atr(highs, lows, closes, 14),
            atr_percentile=self._atr_percentile(),
            bar_range_avg=self._avg_bar_range(5),
            volume_vs_average=volumes[-1] / (sum(volumes[-20:]) / 20) if volumes else 1.0,
            cumulative_delta=sum(deltas),
            delta_slope=self._calculate_slope(deltas[-10:]),
            higher_highs=self._check_higher_highs(highs),
            higher_lows=self._check_higher_lows(lows),
            lower_highs=self._check_lower_highs(highs),
            lower_lows=self._check_lower_lows(lows),
            range_bound_bars=self._count_range_bound_bars(),
            minutes_since_open=self._minutes_since_open(),
            minutes_to_close=self._minutes_to_close(),
            is_news_window=self._is_news_window(),
        )
    
    # Implementation of helper methods omitted for brevity
    # See technical_indicators.py for full implementations
```

---

## 5. Strategy Layer

### 5.1 Strategy-to-Regime Mapping

```python
STRATEGY_REGIME_MAP = {
    Regime.TRENDING_UP: {
        "enabled_patterns": [
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.BUYING_ABSORPTION,
            SignalPattern.SELLING_EXHAUSTION,
            SignalPattern.BULLISH_DELTA_DIVERGENCE,
        ],
        "disabled_patterns": [
            SignalPattern.SELL_IMBALANCE,
            SignalPattern.STACKED_SELL_IMBALANCE,
        ],
        "bias": "LONG",
        "position_size_multiplier": 1.0,
    },
    
    Regime.TRENDING_DOWN: {
        "enabled_patterns": [
            SignalPattern.STACKED_SELL_IMBALANCE,
            SignalPattern.SELLING_ABSORPTION,
            SignalPattern.BUYING_EXHAUSTION,
            SignalPattern.BEARISH_DELTA_DIVERGENCE,
        ],
        "disabled_patterns": [
            SignalPattern.BUY_IMBALANCE,
            SignalPattern.STACKED_BUY_IMBALANCE,
        ],
        "bias": "SHORT",
        "position_size_multiplier": 1.0,
    },
    
    Regime.RANGING: {
        "enabled_patterns": [
            SignalPattern.BUYING_EXHAUSTION,
            SignalPattern.SELLING_EXHAUSTION,
            SignalPattern.BUYING_ABSORPTION,
            SignalPattern.SELLING_ABSORPTION,
            SignalPattern.UNFINISHED_HIGH,
            SignalPattern.UNFINISHED_LOW,
        ],
        "disabled_patterns": [
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.STACKED_SELL_IMBALANCE,
        ],
        "bias": None,
        "position_size_multiplier": 0.75,
    },
    
    Regime.VOLATILE: {
        "enabled_patterns": [
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.STACKED_SELL_IMBALANCE,
        ],
        "disabled_patterns": [
            SignalPattern.BUY_IMBALANCE,
            SignalPattern.SELL_IMBALANCE,
            SignalPattern.BUYING_EXHAUSTION,
            SignalPattern.SELLING_EXHAUSTION,
        ],
        "bias": None,
        "position_size_multiplier": 0.5,
    },
    
    Regime.NO_TRADE: {
        "enabled_patterns": [],
        "disabled_patterns": [],  # All disabled
        "bias": None,
        "position_size_multiplier": 0,
    },
}
```

### 5.2 Strategy Router

```python
class StrategyRouter:
    """Routes signals through regime filter and applies position sizing."""
    
    def __init__(self, config: dict):
        self.config = config
        self.regime_detector = RegimeDetector(config.get("regime", {}))
        self.inputs_calculator = RegimeInputsCalculator(config)
        self.current_regime = Regime.NO_TRADE
        self.regime_confidence = 0.0
    
    def on_bar(self, bar: FootprintBar) -> None:
        """Update regime on each bar."""
        self.inputs_calculator.add_bar(bar)
        inputs = self.inputs_calculator.calculate()
        self.current_regime, self.regime_confidence = self.regime_detector.classify(inputs)
    
    def evaluate_signal(self, signal: Signal) -> Signal:
        """Evaluate signal against current regime. Returns annotated signal."""
        signal.regime = self.current_regime.value
        
        regime_config = STRATEGY_REGIME_MAP.get(self.current_regime)
        if not regime_config:
            signal.approved = False
            signal.rejection_reason = "Unknown regime"
            return signal
        
        # Check if pattern is enabled for this regime
        if signal.pattern in regime_config.get("disabled_patterns", []):
            signal.approved = False
            signal.rejection_reason = f"Pattern disabled in {self.current_regime.value}"
            return signal
        
        if regime_config["enabled_patterns"] and signal.pattern not in regime_config["enabled_patterns"]:
            signal.approved = False
            signal.rejection_reason = f"Pattern not in enabled list for {self.current_regime.value}"
            return signal
        
        # Check bias alignment
        bias = regime_config.get("bias")
        if bias and signal.direction != bias:
            signal.approved = False
            signal.rejection_reason = f"Direction {signal.direction} conflicts with {bias} bias"
            return signal
        
        # Check minimum signal strength
        min_strength = self.config.get("min_signal_strength", 0.5)
        if signal.strength < min_strength:
            signal.approved = False
            signal.rejection_reason = f"Strength {signal.strength:.2f} below minimum {min_strength}"
            return signal
        
        # Check regime confidence
        min_confidence = self.config.get("min_regime_confidence", 0.6)
        if self.regime_confidence < min_confidence:
            signal.approved = False
            signal.rejection_reason = f"Regime confidence {self.regime_confidence:.2f} below minimum"
            return signal
        
        signal.approved = True
        return signal
    
    def get_position_size_multiplier(self) -> float:
        """Get position size multiplier for current regime."""
        regime_config = STRATEGY_REGIME_MAP.get(self.current_regime, {})
        return regime_config.get("position_size_multiplier", 0)
```

---

## 6. Execution Layer

### 6.1 Session Configuration

```python
@dataclass
class TradingSession:
    """Configuration for a trading session."""
    
    # Mode
    mode: Literal["paper", "live"] = "paper"
    paper_starting_balance: float = 10000.0
    
    # Instrument
    symbol: str = "MES"  # Start with micros
    
    # Risk Limits (hard stops)
    daily_profit_target: float = 500.0
    daily_loss_limit: float = -300.0
    max_position_size: int = 2
    max_concurrent_trades: int = 1
    
    # Per-trade risk
    stop_loss_ticks: int = 16      # 4 points on ES
    take_profit_ticks: int = 24    # 6 points on ES
    
    # Time Controls
    trading_start: time = time(9, 30)
    trading_end: time = time(15, 45)
    no_trade_windows: List[Tuple[time, time]] = None
    
    # Strategy Controls
    enabled_patterns: List[str] = None
    min_signal_strength: float = 0.6
    min_regime_confidence: float = 0.7
    
    def __post_init__(self):
        if self.no_trade_windows is None:
            self.no_trade_windows = [
                (time(12, 0), time(13, 0)),  # Lunch
            ]
```

### 6.2 Execution Manager

```python
class ExecutionManager:
    """Manages trade execution and risk."""
    
    def __init__(self, session: TradingSession, executor: 'TradeExecutor'):
        self.session = session
        self.executor = executor
        
        # Session state
        self.daily_pnl: float = 0.0
        self.open_positions: List[Position] = []
        self.completed_trades: List[Trade] = []
        self.is_halted: bool = False
        self.halt_reason: Optional[str] = None
    
    def on_signal(self, signal: Signal) -> Optional[Trade]:
        """Process approved signal into trade."""
        
        # Check halt conditions
        if self.is_halted:
            return None
        
        if not signal.approved:
            return None
        
        # Check daily limits
        if self.daily_pnl >= self.session.daily_profit_target:
            self._halt("Daily profit target reached")
            return None
        
        if self.daily_pnl <= self.session.daily_loss_limit:
            self._halt("Daily loss limit reached")
            return None
        
        # Check position limits
        if len(self.open_positions) >= self.session.max_concurrent_trades:
            return None
        
        # Check time windows
        if not self._in_trading_window():
            return None
        
        # Calculate position size
        base_size = self.session.max_position_size
        regime_multiplier = self._get_regime_multiplier(signal)
        size = max(1, int(base_size * regime_multiplier))
        
        # Generate order
        order = self._create_bracket_order(signal, size)
        
        # Execute
        trade = self.executor.submit_order(order)
        if trade:
            self.open_positions.append(trade.position)
        
        return trade
    
    def on_fill(self, fill: Fill) -> None:
        """Handle order fill."""
        # Update positions, P&L, etc.
        pass
    
    def on_position_close(self, position: Position, pnl: float) -> None:
        """Handle position close."""
        self.daily_pnl += pnl
        self.open_positions.remove(position)
        
        # Check limits after close
        if self.daily_pnl >= self.session.daily_profit_target:
            self._halt("Daily profit target reached")
        elif self.daily_pnl <= self.session.daily_loss_limit:
            self._halt("Daily loss limit reached")
    
    def _halt(self, reason: str) -> None:
        """Halt trading for the session."""
        self.is_halted = True
        self.halt_reason = reason
        
        # Close any open positions
        for position in self.open_positions:
            self.executor.close_position(position, reason="Session halted")
    
    def _in_trading_window(self) -> bool:
        """Check if current time is within trading window."""
        now = datetime.now().time()
        
        if now < self.session.trading_start or now > self.session.trading_end:
            return False
        
        for start, end in self.session.no_trade_windows:
            if start <= now <= end:
                return False
        
        return True
    
    def _create_bracket_order(self, signal: Signal, size: int) -> BracketOrder:
        """Create bracket order with stop and target."""
        tick_size = TICK_SIZES.get(self.session.symbol[:3], 0.25)
        
        entry_price = signal.price
        
        if signal.direction == "LONG":
            stop_price = entry_price - (self.session.stop_loss_ticks * tick_size)
            target_price = entry_price + (self.session.take_profit_ticks * tick_size)
        else:
            stop_price = entry_price + (self.session.stop_loss_ticks * tick_size)
            target_price = entry_price - (self.session.take_profit_ticks * tick_size)
        
        return BracketOrder(
            symbol=self.session.symbol,
            side=signal.direction,
            size=size,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            signal_id=id(signal),
        )
```

### 6.3 NautilusTrader Integration

```python
from nautilus_trader.trading.strategy import Strategy
from nautilus_trader.model.identifiers import InstrumentId
from nautilus_trader.model.orders import MarketOrder


class OrderFlowStrategy(Strategy):
    """NautilusTrader strategy wrapper for our order flow system."""
    
    def __init__(self, config: dict):
        super().__init__()
        
        self.config = config
        self.instrument_id = InstrumentId.from_str(config["instrument_id"])
        
        # Our components
        self.order_flow_engine = OrderFlowEngine(config)
        self.strategy_router = StrategyRouter(config)
        self.execution_manager = None  # Set in on_start
        
        # Wire up callbacks
        self.order_flow_engine.on_signal(self._on_signal)
    
    def on_start(self) -> None:
        """Called when strategy starts."""
        self.execution_manager = ExecutionManager(
            session=TradingSession(**self.config.get("session", {})),
            executor=self
        )
        
        # Subscribe to trade ticks
        self.subscribe_trade_ticks(self.instrument_id)
    
    def on_trade_tick(self, tick) -> None:
        """Called on each trade tick."""
        # Convert to our Tick format
        our_tick = Tick(
            timestamp=tick.ts_event,
            price=float(tick.price),
            volume=int(tick.size),
            side="ASK" if tick.aggressor_side.name == "BUYER" else "BID",
            symbol=self.config["symbol"]
        )
        
        # Process through order flow engine
        self.order_flow_engine.process_tick(our_tick)
    
    def on_bar(self, bar) -> None:
        """Called on bar close (if subscribed to bars)."""
        # Update regime detector
        # This is handled internally by the aggregator callbacks
        pass
    
    def _on_signal(self, signal: Signal) -> None:
        """Handle signal from order flow engine."""
        # Route through strategy filter
        signal = self.strategy_router.evaluate_signal(signal)
        
        # Execute if approved
        if signal.approved:
            self.execution_manager.on_signal(signal)
    
    def submit_order(self, order: BracketOrder) -> Optional[Trade]:
        """Submit order to NautilusTrader."""
        # Create market order for entry
        entry_order = self.order_factory.market(
            instrument_id=self.instrument_id,
            order_side=OrderSide.BUY if order.side == "LONG" else OrderSide.SELL,
            quantity=Quantity.from_int(order.size),
        )
        
        self.submit_order(entry_order)
        
        # Stop and target orders created on fill
        return Trade(order=order, entry_order_id=entry_order.client_order_id)
    
    def on_order_filled(self, event) -> None:
        """Handle order fill."""
        # Create stop and target orders
        pass
    
    def close_position(self, position, reason: str) -> None:
        """Close position immediately."""
        # Submit market order to close
        pass
```

---

## 7. Dashboard & Control Plane

### 7.1 FastAPI Server

```python
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import asyncio

app = FastAPI(title="Order Flow Trading Dashboard")

# State
active_session: Optional[TradingSession] = None
trading_engine: Optional[OrderFlowStrategy] = None
connected_clients: List[WebSocket] = []


# === REST Endpoints ===

class SessionCreate(BaseModel):
    mode: Literal["paper", "live"] = "paper"
    symbol: str = "MES"
    daily_profit_target: float = 500.0
    daily_loss_limit: float = -300.0
    max_position_size: int = 2
    paper_starting_balance: float = 10000.0


@app.post("/api/session/start")
async def start_session(config: SessionCreate):
    """Start a new trading session."""
    global active_session, trading_engine
    
    if active_session:
        return {"error": "Session already active"}
    
    active_session = TradingSession(**config.dict())
    trading_engine = create_engine(active_session)
    trading_engine.start()
    
    return {"status": "started", "session": active_session}


@app.post("/api/session/stop")
async def stop_session():
    """Stop current trading session."""
    global active_session, trading_engine
    
    if not active_session:
        return {"error": "No active session"}
    
    trading_engine.stop()
    
    summary = {
        "daily_pnl": trading_engine.execution_manager.daily_pnl,
        "trades": len(trading_engine.execution_manager.completed_trades),
    }
    
    active_session = None
    trading_engine = None
    
    return {"status": "stopped", "summary": summary}


@app.get("/api/session/status")
async def get_status():
    """Get current session status."""
    if not active_session:
        return {"active": False}
    
    em = trading_engine.execution_manager
    
    return {
        "active": True,
        "mode": active_session.mode,
        "symbol": active_session.symbol,
        "daily_pnl": em.daily_pnl,
        "is_halted": em.is_halted,
        "halt_reason": em.halt_reason,
        "open_positions": len(em.open_positions),
        "completed_trades": len(em.completed_trades),
        "current_regime": trading_engine.strategy_router.current_regime.value,
        "regime_confidence": trading_engine.strategy_router.regime_confidence,
    }


@app.patch("/api/session/limits")
async def update_limits(
    daily_profit_target: Optional[float] = None,
    daily_loss_limit: Optional[float] = None,
    max_position_size: Optional[int] = None,
):
    """Update session limits (live)."""
    if not active_session:
        return {"error": "No active session"}
    
    if daily_profit_target:
        active_session.daily_profit_target = daily_profit_target
    if daily_loss_limit:
        active_session.daily_loss_limit = daily_loss_limit
    if max_position_size:
        active_session.max_position_size = max_position_size
    
    return {"status": "updated", "session": active_session}


@app.get("/api/trades")
async def get_trades(limit: int = 50):
    """Get recent trades."""
    if not trading_engine:
        return []
    
    return trading_engine.execution_manager.completed_trades[-limit:]


@app.get("/api/signals")
async def get_signals(limit: int = 100):
    """Get recent signals."""
    # Query from database
    pass


# === WebSocket for Real-time Updates ===

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    
    try:
        while True:
            # Keep connection alive, push updates from engine
            await asyncio.sleep(0.1)
            
            if trading_engine:
                state = {
                    "type": "state_update",
                    "data": await get_status(),
                }
                await websocket.send_json(state)
            
    except WebSocketDisconnect:
        connected_clients.remove(websocket)


async def broadcast_signal(signal: Signal):
    """Broadcast signal to all connected clients."""
    message = {
        "type": "signal",
        "data": {
            "timestamp": signal.timestamp.isoformat(),
            "pattern": signal.pattern.value,
            "direction": signal.direction,
            "strength": signal.strength,
            "price": signal.price,
            "approved": signal.approved,
            "rejection_reason": signal.rejection_reason,
        }
    }
    
    for client in connected_clients:
        await client.send_json(message)


async def broadcast_trade(trade: Trade):
    """Broadcast trade to all connected clients."""
    message = {
        "type": "trade",
        "data": trade.to_dict(),
    }
    
    for client in connected_clients:
        await client.send_json(message)
```

### 7.2 Dashboard Frontend (React)

```jsx
// src/App.jsx
import { useState, useEffect } from 'react';
import useWebSocket from 'react-use-websocket';

function App() {
  const [session, setSession] = useState(null);
  const [signals, setSignals] = useState([]);
  const [trades, setTrades] = useState([]);
  
  const { lastJsonMessage } = useWebSocket('ws://localhost:8000/ws', {
    onMessage: (event) => {
      const msg = JSON.parse(event.data);
      
      if (msg.type === 'state_update') {
        setSession(msg.data);
      } else if (msg.type === 'signal') {
        setSignals(prev => [msg.data, ...prev].slice(0, 100));
      } else if (msg.type === 'trade') {
        setTrades(prev => [msg.data, ...prev].slice(0, 50));
      }
    }
  });
  
  return (
    <div className="dashboard">
      <Header session={session} />
      <Controls session={session} />
      <div className="panels">
        <RiskPanel session={session} />
        <RegimePanel session={session} />
        <PositionPanel session={session} />
      </div>
      <div className="feeds">
        <SignalFeed signals={signals} />
        <TradeFeed trades={trades} />
      </div>
    </div>
  );
}

function Header({ session }) {
  if (!session?.active) {
    return <header className="inactive">No Active Session</header>;
  }
  
  const pnlClass = session.daily_pnl >= 0 ? 'positive' : 'negative';
  
  return (
    <header className={session.mode}>
      <div className="mode">{session.mode.toUpperCase()}</div>
      <div className="symbol">{session.symbol}</div>
      <div className={`pnl ${pnlClass}`}>
        ${session.daily_pnl.toFixed(2)}
      </div>
      <div className="regime">
        {session.current_regime} ({(session.regime_confidence * 100).toFixed(0)}%)
      </div>
      {session.is_halted && (
        <div className="halted">HALTED: {session.halt_reason}</div>
      )}
    </header>
  );
}

function Controls({ session }) {
  const startSession = async (mode) => {
    const config = {
      mode,
      symbol: document.getElementById('symbol').value,
      daily_profit_target: parseFloat(document.getElementById('profit_target').value),
      daily_loss_limit: parseFloat(document.getElementById('loss_limit').value),
      max_position_size: parseInt(document.getElementById('max_size').value),
    };
    
    await fetch('/api/session/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
  };
  
  const stopSession = async () => {
    if (confirm('Stop trading session?')) {
      await fetch('/api/session/stop', { method: 'POST' });
    }
  };
  
  if (session?.active) {
    return (
      <div className="controls">
        <button onClick={stopSession} className="stop">Stop Session</button>
      </div>
    );
  }
  
  return (
    <div className="controls">
      <select id="symbol">
        <option value="MES">MES (Micro E-mini S&P)</option>
        <option value="ES">ES (E-mini S&P)</option>
        <option value="MNQ">MNQ (Micro E-mini Nasdaq)</option>
        <option value="NQ">NQ (E-mini Nasdaq)</option>
      </select>
      
      <input id="profit_target" type="number" defaultValue="500" placeholder="Profit Target" />
      <input id="loss_limit" type="number" defaultValue="-300" placeholder="Loss Limit" />
      <input id="max_size" type="number" defaultValue="2" placeholder="Max Size" />
      
      <button onClick={() => startSession('paper')} className="paper">
        Start Paper Trading
      </button>
      <button onClick={() => startSession('live')} className="live">
        Start LIVE Trading
      </button>
    </div>
  );
}

function RiskPanel({ session }) {
  if (!session?.active) return null;
  
  const profitProgress = (session.daily_pnl / session.daily_profit_target) * 100;
  const lossProgress = (session.daily_pnl / session.daily_loss_limit) * 100;
  
  return (
    <div className="panel risk">
      <h3>Risk</h3>
      <div className="progress-bar">
        <label>To Target: ${session.daily_profit_target}</label>
        <div className="bar">
          <div className="fill positive" style={{ width: `${Math.max(0, profitProgress)}%` }} />
        </div>
      </div>
      <div className="progress-bar">
        <label>To Limit: ${session.daily_loss_limit}</label>
        <div className="bar">
          <div className="fill negative" style={{ width: `${Math.max(0, lossProgress)}%` }} />
        </div>
      </div>
    </div>
  );
}

function SignalFeed({ signals }) {
  return (
    <div className="feed signals">
      <h3>Signals</h3>
      <div className="list">
        {signals.map((signal, i) => (
          <div key={i} className={`signal ${signal.approved ? 'approved' : 'rejected'}`}>
            <span className="time">{signal.timestamp}</span>
            <span className="pattern">{signal.pattern}</span>
            <span className={`direction ${signal.direction.toLowerCase()}`}>
              {signal.direction}
            </span>
            <span className="strength">{(signal.strength * 100).toFixed(0)}%</span>
            {!signal.approved && (
              <span className="reason">{signal.rejection_reason}</span>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}
```

---

## 8. Database Schema

```sql
-- Sessions
CREATE TABLE sessions (
    id SERIAL PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    ended_at TIMESTAMP,
    mode VARCHAR(10) NOT NULL,  -- 'paper' or 'live'
    symbol VARCHAR(10) NOT NULL,
    config JSONB NOT NULL,
    
    -- Results
    total_pnl DECIMAL(12, 2),
    total_trades INT,
    winning_trades INT,
    is_halted BOOLEAN DEFAULT FALSE,
    halt_reason TEXT
);

-- Trades
CREATE TABLE trades (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES sessions(id),
    
    -- Entry
    entry_time TIMESTAMP NOT NULL,
    entry_price DECIMAL(12, 4) NOT NULL,
    direction VARCHAR(5) NOT NULL,  -- 'LONG' or 'SHORT'
    size INT NOT NULL,
    
    -- Exit
    exit_time TIMESTAMP,
    exit_price DECIMAL(12, 4),
    exit_reason VARCHAR(20),  -- 'TARGET', 'STOP', 'MANUAL', 'HALTED'
    
    -- P&L
    pnl DECIMAL(12, 2),
    pnl_ticks INT,
    
    -- Context
    signal_id INT REFERENCES signals(id),
    regime VARCHAR(20),
    regime_confidence DECIMAL(4, 3),
    
    -- Metadata
    created_at TIMESTAMP DEFAULT NOW()
);

-- Signals
CREATE TABLE signals (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES sessions(id),
    
    timestamp TIMESTAMP NOT NULL,
    symbol VARCHAR(10) NOT NULL,
    pattern VARCHAR(50) NOT NULL,
    direction VARCHAR(5) NOT NULL,
    strength DECIMAL(4, 3) NOT NULL,
    price DECIMAL(12, 4) NOT NULL,
    
    -- Routing result
    regime VARCHAR(20),
    approved BOOLEAN NOT NULL,
    rejection_reason TEXT,
    
    -- Was it traded?
    traded BOOLEAN DEFAULT FALSE,
    trade_id INT REFERENCES trades(id),
    
    -- Raw details
    details JSONB,
    
    created_at TIMESTAMP DEFAULT NOW()
);

-- Regime history
CREATE TABLE regime_history (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES sessions(id),
    
    timestamp TIMESTAMP NOT NULL,
    regime VARCHAR(20) NOT NULL,
    confidence DECIMAL(4, 3) NOT NULL,
    
    -- Inputs that led to this classification
    inputs JSONB
);

-- Bar data (for analysis)
CREATE TABLE bars (
    id SERIAL PRIMARY KEY,
    session_id INT REFERENCES sessions(id),
    
    symbol VARCHAR(10) NOT NULL,
    timeframe INT NOT NULL,
    start_time TIMESTAMP NOT NULL,
    
    open_price DECIMAL(12, 4),
    high_price DECIMAL(12, 4),
    low_price DECIMAL(12, 4),
    close_price DECIMAL(12, 4),
    
    volume INT,
    delta INT,
    buy_volume INT,
    sell_volume INT,
    
    -- Full footprint data
    levels JSONB,
    
    UNIQUE(symbol, timeframe, start_time)
);

-- Indexes
CREATE INDEX idx_trades_session ON trades(session_id);
CREATE INDEX idx_trades_time ON trades(entry_time);
CREATE INDEX idx_signals_session ON signals(session_id);
CREATE INDEX idx_signals_time ON signals(timestamp);
CREATE INDEX idx_signals_pattern ON signals(pattern);
CREATE INDEX idx_regime_session ON regime_history(session_id);
CREATE INDEX idx_bars_symbol_time ON bars(symbol, start_time);
```

---

## 9. Configuration

### 9.1 Main Configuration File

```yaml
# config.yaml

# Data feed
data_feed:
  provider: "cqg"  # or "rithmic"
  credentials:
    username: "${CQG_USERNAME}"
    password: "${CQG_PASSWORD}"
  
# Trading
trading:
  default_symbol: "MES"
  default_timeframe: 300  # 5 minutes
  
# Order Flow Engine
order_flow:
  imbalance_threshold: 3.0
  imbalance_min_volume: 10
  stacked_imbalance_min: 3
  exhaustion_min_levels: 3
  exhaustion_min_decline: 0.30
  divergence_lookback: 5
  absorption_min_volume: 100
  unfinished_max_volume: 5

# Regime Detection
regime:
  min_regime_score: 4.0
  min_regime_confidence: 0.6
  adx_trend_threshold: 25
  adx_weak_threshold: 20
  atr_high_percentile: 70
  news_buffer_minutes: 15
  no_trade_before_open_minutes: 5
  no_trade_before_close_minutes: 15

# Execution
execution:
  default_stop_ticks: 16
  default_target_ticks: 24
  max_slippage_ticks: 2

# Risk (defaults, can be overridden per session)
risk:
  daily_profit_target: 500.0
  daily_loss_limit: -300.0
  max_position_size: 2
  max_concurrent_trades: 1

# Dashboard
dashboard:
  host: "0.0.0.0"
  port: 8000

# Database
database:
  url: "postgresql://localhost/orderflow"

# Logging
logging:
  level: "INFO"
  file: "logs/trading.log"
```

### 9.2 Symbol-Specific Tuning

```python
SYMBOL_PROFILES = {
    "ES": {
        "imbalance_min_volume": 20,
        "absorption_min_volume": 150,
        "typical_bar_volume": 5000,
        "stop_ticks": 16,   # 4 points
        "target_ticks": 24,  # 6 points
    },
    "MES": {
        "imbalance_min_volume": 5,
        "absorption_min_volume": 30,
        "typical_bar_volume": 500,
        "stop_ticks": 16,
        "target_ticks": 24,
    },
    "NQ": {
        "imbalance_min_volume": 15,
        "absorption_min_volume": 100,
        "typical_bar_volume": 3000,
        "stop_ticks": 20,   # 5 points
        "target_ticks": 32,  # 8 points
    },
    "MNQ": {
        "imbalance_min_volume": 5,
        "absorption_min_volume": 25,
        "typical_bar_volume": 300,
        "stop_ticks": 20,
        "target_ticks": 32,
    },
}
```

---

## 10. Implementation Phases

### Phase 1: Core Infrastructure (Week 1) ✅ COMPLETE
- [x] Set up project structure
- [x] Implement data structures (Tick, PriceLevel, FootprintBar, Signal)
- [x] Build FootprintAggregator
- [ ] Set up PostgreSQL database with schema (using in-memory for now)
- [x] Create basic logging

### Phase 2: Order Flow Engine (Week 1-2) ✅ COMPLETE
- [x] Implement ImbalanceDetector
- [x] Implement ExhaustionDetector
- [x] Implement AbsorptionDetector
- [x] Implement DeltaDivergenceDetector
- [x] Implement UnfinishedBusinessDetector
- [x] Build OrderFlowEngine orchestrator
- [x] Unit tests for each detector

### Phase 3: Regime Detection (Week 2) ✅ COMPLETE
- [x] Implement technical indicator calculations (ADX, ATR, EMA)
- [x] Build RegimeInputsCalculator
- [x] Implement RegimeDetector
- [x] Build StrategyRouter
- [x] Integration tests for regime classification

### Phase 4: Execution Layer (Week 2-3) ✅ COMPLETE
- [x] Implement TradingSession configuration
- [x] Build ExecutionManager
- [ ] Implement NautilusTrader Strategy wrapper (using simpler direct execution)
- [x] Paper trading integration
- [x] Position and P&L tracking
- [x] Dynamic symbol switching with correct tick values

### Phase 5: Dashboard (Week 3) ✅ COMPLETE
- [x] Set up FastAPI server
- [x] Implement REST endpoints
- [x] Implement WebSocket streaming
- [x] Build dashboard (single-page HTML instead of React)
- [x] Real-time state updates
- [x] Settings panel for symbol/risk configuration

### Phase 6: Integration & Testing (Week 3-4) 🔄 IN PROGRESS
- [ ] Connect to CQG data feed (using Polygon.io for historical replay instead)
- [x] End-to-end paper trading tests (demo mode)
- [x] Historical replay via Polygon.io API
- [x] Simulated tick generation from minute bars
- [ ] Performance optimization
- [ ] Parameter tuning

### Phase 7: Live Preparation (Week 4+)
- [ ] Live execution testing with 1 MES contract
- [ ] Monitoring and alerting
- [x] Documentation (README, project docs)
- [ ] Gradual scale-up plan

---

## 11. File Structure

```
orderflow-trading/
├── README.md
├── config.yaml
├── requirements.txt
├── docker-compose.yml
│
├── src/
│   ├── __init__.py
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── types.py              # Tick, PriceLevel, FootprintBar, Signal, Regime
│   │   ├── constants.py          # Tick sizes, symbol profiles
│   │   └── config.py             # Configuration loading
│   │
│   ├── data/
│   │   ├── __init__.py
│   │   ├── adapters/
│   │   │   ├── __init__.py
│   │   │   ├── cqg.py            # CQG data adapter
│   │   │   └── rithmic.py        # Rithmic data adapter
│   │   └── aggregator.py         # FootprintAggregator
│   │
│   ├── analysis/
│   │   ├── __init__.py
│   │   ├── detectors/
│   │   │   ├── __init__.py
│   │   │   ├── imbalance.py
│   │   │   ├── exhaustion.py
│   │   │   ├── absorption.py
│   │   │   ├── divergence.py
│   │   │   └── unfinished.py
│   │   ├── indicators.py         # Technical indicators (ADX, ATR, EMA)
│   │   ├── cumulative_delta.py
│   │   ├── volume_profile.py
│   │   └── engine.py             # OrderFlowEngine
│   │
│   ├── regime/
│   │   ├── __init__.py
│   │   ├── inputs.py             # RegimeInputs, RegimeInputsCalculator
│   │   ├── detector.py           # RegimeDetector
│   │   └── router.py             # StrategyRouter
│   │
│   ├── execution/
│   │   ├── __init__.py
│   │   ├── session.py            # TradingSession
│   │   ├── manager.py            # ExecutionManager
│   │   ├── orders.py             # Order types
│   │   └── nautilus_strategy.py  # NautilusTrader integration
│   │
│   ├── api/
│   │   ├── __init__.py
│   │   ├── server.py             # FastAPI application
│   │   ├── routes/
│   │   │   ├── __init__.py
│   │   │   ├── session.py
│   │   │   ├── trades.py
│   │   │   └── signals.py
│   │   └── websocket.py          # WebSocket handlers
│   │
│   └── database/
│       ├── __init__.py
│       ├── models.py             # SQLAlchemy models
│       ├── connection.py
│       └── queries.py
│
├── dashboard/
│   ├── package.json
│   ├── src/
│   │   ├── App.jsx
│   │   ├── components/
│   │   │   ├── Header.jsx
│   │   │   ├── Controls.jsx
│   │   │   ├── RiskPanel.jsx
│   │   │   ├── RegimePanel.jsx
│   │   │   ├── PositionPanel.jsx
│   │   │   ├── SignalFeed.jsx
│   │   │   └── TradeFeed.jsx
│   │   └── hooks/
│   │       └── useWebSocket.js
│   └── public/
│       └── index.html
│
├── tests/
│   ├── __init__.py
│   ├── test_aggregator.py
│   ├── test_detectors.py
│   ├── test_regime.py
│   ├── test_execution.py
│   └── test_integration.py
│
├── scripts/
│   ├── run_backtest.py
│   ├── download_historical.py
│   └── start_trading.py
│
└── logs/
    └── .gitkeep
```

---

## 12. Dependencies

### Python (requirements.txt)

```
# Core
nautilus_trader>=1.180.0
numpy>=1.24.0
pandas>=2.0.0

# API & Dashboard
fastapi>=0.100.0
uvicorn>=0.23.0
websockets>=11.0
pydantic>=2.0.0

# Database
sqlalchemy>=2.0.0
asyncpg>=0.28.0
alembic>=1.11.0

# Technical Analysis
ta-lib>=0.4.28  # Or pandas-ta as alternative

# Utilities
python-dotenv>=1.0.0
pyyaml>=6.0
structlog>=23.1.0

# Development
pytest>=7.4.0
pytest-asyncio>=0.21.0
black>=23.7.0
mypy>=1.4.0
```

### Frontend (package.json)

```json
{
  "dependencies": {
    "react": "^18.2.0",
    "react-dom": "^18.2.0",
    "react-use-websocket": "^4.5.0"
  },
  "devDependencies": {
    "vite": "^4.4.0",
    "@vitejs/plugin-react": "^4.0.0"
  }
}
```

---

## Notes for Claude Code

1. **Start with Phase 1-2** — Get the data structures and order flow engine working first. Use the ORDER_FLOW_PROJECT.md file for detailed implementations of the pattern detectors.

2. **Test with historical data** — Before connecting to live feed, build a simple CSV/JSON loader to replay historical ticks through the system.

3. **NautilusTrader integration** — Check their docs for the exact CQG adapter API. The wrapper shown here is conceptual.

4. **Keep it modular** — Each component should be independently testable.

5. **Database connection pooling** — Use async SQLAlchemy with connection pooling for the dashboard API.

6. **WebSocket batching** — Don't send every tick to the dashboard. Batch updates every 100ms.

7. **Error handling** — Add comprehensive error handling, especially around data feed disconnects.

8. **Logging** — Use structured logging (structlog) for easier analysis later.
