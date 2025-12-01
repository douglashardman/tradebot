"""SQLite database for tracking live/paper trading activity."""

import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional

from src.core.constants import TICK_SIZES

DB_PATH = os.path.join(os.path.dirname(__file__), "../../data/live_trades.db")


def get_connection() -> sqlite3.Connection:
    """Get database connection, creating tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    _migrate_tables(conn)
    return conn


def _migrate_tables(conn: sqlite3.Connection) -> None:
    """Add new columns to existing tables if they don't exist."""
    # Check for new columns in trades table
    cursor = conn.execute("PRAGMA table_info(trades)")
    existing_cols = {row[1] for row in cursor.fetchall()}

    new_columns = [
        ("stop_price", "REAL"),
        ("target_price", "REAL"),
    ]

    for col_name, col_type in new_columns:
        if col_name not in existing_cols:
            try:
                conn.execute(f"ALTER TABLE trades ADD COLUMN {col_name} {col_type}")
            except sqlite3.OperationalError:
                pass  # Column might already exist

    conn.commit()


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""

    # Trading sessions - one per day
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL UNIQUE,
            mode TEXT NOT NULL,
            symbol TEXT NOT NULL,
            contract TEXT,

            -- Tier info at session start
            tier_index INTEGER,
            tier_name TEXT,
            starting_balance REAL,

            -- Session parameters
            max_position_size INTEGER,
            stop_loss_ticks INTEGER,
            take_profit_ticks INTEGER,
            daily_loss_limit REAL,

            -- Results
            ending_balance REAL,
            session_pnl REAL DEFAULT 0,
            total_trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            commissions REAL DEFAULT 0,

            -- Status
            status TEXT DEFAULT 'ACTIVE',
            halted_reason TEXT,

            -- Timestamps
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            ended_at TIMESTAMP,

            -- Notes
            notes TEXT
        )
    """)

    # Orders - every order submitted to broker
    conn.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,

            -- Our identifiers
            internal_order_id TEXT NOT NULL,
            bracket_id TEXT,

            -- Broker identifiers (from Rithmic)
            broker_order_id TEXT,
            exchange_order_id TEXT,

            -- Order details
            symbol TEXT NOT NULL,
            contract TEXT,
            side TEXT NOT NULL,
            order_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            limit_price REAL,
            stop_price REAL,

            -- Fill details
            status TEXT DEFAULT 'PENDING',
            filled_size INTEGER DEFAULT 0,
            avg_fill_price REAL,

            -- Slippage tracking
            expected_price REAL,
            slippage_ticks REAL,

            -- Commission
            commission REAL DEFAULT 0,

            -- Timestamps (microsecond precision from Rithmic)
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            submitted_at TIMESTAMP,
            acknowledged_at TIMESTAMP,
            filled_at TIMESTAMP,
            cancelled_at TIMESTAMP,

            -- Rejection info
            reject_reason TEXT,

            -- Raw broker response (JSON)
            broker_response TEXT,

            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)

    # Trades - completed round trips (entry + exit)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,
            trade_num INTEGER NOT NULL,

            -- Our identifiers
            internal_trade_id TEXT NOT NULL,
            bracket_id TEXT,

            -- Entry order reference
            entry_order_id INTEGER,
            entry_broker_order_id TEXT,

            -- Exit order reference
            exit_order_id INTEGER,
            exit_broker_order_id TEXT,

            -- Trade details
            symbol TEXT NOT NULL,
            contract TEXT,
            direction TEXT NOT NULL,
            size INTEGER NOT NULL,

            -- Entry details
            entry_price REAL NOT NULL,
            entry_time TIMESTAMP NOT NULL,
            entry_slippage_ticks REAL DEFAULT 0,

            -- Bracket levels (planned stop/target)
            stop_price REAL,
            target_price REAL,

            -- Exit details
            exit_price REAL,
            exit_time TIMESTAMP,
            exit_reason TEXT,
            exit_slippage_ticks REAL DEFAULT 0,

            -- P&L
            pnl_gross REAL DEFAULT 0,
            commission REAL DEFAULT 0,
            pnl_net REAL DEFAULT 0,
            pnl_ticks INTEGER DEFAULT 0,

            -- Running totals
            running_pnl REAL DEFAULT 0,
            account_balance REAL,

            -- Signal/Strategy context
            pattern TEXT,
            signal_strength REAL,
            regime TEXT,
            regime_score REAL,

            -- Tier context
            tier_index INTEGER,
            tier_name TEXT,
            instrument TEXT,

            -- Position sizing context
            stacked_count INTEGER DEFAULT 1,
            win_streak INTEGER DEFAULT 0,
            loss_streak INTEGER DEFAULT 0,

            -- Timestamps
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (session_id) REFERENCES sessions(id),
            FOREIGN KEY (entry_order_id) REFERENCES orders(id),
            FOREIGN KEY (exit_order_id) REFERENCES orders(id)
        )
    """)

    # Account snapshots - periodic balance/position snapshots
    conn.execute("""
        CREATE TABLE IF NOT EXISTS account_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,

            -- Account info from broker
            account_id TEXT,
            account_balance REAL,
            available_margin REAL,
            used_margin REAL,
            unrealized_pnl REAL,
            realized_pnl REAL,

            -- Open positions
            open_position_count INTEGER DEFAULT 0,
            open_position_size INTEGER DEFAULT 0,

            -- Timestamps
            snapshot_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            broker_timestamp TIMESTAMP,

            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)

    # Tier changes - log all tier transitions
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tier_changes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,

            -- Change details
            from_tier_index INTEGER,
            from_tier_name TEXT,
            to_tier_index INTEGER,
            to_tier_name TEXT,

            -- Instrument change
            from_instrument TEXT,
            to_instrument TEXT,

            -- Balance at change
            balance_at_change REAL,

            -- What triggered the change
            trigger_reason TEXT,

            -- Timestamps
            changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)

    # Connection health log
    conn.execute("""
        CREATE TABLE IF NOT EXISTS connection_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER,

            event_type TEXT NOT NULL,
            plant_type TEXT,
            details TEXT,

            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

            FOREIGN KEY (session_id) REFERENCES sessions(id)
        )
    """)

    conn.commit()


# =============================================================================
# Session Functions
# =============================================================================

def create_session(
    date: str,
    mode: str,
    symbol: str,
    contract: str = None,
    tier_index: int = None,
    tier_name: str = None,
    starting_balance: float = None,
    max_position_size: int = None,
    stop_loss_ticks: int = None,
    take_profit_ticks: int = None,
    daily_loss_limit: float = None,
) -> int:
    """Create a new trading session."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO sessions (
            date, mode, symbol, contract, tier_index, tier_name,
            starting_balance, max_position_size, stop_loss_ticks,
            take_profit_ticks, daily_loss_limit
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        date, mode, symbol, contract, tier_index, tier_name,
        starting_balance, max_position_size, stop_loss_ticks,
        take_profit_ticks, daily_loss_limit
    ))
    session_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return session_id


