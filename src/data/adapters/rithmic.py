"""Rithmic data feed adapter with automatic reconnection."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, List, Optional

from src.core.types import Tick

logger = logging.getLogger(__name__)


class RithmicAdapter:
    """
    Adapter for Rithmic data feed via async_rithmic.

    Features:
    - Live tick data streaming
    - Automatic reconnection with exponential backoff
    - Connection health monitoring
    - Heartbeat tracking
    """

    def __init__(
        self,
        user: str,
        password: str,
        system_name: str = "Rithmic Test",
        app_name: str = "OrderFlowTrader",
        app_version: str = "1.0",
        server_url: str = "rituz00100.rithmic.com:443",
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
        """
        self.user = user
        self.password = password
        self.system_name = system_name
        self.app_name = app_name
        self.app_version = app_version
        self.server_url = server_url

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


# Factory function for easy instantiation from environment variables
def create_rithmic_adapter_from_env() -> RithmicAdapter:
    """Create RithmicAdapter using environment variables."""
    import os

    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")
    server = os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443")
    system_name = os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Test")

    if not user or not password:
        raise ValueError(
            "RITHMIC_USER and RITHMIC_PASSWORD environment variables required"
        )

    return RithmicAdapter(
        user=user,
        password=password,
        server_url=server,
        system_name=system_name,
    )
