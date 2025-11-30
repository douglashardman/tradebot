#!/usr/bin/env python3
"""
Order Flow Trading System - Main Entry Point

Wires together all components:
- Data Feed (Databento/Rithmic)
- Order Flow Engine (pattern detection)
- Strategy Router (regime filtering)
- Execution Manager (trade execution)
- FastAPI Dashboard (web interface)

Usage:
    python main.py                    # Start with default config
    python main.py --mode paper       # Paper trading mode
    python main.py --mode live        # Live trading mode (requires broker connection)
    python main.py --symbol MES       # Trade MES (micro e-mini S&P)
    python main.py --replay ESH4 2024-01-15 2024-01-16  # Replay historical data
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from typing import Optional

import uvicorn

from src.core.types import Signal, FootprintBar
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.api.server import (
    app,
    broadcast_signal,
    active_session,
    execution_manager as api_execution_manager,
)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


class TradingSystem:
    """
    Main trading system that orchestrates all components.

    Data flow:
    Tick Data → OrderFlowEngine → Signals → StrategyRouter → Approved Signals → ExecutionManager
    """

    def __init__(
        self,
        symbol: str = "MES",
        mode: str = "paper",
        timeframe: int = 300,
        config: dict = None,
    ):
        self.symbol = symbol
        self.mode = mode
        self.timeframe = timeframe
        self.config = config or {}

        # Will be initialized on start
        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None
        self.session: Optional[TradingSession] = None
        self.manager: Optional[ExecutionManager] = None
        self.data_adapter = None

        self._running = False
        self._tick_count = 0

    def initialize(self) -> None:
        """Initialize all components."""
        logger.info(f"Initializing trading system for {self.symbol} in {self.mode} mode")

        # Create Order Flow Engine
        engine_config = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            **self.config.get("order_flow", {}),
        }
        self.engine = OrderFlowEngine(engine_config)

        # Create Strategy Router
        router_config = {
            "min_signal_strength": self.config.get("min_signal_strength", 0.6),
            "min_regime_confidence": self.config.get("min_regime_confidence", 0.7),
            **self.config.get("regime", {}),
        }
        self.router = StrategyRouter(router_config)

        # Create Trading Session
        self.session = TradingSession(
            mode=self.mode,
            symbol=self.symbol,
            daily_profit_target=self.config.get("daily_profit_target", 500.0),
            daily_loss_limit=self.config.get("daily_loss_limit", -300.0),
            max_position_size=self.config.get("max_position_size", 2),
            stop_loss_ticks=self.config.get("stop_loss_ticks", 16),
            take_profit_ticks=self.config.get("take_profit_ticks", 24),
            min_signal_strength=self.config.get("min_signal_strength", 0.6),
            min_regime_confidence=self.config.get("min_regime_confidence", 0.7),
        )
        self.session.started_at = datetime.now()

        # Create Execution Manager
        self.manager = ExecutionManager(self.session)

        # Wire up callbacks
        self._wire_callbacks()

        logger.info("Trading system initialized")

    def _wire_callbacks(self) -> None:
        """Wire up all component callbacks."""
        # Engine -> Router (bar updates for regime detection)
        self.engine.on_bar(self._on_bar)

        # Engine -> Signal handler
        self.engine.on_signal(self._on_signal)

    def _on_bar(self, bar: FootprintBar) -> None:
        """Handle completed footprint bar."""
        # Update regime on each bar
        self.router.on_bar(bar)

        # Update positions with current price
        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

        # Log periodic stats
        if self.engine.bar_count % 10 == 0:
            state = self.router.get_state()
            logger.info(
                f"Bar #{self.engine.bar_count} | "
                f"Regime: {state['current_regime']} ({state['regime_confidence']:.2f}) | "
                f"Delta: {self.engine.cumulative_delta.value:+.0f} | "
                f"Signals: {self.engine.signal_count}"
            )

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal from order flow engine."""
        # Evaluate through strategy router
        signal = self.router.evaluate_signal(signal)

        logger.info(
            f"Signal: {signal.pattern.value} | "
            f"Direction: {signal.direction} | "
            f"Strength: {signal.strength:.2f} | "
            f"Approved: {signal.approved}"
        )

        # Broadcast to connected clients
        asyncio.create_task(broadcast_signal(signal))

        # If approved, execute
        if signal.approved and self.manager:
            multiplier = self.router.get_position_size_multiplier()
            order = self.manager.on_signal(signal, multiplier)

            if order:
                logger.info(
                    f"Order placed: {order.side} {order.size} @ {order.entry_price} "
                    f"(stop: {order.stop_price}, target: {order.target_price})"
                )

    def process_tick(self, tick) -> None:
        """Process incoming tick data."""
        self._tick_count += 1
        self.engine.process_tick(tick)

        # Log every 1000 ticks
        if self._tick_count % 1000 == 0:
            logger.debug(f"Processed {self._tick_count} ticks")

    async def start_with_databento(self, live: bool = True) -> None:
        """Start with Databento data feed."""
        from src.data.adapters.databento import DatabentoAdapter

        self.data_adapter = DatabentoAdapter()
        self.data_adapter.register_callback(self.process_tick)

        if live:
            logger.info(f"Starting live Databento feed for {self.symbol}")
            self.data_adapter.start_live(self.symbol)
            self._running = True

            # Keep running until stopped
            while self._running:
                await asyncio.sleep(1)
        else:
            logger.info("No live data feed started - waiting for API commands")

    async def replay_historical(
        self,
        contract: str,
        start: str,
        end: str,
        speed: float = 10.0,
    ) -> None:
        """Replay historical data for backtesting."""
        from src.data.adapters.databento import DatabentoAdapter

        logger.info(f"Replaying {contract} from {start} to {end} at {speed}x speed")

        self.data_adapter = DatabentoAdapter()
        self.data_adapter.register_callback(self.process_tick)

        # Run replay in thread
        import threading

        def _replay():
            self.data_adapter.replay_historical(contract, start, end, speed)

        thread = threading.Thread(target=_replay, daemon=True)
        thread.start()

        self._running = True
        while self._running and thread.is_alive():
            await asyncio.sleep(1)

        logger.info("Replay complete")
        self._print_summary()

    def stop(self) -> None:
        """Stop the trading system."""
        logger.info("Stopping trading system...")
        self._running = False

        if self.data_adapter:
            self.data_adapter.stop_live()

        self._print_summary()

    def _print_summary(self) -> None:
        """Print session summary."""
        if not self.manager:
            return

        stats = self.manager.get_statistics()
        state = self.manager.get_state()

        logger.info("=" * 50)
        logger.info("SESSION SUMMARY")
        logger.info("=" * 50)
        logger.info(f"Total Ticks:      {self._tick_count:,}")
        logger.info(f"Total Bars:       {self.engine.bar_count if self.engine else 0}")
        logger.info(f"Total Signals:    {self.engine.signal_count if self.engine else 0}")
        logger.info(f"Signals Approved: {self.router.signals_approved if self.router else 0}")
        logger.info(f"Total Trades:     {stats.get('total_trades', 0)}")
        logger.info(f"Win Rate:         {stats.get('win_rate', 0):.1%}")
        logger.info(f"Daily P&L:        ${state.get('daily_pnl', 0):.2f}")
        logger.info(f"Profit Factor:    {stats.get('profit_factor', 0):.2f}")
        logger.info("=" * 50)

    def get_status(self) -> dict:
        """Get current system status."""
        return {
            "running": self._running,
            "symbol": self.symbol,
            "mode": self.mode,
            "tick_count": self._tick_count,
            "engine_state": self.engine.get_state() if self.engine else {},
            "router_state": self.router.get_state() if self.router else {},
            "execution_state": self.manager.get_state() if self.manager else {},
        }