def end_session(
    session_id: int,
    ending_balance: float,
    session_pnl: float,
    total_trades: int,
    wins: int,
    losses: int,
    commissions: float = 0,
    status: str = "COMPLETED",
    halted_reason: str = None,
    notes: str = None,
) -> None:
    """End a trading session with final stats."""
    conn = get_connection()
    conn.execute("""
        UPDATE sessions SET
            ending_balance = ?,
            session_pnl = ?,
            total_trades = ?,
            wins = ?,
            losses = ?,
            commissions = ?,
            status = ?,
            halted_reason = ?,
            ended_at = CURRENT_TIMESTAMP,
            notes = ?
        WHERE id = ?
    """, (
        ending_balance, session_pnl, total_trades, wins, losses,
        commissions, status, halted_reason, notes, session_id
    ))
    conn.commit()
    conn.close()


# =============================================================================
# Order Functions
# =============================================================================

def log_order(
    session_id: int,
    internal_order_id: str,
    symbol: str,
    side: str,
    order_type: str,
    size: int,
    contract: str = None,
    bracket_id: str = None,
    limit_price: float = None,
    stop_price: float = None,
    expected_price: float = None,
) -> int:
    """Log a new order submission."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO orders (
            session_id, internal_order_id, bracket_id, symbol, contract,
            side, order_type, size, limit_price, stop_price, expected_price,
            submitted_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
    """, (
        session_id, internal_order_id, bracket_id, symbol, contract,
        side, order_type, size, limit_price, stop_price, expected_price
    ))
    order_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return order_id


