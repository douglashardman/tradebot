#!/usr/bin/env python3
"""
Simulate trading for testing the dashboard.

This script generates synthetic tick data and runs it through the order flow engine
to generate signals and trades without needing a real data feed.

Usage:
    python scripts/simulate_trading.py
"""

import asyncio
import random
import logging
from datetime import datetime, timedelta

from src.core.types import Tick
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.api.server import broadcast_signal

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("simulator")


class MarketSimulator:
    """
    Generates synthetic tick data that mimics real market behavior.

    Creates patterns like:
    - Trending moves (series of ticks in one direction)
    - Mean reversion (oscillation around a price)
    - Volatility clusters (bursts of activity)
    - Volume imbalances (buy/sell aggression)
    """

    def __init__(self, base_price: float = 5000.0, tick_size: float = 0.25):
        self.price = base_price
        self.tick_size = tick_size
        self.tick_count = 0
        self.trend_direction = random.choice([1, -1])
        self.trend_duration = random.randint(50, 200)
        self.trend_ticks = 0
        # Simulated time starts at market open
        self.sim_time = datetime.now().replace(hour=9, minute=30, second=0, microsecond=0)

    def generate_tick(self) -> Tick:
        """Generate a single tick."""
        self.tick_count += 1
        self.trend_ticks += 1

        # Maybe change trend
        if self.trend_ticks >= self.trend_duration:
            self.trend_direction = random.choice([1, -1, 0])  # 0 = ranging
            self.trend_duration = random.randint(30, 150)
            self.trend_ticks = 0
            logger.debug(f"Trend changed to {'UP' if self.trend_direction > 0 else 'DOWN' if self.trend_direction < 0 else 'RANGE'}")

        # Price movement
        if self.trend_direction != 0:
            # Trending: bias in trend direction
            prob_with_trend = 0.6
            direction = self.trend_direction if random.random() < prob_with_trend else -self.trend_direction
        else:
            # Ranging: mean reversion
            direction = random.choice([1, -1])

        # Random magnitude (usually 1 tick, sometimes more)
        magnitude = 1 if random.random() < 0.8 else random.randint(2, 3)
        self.price += direction * magnitude * self.tick_size

        # Volume - higher during trends, lower during ranges
        base_volume = 10 if self.trend_direction == 0 else 20
        volume = random.randint(1, base_volume + random.randint(0, 50))

        # Aggressor side - bias based on price direction
        if direction > 0:
            side = "ASK" if random.random() < 0.7 else "BID"
        else:
            side = "BID" if random.random() < 0.7 else "ASK"

        # Create imbalances occasionally
        if random.random() < 0.1:
            volume = random.randint(50, 150)  # Large trade

        # Generate timestamp that advances time (100ms per tick average)
        # This simulates real market activity spread across time
        self.sim_time += timedelta(milliseconds=random.randint(50, 200))

        return Tick(
            timestamp=self.sim_time,
            price=round(self.price, 2),
            volume=volume,
            side=side,
            symbol="MES"
        )


