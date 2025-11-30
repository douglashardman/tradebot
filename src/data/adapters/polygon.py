"""Polygon.io data feed adapter for historical replay."""

import os
import random
import time
from datetime import datetime, timezone, timedelta
from typing import Callable, List, Optional
import logging

from src.core.types import Tick

logger = logging.getLogger(__name__)


class PolygonAdapter:
    """
    Adapter for Polygon.io data feed.

    Uses minute bar data and generates realistic tick simulations
    since free tier doesn't include tick-level data.
    """

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.getenv("POLYGON_API_KEY")
        if not self.api_key:
            raise ValueError("Polygon API key required")

        self.callbacks: List[Callable[[Tick], None]] = []
        self._running = False

    def register_callback(self, callback: Callable[[Tick], None]) -> None:
        """Register a callback to receive ticks."""
        self.callbacks.append(callback)

    def _emit_tick(self, tick: Tick) -> None:
        """Emit tick to all registered callbacks."""
        for callback in self.callbacks:
            callback(tick)

    def get_minute_bars(
        self,
        symbol: str,
        date: str,
        start_time: str = "09:30",
        end_time: str = "16:00"
    ) -> List[dict]:
        """
        Get minute bars for a trading day.

        Args:
            symbol: Ticker symbol (e.g., "ES", "SPY")
            date: Date string YYYY-MM-DD
            start_time: Start time HH:MM
            end_time: End time HH:MM

        Returns:
            List of bar dictionaries with OHLCV data
        """
        try:
            from polygon import RESTClient
        except ImportError:
            raise ImportError("polygon-api-client required: pip install polygon-api-client")

        client = RESTClient(self.api_key)

        bars = []
        try:
            aggs = client.get_aggs(
                ticker=symbol,
                multiplier=1,
                timespan="minute",
                from_=date,
                to=date,
                limit=50000
            )

            for agg in aggs:
                # Convert timestamp (ms) to datetime
                ts = datetime.fromtimestamp(agg.timestamp / 1000, tz=timezone.utc)

                # Filter by time
                time_str = ts.strftime("%H:%M")
                if start_time <= time_str <= end_time:
                    bars.append({
                        "timestamp": ts,
                        "open": agg.open,
                        "high": agg.high,
                        "low": agg.low,
                        "close": agg.close,
                        "volume": agg.volume,
                        "vwap": getattr(agg, 'vwap', agg.close),
                        "trades": getattr(agg, 'transactions', 100)
                    })

            logger.info(f"Loaded {len(bars)} minute bars for {symbol} on {date}")
            return bars

        except Exception as e:
            logger.error(f"Error fetching data: {e}")
            return []

    def bars_to_ticks(
        self,
        bars: List[dict],
        symbol: str,
        ticks_per_bar: int = 50
    ) -> List[Tick]:
        """
        Convert minute bars to simulated ticks.

        Creates realistic tick distribution within each bar:
        - Price moves from open -> high/low -> close
        - Volume distributed with heavier ends
        - Side determined by price direction

        Args:
            bars: List of OHLCV bar dictionaries
            symbol: Symbol name
            ticks_per_bar: Average ticks to generate per bar

        Returns:
            List of Tick objects
        """
        all_ticks = []

        for bar in bars:
            bar_ticks = self._simulate_bar_ticks(
                bar, symbol, ticks_per_bar
            )
            all_ticks.extend(bar_ticks)

        return all_ticks

    def _simulate_bar_ticks(
        self,
        bar: dict,
        symbol: str,
        avg_ticks: int
    ) -> List[Tick]:
        """Generate realistic ticks for a single bar."""
        ticks = []

        o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
        bar_ts = bar["timestamp"]
        total_volume = bar["volume"]

        # Determine bar direction
        bullish = c >= o

        # Create price path: open -> extreme1 -> extreme2 -> close
        if bullish:
            # Bullish: open -> low -> high -> close
            path = [o, l, h, c]
        else:
            # Bearish: open -> high -> low -> close
            path = [o, h, l, c]

        # Generate tick count with some randomness
        num_ticks = max(10, int(avg_ticks * (0.5 + random.random())))

        # Distribute ticks along the path
        # More ticks at turns (reversals)
        tick_prices = []
        tick_weights = [0.15, 0.35, 0.35, 0.15]  # Weight towards middle

        for i in range(len(path) - 1):
            segment_ticks = int(num_ticks * tick_weights[i])
            start_price = path[i]
            end_price = path[i + 1]

            for j in range(segment_ticks):
                # Interpolate with some noise
                progress = j / max(1, segment_ticks - 1)
                base_price = start_price + (end_price - start_price) * progress

                # Add tick noise (within 1 tick)
                noise = random.uniform(-0.25, 0.25)
                price = round(base_price + noise, 2)

                # Keep within bar range
                price = max(l, min(h, price))
                tick_prices.append(price)

        # Add close price
        tick_prices.append(c)

        # Distribute volume (heavier at extremes and close)
        volumes = []
        for i, price in enumerate(tick_prices):
            # Higher volume at turns
            if price == h or price == l or i == len(tick_prices) - 1:
                vol = random.randint(20, 100)
            else:
                vol = random.randint(1, 30)
            volumes.append(vol)

        # Normalize to actual bar volume
        vol_sum = sum(volumes)
        if vol_sum > 0:
            volumes = [int(v * total_volume / vol_sum) for v in volumes]

        # Generate timestamps spread across the minute
        time_delta = timedelta(seconds=60 / max(1, len(tick_prices)))

        # Create ticks
        prev_price = o
        for i, (price, vol) in enumerate(zip(tick_prices, volumes)):
            ts = bar_ts + time_delta * i

            # Determine side based on price movement
            if price > prev_price:
                side = "ASK"  # Buyer lifted offer
            elif price < prev_price:
                side = "BID"  # Seller hit bid
            else:
                side = random.choice(["ASK", "BID"])

            if vol > 0:
                ticks.append(Tick(
                    timestamp=ts,
                    price=price,
                    volume=vol,
                    side=side,
                    symbol=symbol
                ))

            prev_price = price

        return ticks

    def replay(
        self,
        symbol: str,
        date: str,
        speed: float = 10.0,
        start_time: str = "09:30",
        end_time: str = "16:00"
    ) -> None:
        """
        Replay historical data through callbacks.

        Args:
            symbol: Ticker symbol
            date: Date to replay (YYYY-MM-DD)
            speed: Replay speed (1.0 = realtime, 10.0 = 10x faster)
            start_time: Session start
            end_time: Session end
        """
        logger.info(f"Loading {symbol} data for {date}...")

        # Get bars
        bars = self.get_minute_bars(symbol, date, start_time, end_time)
        if not bars:
            logger.error("No data found")
            return

        # Convert to ticks
        logger.info("Converting bars to ticks...")
        ticks = self.bars_to_ticks(bars, symbol)

        logger.info(f"Replaying {len(ticks)} ticks at {speed}x speed")

        self._running = True
        last_ts = ticks[0].timestamp

        for i, tick in enumerate(ticks):
            if not self._running:
                break

            # Calculate delay
            if speed > 0:
                delta = (tick.timestamp - last_ts).total_seconds()
                if delta > 0:
                    time.sleep(delta / speed)

            self._emit_tick(tick)
            last_ts = tick.timestamp

            # Progress update every 1000 ticks
            if i > 0 and i % 1000 == 0:
                logger.info(f"Progress: {i}/{len(ticks)} ticks ({100*i//len(ticks)}%)")

        self._running = False
        logger.info("Replay complete")

    def stop(self) -> None:
        """Stop replay."""
        self._running = False