def update_order_acknowledged(
    order_id: int,
    broker_order_id: str,
    exchange_order_id: str = None,
) -> None:
    """Update order with broker acknowledgment."""
    conn = get_connection()
    conn.execute("""
        UPDATE orders SET
            broker_order_id = ?,
            exchange_order_id = ?,
            status = 'ACKNOWLEDGED',
            acknowledged_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (broker_order_id, exchange_order_id, order_id))
    conn.commit()
    conn.close()


def update_order_filled(
    order_id: int,
    filled_size: int,
    avg_fill_price: float,
    commission: float = 0,
    broker_response: str = None,
) -> None:
    """Update order with fill information."""
    conn = get_connection()

    # Get expected price and symbol to calculate slippage
    cursor = conn.execute(
        "SELECT expected_price, side, symbol FROM orders WHERE id = ?", (order_id,)
    )
    row = cursor.fetchone()
    slippage_ticks = None
    if row and row["expected_price"]:
        price_diff = avg_fill_price - row["expected_price"]
        # Negative slippage is bad for buys, positive is bad for sells
        if row["side"] == "SELL":
            price_diff = -price_diff

        # Get tick size for this symbol (try 3-char prefix first, then 2-char)
        symbol = row["symbol"] or "ES"
        symbol_base = symbol[:3] if symbol[:3] in TICK_SIZES else symbol[:2]
        tick_size = TICK_SIZES.get(symbol_base, 0.25)

        slippage_ticks = price_diff / tick_size

    conn.execute("""
        UPDATE orders SET
            status = 'FILLED',
            filled_size = ?,
            avg_fill_price = ?,
            slippage_ticks = ?,
            commission = ?,
            filled_at = CURRENT_TIMESTAMP,
            broker_response = ?
        WHERE id = ?
    """, (filled_size, avg_fill_price, slippage_ticks, commission, broker_response, order_id))
    conn.commit()
    conn.close()


def update_order_rejected(
    order_id: int,
    reject_reason: str,
) -> None:
    """Update order with rejection."""
    conn = get_connection()
    conn.execute("""
        UPDATE orders SET
            status = 'REJECTED',
            reject_reason = ?
        WHERE id = ?
    """, (reject_reason, order_id))
    conn.commit()
    conn.close()


def update_order_cancelled(
    order_id: int,
) -> None:
    """Update order as cancelled."""
    conn = get_connection()
    conn.execute("""
        UPDATE orders SET
            status = 'CANCELLED',
            cancelled_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (order_id,))
    conn.commit()
    conn.close()


# =============================================================================
# Trade Functions
# =============================================================================

def log_trade(
    session_id: int,
    trade_num: int,
    internal_trade_id: str,
    symbol: str,
    direction: str,
    size: int,
    entry_price: float,
    entry_time: datetime,
    contract: str = None,
    bracket_id: str = None,
    entry_order_id: int = None,
    entry_broker_order_id: str = None,
    entry_slippage_ticks: float = 0,
    stop_price: float = None,
    target_price: float = None,
    pattern: str = None,
    signal_strength: float = None,
    regime: str = None,
    regime_score: float = None,
    tier_index: int = None,
    tier_name: str = None,
    instrument: str = None,
    stacked_count: int = 1,
    win_streak: int = 0,
    loss_streak: int = 0,
) -> int:
    """Log a new trade entry."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO trades (
            session_id, trade_num, internal_trade_id, bracket_id,
            entry_order_id, entry_broker_order_id,
            symbol, contract, direction, size,
            entry_price, entry_time, entry_slippage_ticks,
            stop_price, target_price,
            pattern, signal_strength, regime, regime_score,
            tier_index, tier_name, instrument,
            stacked_count, win_streak, loss_streak
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id, trade_num, internal_trade_id, bracket_id,
        entry_order_id, entry_broker_order_id,
        symbol, contract, direction, size,
        entry_price, entry_time.isoformat() if entry_time else None, entry_slippage_ticks,
        stop_price, target_price,
        pattern, signal_strength, regime, regime_score,
        tier_index, tier_name, instrument,
        stacked_count, win_streak, loss_streak
    ))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def update_trade_exit(
    trade_id: int,
    exit_price: float,
    exit_time: datetime,
    exit_reason: str,
    pnl_gross: float,
    pnl_ticks: int,
    commission: float = 0,
    running_pnl: float = 0,
    account_balance: float = None,
    exit_order_id: int = None,
    exit_broker_order_id: str = None,
    exit_slippage_ticks: float = 0,
) -> None:
    """Update trade with exit information."""
    pnl_net = pnl_gross - commission
    conn = get_connection()
    conn.execute("""
        UPDATE trades SET
            exit_order_id = ?,
            exit_broker_order_id = ?,
            exit_price = ?,
            exit_time = ?,
            exit_reason = ?,
            exit_slippage_ticks = ?,
            pnl_gross = ?,
            commission = ?,
            pnl_net = ?,
            pnl_ticks = ?,
            running_pnl = ?,
            account_balance = ?
        WHERE id = ?
    """, (
        exit_order_id, exit_broker_order_id,
        exit_price, exit_time.isoformat() if exit_time else None, exit_reason,
        exit_slippage_ticks,
        pnl_gross, commission, pnl_net, pnl_ticks,
        running_pnl, account_balance, trade_id
    ))
    conn.commit()
    conn.close()


# =============================================================================
# Account Snapshot Functions
# =============================================================================

def log_account_snapshot(
    session_id: int,
    account_id: str = None,
    account_balance: float = None,
    available_margin: float = None,
    used_margin: float = None,
    unrealized_pnl: float = None,
    realized_pnl: float = None,
    open_position_count: int = 0,
    open_position_size: int = 0,
    broker_timestamp: datetime = None,
) -> int:
    """Log an account snapshot from broker."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO account_snapshots (
            session_id, account_id, account_balance, available_margin,
            used_margin, unrealized_pnl, realized_pnl,
            open_position_count, open_position_size, broker_timestamp
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id, account_id, account_balance, available_margin,
        used_margin, unrealized_pnl, realized_pnl,
        open_position_count, open_position_size,
        broker_timestamp.isoformat() if broker_timestamp else None
    ))
    snapshot_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return snapshot_id


