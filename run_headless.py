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
from typing import List, Optional

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
from src.core.capital import (
    TierManager,
    initialize_tier_manager,
    get_tier_manager,
    TIERS,
)
from src.execution.bridge import ExecutionBridge
from src.data.live_db import (
    get_or_create_session as db_get_or_create_session,
    end_session as db_end_session,
    log_order as db_log_order,
    update_order_filled as db_update_order_filled,
    update_order_rejected as db_update_order_rejected,
    log_trade as db_log_trade,
    update_trade_exit as db_update_trade_exit,
    log_tier_change as db_log_tier_change,
    log_connection_event as db_log_connection,
    log_account_snapshot as db_log_snapshot,
)
from src.data.tick_logger import TickLogger, get_tick_logger
from src.data.bar_db import (
    save_bar as db_save_bar,
    get_recent_bars as db_get_recent_bars,
    save_regime as db_save_regime,
    get_last_regime as db_get_last_regime,
    cleanup_old_bars as db_cleanup_old_bars,
)

# Heartbeat file for watchdog monitoring
HEARTBEAT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "heartbeat.json")

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
        self.tier_manager: Optional[TierManager] = None
        self.execution_bridge: Optional[ExecutionBridge] = None

        # State
        self._running = False
        self._tick_count = 0
        self._session_start_time: Optional[datetime] = None
        self._starting_balance: float = 2500.0  # Starting capital
        self._current_bar_signals: List[Signal] = []  # Track signals per bar for stacking
        self._balance_poll_task: Optional[asyncio.Task] = None

        # Database tracking
        self._db_session_id: Optional[int] = None
        self._trade_count: int = 0
        self._pending_trade_context: dict = {}  # Context for trade being opened
        self._open_trade_ids: dict = {}  # bracket_id -> db trade id
        self._db_order_ids: dict = {}  # bracket_id -> db order id (for live mode)
        self._total_commissions: float = 0.0

        # Heartbeat for watchdog monitoring
        self._last_tick_time: Optional[datetime] = None
        self._feed_connected: bool = False
        self._reconnect_count: int = 0
        self._heartbeat_interval: int = 30  # Write heartbeat every 30 seconds
        self._last_heartbeat_write: Optional[datetime] = None

        # Margin tracking - alert once when high, once when normal
        self._margin_is_high: bool = False
        self._last_margin_check: Optional[datetime] = None
        self._margin_check_interval: int = 60  # Check at most every 60 seconds

        # Tick logging for Parquet storage
        self.tick_logger: Optional[TickLogger] = None

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
            # Exit with code 0 so systemd doesn't restart (this is expected, not a failure)
            sys.exit(0)

        # Initialize tier manager for capital progression
        starting_balance = float(os.getenv("STARTING_BALANCE", "2500"))
        self.tier_manager = initialize_tier_manager(
            starting_balance=starting_balance,
            on_tier_change=lambda change: asyncio.create_task(self._on_tier_change(change)),
        )

        # Start session and get tier-based settings
        tier_config = self.tier_manager.start_session()
        self.symbol = tier_config["instrument"]  # MES or ES based on tier
        self._starting_balance = tier_config["balance"]

        logger.info(f"Tier: {tier_config['tier_name']}")
        logger.info(f"Instrument: {tier_config['instrument']}")
        logger.info(f"Max contracts: {tier_config['max_contracts']}")
        logger.info(f"Loss limit: ${abs(tier_config['daily_loss_limit'])}")
        logger.info(f"Balance: ${tier_config['balance']:,.2f}")

        # Create trading session with tier-based settings
        self.session = TradingSession(
            mode=self.mode,
            symbol=self.symbol,
            daily_profit_target=float(os.getenv("DAILY_PROFIT_TARGET", "500")),
            daily_loss_limit=tier_config["daily_loss_limit"],
            max_position_size=tier_config["max_contracts"],
            stop_loss_ticks=int(os.getenv("STOP_LOSS_TICKS", "16")),
            take_profit_ticks=int(os.getenv("TAKE_PROFIT_TICKS", "24")),
            paper_starting_balance=tier_config["balance"],
        )
        self.session.started_at = datetime.now()
        self._session_start_time = datetime.now()

        # Create execution manager
        self.manager = ExecutionManager(self.session)

        # Wire up trade callbacks for Discord alerts
        self.manager.on_trade(self._on_trade_complete)
        self.manager.on_position(self._on_position_opened)

        # Get or create database session for logging (handles restarts gracefully)
        today = datetime.now().strftime("%Y-%m-%d")
        self._db_session_id = db_get_or_create_session(
            date=today,
            mode=self.mode,
            symbol=self.symbol,
            tier_index=tier_config.get("tier_index", 0),
            tier_name=tier_config.get("tier_name"),
            starting_balance=tier_config["balance"],
            max_position_size=tier_config["max_contracts"],
            stop_loss_ticks=self.session.stop_loss_ticks,
            take_profit_ticks=self.session.take_profit_ticks,
            daily_loss_limit=tier_config["daily_loss_limit"],
        )
        logger.info(f"Using database session #{self._db_session_id}")

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

        # Initialize tick logger for Parquet storage
        self.tick_logger = get_tick_logger()
        logger.info("Tick logger initialized")

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
            account_id = os.getenv("RITHMIC_ACCOUNT_ID")

            if not user or not password:
                logger.error("RITHMIC_USER and RITHMIC_PASSWORD required")
                await self.notifications.alert_system_error(
                    "Rithmic credentials missing",
                    "Set RITHMIC_USER and RITHMIC_PASSWORD environment variables",
                )
                return False

            # For live mode, account_id is required
            if self.mode == "live" and not account_id:
                logger.error("RITHMIC_ACCOUNT_ID required for live trading")
                await self.notifications.alert_system_error(
                    "Rithmic account ID missing",
                    "Set RITHMIC_ACCOUNT_ID for live trading",
                )
                return False

            self.data_adapter = RithmicAdapter(
                user=user,
                password=password,
                system_name=os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Test"),
                server_url=os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443"),
                account_id=account_id,
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

            # Mark feed as connected for heartbeat
            self._feed_connected = True

            # Create execution bridge for live mode
            if self.mode == "live" and self.manager:
                self.execution_bridge = ExecutionBridge(
                    execution_manager=self.manager,
                    rithmic_adapter=self.data_adapter,
                    on_fill_callback=self._on_live_fill,
                    on_rejection_callback=self._on_live_rejection,
                )

                # Reconcile positions on startup
                reconcile_result = await self.execution_bridge.reconcile_on_startup()
                if not reconcile_result.get("reconciled"):
                    # Position mismatch - session already halted by bridge
                    await self.notifications.alert_system_error(
                        "Position mismatch on startup",
                        reconcile_result.get("action_required", "Manual review required"),
                    )
                    return False

                logger.info("Execution bridge initialized for live trading")

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

    async def warmup_historical(self, min_bars: int = 30) -> bool:
        """
        Warm up the router with recent bar history.

        Data sources (in order of preference):
        1. Persisted bars from SQLite (instant, FREE)
        2. Local Parquet tick cache (fast, FREE)
        3. Databento historical API (slow, PAID ~$2/day)

        Args:
            min_bars: Minimum number of bars needed for regime detection (default 30)

        Returns:
            True if warmup successful, False otherwise
        """
        logger.info(f"Starting warmup (need {min_bars}+ bars for regime detection)...")

        # Clean up old bars (keep 7 days)
        try:
            deleted = db_cleanup_old_bars(days=7)
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old bars from database")
        except Exception as e:
            logger.warning(f"Bar cleanup failed: {e}")

        # OPTION 1: Load persisted bars from SQLite (INSTANT, FREE)
        bars = db_get_recent_bars(self.symbol, limit=min_bars + 20)
        source = "persisted bars"

        if len(bars) >= min_bars:
            logger.info(f"Found {len(bars)} persisted bars - using for warmup")
        else:
            # OPTION 2: Load from local Parquet cache (FREE)
            logger.info(f"Only {len(bars)} persisted bars, trying Parquet cache...")
            now_et = datetime.now(ET)
            start_time = now_et - timedelta(hours=3)
            ticks = self._load_local_ticks(start_time, now_et)

            if ticks:
                bars = self._ticks_to_bars(ticks)
                source = "Parquet cache"
                logger.info(f"Built {len(bars)} bars from {len(ticks):,} cached ticks")

        if len(bars) < min_bars:
            # OPTION 3: Databento historical API (PAID)
            api_key = os.getenv("DATABENTO_API_KEY")
            if api_key:
                logger.info(f"Only {len(bars)} bars, fetching from Databento...")
                now_et = datetime.now(ET)
                start_time = now_et - timedelta(hours=3)
                ticks = self._fetch_databento_ticks(api_key, start_time, now_et)
                if ticks:
                    bars = self._ticks_to_bars(ticks)
                    source = "Databento API"
                    logger.info(f"Built {len(bars)} bars from Databento")

        # Feed bars to router
        warmup_bars = 0
        for bar in bars:
            if self.router:
                self.router.on_bar(bar)
                warmup_bars += 1

        # If we still don't have enough bars, try to restore last known regime
        if warmup_bars < min_bars:
            last_regime, last_confidence = db_get_last_regime(self.symbol)
            if last_regime and self.router:
                from src.core.types import Regime
                try:
                    self.router.current_regime = Regime(last_regime)
                    self.router.regime_confidence = last_confidence
                    logger.info(f"Restored last regime: {last_regime} ({last_confidence:.0%})")
                    source = f"restored ({source})"
                except ValueError:
                    pass

        # Log results
        regime = self.router.current_regime.value if self.router else "N/A"
        confidence = self.router.regime_confidence if self.router else 0

        logger.info(
            f"Warmup complete: {warmup_bars} bars from {source} | "
            f"Regime: {regime} ({confidence:.0%} confidence)"
        )

        # Send Discord notification
        await self.notifications.send_alert(
            title="System Warmed Up",
            message=(
                f"Loaded {warmup_bars} bars from {source}\n"
                f"Starting regime: **{regime}** ({confidence:.0%})"
            ),
            alert_type=AlertType.INFO,
        )

        return True

    def _ticks_to_bars(self, ticks: List) -> List:
        """Convert ticks to bars using a temporary aggregator."""
        from src.data.aggregator import FootprintAggregator

        aggregator = FootprintAggregator(self.timeframe)
        bars = []

        for tick in ticks:
            if tick.symbol == self.symbol or tick.symbol.startswith(self.symbol):
                completed_bar = aggregator.process_tick(tick)
                if completed_bar:
                    bars.append(completed_bar)

        return bars

    def _load_local_ticks(self, start_time: datetime, end_time: datetime) -> List:
        """
        Load ticks from local Parquet cache.

        Returns ticks within the time range from today's and yesterday's files.
        """
        from src.data.tick_logger import TickLogger

        ticks = []
        tick_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ticks")

        if not os.path.exists(tick_dir):
            return ticks

        # Check today's and yesterday's files (warmup may span midnight)
        dates_to_check = [
            start_time.strftime("%Y-%m-%d"),
            end_time.strftime("%Y-%m-%d"),
        ]

        for date_str in set(dates_to_check):
            parquet_file = os.path.join(tick_dir, f"{date_str}.parquet")
            if os.path.exists(parquet_file):
                try:
                    file_ticks = TickLogger.load_parquet(parquet_file)
                    # Filter to time range
                    for tick in file_ticks:
                        # Make tick timestamp timezone-aware if needed
                        tick_ts = tick.timestamp
                        if tick_ts.tzinfo is None:
                            tick_ts = tick_ts.replace(tzinfo=start_time.tzinfo)
                        if start_time <= tick_ts <= end_time:
                            ticks.append(tick)
                    logger.info(f"Loaded {len(file_ticks):,} ticks from {parquet_file}")
                except Exception as e:
                    logger.warning(f"Failed to load {parquet_file}: {e}")

        # Sort by timestamp
        ticks.sort(key=lambda t: t.timestamp)
        return ticks

    def _fetch_databento_ticks(self, api_key: str, start_time: datetime, end_time: datetime) -> List:
        """
        Fetch ticks from Databento historical API (paid).
        """
        try:
            from src.data.adapters.databento import DatabentoAdapter

            # Format for Databento API
            start_str = start_time.strftime("%Y-%m-%dT%H:%M:%S-05:00")
            end_str = end_time.strftime("%Y-%m-%dT%H:%M:%S-05:00")

            adapter = DatabentoAdapter(api_key=api_key)
            contract = DatabentoAdapter.get_front_month_contract(self.symbol)

            logger.info(f"Fetching {contract} from Databento ({start_time.strftime('%H:%M')} to {end_time.strftime('%H:%M')} ET)")

            ticks = adapter.get_historical(
                symbol=contract,
                start=start_str,
                end=end_str,
            )
            return ticks or []

        except Exception as e:
            logger.error(f"Databento fetch failed: {e}")
            return []

    async def _connect_databento(self) -> bool:
        """Connect to Databento live data feed."""
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

            # Register connection callbacks
            self.data_adapter.on_connected(self._on_feed_connected)
            self.data_adapter.on_disconnected(self._on_feed_disconnected)
            self.data_adapter.register_callback(self._process_tick)

            # Subscribe to both ES and MES for multi-tenant support
            # Ticks will include the symbol so the system can filter as needed
            symbols = ["ES", "MES"]
            logger.info(f"Starting Databento live feed for {symbols}")

            # Use async version for proper integration
            await self.data_adapter.start_live_async(symbols)

            # Mark feed as connected for heartbeat
            self._feed_connected = True

            logger.info(f"Databento live feed active for {self.symbol}")
            return True

        except Exception as e:
            logger.error(f"Databento connection error: {e}")
            await self.notifications.alert_system_error(
                "Databento connection error",
                str(e),
            )
            return False

    async def _check_margin_requirements(self) -> bool:
        """
        Check if margin requirements are within acceptable limits.

        Called before each trade. High margin periods (FOMC, CPI, etc.) can
        spike margins temporarily. We skip trading during those periods but
        keep running - margins usually normalize within hours.

        Returns:
            True if margins are acceptable, False if too high.
        """
        # Rate limit margin checks to avoid hammering the broker
        now = datetime.now()
        if self._last_margin_check:
            elapsed = (now - self._last_margin_check).total_seconds()
            if elapsed < self._margin_check_interval:
                # Use cached state
                return not self._margin_is_high
        self._last_margin_check = now

        # Margin thresholds - don't trade if above these
        MES_LIMIT = float(os.getenv("MES_MARGIN_LIMIT", "50"))
        ES_LIMIT = float(os.getenv("ES_MARGIN_LIMIT", "500"))

        if not self.data_adapter:
            return True

        # Get the base symbol
        base_symbol = "MES" if "MES" in self.symbol else "ES"
        margin_limit = MES_LIMIT if base_symbol == "MES" else ES_LIMIT

        try:
            current_margin = await self.data_adapter.get_margin_requirement(self.symbol, "CME")

            if current_margin is None:
                # Can't query - use cached state or allow trade
                return not self._margin_is_high

            margin_high = current_margin > margin_limit

            # State transition: normal -> high
            if margin_high and not self._margin_is_high:
                self._margin_is_high = True
                logger.warning(f"Margins elevated: ${current_margin:.2f} > ${margin_limit:.2f} limit")
                await self.notifications.send_alert(
                    title="⚠️ High Margins - Pausing Trades",
                    message=(
                        f"**Symbol:** {self.symbol}\n"
                        f"**Current Margin:** ${current_margin:.2f}\n"
                        f"**Normal Limit:** ${margin_limit:.2f}\n\n"
                        f"Skipping new trades until margins normalize.\n"
                        f"Existing positions are unaffected."
                    ),
                    alert_type=AlertType.WARNING,
                )
                return False

            # State transition: high -> normal
            if not margin_high and self._margin_is_high:
                self._margin_is_high = False
                logger.info(f"Margins normalized: ${current_margin:.2f} <= ${margin_limit:.2f}")
                await self.notifications.send_alert(
                    title="✅ Margins Normalized - Resuming Trades",
                    message=(
                        f"**Symbol:** {self.symbol}\n"
                        f"**Current Margin:** ${current_margin:.2f}\n\n"
                        f"Margins are back to normal. Trading resumed."
                    ),
                    alert_type=AlertType.SUCCESS,
                )
                return True

            return not self._margin_is_high

        except Exception as e:
            logger.warning(f"Margin check error: {e}")
            return not self._margin_is_high  # Use cached state on error

    async def run(self) -> None:
        """Main run loop."""
        # Wait for market open
        await self._wait_for_market_open()

        # Send startup notification with tier info
        tier_info = self.tier_manager.get_status() if self.tier_manager else None
        tier_msg = ""
        if tier_info:
            tier_msg = (
                f"**Tier:** {tier_info['tier_name']}\n"
                f"**Balance:** ${tier_info['balance']:,.2f}\n"
                f"**Win Streak:** {tier_info['win_streak']} | **Loss Streak:** {tier_info['loss_streak']}\n\n"
            )

        await self.notifications.send_alert(
            title="Trading Session Started",
            message=(
                f"{tier_msg}"
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

        # Start balance polling if using Rithmic
        if os.getenv("USE_RITHMIC", "true").lower() == "true":
            self._balance_poll_task = asyncio.create_task(self._poll_balance_loop())

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

                # Write heartbeat even if no ticks (for watchdog monitoring)
                self._write_heartbeat()

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

        # Cancel balance polling
        if self._balance_poll_task:
            self._balance_poll_task.cancel()
            try:
                await self._balance_poll_task
            except asyncio.CancelledError:
                pass

        # Stop scheduler
        if self.scheduler:
            self.scheduler.stop()

        # Flatten any open positions
        if self.manager and self.manager.open_positions:
            await self._auto_flatten()

        # End tier session and save state
        if self.tier_manager and self.manager:
            session_result = self.tier_manager.end_session(self.manager.daily_pnl)
            if session_result.get("tier_changed"):
                logger.info(
                    f"Session ended with tier change to {session_result['new_tier']}"
                )

        # End database session
        if self._db_session_id and self.manager:
            stats = self.manager.get_statistics()
            state = self.manager.get_state()
            ending_balance = self.tier_manager.state.balance if self.tier_manager else self._starting_balance + self.manager.daily_pnl

            status = "COMPLETED"
            halted_reason = None
            if self.manager.is_halted:
                status = "HALTED"
                halted_reason = self.manager.halt_reason

            db_end_session(
                session_id=self._db_session_id,
                ending_balance=ending_balance,
                session_pnl=self.manager.daily_pnl,
                total_trades=stats.get("total_trades", 0),
                wins=state.get("win_count", 0),
                losses=state.get("loss_count", 0),
                commissions=self._total_commissions,
                status=status,
                halted_reason=halted_reason,
            )
            logger.info(f"Ended database session #{self._db_session_id}")

        # Send daily digest
        await self._send_daily_digest()

        # Disconnect data feed
        if self.data_adapter:
            if hasattr(self.data_adapter, 'disconnect'):
                await self.data_adapter.disconnect()
            elif hasattr(self.data_adapter, 'stop_live_async'):
                await self.data_adapter.stop_live_async()
            elif hasattr(self.data_adapter, 'stop_live'):
                self.data_adapter.stop_live()

        # Close notifications
        if self.notifications:
            await self.notifications.close()

        # Flush tick data to Parquet before shutdown
        if self.tick_logger:
            logger.info("Flushing tick data to Parquet...")
            paths = self.tick_logger.flush_all()
            if paths:
                logger.info(f"Saved tick data to: {', '.join(paths)}")

        # Clear persistence (clean shutdown)
        if self.persistence:
            self.persistence.clear_state()

        logger.info("Shutdown complete")

    # === Callbacks ===

    def _process_tick(self, tick) -> None:
        """Process incoming tick."""
        # Filter to only process ticks for our configured symbol
        # (Databento streams both ES and MES, we only want one)
        if hasattr(tick, 'symbol') and tick.symbol != self.symbol:
            return

        self._tick_count += 1
        self._last_tick_time = datetime.now()

        # Log tick to Parquet storage
        if self.tick_logger:
            self.tick_logger.log_tick(tick)

        if self.engine:
            self.engine.process_tick(tick)

        # Save state periodically
        if self._tick_count % 10000 == 0:
            self._save_state()

        # Write heartbeat for watchdog monitoring
        self._write_heartbeat()

    def _on_bar(self, bar: FootprintBar) -> None:
        """Handle completed bar."""
        # Reset stacked signals for new bar
        self._current_bar_signals = []

        # Log bar completion for visibility
        logger.info(
            f"Bar complete: {bar.start_time.strftime('%H:%M')} | "
            f"O:{bar.open_price:.2f} H:{bar.high_price:.2f} L:{bar.low_price:.2f} C:{bar.close_price:.2f} | "
            f"Vol:{bar.total_volume:,} Delta:{bar.delta:+,} | "
            f"Levels:{len(bar.levels)}"
        )

        # Persist bar to SQLite for warmup on restart
        try:
            db_save_bar(bar)
        except Exception as e:
            logger.warning(f"Failed to persist bar: {e}")

        if self.router:
            self.router.on_bar(bar)

            # Persist regime state after each bar (for quick restore on restart)
            try:
                db_save_regime(
                    self.symbol,
                    self.router.current_regime.value,
                    self.router.regime_confidence
                )
            except Exception as e:
                logger.warning(f"Failed to persist regime: {e}")

        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal from engine."""
        # Log all signals detected (before routing/filtering)
        logger.info(
            f"Signal detected: {signal.pattern} | {signal.direction} @ {signal.price:.2f} | "
            f"Strength:{getattr(signal, 'strength', 'N/A')}"
        )

        if not self.router or not self.manager:
            return

        # Track signal for stacking detection
        self._current_bar_signals.append(signal)

        # Evaluate through router
        signal = self.router.evaluate_signal(signal)

        # Log routing decision
        if signal.approved:
            logger.info(f"Signal APPROVED: {signal.pattern} -> ready to execute")
        else:
            logger.debug(f"Signal REJECTED: {signal.pattern} - {signal.rejection_reason}")

        if signal.approved and not self.dry_run:
            # Check margin requirements before trading (async check)
            asyncio.create_task(self._execute_signal_with_margin_check(signal))

    async def _execute_signal_with_margin_check(self, signal: Signal) -> None:
        """Execute signal after checking margin requirements."""
        # Check margins (rate-limited, alerts on state change)
        if not await self._check_margin_requirements():
            logger.info(f"Signal skipped due to high margins: {signal.pattern}")
            return

        if not self.router or not self.manager:
            return

        # Count stacked signals (same direction signals in current bar)
        stacked_count = sum(
            1 for s in self._current_bar_signals
            if s.direction == signal.direction
        )

        # Use tier manager for position sizing (combined logic)
        current_regime = self.router.current_regime if self.router else "UNKNOWN"
        if self.tier_manager:
            position_size = self.tier_manager.get_position_size(
                regime=current_regime,
                stacked_count=stacked_count,
                use_streaks=True,
            )
        else:
            # Fallback to router multiplier if no tier manager
            position_size = int(self.router.get_position_size_multiplier())

        # Capture context BEFORE executing (for database logging)
        self._pending_trade_context = {
            "pattern": signal.pattern,
            "signal_strength": getattr(signal, "strength", 0),
            "regime": current_regime,
            "regime_score": getattr(self.router, "regime_score", None),
            "stacked_count": stacked_count,
            "tier_index": self.tier_manager.state.tier_index if self.tier_manager else 0,
            "tier_name": self.tier_manager.state.tier_name if self.tier_manager else None,
            "instrument": self.tier_manager.state.instrument if self.tier_manager else self.symbol,
            "win_streak": self.tier_manager.state.win_streak if self.tier_manager else 0,
            "loss_streak": self.tier_manager.state.loss_streak if self.tier_manager else 0,
        }

        order = self.manager.on_signal(signal, absolute_size=position_size)

        if order:
            logger.info(
                f"Order: {order.side} {order.size} @ {order.entry_price} "
                f"(stacked={stacked_count}, regime={current_regime})"
            )

            # In live mode, submit the order through the execution bridge
            if self.mode == "live" and self.execution_bridge:
                # Log order to database before submission
                if self._db_session_id:
                    db_order_id = db_log_order(
                        session_id=self._db_session_id,
                        internal_order_id=order.bracket_id,
                        symbol=order.symbol,
                        side=order.side,
                        order_type="BRACKET",
                        size=order.size,
                        bracket_id=order.bracket_id,
                        expected_price=order.entry_price,
                        stop_price=order.stop_price,
                    )
                    # Track DB order ID for fill/rejection updates
                    self._db_order_ids[order.bracket_id] = db_order_id

                # Remove from pending_orders (we're submitting directly)
                if order in self.manager.pending_orders:
                    self.manager.pending_orders.remove(order)

                # Submit to broker asynchronously with retry logic and tracking
                # Uses _submit_and_track to record success/failure in bridge
                task = asyncio.create_task(
                    self.execution_bridge._submit_and_track(order)
                )
                self.execution_bridge._submission_tasks[order.bracket_id] = task

    def _on_trade_complete(self, trade) -> None:
        """Handle completed trade - send Discord alert and update tier manager."""
        # Record trade with tier manager for balance tracking and tier progression
        if self.tier_manager:
            self.tier_manager.record_trade(trade.pnl)

        # Calculate commission (round-trip estimate)
        # Typical futures commission: ~$2.25-$4.50 per side per contract
        commission_per_contract = float(os.getenv("COMMISSION_PER_CONTRACT", "4.50"))
        commission = commission_per_contract * trade.size * 2  # Round trip
        self._total_commissions += commission

        # Update trade exit in database
        bracket_id = getattr(trade, 'bracket_id', None) or trade.trade_id
        db_trade_id = self._open_trade_ids.pop(bracket_id, None)

        if db_trade_id and self._db_session_id:
            balance_after = self.tier_manager.state.balance if self.tier_manager else None
            running_pnl = self.manager.daily_pnl if self.manager else 0

            db_update_trade_exit(
                trade_id=db_trade_id,
                exit_price=trade.exit_price,
                exit_time=trade.exit_time,
                exit_reason=trade.exit_reason,
                pnl_gross=trade.pnl,
                pnl_ticks=trade.pnl_ticks,
                commission=commission,
                running_pnl=running_pnl,
                account_balance=balance_after,
            )
            logger.debug(f"Updated trade #{db_trade_id} with exit: {trade.exit_reason}, P&L: ${trade.pnl:+,.2f}")

        asyncio.create_task(self._alert_trade_closed(trade))
        self._save_state()

    def _on_position_opened(self, position) -> None:
        """Handle new position - send Discord alert and log to database."""
        # Get context captured at signal time
        ctx = self._pending_trade_context or {}
        self._trade_count += 1

        # Log trade entry to database
        if self._db_session_id:
            trade_id = db_log_trade(
                session_id=self._db_session_id,
                trade_num=self._trade_count,
                internal_trade_id=position.position_id,
                symbol=position.symbol,
                direction=position.side,
                size=position.size,
                entry_price=position.entry_price,
                entry_time=position.entry_time,
                bracket_id=position.bracket_id,
                stop_price=position.stop_price,
                target_price=position.target_price,
                pattern=ctx.get("pattern"),
                signal_strength=ctx.get("signal_strength"),
                regime=ctx.get("regime"),
                regime_score=ctx.get("regime_score"),
                tier_index=ctx.get("tier_index"),
                tier_name=ctx.get("tier_name"),
                instrument=ctx.get("instrument"),
                stacked_count=ctx.get("stacked_count", 1),
                win_streak=ctx.get("win_streak", 0),
                loss_streak=ctx.get("loss_streak", 0),
            )
            # Track for later exit update
            if position.bracket_id:
                self._open_trade_ids[position.bracket_id] = trade_id
            logger.debug(f"Logged trade entry #{trade_id} to database")

        # Clear pending context
        self._pending_trade_context = {}

        asyncio.create_task(self._alert_position_opened(position))
        self._save_state()

    async def _on_feed_connected(self, plant_type: str = "") -> None:
        """Handle data feed connection."""
        logger.info(f"Data feed connected: {plant_type}")

        # Track connection state for heartbeat
        if self._feed_connected:
            # This is a reconnection
            self._reconnect_count += 1
        self._feed_connected = True

        # Log to database
        if self._db_session_id:
            db_log_connection(
                session_id=self._db_session_id,
                event_type="CONNECTED",
                plant_type=plant_type,
            )

        await self.notifications.alert_connection_restored(plant_type)

    async def _on_feed_disconnected(self, plant_type: str = "") -> None:
        """Handle data feed disconnection."""
        logger.warning(f"Data feed disconnected: {plant_type}")

        # Track connection state for heartbeat
        self._feed_connected = False

        # Log to database
        if self._db_session_id:
            db_log_connection(
                session_id=self._db_session_id,
                event_type="DISCONNECTED",
                plant_type=plant_type,
            )

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

    async def _on_live_fill(self, fill_data: dict) -> None:
        """Handle fill notification from live trading."""
        rithmic_order_id = fill_data.get("order_id")
        fill_price = fill_data.get("fill_price", 0)
        fill_qty = fill_data.get("fill_qty", 0)
        live_order = fill_data.get("order")

        logger.info(f"Live fill: {rithmic_order_id} - {fill_qty} @ {fill_price}")

        # Get our bracket_id to find the DB order ID
        bracket_id = live_order.bracket_id if live_order else None
        db_order_id = self._db_order_ids.get(bracket_id) if bracket_id else None

        # Log to database using the correct DB order ID
        if self._db_session_id and db_order_id:
            db_update_order_filled(
                order_id=db_order_id,
                filled_size=fill_qty,
                avg_fill_price=fill_price,
            )
            # Clean up tracking after fill
            if bracket_id:
                self._db_order_ids.pop(bracket_id, None)

    async def _on_live_rejection(self, rejection_data: dict) -> None:
        """Handle order rejection from live trading."""
        rithmic_order_id = rejection_data.get("order_id")
        reason = rejection_data.get("reason", "Unknown")
        live_order = rejection_data.get("order")

        logger.error(f"Live order rejected: {rithmic_order_id} - {reason}")

        # Get our bracket_id to find the DB order ID
        bracket_id = live_order.bracket_id if live_order else None
        db_order_id = self._db_order_ids.get(bracket_id) if bracket_id else None

        # Log to database using the correct DB order ID
        if self._db_session_id and db_order_id:
            db_update_order_rejected(
                order_id=db_order_id,
                reject_reason=reason,
            )
            # Clean up tracking after rejection
            if bracket_id:
                self._db_order_ids.pop(bracket_id, None)

        # Alert via Discord
        await self.notifications.send_alert(
            title="Order Rejected",
            message=(
                f"**Order ID:** {rithmic_order_id}\n"
                f"**Reason:** {reason}\n\n"
                "Manual intervention may be required."
            ),
            alert_type=AlertType.ERROR,
        )

    async def _on_tier_change(self, change: dict) -> None:
        """Handle tier change - send Discord notification, update session, and log to DB."""
        old_tier = TIERS[change["from_tier"]]
        new_tier = TIERS[change["to_tier"]]

        direction = "UP" if change["to_tier"] > change["from_tier"] else "DOWN"
        emoji = "🎉" if direction == "UP" else "⚠️"

        old_symbol = self.symbol
        new_instrument = new_tier["instrument"]

        # Extract contract month from old symbol (e.g., "MESH25" -> "H25", "ESZ25" -> "Z25")
        # Then construct full symbol for new instrument
        contract_month = ""
        if len(old_symbol) > 3 and old_symbol[:3] in ("MES", "MNQ"):
            contract_month = old_symbol[3:]  # e.g., "MESH25" -> "H25"
        elif len(old_symbol) > 2 and old_symbol[:2] in ("ES", "NQ"):
            contract_month = old_symbol[2:]  # e.g., "ESH25" -> "H25"

        # Construct new symbol with contract month
        if contract_month:
            new_symbol = f"{new_instrument}{contract_month}"
        else:
            new_symbol = new_instrument  # Fallback to generic if no month found
            logger.warning(
                f"Could not extract contract month from {old_symbol}, "
                f"using generic symbol: {new_symbol}"
            )

        # Log tier change to database
        if self._db_session_id:
            db_log_tier_change(
                session_id=self._db_session_id,
                from_tier_index=change["from_tier"],
                from_tier_name=old_tier["name"],
                to_tier_index=change["to_tier"],
                to_tier_name=new_tier["name"],
                from_instrument=change["from_instrument"],
                to_instrument=change["to_instrument"],
                balance_at_change=change["balance"],
                trigger_reason=direction,
            )

        # Update session settings for new tier
        if self.session:
            self.session.symbol = new_symbol
            self.session.daily_loss_limit = new_tier["daily_loss_limit"]
            self.session.max_position_size = new_tier["max_contracts"]
            self.symbol = new_symbol

        # Update execution manager (recalculates tick values)
        if self.manager:
            self.manager.update_symbol(new_symbol)

        # Update engine symbol
        if self.engine:
            self.engine.config["symbol"] = new_symbol

        # Switch data feed if instrument changed (MES <-> ES)
        if old_symbol != new_symbol and self.data_adapter:
            logger.info(f"Switching data feed: {old_symbol} -> {new_symbol}")
            try:
                # Unsubscribe from old symbol
                if hasattr(self.data_adapter, 'client') and self.data_adapter.client:
                    from async_rithmic import DataType
                    await self.data_adapter.client.unsubscribe_from_market_data(
                        self.data_adapter._current_symbol,
                        self.data_adapter._current_exchange or "CME",
                        DataType.LAST_TRADE,
                    )
                # Subscribe to new symbol
                await self.data_adapter.subscribe(new_symbol, "CME")
                logger.info(f"Data feed switched to {new_symbol}")
            except Exception as e:
                logger.error(f"Failed to switch data feed: {e}")
                # Alert but continue - positions still managed

        logger.info(
            f"TIER CHANGE {direction}: {old_tier['name']} -> {new_tier['name']} "
            f"({change['from_instrument']} -> {change['to_instrument']})"
        )

        # Send Discord notification
        await self.notifications.send_alert(
            title=f"{emoji} Tier Change: {direction}!",
            message=(
                f"**{old_tier['name']}** -> **{new_tier['name']}**\n\n"
                f"**Balance:** ${change['balance']:,.2f}\n"
                f"**Instrument:** {change['from_instrument']} -> {change['to_instrument']}\n"
                f"**Max Contracts:** {old_tier['max_contracts']} -> {new_tier['max_contracts']}\n"
                f"**Loss Limit:** ${abs(old_tier['daily_loss_limit'])} -> ${abs(new_tier['daily_loss_limit'])}"
            ),
            alert_type=AlertType.SUCCESS if direction == "UP" else AlertType.WARNING,
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

        num_positions = len(self.manager.open_positions)

        # In live mode, use the execution bridge to flatten
        if self.mode == "live" and self.execution_bridge:
            flatten_result = await self.execution_bridge.flatten_all()
            if flatten_result.get("success"):
                verified_msg = ""
                if flatten_result.get("verified"):
                    verified_msg = "\n**Status:** Verified - all positions closed"
                elif flatten_result.get("broker_positions") is not None:
                    verified_msg = f"\n**Warning:** {flatten_result['broker_positions']} position(s) may still be open"
                await self.notifications.send_alert(
                    title="Auto-Flatten Initiated",
                    message=(
                        f"Sent flatten request for {num_positions} position(s) before market close.\n"
                        f"**Current Daily P&L:** ${self.manager.daily_pnl:+,.2f}"
                        f"{verified_msg}"
                    ),
                    alert_type=AlertType.INFO,
                )
            else:
                await self.notifications.send_alert(
                    title="Auto-Flatten FAILED",
                    message=(
                        f"Failed to send flatten request!\n"
                        f"**Open Positions:** {num_positions}\n\n"
                        "MANUAL INTERVENTION REQUIRED"
                    ),
                    alert_type=AlertType.ERROR,
                )
            return

        # Paper mode: simulate flatten locally
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

        # Balance - use tier manager if available
        if self.tier_manager:
            ending_balance = self.tier_manager.state.balance
            tier_info = self.tier_manager.get_status()
        else:
            ending_balance = getattr(
                self.manager, 'paper_balance',
                self._starting_balance + self.manager.daily_pnl
            )
            tier_info = None

        # Status
        status = "COMPLETED"
        if self.manager.is_halted:
            status = f"STOPPED EARLY ({self.manager.halt_reason})"

        # Add tier status to status string
        if tier_info:
            status += f" | {tier_info['tier_name']}"

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

    # === Balance Polling ===

    async def _poll_balance_loop(self) -> None:
        """Periodically poll Rithmic for account balance and update tier manager."""
        poll_interval = int(os.getenv("BALANCE_POLL_INTERVAL", "60"))  # Default: 60 seconds
        logger.info(f"Starting balance polling (interval: {poll_interval}s)")

        while self._running:
            try:
                await asyncio.sleep(poll_interval)

                if not self._running:
                    break

                # Try to get balance from Rithmic
                if self.data_adapter and hasattr(self.data_adapter, 'get_account_balance'):
                    balance = await self.data_adapter.get_account_balance()

                    if balance is not None:
                        # Log account snapshot to database
                        if self._db_session_id:
                            open_positions = len(self.manager.open_positions) if self.manager else 0
                            open_size = sum(p.size for p in self.manager.open_positions) if self.manager else 0
                            unrealized = sum(p.unrealized_pnl for p in self.manager.open_positions) if self.manager else 0
                            realized = self.manager.daily_pnl if self.manager else 0

                            db_log_snapshot(
                                session_id=self._db_session_id,
                                account_balance=balance,
                                unrealized_pnl=unrealized,
                                realized_pnl=realized,
                                open_position_count=open_positions,
                                open_position_size=open_size,
                            )

                        # Update tier manager if balance changed
                        if self.tier_manager:
                            old_balance = self.tier_manager.state.balance
                            if abs(balance - old_balance) > 0.01:
                                logger.info(
                                    f"Balance sync from Rithmic: ${old_balance:,.2f} -> ${balance:,.2f}"
                                )
                                self.tier_manager.set_balance(balance)

            except asyncio.CancelledError:
                logger.debug("Balance polling cancelled")
                break
            except Exception as e:
                logger.warning(f"Balance poll error: {e}")
                # Continue polling despite errors
                await asyncio.sleep(poll_interval)

    # === State Management ===

    def _save_state(self) -> None:
        """Save current state for crash recovery."""
        if not self.persistence or not self.manager:
            return

        from src.core.persistence import serialize_positions, serialize_trades

        # Get tier status for persistence
        tier_status = None
        if self.tier_manager:
            tier_status = self.tier_manager.get_status()

        state = {
            "daily_pnl": self.manager.daily_pnl,
            "is_halted": self.manager.is_halted,
            "halt_reason": self.manager.halt_reason,
            "positions": serialize_positions(self.manager.open_positions),
            "trades": serialize_trades(self.manager.completed_trades),
            "tick_count": self._tick_count,
            "paper_balance": getattr(self.manager, 'paper_balance', None),
            "tier_status": tier_status,
        }

        self.persistence.save_state(state)

    def _write_heartbeat(self) -> None:
        """Write heartbeat file for watchdog monitoring."""
        now = datetime.now()

        # Only write every heartbeat_interval seconds
        if self._last_heartbeat_write:
            elapsed = (now - self._last_heartbeat_write).total_seconds()
            if elapsed < self._heartbeat_interval:
                return

        try:
            # Ensure data directory exists
            os.makedirs(os.path.dirname(HEARTBEAT_FILE), exist_ok=True)

            # Gather status info
            daily_pnl = self.manager.daily_pnl if self.manager else 0
            trade_count = len(self.manager.completed_trades) if self.manager else 0
            open_positions = len(self.manager.open_positions) if self.manager else 0
            is_halted = self.manager.is_halted if self.manager else False
            halt_reason = self.manager.halt_reason if self.manager else None

            tier_name = None
            balance = 0
            if self.tier_manager:
                tier_name = self.tier_manager.state.tier_name
                balance = self.tier_manager.state.balance

            # Get engine stats if available
            bar_count = self.engine.bar_count if self.engine else 0
            signal_count = self.engine.signal_count if self.engine else 0

            heartbeat_data = {
                "timestamp": now.isoformat(),
                "last_tick_time": self._last_tick_time.isoformat() if self._last_tick_time else None,
                "tick_count": self._tick_count,
                "bar_count": bar_count,
                "signal_count": signal_count,
                "feed_connected": self._feed_connected,
                "reconnect_count": self._reconnect_count,
                "daily_pnl": daily_pnl,
                "trade_count": trade_count,
                "open_positions": open_positions,
                "is_halted": is_halted,
                "halt_reason": halt_reason,
                "tier_name": tier_name,
                "balance": balance,
                "mode": self.mode,
                "symbol": self.symbol,
            }

            # Write atomically
            import json
            temp_file = HEARTBEAT_FILE + ".tmp"
            with open(temp_file, "w") as f:
                json.dump(heartbeat_data, f, indent=2)
            os.replace(temp_file, HEARTBEAT_FILE)

            self._last_heartbeat_write = now

        except Exception as e:
            logger.warning(f"Failed to write heartbeat: {e}")


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

    # Warm up with historical bars (for regime detection)
    # Uses: 1) persisted bars (instant), 2) Parquet cache, 3) Databento API
    min_warmup_bars = int(os.getenv("WARMUP_MIN_BARS", "30"))
    if min_warmup_bars > 0:
        await system.warmup_historical(min_bars=min_warmup_bars)

    # Connect to data feed
    if not await system.connect_data_feed():
        logger.error("Data feed connection failed")
        sys.exit(1)

    # Run
    await system.run()


if __name__ == "__main__":
    asyncio.run(main())
