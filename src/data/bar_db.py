"""
Bar Persistence - SQLite storage for completed footprint bars.

Stores completed bars for regime detector warmup on restart.
Trivial I/O: ~78 inserts/day during RTH, one every 5 minutes.

Usage:
    from src.data.bar_db import save_bar, get_recent_bars, get_last_regime, save_regime

    # On bar complete
    save_bar(bar)

    # On startup (warmup)
    bars = get_recent_bars("MES", limit=50)
    for bar in bars:
        router.on_bar(bar)

    # Regime persistence
    save_regime("MES", "TRENDING_UP", 0.85)
    regime, confidence = get_last_regime("MES")
"""

import os
import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional, Tuple
from contextlib import contextmanager

from src.core.types import FootprintBar, PriceLevel

# Database path
DB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data")
DB_PATH = os.path.join(DB_DIR, "bars.db")


@contextmanager
def get_connection():
    """Get database connection with proper cleanup."""
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    """Initialize database schema."""
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                open_price REAL NOT NULL,
                high_price REAL NOT NULL,
                low_price REAL NOT NULL,
                close_price REAL NOT NULL,
                total_volume INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                buy_volume INTEGER NOT NULL,
                sell_volume INTEGER NOT NULL,
                level_count INTEGER NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol, start_time)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bars_symbol_time
            ON bars(symbol, start_time DESC)
        """)

        # Regime state table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS regime_state (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                regime TEXT NOT NULL,
                confidence REAL NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(symbol)
            )
        """)


def save_bar(bar: FootprintBar) -> int:
    """
    Save a completed bar to the database.

    Args:
        bar: Completed FootprintBar

    Returns:
        Row ID of inserted bar
    """
    init_db()

    # Calculate buy/sell volumes from levels
    buy_volume = sum(level.ask_volume for level in bar.levels.values())
    sell_volume = sum(level.bid_volume for level in bar.levels.values())

    with get_connection() as conn:
        cursor = conn.execute("""
            INSERT OR REPLACE INTO bars
            (symbol, start_time, end_time, open_price, high_price, low_price,
             close_price, total_volume, delta, buy_volume, sell_volume, level_count)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            bar.symbol,
            bar.start_time.isoformat(),
            bar.end_time.isoformat() if bar.end_time else bar.start_time.isoformat(),
            bar.open_price,
            bar.high_price,
            bar.low_price,
            bar.close_price,
            bar.total_volume,
            bar.delta,
            buy_volume,
            sell_volume,
            len(bar.levels),
        ))
        return cursor.lastrowid


def get_recent_bars(symbol: str, limit: int = 50) -> List[FootprintBar]:
    """
    Get recent bars for a symbol.

    Args:
        symbol: Trading symbol (e.g., "MES")
        limit: Maximum number of bars to return

    Returns:
        List of FootprintBar objects, oldest first
    """
    init_db()

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM bars
            WHERE symbol = ?
            ORDER BY start_time DESC
            LIMIT ?
        """, (symbol, limit)).fetchall()

    bars = []
    for row in reversed(rows):  # Reverse to get oldest first
        bar = _row_to_bar(row)
        bars.append(bar)

    return bars


def _row_to_bar(row: sqlite3.Row) -> FootprintBar:
    """
    Convert a database row to a FootprintBar.

    Creates a synthetic price level to hold aggregate volume data,
    so the bar's computed properties (total_volume, delta) work correctly.
    """
    # Create a synthetic level at close price to hold aggregate volumes
    # This allows the bar's computed properties to return correct values
    close_price = row["close_price"]
    synthetic_level = PriceLevel(
        price=close_price,
        bid_volume=row["sell_volume"],  # bid = sell
        ask_volume=row["buy_volume"],   # ask = buy
    )

    bar = FootprintBar(
        symbol=row["symbol"],
        start_time=datetime.fromisoformat(row["start_time"]),
        end_time=datetime.fromisoformat(row["end_time"]),
        timeframe=300,  # 5-minute bars
        open_price=row["open_price"],
        high_price=row["high_price"],
        low_price=row["low_price"],
        close_price=close_price,
        levels={close_price: synthetic_level},
    )

    return bar


def get_bars_since(symbol: str, since: datetime, limit: int = 200) -> List[FootprintBar]:
    """
    Get bars since a specific time.

    Args:
        symbol: Trading symbol
        since: Start time (inclusive)
        limit: Maximum bars to return

    Returns:
        List of FootprintBar objects, oldest first
    """
    init_db()

    with get_connection() as conn:
        rows = conn.execute("""
            SELECT * FROM bars
            WHERE symbol = ? AND start_time >= ?
            ORDER BY start_time ASC
            LIMIT ?
        """, (symbol, since.isoformat(), limit)).fetchall()

    return [_row_to_bar(row) for row in rows]


def save_regime(symbol: str, regime: str, confidence: float) -> None:
    """
    Save current regime state.

    Args:
        symbol: Trading symbol
        regime: Regime name (e.g., "TRENDING_UP")
        confidence: Confidence score (0.0-1.0)
    """
    init_db()

    with get_connection() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO regime_state (symbol, regime, confidence, updated_at)
            VALUES (?, ?, ?, ?)
        """, (symbol, regime, confidence, datetime.now().isoformat()))


def get_last_regime(symbol: str) -> Tuple[Optional[str], float]:
    """
    Get last known regime state.

    Args:
        symbol: Trading symbol

    Returns:
        Tuple of (regime_name, confidence) or (None, 0.0) if not found
    """
    init_db()

    with get_connection() as conn:
        row = conn.execute("""
            SELECT regime, confidence FROM regime_state
            WHERE symbol = ?
        """, (symbol,)).fetchone()

    if row:
        return row["regime"], row["confidence"]
    return None, 0.0


def cleanup_old_bars(days: int = 7) -> int:
    """
    Remove bars older than N days.

    Args:
        days: Number of days to keep

    Returns:
        Number of rows deleted
    """
    init_db()

    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    with get_connection() as conn:
        cursor = conn.execute("""
            DELETE FROM bars WHERE start_time < ?
        """, (cutoff,))
        return cursor.rowcount


def get_bar_count(symbol: str) -> int:
    """Get total number of bars for a symbol."""
    init_db()

    with get_connection() as conn:
        row = conn.execute("""
            SELECT COUNT(*) as count FROM bars WHERE symbol = ?
        """, (symbol,)).fetchone()
        return row["count"] if row else 0
