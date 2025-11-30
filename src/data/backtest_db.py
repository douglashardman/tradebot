"""SQLite database for tracking backtest results and Databento spending."""

import sqlite3
import os
from datetime import datetime
from typing import List, Dict, Optional

DB_PATH = os.path.join(os.path.dirname(__file__), "../../data/backtests.db")


def get_connection() -> sqlite3.Connection:
    """Get database connection, creating tables if needed."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    _create_tables(conn)
    return conn


def _create_tables(conn: sqlite3.Connection) -> None:
    """Create tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS backtests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            symbol TEXT NOT NULL,
            contract TEXT NOT NULL,
            date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            end_time TEXT NOT NULL,
            ticks INTEGER NOT NULL,
            estimated_cost REAL NOT NULL,
            signals_generated INTEGER DEFAULT 0,
            signals_approved INTEGER DEFAULT 0,
            trades INTEGER DEFAULT 0,
            wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0,
            pnl REAL DEFAULT 0,
            win_rate REAL DEFAULT 0,
            notes TEXT
        )
    """)

    # Individual trade tracking for detailed analysis
    conn.execute("""
        CREATE TABLE IF NOT EXISTS trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            backtest_id INTEGER NOT NULL,
            trade_num INTEGER NOT NULL,
            entry_time TIMESTAMP NOT NULL,
            exit_time TIMESTAMP,
            pattern TEXT NOT NULL,
            direction TEXT NOT NULL,
            regime TEXT,
            regime_score REAL,
            signal_strength REAL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            stop_price REAL,
            target_price REAL,
            size INTEGER DEFAULT 1,
            pnl REAL DEFAULT 0,
            pnl_ticks REAL DEFAULT 0,
            exit_reason TEXT,
            running_equity REAL DEFAULT 0,
            FOREIGN KEY (backtest_id) REFERENCES backtests(id)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS spending (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            backtest_id INTEGER,
            ticks INTEGER NOT NULL,
            estimated_cost REAL NOT NULL,
            description TEXT,
            FOREIGN KEY (backtest_id) REFERENCES backtests(id)
        )
    """)
    conn.commit()


def log_backtest(
    symbol: str,
    contract: str,
    date: str,
    start_time: str,
    end_time: str,
    ticks: int,
    signals_generated: int = 0,
    signals_approved: int = 0,
    trades: int = 0,
    wins: int = 0,
    losses: int = 0,
    pnl: float = 0,
    notes: str = None,
    from_cache: bool = False
) -> int:
    """
    Log a backtest run to the database.

    Cost estimate: ~$0.0016 per 1000 ticks ($1.60 per million)
    If from_cache=True, don't log to spending table (data was free).
    """
    estimated_cost = 0 if from_cache else (ticks / 1_000_000) * 1.60
    win_rate = wins / trades if trades > 0 else 0

    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO backtests (
            symbol, contract, date, start_time, end_time, ticks,
            estimated_cost, signals_generated, signals_approved,
            trades, wins, losses, pnl, win_rate, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        symbol, contract, date, start_time, end_time, ticks,
        estimated_cost, signals_generated, signals_approved,
        trades, wins, losses, pnl, win_rate, notes
    ))

    backtest_id = cursor.lastrowid

    # Only log to spending table if not from cache
    if not from_cache:
        conn.execute("""
            INSERT INTO spending (backtest_id, ticks, estimated_cost, description)
            VALUES (?, ?, ?, ?)
        """, (backtest_id, ticks, estimated_cost, f"{contract} {date} {start_time}-{end_time}"))

    conn.commit()
    conn.close()

    return backtest_id


def log_trade(
    backtest_id: int,
    trade_num: int,
    entry_time: datetime,
    pattern: str,
    direction: str,
    entry_price: float,
    signal_strength: float = 0,
    regime: str = None,
    regime_score: float = None,
    stop_price: float = None,
    target_price: float = None,
    size: int = 1,
    exit_time: datetime = None,
    exit_price: float = None,
    pnl: float = 0,
    pnl_ticks: float = 0,
    exit_reason: str = None,
    running_equity: float = 0
) -> int:
    """Log an individual trade to the database."""
    conn = get_connection()
    cursor = conn.execute("""
        INSERT INTO trades (
            backtest_id, trade_num, entry_time, exit_time, pattern, direction,
            regime, regime_score, signal_strength, entry_price, exit_price,
            stop_price, target_price, size, pnl, pnl_ticks, exit_reason, running_equity
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        backtest_id, trade_num, entry_time.isoformat() if entry_time else None,
        exit_time.isoformat() if exit_time else None, pattern, direction,
        regime, regime_score, signal_strength, entry_price, exit_price,
        stop_price, target_price, size, pnl, pnl_ticks, exit_reason, running_equity
    ))
    trade_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return trade_id


