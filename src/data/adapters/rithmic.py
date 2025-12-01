"""Rithmic data feed adapter with automatic reconnection and order execution."""

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Callable, Dict, List, Optional, Any

from src.core.types import Tick

logger = logging.getLogger(__name__)


class OrderState(Enum):
    """Order lifecycle states."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    WORKING = "WORKING"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


@dataclass
class LiveOrder:
    """Tracks a live order submitted to Rithmic."""
    order_id: str
    symbol: str
    exchange: str
    side: str  # BUY or SELL
    quantity: int
    order_type: str  # MARKET or LIMIT
    price: Optional[float] = None
    stop_ticks: Optional[int] = None
    target_ticks: Optional[int] = None

    # Broker tracking
    broker_order_id: Optional[str] = None
    state: OrderState = OrderState.PENDING
    filled_quantity: int = 0
    filled_price: Optional[float] = None

    # Timestamps
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None

    # Bracket linkage
    bracket_id: Optional[str] = None
    is_entry: bool = False

    # Rejection info
    rejection_reason: Optional[str] = None


@dataclass
class LivePosition:
    """Tracks a live position from Rithmic."""
    symbol: str
    exchange: str
    side: str  # LONG or SHORT
    quantity: int
    avg_price: float
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    bracket_id: Optional[str] = None


class RithmicAdapter:
    """
    Adapter for Rithmic data feed and order execution via async_rithmic.

    Features:
    - Live tick data streaming
    - Automatic reconnection with exponential backoff
    - Connection health monitoring
    - Bracket order submission with server-side OCO
    - Fill and rejection callback handling
    - Position tracking and reconciliation
    """

    def __init__(
        self,
        user: str,
        password: str,
        system_name: str = "Rithmic Test",
        app_name: str = "OrderFlowTrader",
        app_version: str = "1.0",
        server_url: str = "rituz00100.rithmic.com:443",
        account_id: Optional[str] = None,
    ):
        """
        Initialize Rithmic adapter.

        Args:
            user: Rithmic username
            password: Rithmic password
            system_name: Rithmic system name (e.g., "Rithmic Test", "Rithmic Paper Trading")
            app_name: Application name for identification
            app_version: Application version
            server_url: Rithmic server URL
            account_id: Trading account ID (required for order submission)
        """
        self.user = user
        self.password = password
        self.system_name = system_name
        self.app_name = app_name
        self.app_version = app_version
        self.server_url = server_url
        self.account_id = account_id

        self.client = None
        self.callbacks: List[Callable[[Tick], None]] = []
        self._running = False
        self._connected = False
        self._last_tick_time: Optional[datetime] = None
        self._tick_count = 0
        self._reconnect_attempts = 0
        self._current_symbol: Optional[str] = None
        self._current_exchange: Optional[str] = None

        # Connection health tracking
        self._connection_lost_at: Optional[datetime] = None
        self._on_connected_callbacks: List[Callable] = []
        self._on_disconnected_callbacks: List[Callable] = []

        # Order tracking
        self._orders: Dict[str, LiveOrder] = {}  # order_id -> LiveOrder
        self._positions: Dict[str, LivePosition] = {}  # symbol -> LivePosition
        self._on_fill_callbacks: List[Callable] = []
        self._on_rejection_callbacks: List[Callable] = []
        self._order_lock = asyncio.Lock() if asyncio else None

    def register_callback(self, callback: Callable[[Tick], None]) -> None:
        """Register a callback to receive ticks."""
        self.callbacks.append(callback)

    def on_connected(self, callback: Callable) -> None:
        """Register callback for connection events."""
        self._on_connected_callbacks.append(callback)

    def on_disconnected(self, callback: Callable) -> None:
        """Register callback for disconnection events."""
        self._on_disconnected_callbacks.append(callback)

    def _emit_tick(self, tick: Tick) -> None:
        """Emit tick to all registered callbacks."""
        self._last_tick_time = datetime.now(timezone.utc)
        self._tick_count += 1
        for callback in self.callbacks:
            try:
                callback(tick)
            except Exception as e:
                logger.error(f"Error in tick callback: {e}")

    async def _handle_connected(self, plant_type: str) -> None:
        """Handle connection event."""
        logger.info(f"Connected to Rithmic plant: {plant_type}")
        self._connected = True
        self._reconnect_attempts = 0
        self._connection_lost_at = None

        for callback in self._on_connected_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(plant_type)
                else:
                    callback(plant_type)
            except Exception as e:
                logger.error(f"Error in connected callback: {e}")

    async def _handle_disconnected(self, plant_type: str) -> None:
        """Handle disconnection event."""
        logger.warning(f"Disconnected from Rithmic plant: {plant_type}")
        self._connected = False
        self._connection_lost_at = datetime.now(timezone.utc)

        for callback in self._on_disconnected_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    await callback(plant_type)
                else:
                    callback(plant_type)
            except Exception as e:
                logger.error(f"Error in disconnected callback: {e}")

    async def _tick_callback(self, data: dict) -> None:
        """Process incoming tick data from Rithmic."""
        try:
            # Import here to avoid import errors if async_rithmic not installed
            from async_rithmic import DataType, LastTradePresenceBits

            if data.get("data_type") == DataType.LAST_TRADE:
                if data.get("presence_bits", 0) & LastTradePresenceBits.LAST_TRADE:
                    # Convert Rithmic data to our Tick format
                    tick = Tick(
                        timestamp=datetime.now(timezone.utc),
                        price=float(data.get("trade_price", 0)),
                        volume=int(data.get("trade_size", 0)),
                        side="ASK" if data.get("aggressor") == 1 else "BID",
                        symbol=self._current_symbol or "ES",
                    )
                    self._emit_tick(tick)
        except Exception as e:
            logger.error(f"Error processing Rithmic tick: {e}")

    async def connect(self) -> bool:
        """
        Connect to Rithmic.

        Returns:
            True if connected successfully, False otherwise.
        """
        try:
            from async_rithmic import RithmicClient, ReconnectionSettings

            # Create reconnection settings with exponential backoff
            reconnection = ReconnectionSettings(
                max_retries=None,  # Infinite retries
                backoff_type="exponential",
                interval=2,
                max_delay=60,
                jitter_range=(0.5, 2.0),
            )

            self.client = RithmicClient(
                user=self.user,
                password=self.password,
                system_name=self.system_name,
                app_name=self.app_name,
                app_version=self.app_version,
                url=self.server_url,
                reconnection_settings=reconnection,
            )

            # Register event handlers
            self.client.on_connected += self._handle_connected
            self.client.on_disconnected += self._handle_disconnected
            self.client.on_tick += self._tick_callback

            logger.info(f"Connecting to Rithmic at {self.server_url}...")
            await self.client.connect()
            self._connected = True
            logger.info("Successfully connected to Rithmic")

            # Set up order notification callbacks
            await self._setup_order_callbacks()

            # Reconcile positions on connect
            if self.account_id:
                await self.reconcile_positions()

            return True

        except ImportError:
            logger.error("async_rithmic not installed. Run: pip install async_rithmic")
            return False
        except Exception as e:
            logger.error(f"Failed to connect to Rithmic: {e}")
            return False

    async def disconnect(self) -> None:
        """Disconnect from Rithmic."""
        self._running = False
        if self.client:
            try:
                # Unsubscribe from market data if subscribed
                if self._current_symbol and self._current_exchange:
                    from async_rithmic import DataType
                    await self.client.unsubscribe_from_market_data(
                        self._current_symbol,
                        self._current_exchange,
                        DataType.LAST_TRADE,
                    )
                await self.client.disconnect()
                logger.info("Disconnected from Rithmic")
            except Exception as e:
                logger.error(f"Error disconnecting from Rithmic: {e}")
        self._connected = False

    async def subscribe(self, symbol: str, exchange: str = "CME") -> bool:
        """
        Subscribe to market data for a symbol.

        Args:
            symbol: Symbol to subscribe to (e.g., "ES", "MES")
            exchange: Exchange (default "CME")

        Returns:
            True if subscription successful, False otherwise.
        """
        if not self.client or not self._connected:
            logger.error("Not connected to Rithmic")
            return False

        try:
            from async_rithmic import DataType

            # Get front month contract
            security_code = await self.client.get_front_month_contract(symbol, exchange)
            logger.info(f"Subscribing to {security_code} on {exchange}")

            # Subscribe to last trade data
            await self.client.subscribe_to_market_data(
                security_code,
                exchange,
                DataType.LAST_TRADE,
            )

            self._current_symbol = security_code
            self._current_exchange = exchange
            self._running = True
            logger.info(f"Successfully subscribed to {security_code}")
            return True

        except Exception as e:
            logger.error(f"Failed to subscribe to {symbol}: {e}")
            return False

    async def start_live(self, symbol: str, exchange: str = "CME") -> None:
        """
        Start live data streaming.

        Args:
            symbol: Symbol to stream (e.g., "ES", "MES")
            exchange: Exchange (default "CME")
        """
        if not await self.connect():
            logger.error("Failed to connect to Rithmic")
            return

        if not await self.subscribe(symbol, exchange):
            logger.error(f"Failed to subscribe to {symbol}")
            return

        logger.info(f"Streaming live data for {symbol}")
        self._running = True

        # Keep running until stopped
        while self._running:
            await asyncio.sleep(1)

    def stop_live(self) -> None:
        """Stop live data streaming."""
        self._running = False

    def get_health(self) -> dict:
        """
        Get connection health status.

        Returns:
            Dictionary with health information.
        """
        return {
            "connected": self._connected,
            "last_tick": self._last_tick_time.isoformat() if self._last_tick_time else None,
            "tick_count": self._tick_count,
            "reconnect_attempts": self._reconnect_attempts,
            "connection_lost_at": (
                self._connection_lost_at.isoformat() if self._connection_lost_at else None
            ),
            "current_symbol": self._current_symbol,
            "current_exchange": self._current_exchange,
        }

    @property
    def is_healthy(self) -> bool:
        """Check if connection is healthy (connected and receiving data)."""
        if not self._connected:
            return False

        if self._last_tick_time is None:
            return True  # Just connected, no ticks yet

        # Consider unhealthy if no ticks for 60 seconds during market hours
        elapsed = (datetime.now(timezone.utc) - self._last_tick_time).total_seconds()
        return elapsed < 60

    async def get_account_balance(self, account_id: Optional[str] = None) -> Optional[float]:
        """
        Query account balance from Rithmic.

        Args:
            account_id: Specific account ID to query. If None, uses default account.

        Returns:
            Account balance in dollars, or None if query fails.
        """
        if not self.client or not self._connected:
            logger.warning("Cannot query balance: not connected to Rithmic")
            return None

        try:
            # Try to get account list and balance
            # Note: This depends on async_rithmic supporting account queries
            # The exact method name may vary based on the library version
            if hasattr(self.client, 'get_account_list'):
                accounts = await self.client.get_account_list()
                if accounts:
                    # Get the first account or specified account
                    target_account = account_id or accounts[0].get('account_id')
                    if hasattr(self.client, 'get_account_balance'):
                        balance_data = await self.client.get_account_balance(target_account)
                        if balance_data:
                            # Return the available balance
                            return float(balance_data.get('available_balance', 0))

            # Alternative: Try get_pnl_position_updates if available
            if hasattr(self.client, 'get_pnl_position_updates'):
                pnl_data = await self.client.get_pnl_position_updates()
                if pnl_data and 'account_balance' in pnl_data:
                    return float(pnl_data['account_balance'])

            logger.debug("Account balance query not supported by this Rithmic connection")
            return None

        except Exception as e:
            logger.warning(f"Failed to query account balance: {e}")
            return None

    async def get_margin_requirement(self, symbol: str, exchange: str = "CME") -> Optional[float]:
        """
        Query initial margin requirement for a symbol from Rithmic.

        Args:
            symbol: Trading symbol (e.g., "MES", "ES")
            exchange: Exchange code (default: "CME")

        Returns:
            Initial margin requirement in dollars per contract, or None if query fails.
        """
        if not self.client or not self._connected:
            logger.warning("Cannot query margin: not connected to Rithmic")
            return None

        try:
            # Try to get margin info from Rithmic
            # The exact method depends on async_rithmic version
            if hasattr(self.client, 'get_product_margin'):
                margin_data = await self.client.get_product_margin(symbol, exchange)
                if margin_data:
                    # Return initial margin (day trading margin)
                    return float(margin_data.get('initial_margin', margin_data.get('day_margin', 0)))

            # Alternative: get_reference_data may include margin info
            if hasattr(self.client, 'get_reference_data'):
                ref_data = await self.client.get_reference_data(symbol, exchange)
                if ref_data and 'margin' in ref_data:
                    return float(ref_data['margin'])

            # Alternative: search_symbols may include margin info
            if hasattr(self.client, 'search_symbols'):
                results = await self.client.search_symbols(symbol, exchange)
                if results:
                    for result in results:
                        if result.get('symbol') == symbol:
                            margin = result.get('initial_margin') or result.get('margin')
                            if margin:
                                return float(margin)

            logger.debug(f"Margin query not supported for {symbol}")
            return None

        except Exception as e:
            logger.warning(f"Failed to query margin for {symbol}: {e}")
            return None

    def on_account_update(self, callback: Callable) -> None:
        """
        Register callback for account updates (balance changes, fills, etc.).

        Args:
            callback: Async callback function that receives account update data.
        """
        if not hasattr(self, '_account_callbacks'):
            self._account_callbacks: List[Callable] = []
        self._account_callbacks.append(callback)

    async def _handle_account_update(self, data: dict) -> None:
        """Process account update from Rithmic."""
        if hasattr(self, '_account_callbacks'):
            for callback in self._account_callbacks:
                try:
                    if asyncio.iscoroutinefunction(callback):
                        await callback(data)
                    else:
                        callback(data)
                except Exception as e:
                    logger.error(f"Error in account callback: {e}")

    # ==================== ORDER EXECUTION ====================

    def on_fill(self, callback: Callable) -> None:
        """Register callback for order fills."""
        self._on_fill_callbacks.append(callback)

    def on_rejection(self, callback: Callable) -> None:
        """Register callback for order rejections."""
        self._on_rejection_callbacks.append(callback)

    async def _setup_order_callbacks(self) -> None:
        """Set up order notification handlers with Rithmic client."""
        if not self.client:
            return

        try:
            # Register for order notifications
            if hasattr(self.client, 'on_exchange_order_notification'):
                self.client.on_exchange_order_notification += self._handle_order_notification
                logger.info("Registered for order notifications")
        except Exception as e:
            logger.error(f"Failed to setup order callbacks: {e}")

    async def _handle_order_notification(self, notification: dict) -> None:
        """
        Process order notification from Rithmic.

        Handles fills, rejections, and order state changes.
        """
        try:
            from async_rithmic import ExchangeOrderNotificationType

            order_id = notification.get("order_id") or notification.get("user_tag")
            notify_type = notification.get("notify_type")

            # Find our tracked order
            order = self._orders.get(order_id)

            if notify_type == ExchangeOrderNotificationType.FILL:
                fill_price = float(notification.get("fill_price", 0))
                fill_qty = int(notification.get("fill_qty", 0))

                logger.info(
                    f"FILL: {order_id} - {fill_qty} @ {fill_price}"
                )

                if order:
                    order.filled_quantity += fill_qty
                    order.filled_price = fill_price
                    order.filled_at = datetime.now(timezone.utc)

                    if order.filled_quantity >= order.quantity:
                        order.state = OrderState.FILLED
                    else:
                        order.state = OrderState.PARTIALLY_FILLED

                # Notify callbacks
                fill_data = {
                    "order_id": order_id,
                    "fill_price": fill_price,
                    "fill_qty": fill_qty,
                    "order": order,
                    "raw": notification,
                }
                for callback in self._on_fill_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(fill_data)
                        else:
                            callback(fill_data)
                    except Exception as e:
                        logger.error(f"Error in fill callback: {e}")

            elif notify_type == ExchangeOrderNotificationType.REJECT:
                reason = notification.get("text", "Unknown rejection reason")
                logger.warning(f"REJECTED: {order_id} - {reason}")

                if order:
                    order.state = OrderState.REJECTED
                    order.rejection_reason = reason

                # Notify callbacks
                rejection_data = {
                    "order_id": order_id,
                    "reason": reason,
                    "order": order,
                    "raw": notification,
                }
                for callback in self._on_rejection_callbacks:
                    try:
                        if asyncio.iscoroutinefunction(callback):
                            await callback(rejection_data)
                        else:
                            callback(rejection_data)
                    except Exception as e:
                        logger.error(f"Error in rejection callback: {e}")

            elif notify_type == ExchangeOrderNotificationType.CANCEL:
                logger.info(f"CANCELLED: {order_id}")
                if order:
                    order.state = OrderState.CANCELLED

            elif notify_type == ExchangeOrderNotificationType.MODIFY:
                logger.debug(f"MODIFIED: {order_id}")
                if order:
                    order.state = OrderState.WORKING

        except Exception as e:
            logger.error(f"Error handling order notification: {e}")

    async def submit_bracket_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        stop_ticks: int,
        target_ticks: int,
        exchange: str = "CME",
        bracket_id: Optional[str] = None,
    ) -> Optional[LiveOrder]:
        """
        Submit a bracket order with server-side OCO stop/target.

        Args:
            symbol: Contract symbol (e.g., "MESH5")
            side: "LONG" or "SHORT"
            quantity: Number of contracts
            stop_ticks: Stop loss distance in ticks
            target_ticks: Take profit distance in ticks
            exchange: Exchange (default "CME")
            bracket_id: Optional ID to link related orders

        Returns:
            LiveOrder if submitted successfully, None otherwise.
        """
        if not self.client or not self._connected:
            logger.error("Cannot submit order: not connected to Rithmic")
            return None

        if not self.account_id:
            logger.error("Cannot submit order: account_id not configured")
            return None

        try:
            from async_rithmic import OrderType, TransactionType

            # Generate order ID
            order_id = str(uuid.uuid4())[:12]

            # Map side to transaction type
            if side == "LONG":
                txn_type = TransactionType.BUY
            elif side == "SHORT":
                txn_type = TransactionType.SELL
            else:
                logger.error(f"Invalid side: {side}")
                return None

            # Create order tracking object
            order = LiveOrder(
                order_id=order_id,
                symbol=symbol,
                exchange=exchange,
                side="BUY" if side == "LONG" else "SELL",
                quantity=quantity,
                order_type="MARKET",
                stop_ticks=stop_ticks,
                target_ticks=target_ticks,
                bracket_id=bracket_id or str(uuid.uuid4())[:8],
                is_entry=True,
            )

            # Store before submission
            async with self._order_lock:
                self._orders[order_id] = order

            logger.info(
                f"Submitting bracket order: {side} {quantity} {symbol} "
                f"(stop: {stop_ticks} ticks, target: {target_ticks} ticks)"
            )

            # Submit to Rithmic with server-side bracket
            await self.client.submit_order(
                order_id=order_id,
                security_code=symbol,
                exchange=exchange,
                qty=quantity,
                order_type=OrderType.MARKET,
                transaction_type=txn_type,
                account_id=self.account_id,
                stop_ticks=stop_ticks,
                target_ticks=target_ticks,
            )

            order.state = OrderState.SUBMITTED
            order.submitted_at = datetime.now(timezone.utc)

            logger.info(f"Bracket order submitted: {order_id}")
            return order

        except Exception as e:
            logger.error(f"Failed to submit bracket order: {e}")
            # Remove failed order from tracking
            async with self._order_lock:
                self._orders.pop(order_id, None)
            return None

    async def cancel_order(self, order_id: str) -> bool:
        """
        Cancel a specific order.

        Args:
            order_id: Order ID to cancel

        Returns:
            True if cancellation sent, False otherwise.
        """
        if not self.client or not self._connected:
            logger.error("Cannot cancel: not connected")
            return False

        try:
            await self.client.cancel_order(order_id)
            logger.info(f"Cancel request sent for {order_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {order_id}: {e}")
            return False

    async def cancel_all_orders(self) -> bool:
        """
        Cancel all open orders.

        Returns:
            True if cancellation sent, False otherwise.
        """
        if not self.client or not self._connected:
            logger.error("Cannot cancel: not connected")
            return False

        try:
            await self.client.cancel_all_orders()
            logger.info("Cancel all orders request sent")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel all orders: {e}")
            return False

    async def exit_position(
        self,
        symbol: str,
        exchange: str = "CME",
    ) -> bool:
        """
        Exit all positions for a symbol (flatten).

        Args:
            symbol: Contract symbol
            exchange: Exchange

        Returns:
            True if exit order sent, False otherwise.
        """
        if not self.client or not self._connected:
            logger.error("Cannot exit: not connected")
            return False

        try:
            if hasattr(self.client, 'exit_position'):
                await self.client.exit_position(
                    security_code=symbol,
                    exchange=exchange,
                    account_id=self.account_id,
                )
                logger.info(f"Exit position sent for {symbol}")
                return True
            else:
                logger.warning("exit_position not available, using cancel_all_orders")
                return await self.cancel_all_orders()
        except Exception as e:
            logger.error(f"Failed to exit position: {e}")
            return False

    async def list_orders(self) -> List[Dict[str, Any]]:
        """
        List current orders from Rithmic.

        Returns:
            List of order dictionaries from the broker.
        """
        if not self.client or not self._connected:
            return []

        try:
            if hasattr(self.client, 'list_orders'):
                orders = await self.client.list_orders()
                return orders if orders else []
            return []
        except Exception as e:
            logger.error(f"Failed to list orders: {e}")
            return []

    async def get_positions(self) -> List[Dict[str, Any]]:
        """
        Get current positions from Rithmic.

        Returns:
            List of position dictionaries from the broker.
        """
        if not self.client or not self._connected:
            return []

        try:
            # Try different methods that might be available
            if hasattr(self.client, 'get_positions'):
                positions = await self.client.get_positions()
                return positions if positions else []
            elif hasattr(self.client, 'list_positions'):
                positions = await self.client.list_positions()
                return positions if positions else []
            return []
        except Exception as e:
            logger.error(f"Failed to get positions: {e}")
            return []

    async def reconcile_positions(self) -> Dict[str, LivePosition]:
        """
        Reconcile local position tracking with broker positions.

        Call this on startup and after reconnection to ensure
        we have accurate position state.

        Returns:
            Dictionary of reconciled positions.
        """
        logger.info("Reconciling positions with broker...")

        broker_positions = await self.get_positions()

        # Clear and rebuild local tracking
        self._positions.clear()

        for pos in broker_positions:
            symbol = pos.get("symbol") or pos.get("security_code", "")
            exchange = pos.get("exchange", "CME")
            qty = int(pos.get("quantity", 0) or pos.get("net_qty", 0))
            avg_price = float(pos.get("avg_price", 0) or pos.get("avg_fill_price", 0))

            if qty != 0:
                side = "LONG" if qty > 0 else "SHORT"
                live_pos = LivePosition(
                    symbol=symbol,
                    exchange=exchange,
                    side=side,
                    quantity=abs(qty),
                    avg_price=avg_price,
                    unrealized_pnl=float(pos.get("unrealized_pnl", 0)),
                    realized_pnl=float(pos.get("realized_pnl", 0)),
                )
                self._positions[symbol] = live_pos
                logger.info(f"Position: {side} {abs(qty)} {symbol} @ {avg_price}")

        logger.info(f"Reconciliation complete: {len(self._positions)} positions")
        return self._positions

    def get_tracked_orders(self) -> Dict[str, LiveOrder]:
        """Get all locally tracked orders."""
        return self._orders.copy()

    def get_tracked_positions(self) -> Dict[str, LivePosition]:
        """Get all locally tracked positions."""
        return self._positions.copy()


# Factory function for easy instantiation from environment variables
def create_rithmic_adapter_from_env() -> RithmicAdapter:
    """Create RithmicAdapter using environment variables."""
    import os

    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")
    server = os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443")
    system_name = os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Test")
    account_id = os.getenv("RITHMIC_ACCOUNT_ID")

    if not user or not password:
        raise ValueError(
            "RITHMIC_USER and RITHMIC_PASSWORD environment variables required"
        )

    if not account_id:
        logger.warning(
            "RITHMIC_ACCOUNT_ID not set - order submission will be disabled"
        )

    return RithmicAdapter(
        user=user,
        password=password,
        server_url=server,
        system_name=system_name,
        account_id=account_id,
    )