# Global trading system instance
trading_system: Optional[TradingSystem] = None


async def main_async(args) -> None:
    """Async main entry point."""
    global trading_system

    # Create trading system
    config = {
        "daily_profit_target": args.profit_target,
        "daily_loss_limit": args.loss_limit,
        "max_position_size": args.position_size,
    }

    trading_system = TradingSystem(
        symbol=args.symbol,
        mode=args.mode,
        timeframe=args.timeframe,
        config=config,
    )
    trading_system.initialize()

    # Handle shutdown signals
    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received")
        if trading_system:
            trading_system.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    # Start based on mode
    if args.replay:
        # Historical replay mode
        contract, start, end = args.replay
        await trading_system.replay_historical(contract, start, end, args.speed)
    elif args.no_data:
        # Dashboard only mode - no data feed, controlled via API
        logger.info("Starting in dashboard-only mode (no data feed)")
        logger.info(f"Dashboard available at http://0.0.0.0:{args.port}")

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=args.port,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()
    else:
        # Live mode with Databento
        logger.info(f"Starting live trading on {args.symbol}")
        logger.info(f"Dashboard available at http://0.0.0.0:{args.port}")

        # Start both the data feed and the API server
        api_task = asyncio.create_task(run_api_server(args.port))
        data_task = asyncio.create_task(trading_system.start_with_databento(live=True))

        await asyncio.gather(api_task, data_task)


