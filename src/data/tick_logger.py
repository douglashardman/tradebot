"""
Tick Data Logger - Stores live tick data in Parquet format for backtesting.

Design:
- Accumulates ticks in memory during the trading day
- Trading day rolls at 5 PM ET (during daily futures halt 5-6 PM ET)
- Files organized by trading date: data/ticks/YYYY-MM-DD.parquet
- A file contains ticks from previous day 5 PM ET to current day 5 PM ET
- SCP cron job exports to remote server at 5:01 PM ET nightly
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq
from zoneinfo import ZoneInfo

from src.core.types import Tick

logger = logging.getLogger(__name__)

# Trading day rolls at 5 PM ET (17:00) during the daily futures halt
ROLLOVER_HOUR_ET = 17
ET_TIMEZONE = ZoneInfo("America/New_York")


def get_trading_date(timestamp: datetime) -> str:
    """
    Get the trading date for a given timestamp.

    Trading day rolls at 5 PM ET. Ticks before 5 PM belong to that calendar day.
    Ticks at or after 5 PM belong to the NEXT calendar day.

    Examples (all times ET):
        - 2025-12-01 09:30:00 -> "2025-12-01" (Monday morning -> Monday's file)
        - 2025-12-01 16:59:59 -> "2025-12-01" (Before 5 PM -> Monday's file)
        - 2025-12-01 17:00:00 -> "2025-12-02" (At 5 PM -> Tuesday's file)
        - 2025-12-01 23:30:00 -> "2025-12-02" (Evening -> Tuesday's file)

    Args:
        timestamp: UTC or timezone-aware datetime

    Returns:
        Trading date string in YYYY-MM-DD format
    """
    # Convert to ET
    if timestamp.tzinfo is None:
        # Assume UTC if no timezone
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    et_time = timestamp.astimezone(ET_TIMEZONE)

    # If at or after 5 PM ET, this tick belongs to the next calendar day's file
    if et_time.hour >= ROLLOVER_HOUR_ET:
        trading_date = et_time.date() + timedelta(days=1)
    else:
        trading_date = et_time.date()

    return trading_date.strftime("%Y-%m-%d")


class TickLogger:
    """
    In-memory tick accumulator with Parquet persistence.

    Trading day rolls at 5 PM ET to align with futures daily halt (5-6 PM ET).
    Files are named by trading date, containing ticks from previous 5 PM to current 5 PM.

    Usage:
        logger = TickLogger()
        logger.log_tick(tick)  # Called for each tick
        logger.flush()         # Called at 5 PM ET to persist
    """

    # Parquet schema for tick data
    SCHEMA = pa.schema([
        ("timestamp", pa.timestamp("us", tz="UTC")),  # Microsecond precision
        ("symbol", pa.string()),
        ("price", pa.float64()),
        ("volume", pa.int32()),
        ("side", pa.string()),  # "BID" or "ASK"
    ])

    def __init__(self, output_dir: str = "data/ticks"):
        """
        Initialize the tick logger.

        Args:
            output_dir: Directory for Parquet files
        """
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)

        # In-memory storage: date -> list of ticks
        self._ticks: Dict[str, List[dict]] = defaultdict(list)
        self._tick_count = 0
        self._current_date: Optional[str] = None

    @property
    def tick_count(self) -> int:
        """Total ticks logged this session."""
        return self._tick_count

    def log_tick(self, tick: Tick) -> None:
        """
        Log a tick to memory.

        Args:
            tick: Tick data to log
        """
        # Get trading date (rolls at 5 PM ET)
        trading_date = get_trading_date(tick.timestamp)

        # Check if we crossed into a new trading day (5 PM ET rollover)
        if self._current_date and trading_date != self._current_date:
            # Auto-flush previous trading day's data
            logger.info(f"Trading day rolled from {self._current_date} to {trading_date} (5 PM ET), flushing old data")
            self.flush(self._current_date)

        self._current_date = trading_date

        # Convert tick to dict for storage
        tick_dict = {
            "timestamp": tick.timestamp,
            "symbol": tick.symbol,
            "price": tick.price,
            "volume": tick.volume,
            "side": tick.side,
        }

        self._ticks[trading_date].append(tick_dict)
        self._tick_count += 1

        # Log progress periodically
        if self._tick_count % 100000 == 0:
            logger.info(f"Tick logger: {self._tick_count:,} ticks accumulated")

    def flush(self, date: str = None) -> Optional[str]:
        """
        Flush accumulated ticks to Parquet file.

        If file already exists, appends new ticks to it (preserves data across restarts).

        Args:
            date: Specific date to flush (YYYY-MM-DD), or None for current date

        Returns:
            Path to written file, or None if no data
        """
        date_to_flush = date or self._current_date

        if not date_to_flush:
            logger.warning("No date to flush")
            return None

        ticks = self._ticks.get(date_to_flush, [])

        if not ticks:
            logger.warning(f"No ticks to flush for {date_to_flush}")
            return None

        # Convert new ticks to PyArrow table
        new_table = pa.Table.from_pylist(ticks, schema=self.SCHEMA)

        # Check if file already exists - if so, append to it
        output_path = os.path.join(self.output_dir, f"{date_to_flush}.parquet")
        if os.path.exists(output_path):
            try:
                existing_table = pq.read_table(output_path)
                # Concatenate existing + new ticks
                combined_table = pa.concat_tables([existing_table, new_table])
                logger.info(f"Appending {len(ticks):,} ticks to existing {len(existing_table):,} ticks")
            except Exception as e:
                logger.warning(f"Could not read existing parquet, overwriting: {e}")
                combined_table = new_table
        else:
            combined_table = new_table

        # Write combined data to Parquet
        pq.write_table(
            combined_table,
            output_path,
            compression="snappy",  # Good balance of speed/size
            use_dictionary=True,   # Efficient for string columns
        )

        # Get file stats
        file_size = os.path.getsize(output_path)
        file_size_mb = file_size / (1024 * 1024)

        logger.info(
            f"Flushed {len(ticks):,} new ticks for {date_to_flush} "
            f"(total: {len(combined_table):,}) to {output_path} ({file_size_mb:.2f} MB)"
        )

        # Clear flushed data from memory
        del self._ticks[date_to_flush]

        return output_path

    def flush_all(self) -> List[str]:
        """
        Flush all accumulated data to Parquet files.

        Returns:
            List of paths to written files
        """
        paths = []
        dates = list(self._ticks.keys())

        for date in dates:
            path = self.flush(date)
            if path:
                paths.append(path)

        return paths

    def get_stats(self) -> dict:
        """Get current logger statistics."""
        return {
            "tick_count": self._tick_count,
            "current_date": self._current_date,
            "dates_in_memory": list(self._ticks.keys()),
            "ticks_per_date": {d: len(t) for d, t in self._ticks.items()},
        }

    @staticmethod
    def load_parquet(file_path: str) -> List[Tick]:
        """
        Load ticks from a Parquet file.

        Args:
            file_path: Path to Parquet file

        Returns:
            List of Tick objects
        """
        table = pq.read_table(file_path)
        df = table.to_pandas()

        ticks = []
        for _, row in df.iterrows():
            tick = Tick(
                timestamp=row["timestamp"].to_pydatetime(),
                symbol=row["symbol"],
                price=row["price"],
                volume=row["volume"],
                side=row["side"],
            )
            ticks.append(tick)

        return ticks

    @staticmethod
    def list_available_dates(output_dir: str = "data/ticks") -> List[str]:
        """
        List dates with available tick data.

        Returns:
            List of date strings (YYYY-MM-DD)
        """
        if not os.path.exists(output_dir):
            return []

        dates = []
        for filename in os.listdir(output_dir):
            if filename.endswith(".parquet"):
                date = filename.replace(".parquet", "")
                dates.append(date)

        return sorted(dates)


# Singleton instance for use across the application
_tick_logger: Optional[TickLogger] = None


def get_tick_logger() -> TickLogger:
    """Get the global tick logger instance."""
    global _tick_logger
    if _tick_logger is None:
        _tick_logger = TickLogger()
    return _tick_logger


def log_tick(tick: Tick) -> None:
    """Convenience function to log a tick."""
    get_tick_logger().log_tick(tick)


def flush_ticks() -> List[str]:
    """Convenience function to flush all ticks."""
    return get_tick_logger().flush_all()
