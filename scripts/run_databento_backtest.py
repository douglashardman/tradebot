#!/usr/bin/env python3
"""
Backtest using Databento ES futures tick data.

Tracks spending against budget and logs results to SQLite database.
"""

import argparse
import os
import sys
from datetime import datetime, time

from src.core.types import Tick, Signal, FootprintBar
from src.data.adapters.databento import DatabentoAdapter
from src.data.backtest_db import log_backtest, get_total_spending, print_summary
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession


def run_backtest(
    contract: str,
    date: str,
    start_time: str,
    end_time: str,
    budget_remaining: float
) -> dict:
    """
    Run a single backtest using Databento data.

    Returns dict with results or None if budget exceeded.
    """
    # Estimate cost before fetching
    # RTH (6.5 hours) = ~750k ticks = ~$1.20
    # Calculate hours
    start_h, start_m = map(int, start_time.split(":"))
    end_h, end_m = map(int, end_time.split(":"))
    hours = (end_h + end_m/60) - (start_h + start_m/60)
    estimated_ticks = int(hours * 115000)  # ~115k ticks/hour
    estimated_cost = (estimated_ticks / 1_000_000) * 1.60

    print(f"\n{'='*60}")
    print(f"Backtest: {contract} on {date} ({start_time}-{end_time})")
    print(f"Estimated: {estimated_ticks:,} ticks, ${estimated_cost:.2f}")
    print(f"Budget remaining: ${budget_remaining:.2f}")
    print(f"{'='*60}")

    if estimated_cost > budget_remaining:
        print(f"SKIPPING: Would exceed budget (${estimated_cost:.2f} > ${budget_remaining:.2f})")
        return None

    # Determine symbol from contract
    symbol = contract[:2] if contract[:3] not in ["MES", "MNQ"] else contract[:3]

    # Fetch data
    adapter = DatabentoAdapter()
    ticks = adapter.get_session_ticks(
        contract=contract,
        date=date,
        start_time=start_time,
        end_time=end_time
    )

    if not ticks:
        print("No data returned")
        return None

    print(f"Fetched {len(ticks):,} ticks")

    # Setup components
    engine = OrderFlowEngine({
        "symbol": symbol,
        "timeframe": 300,  # 5-minute bars
    })

    router = StrategyRouter({
        "min_signal_strength": 0.60,
        "min_regime_confidence": 0.50,
        "session_open": time(9, 30),
        "session_close": time(16, 0),
        "regime": {
            "min_regime_score": 3.5,
            "adx_trend_threshold": 25,
        },
    })

    session = TradingSession(
        mode="paper",
        symbol=symbol,
        daily_profit_target=500.0,
        daily_loss_limit=-300.0,
        max_position_size=1,
        max_concurrent_trades=1,
        stop_loss_ticks=16,  # 4 points for ES
        take_profit_ticks=24,  # 6 points for ES
        trading_start=time(9, 30),
        trading_end=time(16, 0),
    )
    session.started_at = datetime.now()
    session.is_within_trading_hours = lambda: True  # Override for backtesting

    manager = ExecutionManager(session)

    # Stats
    signals_generated = 0
    signals_approved = 0
    trades = []

    def on_bar(bar: FootprintBar):
        router.on_bar(bar)
        if bar.close_price:
            manager.update_prices(bar.close_price)

    def on_signal(signal: Signal):
        nonlocal signals_generated, signals_approved
        signals_generated += 1

        signal = router.evaluate_signal(signal)

        if signal.approved:
            signals_approved += 1
            if not manager.pending_orders and not manager.open_positions:
                if signal.strength >= 0.60:
                    multiplier = router.get_position_size_multiplier()
                    order = manager.on_signal(signal, multiplier)
                    if order:
                        trades.append({
                            "pattern": signal.pattern.value if hasattr(signal.pattern, 'value') else str(signal.pattern),
                            "direction": signal.direction,
                            "price": signal.price,
                            "strength": signal.strength,
                        })
                        print(f"  TRADE #{len(trades)}: {order.side} @ {order.entry_price:.2f}")

    engine.on_bar(on_bar)
    engine.on_signal(on_signal)

    # Process ticks
    for tick in ticks:
        engine.process_tick(tick)

    # Get results
    stats = manager.get_statistics()
    state = manager.get_state()

    wins = stats.get("winning_trades", 0)
    losses = stats.get("losing_trades", 0)
    pnl = state.get("daily_pnl", 0)

    result = {
        "symbol": symbol,
        "contract": contract,
        "date": date,
        "start_time": start_time,
        "end_time": end_time,
        "ticks": len(ticks),
        "signals_generated": signals_generated,
        "signals_approved": signals_approved,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "pnl": pnl,
    }

    # Log to database
    log_backtest(
        symbol=symbol,
        contract=contract,
        date=date,
        start_time=start_time,
        end_time=end_time,
        ticks=len(ticks),
        signals_generated=signals_generated,
        signals_approved=signals_approved,
        trades=len(trades),
        wins=wins,
        losses=losses,
        pnl=pnl
    )

    print(f"\nResults:")
    print(f"  Ticks: {len(ticks):,}")
    print(f"  Signals: {signals_generated} generated, {signals_approved} approved")
    print(f"  Trades: {len(trades)} (W: {wins}, L: {losses})")
    print(f"  P&L: ${pnl:.2f}")

    return result


