#!/usr/bin/env python3
"""
Headless Trading System - No Web Server, Discord Only

Designed for locked-down servers:
- No exposed ports
- All status via Discord webhooks (outbound only)
- Auto-starts via systemd
- Runs trading session 9:30 AM - 4:00 PM ET
- Auto-flattens 5 minutes before close
- Sends daily digest at 4:00 PM ET

Environment variables required:
- RITHMIC_USER, RITHMIC_PASSWORD
- DISCORD_WEBHOOK_URL

Usage:
    python run_headless.py              # Production with Rithmic
    python run_headless.py --paper      # Paper trading with Databento
    python run_headless.py --dry-run    # Test without trading
"""

import argparse
import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, time, timedelta
from typing import Optional

import pytz

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.types import Signal, FootprintBar
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.core.notifications import (
    NotificationService,
    DailyDigest,
    AlertType,
    configure_notifications,
)
from src.core.persistence import StatePersistence, get_persistence
from src.core.scheduler import (
    TradingScheduler,
    get_market_close_time,
    is_trading_day,
    is_market_holiday,
)

# Configure logging
LOG_DIR = os.getenv("LOG_DIR", "/var/log/tradebot")
if not os.path.exists(LOG_DIR):
    # Fallback to local logs directory
    LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
    os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "trading.log"), mode="a"),
    ],
)
logger = logging.getLogger("headless")

# Eastern timezone
ET = pytz.timezone("America/New_York")


