#!/usr/bin/env python3
"""
Test different daily loss limits and analyze win/loss streaks.

Answers: "How many times would I fail before I succeed?"
"""

import os
import sys
import re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.run_databento_backtest import run_backtest

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")


def get_cached_sessions():
    """Get all cached sessions from tick_cache directory."""
    sessions = []
    for filename in os.listdir(CACHE_DIR):
        if not filename.endswith('.json'):
            continue
        match = re.match(r'(\w+)_(\d{4}-\d{2}-\d{2})_(\d{4})_(\d{4})\.json', filename)
        if match:
            contract, date, start, end = match.groups()
            sessions.append({
                'contract': contract,
                'date': date,
                'start_time': f"{start[:2]}:{start[2:]}",
                'end_time': f"{end[:2]}:{end[2:]}",
            })
    sessions.sort(key=lambda x: x['date'])
    return sessions


def analyze_streaks(results):
    """Analyze winning and losing streaks."""
    streaks = []
    current_streak = 0
    streak_type = None

    for r in results:
        pnl = r.get('pnl', 0)
        if pnl > 0:
            day_type = 'win'
        elif pnl < 0:
            day_type = 'loss'
        else:
            day_type = 'flat'
            continue  # Skip flat days for streak counting

        if streak_type == day_type:
            current_streak += 1
        else:
            if streak_type is not None:
                streaks.append((streak_type, current_streak))
            streak_type = day_type
            current_streak = 1

    # Don't forget the last streak
    if streak_type is not None:
        streaks.append((streak_type, current_streak))

    return streaks