def update_trade_exit(
    trade_id: int,
    exit_time: datetime,
    exit_price: float,
    pnl: float,
    pnl_ticks: float,
    exit_reason: str,
    running_equity: float
) -> None:
    """Update a trade with exit information."""
    conn = get_connection()
    conn.execute("""
        UPDATE trades SET
            exit_time = ?,
            exit_price = ?,
            pnl = ?,
            pnl_ticks = ?,
            exit_reason = ?,
            running_equity = ?
        WHERE id = ?
    """, (
        exit_time.isoformat() if exit_time else None,
        exit_price, pnl, pnl_ticks, exit_reason, running_equity, trade_id
    ))
    conn.commit()
    conn.close()


def get_trades_for_backtest(backtest_id: int) -> List[Dict]:
    """Get all trades for a specific backtest."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM trades WHERE backtest_id = ? ORDER BY trade_num
    """, (backtest_id,)).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_trade_analysis() -> Dict:
    """Get comprehensive trade-level analysis across all backtests."""
    conn = get_connection()

    # Overall trade stats
    row = conn.execute("""
        SELECT
            COUNT(*) as total_trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as winning_trades,
            SUM(CASE WHEN pnl < 0 THEN 1 ELSE 0 END) as losing_trades,
            SUM(CASE WHEN pnl > 0 THEN pnl ELSE 0 END) as gross_profit,
            SUM(CASE WHEN pnl < 0 THEN ABS(pnl) ELSE 0 END) as gross_loss,
            SUM(pnl) as net_pnl,
            AVG(CASE WHEN pnl > 0 THEN pnl ELSE NULL END) as avg_win,
            AVG(CASE WHEN pnl < 0 THEN pnl ELSE NULL END) as avg_loss,
            MAX(pnl) as largest_win,
            MIN(pnl) as largest_loss
        FROM trades WHERE exit_price IS NOT NULL
    """).fetchone()

    total = row["total_trades"] or 0
    wins = row["winning_trades"] or 0
    gross_profit = row["gross_profit"] or 0
    gross_loss = row["gross_loss"] or 0.001  # Avoid divide by zero

    # Stats by regime
    regime_rows = conn.execute("""
        SELECT
            regime,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl) as pnl
        FROM trades
        WHERE exit_price IS NOT NULL AND regime IS NOT NULL
        GROUP BY regime
    """).fetchall()

    # Stats by pattern
    pattern_rows = conn.execute("""
        SELECT
            pattern,
            COUNT(*) as trades,
            SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
            SUM(pnl) as pnl
        FROM trades
        WHERE exit_price IS NOT NULL
        GROUP BY pattern
    """).fetchall()

    conn.close()

    return {
        "total_trades": total,
        "winning_trades": wins,
        "losing_trades": row["losing_trades"] or 0,
        "win_rate": wins / total if total > 0 else 0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else 0,
        "net_pnl": row["net_pnl"] or 0,
        "avg_win": row["avg_win"] or 0,
        "avg_loss": row["avg_loss"] or 0,
        "largest_win": row["largest_win"] or 0,
        "largest_loss": row["largest_loss"] or 0,
        "by_regime": [dict(r) for r in regime_rows],
        "by_pattern": [dict(r) for r in pattern_rows],
    }


def get_equity_curve(backtest_id: int = None) -> List[Dict]:
    """Get equity curve data (running P&L over time)."""
    conn = get_connection()
    if backtest_id:
        rows = conn.execute("""
            SELECT exit_time, pnl, running_equity
            FROM trades
            WHERE backtest_id = ? AND exit_time IS NOT NULL
            ORDER BY exit_time
        """, (backtest_id,)).fetchall()
    else:
        rows = conn.execute("""
            SELECT t.exit_time, t.pnl, t.running_equity, b.date
            FROM trades t
            JOIN backtests b ON t.backtest_id = b.id
            WHERE t.exit_time IS NOT NULL
            ORDER BY b.date, t.exit_time
        """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_max_drawdown() -> Dict:
    """Calculate maximum drawdown from equity curve."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT t.exit_time, t.pnl, t.running_equity, b.date
        FROM trades t
        JOIN backtests b ON t.backtest_id = b.id
        WHERE t.exit_time IS NOT NULL
        ORDER BY b.date, t.exit_time
    """).fetchall()
    conn.close()

    if not rows:
        return {"max_drawdown": 0, "max_drawdown_pct": 0}

    # Calculate running equity and drawdown
    peak = 0
    max_dd = 0
    max_dd_pct = 0
    cumulative = 0

    for row in rows:
        cumulative += row["pnl"]
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd / peak if peak > 0 else 0

    return {
        "max_drawdown": max_dd,
        "max_drawdown_pct": max_dd_pct,
        "peak_equity": peak,
        "final_equity": cumulative
    }


