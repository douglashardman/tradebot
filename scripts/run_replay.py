#!/usr/bin/env python3
"""
Replay historical market data through the trading system.

Uses Polygon.io data to replay a specific trading day.

Usage:
    python scripts/run_replay.py --date 2024-11-13 --symbol ES --speed 50
    Then open http://localhost:8000/dashboard in your browser
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, time

import uvicorn

from src.core.types import Tick, Signal, FootprintBar
from src.data.adapters.polygon import PolygonAdapter
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
logger = logging.getLogger("replay")


class ReplaySystem:
    """Trading system fed by historical replay data."""

    def __init__(self, symbol: str = "ES", api_key: str = None):
        self.symbol = symbol

        # Polygon adapter
        self.adapter = PolygonAdapter(api_key)

        # Engine with 5-minute bars for more meaningful signals
        self.engine = OrderFlowEngine({
            "symbol": symbol,
            "timeframe": 300,  # 5-minute bars
        })

        # Router
        self.router = StrategyRouter({
            "min_signal_strength": 0.70,
            "min_regime_confidence": 0.60,
            "session_open": time(9, 30),
            "session_close": time(16, 0),
            "regime": {
                "min_regime_score": 4.0,
                "adx_trend_threshold": 25,
            },
        })

        # Session
        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=500.0,
            daily_loss_limit=-300.0,
            max_position_size=1,
            max_concurrent_trades=1,
            stop_loss_ticks=5,
            take_profit_ticks=4,
            trading_start=time(9, 30),
            trading_end=time(16, 0),
            no_trade_windows=[],
        )
        self.session.started_at = datetime.now()

        # Execution manager
        self.manager = ExecutionManager(self.session)

        # Trade cooldown
        self.last_trade_time = None
        self.trade_cooldown_seconds = 60  # 1 minute between trades

        # Wire callbacks
        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)
        self.manager.on_trade(self._on_trade)
        self.adapter.register_callback(self._on_tick)

        # Set up server globals
        server.active_session = self.session
        server.execution_manager = self.manager
        server.strategy_router = self.router

        # Stats
        self.tick_count = 0
        self._running = False

    def _on_tick(self, tick: Tick):
        """Process incoming tick."""
        self.tick_count += 1
        self.engine.process_tick(tick)

    def _on_bar(self, bar: FootprintBar):
        """Handle bar completion."""
        self.router.on_bar(bar)
        if bar.close_price:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal):
        """Handle signal from engine."""
        signal = self.router.evaluate_signal(signal)

        # Broadcast to dashboard (fire and forget from thread)
        try:
            import threading
            if hasattr(self, '_main_loop') and self._main_loop:
                self._main_loop.call_soon_threadsafe(
                    lambda s=signal: self._main_loop.create_task(broadcast_signal(s))
                )
        except Exception:
            pass  # Skip broadcast if no loop available

        if signal.approved:
            # Check cooldown
            now = datetime.now()
            if self.last_trade_time:
                seconds_since = (now - self.last_trade_time).total_seconds()
                if seconds_since < self.trade_cooldown_seconds:
                    return

            # Check position
            if self.manager.pending_orders or self.manager.open_positions:
                return

            # Strength check
            if signal.strength < 0.70:
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
        """Handle trade completion."""
        try:
            if hasattr(self, '_main_loop') and self._main_loop:
                self._main_loop.call_soon_threadsafe(
                    lambda t=trade: self._main_loop.create_task(broadcast_trade(t))
                )
        except Exception:
            pass

    def set_loop(self, loop):
        """Set the main event loop for async callbacks."""
        self._main_loop = loop

    def run_replay(self, date: str, speed: float):
        """Run the replay in a thread."""
        logger.info(f"Starting replay of {self.symbol} for {date} at {speed}x speed")
        self._running = True
        self.adapter.replay(
            symbol=self.symbol,
            date=date,
            speed=speed,
            start_time="09:30",
            end_time="16:00"
        )
        self._running = False

        # Final stats
        stats = self.manager.get_statistics()
        state = self.manager.get_state()
        logger.info("=" * 50)
        logger.info("REPLAY COMPLETE")
        logger.info("=" * 50)
        logger.info(f"Ticks processed: {self.tick_count}")
        logger.info(f"Bars created: {self.engine.bar_count}")
        logger.info(f"Signals generated: {self.engine.signal_count}")
        logger.info(f"Trades executed: {stats.get('total_trades', 0)}")
        logger.info(f"Win rate: {stats.get('win_rate', 0):.1%}")
        logger.info(f"Daily P&L: ${state.get('daily_pnl', 0):.2f}")
        logger.info("=" * 50)

    def stop(self):
        """Stop the replay."""
        self._running = False
        self.adapter.stop()


async def main(args):
    """Run the replay."""
    logger.info("=" * 50)
    logger.info(f"Order Flow Trading - Historical Replay")
    logger.info(f"Symbol: {args.symbol}")
    logger.info(f"Date: {args.date}")
    logger.info(f"Speed: {args.speed}x")
    logger.info("=" * 50)
    logger.info(f"Dashboard: http://localhost:8000/dashboard")
    logger.info("=" * 50)

    # Create replay system
    api_key = args.api_key or os.getenv("POLYGON_API_KEY")
    if not api_key:
        logger.error("Polygon API key required. Set POLYGON_API_KEY or use --api-key")
        sys.exit(1)

    replay = ReplaySystem(symbol=args.symbol, api_key=api_key)

    # Handle shutdown
    def shutdown_handler(signum, frame):
        logger.info("Shutting down...")
        replay.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Configure server
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8000,
        log_level="warning",
    )
    server_instance = uvicorn.Server(config)

    # Get the current event loop and pass to replay
    loop = asyncio.get_running_loop()
    replay.set_loop(loop)

    # Run replay in background thread
    import threading
    replay_thread = threading.Thread(
        target=replay.run_replay,
        args=(args.date, args.speed),
        daemon=True
    )

    # Start server and replay
    try:
        replay_thread.start()
        await server_instance.serve()
    except asyncio.CancelledError:
        pass
    finally:
        replay.stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Replay historical market data")
    parser.add_argument(
        "--date",
        type=str,
        default="2024-11-13",
        help="Date to replay (YYYY-MM-DD)"
    )
    parser.add_argument(
        "--symbol",
        type=str,
        default="ES",
        help="Symbol to replay (ES, SPY, etc.)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=50.0,
        help="Replay speed multiplier (1=realtime, 50=50x faster)"
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Polygon API key (or set POLYGON_API_KEY env var)"
    )

    args = parser.parse_args()
    asyncio.run(main(args))