def main():
    daily_loss_limit = -300.0  # THE KEY SETTING

    print("=" * 70)
    print(f"BACKTEST WITH ${abs(daily_loss_limit):.0f} DAILY LOSS LIMIT")
    print("Conservative fills + $300 max daily loss")
    print("=" * 70)

    sessions = get_cached_sessions()
    print(f"\nTesting {len(sessions)} trading days...")
    print()

    results = []
    total_pnl = 0
    running_balance = 10000  # Starting balance
    min_balance = running_balance
    max_balance = running_balance

    for i, session in enumerate(sessions):
        result = run_backtest(
            contract=session['contract'],
            date=session['date'],
            start_time=session['start_time'],
            end_time=session['end_time'],
            budget_remaining=999999,
            conservative_fills=True,
            daily_loss_limit=daily_loss_limit
        )

        if result:
            pnl = result.get('pnl', 0)
            trades = result.get('trades', 0)
            wins = result.get('wins', 0)
            losses = result.get('losses', 0)

            total_pnl += pnl
            running_balance += pnl
            min_balance = min(min_balance, running_balance)
            max_balance = max(max_balance, running_balance)

            result['running_balance'] = running_balance
            results.append(result)

            # Status indicator
            if pnl > 0:
                status = "WIN "
            elif pnl < 0:
                status = "LOSS"
            else:
                status = "FLAT"

            hit_limit = pnl <= daily_loss_limit + 50  # Within $50 of limit
            limit_marker = " [HIT LIMIT]" if hit_limit else ""

            print(f"[{i+1:3d}] {session['date']} | {status} | {trades:2d} trades | "
                  f"W:{wins:2d} L:{losses:2d} | ${pnl:+8.0f} | "
                  f"Balance: ${running_balance:,.0f}{limit_marker}")

    # Analyze streaks
    streaks = analyze_streaks(results)

    # Calculate stats
    winning_days = sum(1 for r in results if r.get('pnl', 0) > 0)
    losing_days = sum(1 for r in results if r.get('pnl', 0) < 0)
    flat_days = sum(1 for r in results if r.get('pnl', 0) == 0)

    losing_streaks = [s[1] for s in streaks if s[0] == 'loss']
    winning_streaks = [s[1] for s in streaks if s[0] == 'win']

    max_losing_streak = max(losing_streaks) if losing_streaks else 0
    max_winning_streak = max(winning_streaks) if winning_streaks else 0
    avg_losing_streak = sum(losing_streaks) / len(losing_streaks) if losing_streaks else 0
    avg_winning_streak = sum(winning_streaks) / len(winning_streaks) if winning_streaks else 0

    # Days that hit the $300 loss limit
    limit_hit_days = sum(1 for r in results if r.get('pnl', 0) <= daily_loss_limit + 50)

    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    print(f"\n--- Performance ---")
    print(f"Days tested:      {len(results)}")
    print(f"Total P&L:        ${total_pnl:,.0f}")
    print(f"Avg daily P&L:    ${total_pnl/len(results):,.0f}")
    print(f"Starting balance: $10,000")
    print(f"Final balance:    ${running_balance:,.0f}")
    print(f"Peak balance:     ${max_balance:,.0f}")
    print(f"Low balance:      ${min_balance:,.0f}")
    print(f"Max drawdown:     ${max_balance - min_balance:,.0f}")

    print(f"\n--- Win/Loss Days ---")
    print(f"Winning days:     {winning_days} ({100*winning_days/len(results):.1f}%)")
    print(f"Losing days:      {losing_days} ({100*losing_days/len(results):.1f}%)")
    print(f"Flat days:        {flat_days}")
    print(f"Hit loss limit:   {limit_hit_days} days")

    print(f"\n--- Streak Analysis ---")
    print(f"Max losing streak:  {max_losing_streak} days")
    print(f"Avg losing streak:  {avg_losing_streak:.1f} days")
    print(f"Max winning streak: {max_winning_streak} days")
    print(f"Avg winning streak: {avg_winning_streak:.1f} days")

    # Show all losing streaks
    print(f"\n--- All Losing Streaks ---")
    loss_streak_counts = {}
    for streak in losing_streaks:
        loss_streak_counts[streak] = loss_streak_counts.get(streak, 0) + 1
    for length in sorted(loss_streak_counts.keys()):
        count = loss_streak_counts[length]
        print(f"  {length}-day losing streak: {count} time(s)")

    # Recovery analysis
    print(f"\n--- Recovery Analysis ---")
    print(f"If you lose $300, how many winning days to recover?")
    avg_win = sum(r.get('pnl', 0) for r in results if r.get('pnl', 0) > 0) / winning_days if winning_days > 0 else 0
    avg_loss = sum(r.get('pnl', 0) for r in results if r.get('pnl', 0) < 0) / losing_days if losing_days > 0 else 0
    print(f"  Avg winning day: ${avg_win:,.0f}")
    print(f"  Avg losing day:  ${avg_loss:,.0f}")
    print(f"  Days to recover $300 loss: {abs(300/avg_win):.1f} winning days" if avg_win > 0 else "  N/A")

    # Expectancy
    win_rate = winning_days / (winning_days + losing_days) if (winning_days + losing_days) > 0 else 0
    expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)
    print(f"\n--- Expectancy ---")
    print(f"  Win rate: {100*win_rate:.1f}%")
    print(f"  Expectancy per day: ${expectancy:,.0f}")

    print("\n" + "=" * 70)
    print("KEY TAKEAWAY")
    print("=" * 70)
    if max_losing_streak <= 2:
        print(f"\nWith a ${abs(daily_loss_limit):.0f} daily loss limit:")
        print(f"  - Max consecutive losing days: {max_losing_streak}")
        print(f"  - You'd need ${max_losing_streak * abs(daily_loss_limit):,.0f} to survive worst streak")
        print(f"  - Typical recovery: {abs(daily_loss_limit)/avg_win:.1f} winning days")
    else:
        print(f"\nWith a ${abs(daily_loss_limit):.0f} daily loss limit:")
        print(f"  - Max consecutive losing days: {max_losing_streak}")
        print(f"  - Worst case drawdown: ${max_losing_streak * abs(daily_loss_limit):,.0f}")
        print(f"  - But you recover because win rate is {100*win_rate:.0f}%")


if __name__ == "__main__":
    main()