class HeadlessTradingSystem:
    """
    Fully headless trading system.

    No web server, all status via Discord.
    """

    def __init__(
        self,
        symbol: str = "MES",
        mode: str = "paper",
        dry_run: bool = False,
        timeframe: int = 300,
    ):
        self.symbol = symbol
        self.mode = mode
        self.dry_run = dry_run
        self.timeframe = timeframe

        # Components (initialized in setup)
        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None
        self.session: Optional[TradingSession] = None
        self.manager: Optional[ExecutionManager] = None
        self.data_adapter = None
        self.notifications: Optional[NotificationService] = None
        self.persistence: Optional[StatePersistence] = None
        self.scheduler: Optional[TradingScheduler] = None

        # State
        self._running = False
        self._tick_count = 0
        self._session_start_time: Optional[datetime] = None
        self._starting_balance: float = 10000.0

    async def setup(self) -> bool:
        """Initialize all components."""
        logger.info("=" * 60)
        logger.info("HEADLESS TRADING SYSTEM STARTUP")
        logger.info("=" * 60)
        logger.info(f"Symbol: {self.symbol}")
        logger.info(f"Mode: {self.mode}")
        logger.info(f"Dry run: {self.dry_run}")

        # Set up notifications first (so we can alert on errors)
        webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
        if not webhook_url:
            logger.error("DISCORD_WEBHOOK_URL not set!")
            return False

        self.notifications = configure_notifications(
            webhook_url=webhook_url,
            alert_on_trades=True,  # We want all notifications in headless mode
            alert_on_connection=True,
            alert_on_limits=True,
            alert_on_errors=True,
        )

        # Set up persistence
        self.persistence = get_persistence()

        # Check if today is a trading day
        now_et = datetime.now(ET)
        if not is_trading_day(now_et):
            reason = "weekend" if now_et.weekday() >= 5 else "holiday"
            logger.info(f"Not a trading day ({reason}). Exiting.")
            await self.notifications.send_alert(
                title="No Trading Today",
                message=f"Market closed ({reason}). System will try again tomorrow.",
                alert_type=AlertType.INFO,
            )
            return False

        # Create trading session
        self.session = TradingSession(
            mode=self.mode,
            symbol=self.symbol,
            daily_profit_target=float(os.getenv("DAILY_PROFIT_TARGET", "500")),
            daily_loss_limit=float(os.getenv("DAILY_LOSS_LIMIT", "-300")),
            max_position_size=int(os.getenv("MAX_POSITION_SIZE", "1")),
            stop_loss_ticks=int(os.getenv("STOP_LOSS_TICKS", "16")),
            take_profit_ticks=int(os.getenv("TAKE_PROFIT_TICKS", "24")),
        )
        self.session.started_at = datetime.now()
        self._session_start_time = datetime.now()
        self._starting_balance = self.session.paper_starting_balance

        # Create execution manager
        self.manager = ExecutionManager(self.session)

        # Wire up trade callbacks for Discord alerts
        self.manager.on_trade(self._on_trade_complete)
        self.manager.on_position(self._on_position_opened)

        # Create order flow engine
        self.engine = OrderFlowEngine({
            "symbol": self.symbol,
            "timeframe": self.timeframe,
        })

        # Create strategy router
        self.router = StrategyRouter({})

        # Wire up callbacks
        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

        # Set up scheduler for auto-flatten and daily digest
        market_close = get_market_close_time(now_et)
        flatten_minutes = int(os.getenv("FLATTEN_BEFORE_CLOSE_MINUTES", "5"))

        self.scheduler = TradingScheduler(
            flatten_callback=self._auto_flatten,
            digest_callback=self._send_daily_digest,
            flatten_before_close_minutes=flatten_minutes,
            market_close=market_close,
        )

        logger.info("All components initialized")
        return True

    async def connect_data_feed(self) -> bool:
        """Connect to data feed (Rithmic or Databento)."""
        if self.dry_run:
            logger.info("Dry run mode - no data feed connection")
            return True

        use_rithmic = os.getenv("USE_RITHMIC", "true").lower() == "true"

        if use_rithmic:
            return await self._connect_rithmic()
        else:
            return await self._connect_databento()

    async def _connect_rithmic(self) -> bool:
        """Connect to Rithmic."""
        try:
            from src.data.adapters.rithmic import RithmicAdapter

            user = os.getenv("RITHMIC_USER")
            password = os.getenv("RITHMIC_PASSWORD")

            if not user or not password:
                logger.error("RITHMIC_USER and RITHMIC_PASSWORD required")
                await self.notifications.alert_system_error(
                    "Rithmic credentials missing",
                    "Set RITHMIC_USER and RITHMIC_PASSWORD environment variables",
                )
                return False

            self.data_adapter = RithmicAdapter(
                user=user,
                password=password,
                system_name=os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Test"),
                server_url=os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443"),
            )

            # Register connection callbacks
            self.data_adapter.on_connected(self._on_feed_connected)
            self.data_adapter.on_disconnected(self._on_feed_disconnected)
            self.data_adapter.register_callback(self._process_tick)

            logger.info("Connecting to Rithmic...")
            if not await self.data_adapter.connect():
                await self.notifications.alert_system_error(
                    "Failed to connect to Rithmic",
                    "Check credentials and network connectivity",
                )
                return False

            # Subscribe to market data
            exchange = "CME"
            if not await self.data_adapter.subscribe(self.symbol, exchange):
                await self.notifications.alert_system_error(
                    f"Failed to subscribe to {self.symbol}",
                    "Check symbol and exchange",
                )
                return False

            logger.info(f"Connected to Rithmic, streaming {self.symbol}")
            return True

        except ImportError:
            logger.error("async_rithmic not installed")
            await self.notifications.alert_system_error(
                "async_rithmic not installed",
                "Run: pip install async_rithmic",
            )
            return False
        except Exception as e:
            logger.error(f"Rithmic connection error: {e}")
            await self.notifications.alert_system_error(
                "Rithmic connection error",
                str(e),
            )
            return False

    async def _connect_databento(self) -> bool:
        """Connect to Databento (for paper trading)."""
        try:
            from src.data.adapters.databento import DatabentoAdapter

            api_key = os.getenv("DATABENTO_API_KEY")
            if not api_key:
                logger.error("DATABENTO_API_KEY required")
                await self.notifications.alert_system_error(
                    "Databento API key missing",
                    "Set DATABENTO_API_KEY environment variable",
                )
                return False

            self.data_adapter = DatabentoAdapter(api_key=api_key)
            self.data_adapter.register_callback(self._process_tick)

            logger.info(f"Starting Databento live feed for {self.symbol}")
            self.data_adapter.start_live(self.symbol)
            return True

        except Exception as e:
            logger.error(f"Databento connection error: {e}")
            await self.notifications.alert_system_error(
                "Databento connection error",
                str(e),
            )
            return False

    async def run(self) -> None:
        """Main run loop."""
        # Wait for market open
        await self._wait_for_market_open()

        # Send startup notification
        await self.notifications.send_alert(
            title="Trading Session Started",
            message=(
                f"**Symbol:** {self.symbol}\n"
                f"**Mode:** {self.mode}\n"
                f"**Profit Target:** ${self.session.daily_profit_target:,.0f}\n"
                f"**Loss Limit:** ${abs(self.session.daily_loss_limit):,.0f}\n"
                f"**Max Position:** {self.session.max_position_size} contracts"
            ),
            alert_type=AlertType.SUCCESS,
        )

        # Start scheduler
        self.scheduler.start()

        # Main loop
        self._running = True
        logger.info("Trading session active")

        try:
            while self._running:
                # Check if we should still be trading
                now_et = datetime.now(ET)
                market_close = get_market_close_time(now_et)
                close_dt = now_et.replace(
                    hour=market_close.hour,
                    minute=market_close.minute,
                    second=0,
                )

                if now_et >= close_dt:
                    logger.info("Market closed, ending session")
                    break

                # Check for halt conditions
                if self.manager.is_halted:
                    logger.info(f"Session halted: {self.manager.halt_reason}")
                    await self._on_session_halted()
                    break

                await asyncio.sleep(1)

        except asyncio.CancelledError:
            logger.info("Session cancelled")
        except Exception as e:
            logger.error(f"Session error: {e}")
            await self.notifications.alert_system_error("Session error", str(e))
        finally:
            await self.shutdown()

    async def _wait_for_market_open(self) -> None:
        """Wait until market opens (9:30 AM ET)."""
        market_open = time(9, 30)
        now_et = datetime.now(ET)
        open_dt = now_et.replace(hour=9, minute=30, second=0, microsecond=0)

        if now_et.time() < market_open:
            wait_seconds = (open_dt - now_et).total_seconds()
            logger.info(f"Waiting {wait_seconds/60:.1f} minutes for market open")

            await self.notifications.send_alert(
                title="Waiting for Market Open",
                message=f"Trading will begin at 9:30 AM ET ({wait_seconds/60:.0f} minutes)",
                alert_type=AlertType.INFO,
            )

            await asyncio.sleep(wait_seconds)

    async def shutdown(self) -> None:
        """Clean shutdown."""
        logger.info("Shutting down...")
        self._running = False

        # Stop scheduler
        if self.scheduler:
            self.scheduler.stop()

        # Flatten any open positions
        if self.manager and self.manager.open_positions:
            await self._auto_flatten()

        # Send daily digest
        await self._send_daily_digest()

        # Disconnect data feed
        if self.data_adapter:
            if hasattr(self.data_adapter, 'disconnect'):
                await self.data_adapter.disconnect()
            elif hasattr(self.data_adapter, 'stop_live'):
                self.data_adapter.stop_live()

        # Close notifications
        if self.notifications:
            await self.notifications.close()

        # Clear persistence (clean shutdown)
        if self.persistence:
            self.persistence.clear_state()

        logger.info("Shutdown complete")

    # === Callbacks ===

    def _process_tick(self, tick) -> None:
        """Process incoming tick."""
        self._tick_count += 1
        if self.engine:
            self.engine.process_tick(tick)

        # Save state periodically
        if self._tick_count % 10000 == 0:
            self._save_state()

    def _on_bar(self, bar: FootprintBar) -> None:
        """Handle completed bar."""
        if self.router:
            self.router.on_bar(bar)

        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal from engine."""
        if not self.router or not self.manager:
            return

        # Evaluate through router
        signal = self.router.evaluate_signal(signal)

        if signal.approved and not self.dry_run:
            multiplier = self.router.get_position_size_multiplier()
            order = self.manager.on_signal(signal, multiplier)

            if order:
                logger.info(
                    f"Order: {order.side} {order.size} @ {order.entry_price}"
                )

    def _on_trade_complete(self, trade) -> None:
        """Handle completed trade - send Discord alert."""
        asyncio.create_task(self._alert_trade_closed(trade))
        self._save_state()

    def _on_position_opened(self, position) -> None:
        """Handle new position - send Discord alert."""
        asyncio.create_task(self._alert_position_opened(position))
        self._save_state()

    async def _on_feed_connected(self, plant_type: str = "") -> None:
        """Handle data feed connection."""
        logger.info(f"Data feed connected: {plant_type}")
        await self.notifications.alert_connection_restored(plant_type)

    async def _on_feed_disconnected(self, plant_type: str = "") -> None:
        """Handle data feed disconnection."""
        logger.warning(f"Data feed disconnected: {plant_type}")
        await self.notifications.alert_connection_lost(plant_type)

    async def _on_session_halted(self) -> None:
        """Handle session halt."""
        reason = self.manager.halt_reason or "Unknown"
        pnl = self.manager.daily_pnl

        if "loss limit" in reason.lower():
            await self.notifications.alert_daily_loss_limit(pnl)
        elif "profit target" in reason.lower():
            await self.notifications.alert_daily_profit_target(pnl)
        else:
            await self.notifications.send_alert(
                title="Session Halted",
                message=f"**Reason:** {reason}\n**Daily P&L:** ${pnl:+,.2f}",
                alert_type=AlertType.WARNING,
            )

    # === Alert Helpers ===

    async def _alert_position_opened(self, position) -> None:
        """Send Discord alert for new position."""
        emoji = "📈" if position.side == "LONG" else "📉"
        await self.notifications.send_alert(
            title=f"Position Opened",
            message=(
                f"{emoji} **{position.side}** {position.size} {position.symbol}\n"
                f"**Entry:** {position.entry_price:.2f}\n"
                f"**Stop:** {position.stop_price:.2f}\n"
                f"**Target:** {position.target_price:.2f}"
            ),
            alert_type=AlertType.TRADE_OPEN,
        )

    async def _alert_trade_closed(self, trade) -> None:
        """Send Discord alert for closed trade."""
        emoji = "✅" if trade.pnl >= 0 else "❌"
        pnl_str = f"+${trade.pnl:,.2f}" if trade.pnl >= 0 else f"-${abs(trade.pnl):,.2f}"

        await self.notifications.send_alert(
            title=f"Trade Closed",
            message=(
                f"{emoji} **{trade.side}** {trade.size} {trade.symbol}\n"
                f"**Entry:** {trade.entry_price:.2f} → **Exit:** {trade.exit_price:.2f}\n"
                f"**P&L:** {pnl_str} ({trade.exit_reason})\n"
                f"**Daily P&L:** ${self.manager.daily_pnl:+,.2f}"
            ),
            alert_type=AlertType.TRADE_CLOSE if trade.pnl >= 0 else AlertType.WARNING,
        )

    # === Scheduled Tasks ===

    async def _auto_flatten(self) -> None:
        """Auto-flatten all positions before market close."""
        if not self.manager or not self.manager.open_positions:
            logger.info("No positions to flatten")
            return

        logger.info("Auto-flattening positions...")

        # Get current price
        current_price = None
        for pos in self.manager.open_positions:
            if pos.current_price:
                current_price = pos.current_price
                break

        if current_price is None:
            current_price = self.manager.open_positions[0].entry_price

        # Close all
        trades = self.manager.close_all_positions(current_price, "AUTO_FLATTEN")

        total_pnl = sum(t.pnl for t in trades)
        await self.notifications.send_alert(
            title="Auto-Flatten Complete",
            message=(
                f"Closed {len(trades)} position(s) before market close.\n"
                f"**P&L from flatten:** ${total_pnl:+,.2f}\n"
                f"**Final Daily P&L:** ${self.manager.daily_pnl:+,.2f}"
            ),
            alert_type=AlertType.INFO if total_pnl >= 0 else AlertType.WARNING,
        )

    async def _send_daily_digest(self) -> None:
        """Send end-of-day summary."""
        if not self.manager:
            return

        stats = self.manager.get_statistics()
        state = self.manager.get_state()

        # Build regime breakdown
        regime_breakdown = {}
        for trade in self.manager.completed_trades:
            regime = getattr(trade, 'regime', 'UNKNOWN')
            regime_breakdown[regime] = regime_breakdown.get(regime, 0) + 1

        # Build trades detail
        trades_detail = []
        for trade in self.manager.completed_trades[-10:]:
            trades_detail.append({
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "exit_reason": trade.exit_reason,
                "pnl": trade.pnl,
                "entry_time": trade.entry_time.strftime("%H:%M") if trade.entry_time else "",
            })

        # Position status
        position_str = "FLAT"
        if self.manager.open_positions:
            pos = self.manager.open_positions[0]
            position_str = f"{pos.side} {pos.size} @ {pos.entry_price}"

        # Balance
        ending_balance = getattr(
            self.manager, 'paper_balance',
            self._starting_balance + self.manager.daily_pnl
        )

        # Status
        status = "COMPLETED"
        if self.manager.is_halted:
            status = f"STOPPED EARLY ({self.manager.halt_reason})"

        digest = DailyDigest(
            date=datetime.now().strftime("%Y-%m-%d"),
            session_start="09:30",
            session_end=get_market_close_time().strftime("%H:%M"),
            status=status,
            starting_balance=self._starting_balance,
            ending_balance=ending_balance,
            day_pnl=self.manager.daily_pnl,
            trades=stats.get("total_trades", 0),
            wins=state.get("win_count", 0),
            losses=state.get("loss_count", 0),
            win_rate=stats.get("win_rate", 0) * 100,
            trades_detail=trades_detail,
            regime_breakdown=regime_breakdown,
            current_position=position_str,
            account_balance=ending_balance,
        )

        await self.notifications.send_daily_digest(digest)

    # === State Management ===

    def _save_state(self) -> None:
        """Save current state for crash recovery."""
        if not self.persistence or not self.manager:
            return

        from src.core.persistence import serialize_positions, serialize_trades

        state = {
            "daily_pnl": self.manager.daily_pnl,
            "is_halted": self.manager.is_halted,
            "halt_reason": self.manager.halt_reason,
            "positions": serialize_positions(self.manager.open_positions),
            "trades": serialize_trades(self.manager.completed_trades),
            "tick_count": self._tick_count,
            "paper_balance": getattr(self.manager, 'paper_balance', None),
        }

        self.persistence.save_state(state)


async def main():
    """Entry point."""
    parser = argparse.ArgumentParser(description="Headless Trading System")
    parser.add_argument(
        "--symbol", "-s",
        default=os.getenv("TRADING_SYMBOL", "MES"),
        help="Trading symbol (default: MES)",
    )
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Use paper trading mode",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Dry run (no actual trading)",
    )
    args = parser.parse_args()

    mode = "paper" if args.paper else os.getenv("TRADING_MODE", "paper")

    # Create system
    system = HeadlessTradingSystem(
        symbol=args.symbol,
        mode=mode,
        dry_run=args.dry_run,
    )

    # Handle signals for graceful shutdown
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Received shutdown signal")
        system._running = False

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    # Setup
    if not await system.setup():
        logger.error("Setup failed")
        sys.exit(1)

    # Connect to data feed
    if not await system.connect_data_feed():
        logger.error("Data feed connection failed")
        sys.exit(1)

    # Run
    await system.run()


if __name__ == "__main__":
    asyncio.run(main())