# =============================================================================
# Tier Change Functions
# =============================================================================

def log_tier_change(
    session_id: int,
    from_tier_index: int,
    from_tier_name: str,
    to_tier_index: int,
    to_tier_name: str,
    from_instrument: str,
    to_instrument: str,
    balance_at_change: float,
    trigger_reason: str = None,
) -> int:
    """Log a tier change event."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO tier_changes (
            session_id, from_tier_index, from_tier_name,
            to_tier_index, to_tier_name,
            from_instrument, to_instrument,
            balance_at_change, trigger_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_id, from_tier_index, from_tier_name,
        to_tier_index, to_tier_name,
        from_instrument, to_instrument,
        balance_at_change, trigger_reason
    ))
    change_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return change_id


# =============================================================================
# Connection Log Functions
# =============================================================================

def log_connection_event(
    session_id: int,
    event_type: str,
    plant_type: str = None,
    details: str = None,
) -> None:
    """Log a connection event (connect, disconnect, reconnect, etc.)."""
    conn = get_connection()
    conn.execute("""
        INSERT INTO connection_log (session_id, event_type, plant_type, details)
        VALUES (?, ?, ?, ?)
    """, (session_id, event_type, plant_type, details))
    conn.commit()
    conn.close()


# =============================================================================
# Query Functions
# =============================================================================

def get_session(session_id: int) -> Optional[Dict]:
    """Get a session by ID."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_session_by_date(date: str) -> Optional[Dict]:
    """Get session for a specific date."""
    conn = get_connection()
    row = conn.execute("SELECT * FROM sessions WHERE date = ?", (date,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_or_create_session(
    date: str,
    mode: str,
    symbol: str,
    contract: str = None,
    tier_index: int = None,
    tier_name: str = None,
    starting_balance: float = None,
    max_position_size: int = None,
    stop_loss_ticks: int = None,
    take_profit_ticks: int = None,
    daily_loss_limit: float = None,
) -> int:
    """Get existing session for today or create a new one.

    This handles graceful restarts - if the service restarts mid-day,
    it will resume the existing session instead of failing.
    """
    existing = get_session_by_date(date)
    if existing:
        return existing["id"]

    return create_session(
        date=date,
        mode=mode,
        symbol=symbol,
        contract=contract,
        tier_index=tier_index,
        tier_name=tier_name,
        starting_balance=starting_balance,
        max_position_size=max_position_size,
        stop_loss_ticks=stop_loss_ticks,
        take_profit_ticks=take_profit_ticks,
        daily_loss_limit=daily_loss_limit,
    )


def get_trades_for_session(session_id: int) -> List[Dict]:
    """Get all trades for a session."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM trades WHERE session_id = ? ORDER BY trade_num",
        (session_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_orders_for_session(session_id: int) -> List[Dict]:
    """Get all orders for a session."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM orders WHERE session_id = ? ORDER BY created_at",
        (session_id,)
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_today_summary() -> Dict:
    """Get summary of today's trading."""
    today = datetime.now().strftime("%Y-%m-%d")
    conn = get_connection()

    session = conn.execute(
        "SELECT * FROM sessions WHERE date = ?", (today,)
    ).fetchone()

    if not session:
        conn.close()
        return {"date": today, "status": "NO_SESSION"}

    session_id = session["id"]

    # Get trade stats
    trades = conn.execute("""
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_net <= 0 THEN 1 ELSE 0 END) as losses,
            SUM(pnl_gross) as gross_pnl,
            SUM(commission) as total_commission,
            SUM(pnl_net) as net_pnl
        FROM trades WHERE session_id = ? AND exit_price IS NOT NULL
    """, (session_id,)).fetchone()

    conn.close()

    return {
        "date": today,
        "status": session["status"],
        "mode": session["mode"],
        "tier": session["tier_name"],
        "starting_balance": session["starting_balance"],
        "current_balance": session["ending_balance"] or session["starting_balance"],
        "total_trades": trades["total"] or 0,
        "wins": trades["wins"] or 0,
        "losses": trades["losses"] or 0,
        "gross_pnl": trades["gross_pnl"] or 0,
        "commissions": trades["total_commission"] or 0,
        "net_pnl": trades["net_pnl"] or 0,
    }


def get_all_time_stats() -> Dict:
    """Get all-time trading statistics."""
    conn = get_connection()

    stats = conn.execute("""
        SELECT
            COUNT(DISTINCT session_id) as total_sessions,
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
            SUM(CASE WHEN pnl_net <= 0 THEN 1 ELSE 0 END) as losses,
            SUM(pnl_gross) as gross_pnl,
            SUM(commission) as total_commission,
            SUM(pnl_net) as net_pnl,
            AVG(CASE WHEN pnl_net > 0 THEN pnl_net ELSE NULL END) as avg_win,
            AVG(CASE WHEN pnl_net < 0 THEN pnl_net ELSE NULL END) as avg_loss,
            MAX(pnl_net) as best_trade,
            MIN(pnl_net) as worst_trade
        FROM trades WHERE exit_price IS NOT NULL
    """).fetchone()

    # Get by tier
    by_tier = conn.execute("""
        SELECT
            tier_name,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl_net > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl_net) as pnl
        FROM trades
        WHERE exit_price IS NOT NULL AND tier_name IS NOT NULL
        GROUP BY tier_name
    """).fetchall()

    conn.close()

    total = stats["total_trades"] or 0
    wins = stats["wins"] or 0

    return {
        "total_sessions": stats["total_sessions"] or 0,
        "total_trades": total,
        "wins": wins,
        "losses": stats["losses"] or 0,
        "win_rate": wins / total if total > 0 else 0,
        "gross_pnl": stats["gross_pnl"] or 0,
        "total_commission": stats["total_commission"] or 0,
        "net_pnl": stats["net_pnl"] or 0,
        "avg_win": stats["avg_win"] or 0,
        "avg_loss": stats["avg_loss"] or 0,
        "best_trade": stats["best_trade"] or 0,
        "worst_trade": stats["worst_trade"] or 0,
        "by_tier": [dict(row) for row in by_tier],
    }


def print_schema():
    """Print the database schema for review."""
    conn = get_connection()

    tables = ['sessions', 'orders', 'trades', 'account_snapshots', 'tier_changes', 'connection_log']

    print("\n" + "=" * 70)
    print("LIVE TRADING DATABASE SCHEMA")
    print("=" * 70)

    for table in tables:
        print(f"\n=== {table.upper()} ===")
        cursor = conn.execute(f"PRAGMA table_info({table})")
        for row in cursor.fetchall():
            col_id, name, col_type, notnull, default, pk = row
            nullable = "" if notnull else " (nullable)"
            pk_str = " [PK]" if pk else ""
            default_str = f" DEFAULT {default}" if default else ""
            print(f"  {name}: {col_type}{nullable}{default_str}{pk_str}")

    conn.close()


if __name__ == "__main__":
    print_schema()
