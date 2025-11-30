"""
Headless operations module for automated trading.

Integrates:
- Notification service (Discord alerts)
- State persistence (crash recovery)
- Trading scheduler (auto-flatten, daily digest)
- Connection monitoring
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from src.core.notifications import (
    NotificationService,
    DailyDigest,
    AlertType,
    get_notification_service,
    configure_notifications,
)
from src.core.persistence import (
    StatePersistence,
    get_persistence,
    serialize_positions,
    serialize_trades,
)
from src.core.scheduler import (
    TradingScheduler,
    get_market_close_time,
    is_trading_day,
)

logger = logging.getLogger(__name__)


class TradingOperations:
    """
    Central operations manager for headless trading.

    Coordinates:
    - Real-time alerts via Discord
    - Daily digest at 4 PM ET
    - Auto-flatten before market close
    - State persistence for crash recovery
    - Connection health monitoring
    """

    def __init__(
        self,
        discord_webhook_url: Optional[str] = None,
        alert_on_trades: bool = False,
        alert_on_connection: bool = True,
        flatten_before_close_minutes: int = 5,
        enable_persistence: bool = True,
    ):
        """
        Initialize operations manager.

        Args:
            discord_webhook_url: Discord webhook URL for notifications.
            alert_on_trades: Send alerts for each trade (can be noisy).
            alert_on_connection: Send connection status alerts.
            flatten_before_close_minutes: Minutes before close to auto-flatten.
            enable_persistence: Enable state persistence.
        """
        # Set up notifications
        self.notifications = configure_notifications(
            webhook_url=discord_webhook_url,
            alert_on_trades=alert_on_trades,
            alert_on_connection=alert_on_connection,
        )

        # Set up persistence
        self.persistence = get_persistence() if enable_persistence else None

        # Set up scheduler
        self.scheduler = TradingScheduler(
            flatten_callback=self._on_auto_flatten,
            digest_callback=self._on_daily_digest,
            flatten_before_close_minutes=flatten_before_close_minutes,
        )

        # References set by main.py
        self.execution_manager = None
        self.strategy_router = None
        self.data_adapter = None

        # State tracking
        self._session_start_time: Optional[datetime] = None
        self._starting_balance: float = 0.0

    def set_execution_manager(self, manager) -> None:
        """Set reference to execution manager."""
        self.execution_manager = manager

    def set_strategy_router(self, router) -> None:
        """Set reference to strategy router."""
        self.strategy_router = router

    def set_data_adapter(self, adapter) -> None:
        """Set reference to data adapter."""
        self.data_adapter = adapter

        # Wire up connection callbacks if adapter supports them
        if hasattr(adapter, 'on_connected'):
            adapter.on_connected(self._on_feed_connected)
        if hasattr(adapter, 'on_disconnected'):
            adapter.on_disconnected(self._on_feed_disconnected)

    def start(self) -> None:
        """Start operations (scheduler, persistence checks)."""
        self.scheduler.start()
        self._session_start_time = datetime.now()

        if self.execution_manager:
            self._starting_balance = getattr(
                self.execution_manager, 'paper_balance',
                getattr(self.execution_manager.session, 'paper_starting_balance', 10000)
            )

        # Check for state to recover
        if self.persistence and self.persistence.has_saved_state():
            age = self.persistence.get_state_age()
            if age and age < 3600:  # Less than 1 hour old
                logger.info(f"Found recent saved state ({age/60:.1f} minutes old)")
                # Could auto-restore here, but safer to let user decide

    def stop(self) -> None:
        """Stop operations and clean up."""
        self.scheduler.stop()

        # Clear state on clean shutdown
        if self.persistence:
            self.persistence.clear_state()

        asyncio.create_task(self.notifications.close())

    def save_state(self) -> None:
        """Save current trading state."""
        if not self.persistence or not self.execution_manager:
            return

        state = {
            "session": self.execution_manager.session.to_dict() if self.execution_manager.session else {},
            "daily_pnl": self.execution_manager.daily_pnl,
            "is_halted": self.execution_manager.is_halted,
            "halt_reason": self.execution_manager.halt_reason,
            "positions": serialize_positions(self.execution_manager.open_positions),
            "trades": serialize_trades(self.execution_manager.completed_trades),
            "paper_balance": getattr(self.execution_manager, 'paper_balance', None),
        }

        self.persistence.save_state(state)

    def load_state(self) -> Optional[Dict[str, Any]]:
        """Load saved trading state."""
        if not self.persistence:
            return None
        return self.persistence.load_state()

    # === Callbacks ===

    async def _on_feed_connected(self, plant_type: str = "") -> None:
        """Handle data feed connection."""
        logger.info(f"Data feed connected: {plant_type}")
        await self.notifications.alert_connection_restored(plant_type)

    async def _on_feed_disconnected(self, plant_type: str = "") -> None:
        """Handle data feed disconnection."""
        logger.warning(f"Data feed disconnected: {plant_type}")
        await self.notifications.alert_connection_lost(plant_type)

    async def _on_auto_flatten(self) -> None:
        """Handle auto-flatten before market close."""
        if not self.execution_manager:
            return

        if not self.execution_manager.open_positions:
            logger.info("No positions to flatten")
            return

        # Get current price (use last position's current price)
        current_price = None
        for pos in self.execution_manager.open_positions:
            if pos.current_price:
                current_price = pos.current_price
                break

        if current_price is None:
            # Use entry price as fallback
            current_price = self.execution_manager.open_positions[0].entry_price

        # Close all positions
        trades = self.execution_manager.close_all_positions(current_price, "AUTO_FLATTEN")

        logger.info(f"Auto-flattened {len(trades)} positions")

        # Alert
        total_pnl = sum(t.pnl for t in trades)
        await self.notifications.send_alert(
            title="Auto-Flatten Complete",
            message=f"Closed {len(trades)} positions before market close.\nP&L from closed positions: ${total_pnl:+,.2f}",
            alert_type=AlertType.INFO if total_pnl >= 0 else AlertType.WARNING,
        )

        # Save state
        self.save_state()

    async def _on_daily_digest(self) -> None:
        """Send daily trading summary."""
        if not self.execution_manager:
            return

        # Build digest data
        stats = self.execution_manager.get_statistics()
        state = self.execution_manager.get_state()

        # Get regime breakdown
        regime_breakdown = {}
        for trade in self.execution_manager.completed_trades:
            regime = getattr(trade, 'regime', 'UNKNOWN')
            regime_breakdown[regime] = regime_breakdown.get(regime, 0) + 1

        # Build trades detail
        trades_detail = []
        for trade in self.execution_manager.completed_trades[-10:]:  # Last 10 trades
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
        if self.execution_manager.open_positions:
            pos = self.execution_manager.open_positions[0]
            position_str = f"{pos.side} {pos.size} @ {pos.entry_price}"

        # Current balance
        ending_balance = getattr(
            self.execution_manager, 'paper_balance',
            self._starting_balance + self.execution_manager.daily_pnl
        )

        # Determine session status
        status = "COMPLETED"
        if self.execution_manager.is_halted:
            status = f"STOPPED EARLY ({self.execution_manager.halt_reason})"

        digest = DailyDigest(
            date=datetime.now().strftime("%Y-%m-%d"),
            session_start="09:30",
            session_end="16:00",
            status=status,
            starting_balance=self._starting_balance,
            ending_balance=ending_balance,
            day_pnl=self.execution_manager.daily_pnl,
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

    # === Trade Event Handlers ===

    async def on_trade_complete(self, trade) -> None:
        """Handle trade completion - persist state and alert."""
        # Save state after each trade
        self.save_state()

        # Send alert if configured
        if self.notifications.alert_on_trades:
            await self.notifications.alert_trade_closed(
                side=trade.side,
                size=trade.size,
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                pnl=trade.pnl,
                exit_reason=trade.exit_reason,
                symbol=trade.symbol,
            )

    async def on_position_opened(self, position) -> None:
        """Handle new position - persist state and alert."""
        # Save state
        self.save_state()

        # Send alert if configured
        if self.notifications.alert_on_trades:
            await self.notifications.alert_trade_opened(
                side=position.side,
                size=position.size,
                price=position.entry_price,
                symbol=position.symbol,
            )

    async def on_session_halted(self, reason: str, pnl: float) -> None:
        """Handle session halt - send appropriate alert."""
        if "loss limit" in reason.lower():
            await self.notifications.alert_daily_loss_limit(pnl)
        elif "profit target" in reason.lower():
            await self.notifications.alert_daily_profit_target(pnl)
        else:
            await self.notifications.send_alert(
                title="Session Halted",
                message=f"Trading stopped: {reason}\nDaily P&L: ${pnl:+,.2f}",
                alert_type=AlertType.WARNING,
            )

        # Save state
        self.save_state()

    async def on_error(self, error: str, details: str = "") -> None:
        """Handle system errors."""
        await self.notifications.alert_system_error(error, details)


# Global operations instance
_operations: Optional[TradingOperations] = None


def get_operations() -> Optional[TradingOperations]:
    """Get global operations instance."""
    return _operations


def initialize_operations(
    discord_webhook_url: Optional[str] = None,
    **kwargs,
) -> TradingOperations:
    """
    Initialize the global operations manager.

    Args:
        discord_webhook_url: Discord webhook URL.
        **kwargs: Additional configuration options.

    Returns:
        Configured TradingOperations instance.
    """
    global _operations
    _operations = TradingOperations(
        discord_webhook_url=discord_webhook_url,
        **kwargs,
    )
    return _operations
