#!/usr/bin/env python3
"""
Run the trading system with simulated data for demonstration.

This starts the FastAPI server and feeds it simulated market data
so you can see the dashboard in action.

Usage:
    python scripts/run_demo.py
    Then open http://localhost:8000/dashboard in your browser
"""

import asyncio
import logging
import random
import signal
import sys
from datetime import datetime, time, timedelta

import uvicorn

from src.core.types import Tick, Signal, FootprintBar
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.api import server
from src.api.server import app, broadcast, broadcast_signal, broadcast_trade

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo")


class DemoMarketSimulator:
    """Generates realistic market data for demo purposes."""

    def __init__(self, base_price: float = 5050.0):
        self.price = base_price
        self.tick_size = 0.25
        self.trend_direction = random.choice([1, -1])
        self.trend_duration = random.randint(50, 200)
        self.trend_ticks = 0
        self.tick_count = 0

    def generate_tick(self) -> Tick:
        """Generate a single realistic tick."""
        self.tick_count += 1
        self.trend_ticks += 1

        # Maybe change trend
        if self.trend_ticks >= self.trend_duration:
            self.trend_direction = random.choice([1, -1, 0])
            self.trend_duration = random.randint(30, 150)
            self.trend_ticks = 0

        # Price movement
        if self.trend_direction != 0:
            prob_with_trend = 0.6
            direction = self.trend_direction if random.random() < prob_with_trend else -self.trend_direction
        else:
            direction = random.choice([1, -1])

        magnitude = 1 if random.random() < 0.8 else random.randint(2, 3)
        self.price += direction * magnitude * self.tick_size

        # Volume
        base_volume = 10 if self.trend_direction == 0 else 20
        volume = random.randint(1, base_volume + random.randint(0, 50))

        # Side
        if direction > 0:
            side = "ASK" if random.random() < 0.7 else "BID"
        else:
            side = "BID" if random.random() < 0.7 else "ASK"

        # Create imbalances occasionally
        if random.random() < 0.1:
            volume = random.randint(50, 150)

        return Tick(
            timestamp=datetime.now(),
            price=round(self.price, 2),
            volume=volume,
            side=side,
            symbol="MES"
        )


class DemoSystem:
    """Demo trading system with simulated data."""

    def __init__(self):
        # Engine
        self.engine = OrderFlowEngine({
            "symbol": "MES",
            "timeframe": 5,  # 5-second bars for demo
        })

        # Router - STRICT thresholds to reduce overtrading
        self.router = StrategyRouter({
            "min_signal_strength": 0.75,      # Only take 75%+ strength signals
            "min_regime_confidence": 0.65,    # Need 65%+ regime confidence
            "session_open": time(0, 0),       # Extended hours for demo
            "session_close": time(23, 59),    # Extended hours for demo
            "regime": {
                "min_regime_score": 4.0,      # Default - need clear regime
                "adx_trend_threshold": 25,    # ADX > 25 for trend
            },
        })

        # Session with extended hours - SCALPING MODE
        self.session = TradingSession(
            mode="paper",
            symbol="MES",
            daily_profit_target=500.0,
            daily_loss_limit=-300.0,
            max_position_size=1,          # Only 1 contract at a time
            max_concurrent_trades=1,      # Only 1 trade at a time
            stop_loss_ticks=5,            # Scalping: tight stop ($6.25)
            take_profit_ticks=4,          # Scalping: quick target ($5.00)
            trading_start=time(0, 0),
            trading_end=time(23, 59),
            no_trade_windows=[],
        )
        self.session.started_at = datetime.now()

        # Execution manager
        self.manager = ExecutionManager(self.session)

        # Trade cooldown tracking - shorter for scalping
        self.last_trade_time = None
        self.trade_cooldown_seconds = 10  # 10 seconds between trades for scalping

        # Simulator
        self.simulator = DemoMarketSimulator()

        # Wire callbacks
        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)
        self.manager.on_trade(self._on_trade)

        # Set up server globals
        server.active_session = self.session
        server.execution_manager = self.manager
        server.strategy_router = self.router

        self._running = False

    def _on_bar(self, bar: FootprintBar):
        """Handle bar completion."""
        self.router.on_bar(bar)
        if bar.close_price:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal):
        """Handle signal from engine."""
        signal = self.router.evaluate_signal(signal)

        # Broadcast to dashboard
        asyncio.create_task(broadcast_signal(signal))

        if signal.approved:
            # Check cooldown - don't trade too frequently
            now = datetime.now()
            if self.last_trade_time:
                seconds_since_last = (now - self.last_trade_time).total_seconds()
                if seconds_since_last < self.trade_cooldown_seconds:
                    logger.debug(
                        f"Skipping signal - cooldown ({seconds_since_last:.0f}s < {self.trade_cooldown_seconds}s)"
                    )
                    return

            # Check if already in a position
            if self.manager.pending_orders:
                logger.debug("Skipping signal - already in a position")
                return

            # Extra strength check - be very selective
            if signal.strength < 0.75:
                logger.debug(f"Skipping signal - strength {signal.strength:.2f} < 0.75")
                return

            multiplier = self.router.get_position_size_multiplier()
            order = self.manager.on_signal(signal, multiplier)
            if order:
                self.last_trade_time = now
                logger.info(
                    f"ORDER: {order.side} {order.size} @ {order.entry_price:.2f} "
                    f"(strength: {signal.strength:.2f})"
                )

    def _on_trade(self, trade):
        """Handle trade completion - broadcast to dashboard."""
        asyncio.create_task(broadcast_trade(trade))

    async def run_data_feed(self):
        """Generate and process simulated ticks."""
        self._running = True
        tick_count = 0

        while self._running:
            tick = self.simulator.generate_tick()
            self.engine.process_tick(tick)
            tick_count += 1

            # Small delay to simulate real market speed
            # ~10 ticks per second
            await asyncio.sleep(0.1)

            # Log progress
            if tick_count % 100 == 0:
                stats = self.manager.get_statistics()
                logger.info(
                    f"Ticks: {tick_count} | "
                    f"Bars: {self.engine.bar_count} | "
                    f"Signals: {self.engine.signal_count} | "
                    f"Trades: {stats.get('total_trades', 0)} | "
                    f"P&L: ${self.manager.daily_pnl:.2f}"
                )

    def stop(self):
        """Stop the demo."""
        self._running = False


async def main():
    """Run the demo."""
    logger.info("Starting Order Flow Trading Demo")
    logger.info("=" * 50)
    logger.info("Dashboard: http://localhost:8000/dashboard")
    logger.info("API Docs:  http://localhost:8000/docs")
    logger.info("=" * 50)

    # Create demo system
    demo = DemoSystem()

    # Handle shutdown
    def shutdown_handler(signum, frame):
        logger.info("Shutting down...")
        demo.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Configure API server
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",  # Less verbose logging
    )
    server_instance = uvicorn.Server(config)

    # Run both data feed and server concurrently
    try:
        await asyncio.gather(
            demo.run_data_feed(),
            server_instance.serve(),
        )
    except asyncio.CancelledError:
        pass
    finally:
        demo.stop()


if __name__ == "__main__":
    asyncio.run(main())
