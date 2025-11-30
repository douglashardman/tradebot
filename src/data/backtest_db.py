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
    notes: str = None
) -> int:
    """
    Log a backtest run to the database.

    Cost estimate: ~$0.0016 per 1000 ticks ($1.60 per million)
    """
    estimated_cost = (ticks / 1_000_000) * 1.60  # ~$1.60 per million ticks
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

    # Also log to spending table
    conn.execute("""
        INSERT INTO spending (backtest_id, ticks, estimated_cost, description)
        VALUES (?, ?, ?, ?)
    """, (backtest_id, ticks, estimated_cost, f"{contract} {date} {start_time}-{end_time}"))

    conn.commit()
    conn.close()

    return backtest_id


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
