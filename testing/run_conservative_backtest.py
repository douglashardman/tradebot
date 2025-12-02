#!/usr/bin/env python3
"""
Run backtest on all cached days with CONSERVATIVE FILLS.

Conservative fills = require price to go 1 tick BEYOND target for fills.
This simulates being last in the order queue.
"""

import os
import sys
import re

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, time
from scripts.run_databento_backtest import run_backtest, get_cache_path
from src.data.backtest_db import get_total_spending, print_summary, get_trade_analysis, get_max_drawdown
from src.data.adapters.databento import DatabentoAdapter

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")


def get_cached_sessions():
    """Get all cached sessions from tick_cache directory."""
    sessions = []

    for filename in os.listdir(CACHE_DIR):
        if not filename.endswith('.json'):
            continue

        # Parse filename: ESU5_2025-07-01_0930_1600.json
        match = re.match(r'(\w+)_(\d{4}-\d{2}-\d{2})_(\d{4})_(\d{4})\.json', filename)
        if match:
            contract, date, start, end = match.groups()
            start_time = f"{start[:2]}:{start[2:]}"
            end_time = f"{end[:2]}:{end[2:]}"
            sessions.append({
                'contract': contract,
                'date': date,
                'start_time': start_time,
                'end_time': end_time,
                'filename': filename
            })

    # Sort by date
    sessions.sort(key=lambda x: x['date'])
    return sessions


def main():
    print("=" * 70)
    print("CONSERVATIVE FILLS BACKTEST")
    print("Require price to go 1 tick BEYOND target for fills")
    print("(Simulates being last in the order queue)")
    print("=" * 70)

    sessions = get_cached_sessions()
    print(f"\nFound {len(sessions)} cached sessions")

    # Results tracking
    results = []
    total_pnl = 0
    total_trades = 0
    winning_days = 0
    losing_days = 0

    for i, session in enumerate(sessions):
        print(f"\n[{i+1}/{len(sessions)}] Processing {session['date']}...")

        try:
            result = run_backtest(
                contract=session['contract'],
                date=session['date'],
                start_time=session['start_time'],
                end_time=session['end_time'],
                budget_remaining=999999,  # Using cached data, no cost
                conservative_fills=True   # THE KEY SETTING
            )

            if result:
                results.append(result)
                pnl = result.get('pnl', 0)
                trades = result.get('trades', 0)
                total_pnl += pnl
                total_trades += trades

                if pnl > 0:
                    winning_days += 1
                elif pnl < 0:
                    losing_days += 1

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()

    # Final summary
    print("\n" + "=" * 70)
    print("CONSERVATIVE FILLS - FINAL RESULTS")
    print("=" * 70)

    days_tested = len(results)
    flat_days = days_tested - winning_days - losing_days

    print(f"\nDays tested: {days_tested}")
    print(f"Winning days: {winning_days} ({100*winning_days/days_tested:.1f}%)")
    print(f"Losing days: {losing_days} ({100*losing_days/days_tested:.1f}%)")
    print(f"Flat days: {flat_days}")
    print(f"\nTotal trades: {total_trades}")
    print(f"Avg trades/day: {total_trades/days_tested:.1f}")
    print(f"\nTotal P&L: ${total_pnl:,.2f}")
    print(f"Avg daily P&L: ${total_pnl/days_tested:,.2f}")

    # Calculate win rate from results
    wins = sum(r.get('wins', 0) for r in results)
    losses = sum(r.get('losses', 0) for r in results)
    if wins + losses > 0:
        print(f"\nTrade win rate: {100*wins/(wins+losses):.1f}%")

    # Best and worst days
    sorted_results = sorted(results, key=lambda x: x.get('pnl', 0))

    print(f"\nWorst 5 days:")
    for r in sorted_results[:5]:
        print(f"  {r['date']}: ${r.get('pnl', 0):,.2f} ({r.get('trades', 0)} trades)")

    print(f"\nBest 5 days:")
    for r in sorted_results[-5:]:
        print(f"  {r['date']}: ${r.get('pnl', 0):,.2f} ({r.get('trades', 0)} trades)")

    # Compare to previous results
    print("\n" + "=" * 70)
    print("COMPARISON NOTES")
    print("=" * 70)
    print("""
Previous backtest (optimistic fills): $187,900 over 110 days
This test requires price to penetrate targets by 1 tick.

The difference shows how many "lucky" fills we assumed before.
A realistic expectation is somewhere between the two numbers.
""")


if __name__ == "__main__":
    main()