async def run_simulation():
    """Run the trading simulation."""
    logger.info("Starting trading simulation")

    # Initialize components
    engine_config = {
        "symbol": "MES",
        "timeframe": 5,  # 5-second bars for faster testing
    }
    engine = OrderFlowEngine(engine_config)

    router_config = {
        "min_signal_strength": 0.5,
        "min_regime_confidence": 0.3,  # Lower threshold for simulation
        "regime": {
            "min_regime_score": 2.0,  # Lower threshold
            "min_bars_in_regime": 1,
        }
    }
    router = StrategyRouter(router_config)

    from datetime import time
    session = TradingSession(
        mode="paper",
        symbol="MES",
        daily_profit_target=500.0,
        daily_loss_limit=-300.0,
        max_position_size=2,
        # Extended trading hours for simulation
        trading_start=time(0, 0),
        trading_end=time(23, 59),
        no_trade_windows=[],  # No lunch break for simulation
    )
    session.started_at = datetime.now()

    manager = ExecutionManager(session)

    # Track stats
    total_signals = 0
    approved_signals = 0

    def on_bar(bar):
        """Handle bar completion."""
        router.on_bar(bar)
        manager.update_prices(bar.close_price)

        # Log bar info
        logger.debug(
            f"Bar: O={bar.open_price:.2f} H={bar.high_price:.2f} "
            f"L={bar.low_price:.2f} C={bar.close_price:.2f} "
            f"Delta={bar.delta:+d} Vol={bar.total_volume}"
        )

    def on_signal(signal):
        """Handle signal from engine."""
        nonlocal total_signals, approved_signals
        total_signals += 1

        # Route through strategy router
        signal = router.evaluate_signal(signal)

        if signal.approved:
            approved_signals += 1
            logger.info(
                f"APPROVED: {signal.pattern.value} | {signal.direction} | "
                f"Strength: {signal.strength:.2f} | Regime: {signal.regime}"
            )

            # Execute trade
            multiplier = router.get_position_size_multiplier()
            order = manager.on_signal(signal, multiplier)

            if order:
                logger.info(
                    f"ORDER: {order.side} {order.size} @ {order.entry_price:.2f} "
                    f"(stop: {order.stop_price:.2f}, target: {order.target_price:.2f})"
                )
        else:
            logger.debug(
                f"REJECTED: {signal.pattern.value} | {signal.rejection_reason}"
            )

    # Wire callbacks
    engine.on_bar(on_bar)
    engine.on_signal(on_signal)

    # Create simulator
    simulator = MarketSimulator(base_price=5050.0)

    # Run simulation
    logger.info("Generating synthetic tick data...")
    tick_count = 0
    target_ticks = 5000  # Generate 5000 ticks

    while tick_count < target_ticks:
        tick = simulator.generate_tick()
        engine.process_tick(tick)
        tick_count += 1

        # Small delay to not overwhelm
        if tick_count % 100 == 0:
            await asyncio.sleep(0.01)

        # Log progress
        if tick_count % 500 == 0:
            state = router.get_state()
            stats = manager.get_statistics()
            logger.info(
                f"Progress: {tick_count}/{target_ticks} ticks | "
                f"Bars: {engine.bar_count} | "
                f"Regime: {state['current_regime']} | "
                f"Signals: {total_signals} (approved: {approved_signals}) | "
                f"Trades: {stats.get('total_trades', 0)} | "
                f"P&L: ${manager.daily_pnl:.2f}"
            )

    # Final stats
    logger.info("=" * 60)
    logger.info("SIMULATION COMPLETE")
    logger.info("=" * 60)

    state = router.get_state()
    stats = manager.get_statistics()

    logger.info(f"Total Ticks:      {tick_count:,}")
    logger.info(f"Bars Generated:   {engine.bar_count}")
    logger.info(f"Total Signals:    {total_signals}")
    logger.info(f"Approved Signals: {approved_signals}")
    logger.info(f"Total Trades:     {stats.get('total_trades', 0)}")
    logger.info(f"Win Rate:         {stats.get('win_rate', 0):.1%}")
    logger.info(f"Daily P&L:        ${manager.daily_pnl:.2f}")
    logger.info(f"Profit Factor:    {stats.get('profit_factor', 0):.2f}")
    logger.info(f"Paper Balance:    ${manager.paper_balance:.2f}")

    logger.info("=" * 60)

    # Show recent trades
    if manager.completed_trades:
        logger.info("Recent Trades:")
        for trade in manager.completed_trades[-5:]:
            logger.info(
                f"  {trade.side} {trade.size} @ {trade.entry_price:.2f} -> "
                f"{trade.exit_price:.2f} ({trade.exit_reason}) | "
                f"P&L: ${trade.pnl:.2f}"
            )


if __name__ == "__main__":
    asyncio.run(run_simulation())
