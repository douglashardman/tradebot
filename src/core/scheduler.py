"""Scheduled tasks for trading operations."""

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Callable, Optional, List, Any
import pytz

logger = logging.getLogger(__name__)

# Eastern timezone
ET = pytz.timezone("America/New_York")


class TradingScheduler:
    """
    Scheduler for trading-related tasks:
    - Auto-flatten before market close
    - Daily digest at 4:00 PM ET
    - Session start/end notifications
    """

    def __init__(
        self,
        flatten_callback: Optional[Callable] = None,
        digest_callback: Optional[Callable] = None,
        flatten_before_close_minutes: int = 5,
        digest_time: time = time(16, 0),  # 4:00 PM
        market_close: time = time(16, 0),  # 4:00 PM
    ):
        """
        Initialize scheduler.

        Args:
            flatten_callback: Async function to call for auto-flatten.
            digest_callback: Async function to call for daily digest.
            flatten_before_close_minutes: Minutes before close to flatten.
            digest_time: Time to send daily digest (ET).
            market_close: Market close time (ET).
        """
        self.flatten_callback = flatten_callback
        self.digest_callback = digest_callback
        self.flatten_before_close_minutes = flatten_before_close_minutes
        self.digest_time = digest_time
        self.market_close = market_close

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._flattened_today = False
        self._digest_sent_today = False

    def _now_et(self) -> datetime:
        """Get current time in ET."""
        return datetime.now(ET)

    def _today_at(self, t: time) -> datetime:
        """Get datetime for today at given time in ET."""
        now = self._now_et()
        return now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)

    def _seconds_until(self, target: datetime) -> float:
        """Seconds until target datetime."""
        now = self._now_et()
        delta = target - now
        return max(0, delta.total_seconds())

    async def _check_flatten(self) -> None:
        """Check if we should auto-flatten."""
        if self._flattened_today:
            return

        now = self._now_et()
        flatten_time = self._today_at(self.market_close) - timedelta(
            minutes=self.flatten_before_close_minutes
        )

        # Check if it's time to flatten (within 1 minute of flatten time)
        if abs((now - flatten_time).total_seconds()) < 60:
            logger.info(f"Auto-flatten triggered at {now.strftime('%H:%M:%S')} ET")
            self._flattened_today = True

            if self.flatten_callback:
                try:
                    if asyncio.iscoroutinefunction(self.flatten_callback):
                        await self.flatten_callback()
                    else:
                        self.flatten_callback()
                except Exception as e:
                    logger.error(f"Error in flatten callback: {e}")

    async def _check_digest(self) -> None:
        """Check if we should send daily digest."""
        if self._digest_sent_today:
            return

        now = self._now_et()
        digest_datetime = self._today_at(self.digest_time)

        # Check if it's time for digest (within 1 minute of digest time)
        if abs((now - digest_datetime).total_seconds()) < 60:
            logger.info(f"Daily digest triggered at {now.strftime('%H:%M:%S')} ET")
            self._digest_sent_today = True

            if self.digest_callback:
                try:
                    if asyncio.iscoroutinefunction(self.digest_callback):
                        await self.digest_callback()
                    else:
                        self.digest_callback()
                except Exception as e:
                    logger.error(f"Error in digest callback: {e}")

    async def _scheduler_loop(self) -> None:
        """Main scheduler loop."""
        logger.info("Trading scheduler started")

        while self._running:
            try:
                # Reset daily flags at midnight
                now = self._now_et()
                if now.hour == 0 and now.minute == 0:
                    self._flattened_today = False
                    self._digest_sent_today = False

                # Check scheduled tasks
                await self._check_flatten()
                await self._check_digest()

                # Sleep for 30 seconds between checks
                await asyncio.sleep(30)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
                await asyncio.sleep(60)

        logger.info("Trading scheduler stopped")

    def start(self) -> None:
        """Start the scheduler."""
        if self._running:
            return

        self._running = True
        self._task = asyncio.create_task(self._scheduler_loop())

    def stop(self) -> None:
        """Stop the scheduler."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    def get_next_events(self) -> List[dict]:
        """Get upcoming scheduled events."""
        now = self._now_et()
        events = []

        # Flatten event
        if not self._flattened_today:
            flatten_time = self._today_at(self.market_close) - timedelta(
                minutes=self.flatten_before_close_minutes
            )
            if flatten_time > now:
                events.append({
                    "event": "auto_flatten",
                    "time": flatten_time.strftime("%H:%M ET"),
                    "seconds_until": self._seconds_until(flatten_time),
                })

        # Digest event
        if not self._digest_sent_today:
            digest_datetime = self._today_at(self.digest_time)
            if digest_datetime > now:
                events.append({
                    "event": "daily_digest",
                    "time": digest_datetime.strftime("%H:%M ET"),
                    "seconds_until": self._seconds_until(digest_datetime),
                })

        return events


# Early close dates for 2024-2025 (market closes at 1:00 PM ET)
EARLY_CLOSE_DATES = {
    # 2024
    "2024-11-29": time(13, 0),  # Day after Thanksgiving
    "2024-12-24": time(13, 0),  # Christmas Eve

    # 2025
    "2025-07-03": time(13, 0),  # Day before Independence Day
    "2025-11-28": time(13, 0),  # Day after Thanksgiving
    "2025-12-24": time(13, 0),  # Christmas Eve
}

# Market holidays (no trading)
MARKET_HOLIDAYS = [
    # 2024
    "2024-11-28",  # Thanksgiving
    "2024-12-25",  # Christmas

    # 2025
    "2025-01-01",  # New Year's Day
    "2025-01-20",  # MLK Day
    "2025-02-17",  # Presidents' Day
    "2025-04-18",  # Good Friday
    "2025-05-26",  # Memorial Day
    "2025-06-19",  # Juneteenth
    "2025-07-04",  # Independence Day
    "2025-09-01",  # Labor Day
    "2025-11-27",  # Thanksgiving
    "2025-12-25",  # Christmas
]


def get_market_close_time(date: Optional[datetime] = None) -> time:
    """
    Get market close time for a given date, accounting for early closes.

    Args:
        date: Date to check (defaults to today).

    Returns:
        Market close time.
    """
    if date is None:
        date = datetime.now(ET)

    date_str = date.strftime("%Y-%m-%d")

    if date_str in EARLY_CLOSE_DATES:
        return EARLY_CLOSE_DATES[date_str]

    return time(16, 0)  # Standard close


def is_market_holiday(date: Optional[datetime] = None) -> bool:
    """
    Check if given date is a market holiday.

    Args:
        date: Date to check (defaults to today).

    Returns:
        True if market is closed.
    """
    if date is None:
        date = datetime.now(ET)

    date_str = date.strftime("%Y-%m-%d")
    return date_str in MARKET_HOLIDAYS


def is_trading_day(date: Optional[datetime] = None) -> bool:
    """
    Check if given date is a trading day.

    Args:
        date: Date to check (defaults to today).

    Returns:
        True if trading is allowed.
    """
    if date is None:
        date = datetime.now(ET)

    # Weekends
    if date.weekday() >= 5:
        return False

    # Holidays
    if is_market_holiday(date):
        return False

    return True
