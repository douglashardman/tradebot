#!/usr/bin/env python3
"""Run backtests on multiple days from a file."""

import subprocess
import sys
import os

def run_backtest(date: str) -> dict:
    """Run backtest for a single date and return result."""
    result = subprocess.run(
        ["python3", "scripts/run_databento_backtest.py", "--date", date],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PYTHONPATH": "/home/faded-vibes/tradebot"}
    )

    # Parse the output for key metrics
    output = result.stdout + result.stderr

    # Extract P&L from the line that shows the date
    pnl = 0
    trades = 0
    win_rate = 0
    for line in output.split("\n"):
        if date in line and "$" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p.startswith("$") and i > 0:
                    try:
                        pnl = float(p.replace("$", "").replace(",", ""))
                    except:
                        pass
                if "%" in p and i > 0:
                    try:
                        win_rate = float(p.replace("%", ""))
                    except:
                        pass
            # Find trades count
            for i, p in enumerate(parts):
                try:
                    if i > 3 and parts[i+1].endswith("%"):
                        trades = int(p)
                        break
                except:
                    pass

    return {
        "date": date,
        "pnl": pnl,
        "trades": trades,
        "win_rate": win_rate,
        "success": result.returncode == 0
    }

def main():
    # Read dates from file
    with open("/tmp/new_60_days.txt") as f:
        dates = [line.strip() for line in f if line.strip()]

    print(f"Processing {len(dates)} days...")
    print("=" * 70)

    results = []
    total_pnl = 0
    total_trades = 0
    winning_days = 0

    for i, date in enumerate(dates):
        print(f"[{i+1}/{len(dates)}] {date}...", end=" ", flush=True)
        try:
            result = run_backtest(date)
            results.append(result)
            total_pnl += result["pnl"]
            total_trades += result["trades"]
            if result["pnl"] > 0:
                winning_days += 1

            status = "WIN" if result["pnl"] > 0 else "LOSS" if result["pnl"] < 0 else "FLAT"
            print(f"{result['trades']:>3} trades, {result['win_rate']:>5.0f}% win, ${result['pnl']:>8,.0f} [{status}]  Running: ${total_pnl:,.0f}")

        except Exception as e:
            print(f"ERROR: {e}")
            results.append({"date": date, "pnl": 0, "trades": 0, "win_rate": 0, "success": False})

    print("=" * 70)
    print(f"\nBATCH SUMMARY ({len(dates)} days):")
    print(f"  Total P&L: ${total_pnl:,.0f}")
    print(f"  Total Trades: {total_trades}")
    print(f"  Winning Days: {winning_days}/{len(dates)} ({100*winning_days/len(dates):.0f}%)")
    print(f"  Avg Daily P&L: ${total_pnl/len(dates):,.0f}")

    # Show worst and best days
    sorted_results = sorted(results, key=lambda x: x["pnl"])
    print(f"\nWorst 5 days:")
    for r in sorted_results[:5]:
        print(f"  {r['date']}: ${r['pnl']:,.0f} ({r['trades']} trades)")
    print(f"\nBest 5 days:")
    for r in sorted_results[-5:]:
        print(f"  {r['date']}: ${r['pnl']:,.0f} ({r['trades']} trades)")

if __name__ == "__main__":
    main()