async def run_api_server(port: int) -> None:
    """Run the API server."""
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info",
    )
    server = uvicorn.Server(config)
    await server.serve()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Order Flow Trading System",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py                           Start dashboard only
  python main.py --symbol MES --mode paper Start paper trading on MES
  python main.py --replay ESH4 2024-01-15 2024-01-16  Backtest historical data
        """
    )

    # Basic options
    parser.add_argument(
        "--symbol", "-s",
        default="MES",
        help="Trading symbol (default: MES)"
    )
    parser.add_argument(
        "--mode", "-m",
        choices=["paper", "live"],
        default="paper",
        help="Trading mode (default: paper)"
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=8000,
        help="Dashboard port (default: 8000)"
    )
    parser.add_argument(
        "--timeframe", "-t",
        type=int,
        default=300,
        help="Footprint bar timeframe in seconds (default: 300)"
    )

    # Risk parameters
    parser.add_argument(
        "--profit-target",
        type=float,
        default=500.0,
        help="Daily profit target in $ (default: 500)"
    )
    parser.add_argument(
        "--loss-limit",
        type=float,
        default=-300.0,
        help="Daily loss limit in $ (default: -300)"
    )
    parser.add_argument(
        "--position-size",
        type=int,
        default=2,
        help="Max position size (default: 2)"
    )

    # Data options
    parser.add_argument(
        "--replay",
        nargs=3,
        metavar=("CONTRACT", "START", "END"),
        help="Replay historical data (e.g., ESH4 2024-01-15 2024-01-16)"
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=10.0,
        help="Replay speed multiplier (default: 10.0)"
    )
    parser.add_argument(
        "--no-data",
        action="store_true",
        help="Start dashboard only without data feed"
    )

    args = parser.parse_args()

    # Print banner
    print("""
╔═══════════════════════════════════════════════════════════════╗
║           ORDER FLOW TRADING SYSTEM v1.0                      ║
║                                                               ║
║  Real-time order flow analysis with regime-adaptive trading   ║
╚═══════════════════════════════════════════════════════════════╝
    """)

    logger.info(f"Symbol: {args.symbol}")
    logger.info(f"Mode: {args.mode}")
    logger.info(f"Timeframe: {args.timeframe}s")
    logger.info(f"Profit Target: ${args.profit_target}")
    logger.info(f"Loss Limit: ${args.loss_limit}")

    # Run
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
