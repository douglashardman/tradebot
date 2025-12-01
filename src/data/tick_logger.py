"""
Tick Data Logger - Stores live tick data in Parquet format for backtesting.

Design:
- Accumulates ticks in memory during the trading day
- At end of day (or on demand), flushes to Parquet file
- Files organized by date: data/ticks/YYYY-MM-DD.parquet
- SCP cron job exports to remote server nightly
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pyarrow as pa
import pyarrow.parquet as pq

from src.core.types import Tick

logger = logging.getLogger(__name__)


class TickLogger:
    """
    In-memory tick accumulator with Parquet persistence.

    Usage:
        logger = TickLogger()
        logger.log_tick(tick)  # Called for each tick
        logger.flush()         # Called at end of day to persist
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
        # Get date string for partitioning
        date_str = tick.timestamp.strftime("%Y-%m-%d")

        # Check if we crossed into a new day
        if self._current_date and date_str != self._current_date:
            # Auto-flush previous day's data
            logger.info(f"Date changed from {self._current_date} to {date_str}, flushing old data")
            self.flush(self._current_date)

        self._current_date = date_str

        # Convert tick to dict for storage
        tick_dict = {
            "timestamp": tick.timestamp,
            "symbol": tick.symbol,
            "price": tick.price,
            "volume": tick.volume,
            "side": tick.side,
        }

        self._ticks[date_str].append(tick_dict)
        self._tick_count += 1

        # Log progress periodically
        if self._tick_count % 100000 == 0:
            logger.info(f"Tick logger: {self._tick_count:,} ticks accumulated")

    def flush(self, date: str = None) -> Optional[str]:
        """
        Flush accumulated ticks to Parquet file.

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

        # Convert to PyArrow table
        table = pa.Table.from_pylist(ticks, schema=self.SCHEMA)

        # Write to Parquet
        output_path = os.path.join(self.output_dir, f"{date_to_flush}.parquet")
        pq.write_table(
            table,
            output_path,
            compression="snappy",  # Good balance of speed/size
            use_dictionary=True,   # Efficient for string columns
        )

        # Get file stats
        file_size = os.path.getsize(output_path)
        file_size_mb = file_size / (1024 * 1024)

        logger.info(
            f"Flushed {len(ticks):,} ticks for {date_to_flush} "
            f"to {output_path} ({file_size_mb:.2f} MB)"
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