def main():
    parser = argparse.ArgumentParser(description="Databento ES backtest runner")
    parser.add_argument("--date", type=str, help="Single date to backtest (YYYY-MM-DD)")
    parser.add_argument("--start", type=str, default="09:30", help="Start time (HH:MM)")
    parser.add_argument("--end", type=str, default="16:00", help="End time (HH:MM)")
    parser.add_argument("--contract", type=str, help="Contract symbol (e.g., ESZ5)")
    parser.add_argument("--batch", action="store_true", help="Run batch of predefined dates")
    parser.add_argument("--summary", action="store_true", help="Show spending summary only")

    args = parser.parse_args()

    if args.summary:
        print_summary()
        return

    # Check budget
    spending = get_total_spending()
    budget_remaining = spending["remaining"]

    if budget_remaining <= 0:
        print(f"Budget exhausted! Spent: ${spending['total_cost']:.2f}")
        print_summary()
        return

    print(f"Budget: ${spending['budget']:.2f}")
    print(f"Spent: ${spending['total_cost']:.2f}")
    print(f"Remaining: ${budget_remaining:.2f}")

    if args.batch:
        # Random selection of dates across different periods
        # Mix of RTH, pre-market, after-hours
        batch_tests = [
            # RTH sessions (9:30-16:00 ET)
            ("2025-11-24", "09:30", "12:00"),  # Monday morning
            ("2025-11-25", "13:00", "16:00"),  # Tuesday afternoon
            ("2025-11-20", "09:30", "11:30"),  # Last Thursday morning
            ("2025-11-19", "14:00", "16:00"),  # Wednesday afternoon
            ("2025-11-18", "09:30", "10:30"),  # Monday open (1 hour)

            # Pre-market / overnight (CME Globex)
            ("2025-11-25", "06:00", "08:00"),  # Pre-market
            ("2025-11-24", "18:00", "20:00"),  # Evening session

            # Volatility events
            ("2025-11-06", "09:30", "12:00"),  # Post-election
            ("2025-11-07", "09:30", "11:00"),  # FOMC day

            # October dates
            ("2025-10-30", "09:30", "12:00"),  # Wednesday
            ("2025-10-15", "13:00", "16:00"),  # Tuesday afternoon
            ("2025-10-07", "09:30", "11:00"),  # Monday morning
        ]

        results = []
        for date, start, end in batch_tests:
            spending = get_total_spending()
            if spending["remaining"] <= 0:
                print("\nBudget exhausted!")
                break

            # Get appropriate contract for the date
            contract = DatabentoAdapter.get_front_month_contract("ES", date)

            result = run_backtest(
                contract=contract,
                date=date,
                start_time=start,
                end_time=end,
                budget_remaining=spending["remaining"]
            )
            if result:
                results.append(result)

        # Final summary
        print_summary()

    elif args.date:
        contract = args.contract or DatabentoAdapter.get_front_month_contract("ES", args.date)
        run_backtest(
            contract=contract,
            date=args.date,
            start_time=args.start,
            end_time=args.end,
            budget_remaining=budget_remaining
        )
        print_summary()

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