def get_total_spending() -> Dict:
    """Get total spending summary."""
    conn = get_connection()
    row = conn.execute("""
        SELECT
            COUNT(*) as total_runs,
            SUM(ticks) as total_ticks,
            SUM(estimated_cost) as total_cost
        FROM spending
    """).fetchone()
    conn.close()

    return {
        "total_runs": row["total_runs"] or 0,
        "total_ticks": row["total_ticks"] or 0,
        "total_cost": row["total_cost"] or 0,
        "budget": 110.0,
        "remaining": 110.0 - (row["total_cost"] or 0)
    }


def get_backtest_summary() -> Dict:
    """Get summary of all backtests."""
    conn = get_connection()
    row = conn.execute("""
        SELECT
            COUNT(*) as total_backtests,
            SUM(ticks) as total_ticks,
            SUM(estimated_cost) as total_cost,
            SUM(signals_generated) as total_signals,
            SUM(signals_approved) as total_approved,
            SUM(trades) as total_trades,
            SUM(wins) as total_wins,
            SUM(losses) as total_losses,
            SUM(pnl) as total_pnl
        FROM backtests
    """).fetchone()
    conn.close()

    total_trades = row["total_trades"] or 0
    total_wins = row["total_wins"] or 0

    return {
        "total_backtests": row["total_backtests"] or 0,
        "total_ticks": row["total_ticks"] or 0,
        "total_cost": row["total_cost"] or 0,
        "total_signals": row["total_signals"] or 0,
        "total_approved": row["total_approved"] or 0,
        "total_trades": total_trades,
        "total_wins": total_wins,
        "total_losses": row["total_losses"] or 0,
        "overall_win_rate": total_wins / total_trades if total_trades > 0 else 0,
        "total_pnl": row["total_pnl"] or 0,
    }


def get_all_backtests() -> List[Dict]:
    """Get all backtest records."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT * FROM backtests ORDER BY run_at DESC
    """).fetchall()
    conn.close()

    return [dict(row) for row in rows]


def print_summary():
    """Print a formatted summary of backtests and spending."""
    spending = get_total_spending()
    summary = get_backtest_summary()

    print("\n" + "=" * 60)
    print("BACKTEST DATABASE SUMMARY")
    print("=" * 60)

    print(f"\nSpending:")
    print(f"  Total runs: {spending['total_runs']}")
    print(f"  Total ticks: {spending['total_ticks']:,}")
    print(f"  Estimated cost: ${spending['total_cost']:.2f}")
    print(f"  Budget: ${spending['budget']:.2f}")
    print(f"  Remaining: ${spending['remaining']:.2f}")

    print(f"\nResults:")
    print(f"  Total backtests: {summary['total_backtests']}")
    print(f"  Total signals: {summary['total_signals']} generated, {summary['total_approved']} approved")
    print(f"  Total trades: {summary['total_trades']}")
    print(f"  Win rate: {summary['overall_win_rate']:.1%}")
    print(f"  Total P&L: ${summary['total_pnl']:.2f}")

    # Recent backtests
    backtests = get_all_backtests()
    if backtests:
        print(f"\nRecent backtests:")
        print(f"{'Date':<12} {'Time':<13} {'Ticks':>10} {'Cost':>8} {'Trades':>7} {'Win%':>6} {'P&L':>10}")
        print("-" * 70)
        for bt in backtests[:10]:
            print(f"{bt['date']:<12} {bt['start_time']}-{bt['end_time']:<5} {bt['ticks']:>10,} ${bt['estimated_cost']:>6.2f} {bt['trades']:>7} {bt['win_rate']:>5.0%} ${bt['pnl']:>9.2f}")


if __name__ == "__main__":
    print_summary()
