#!/usr/bin/env python3
"""
Fetch MES tick data directly from Databento for comparison testing.

This fetches the official Databento data for a given date, independent of
what Bishop recorded, so we can triangulate:
1. Bishop's recorded ticks
2. Databento's official ticks
3. Bishop's live trading result

Usage:
    PYTHONPATH=. python testing/databento_comparison/fetch_databento_ticks.py --date 2025-12-03
"""

import argparse
import json
import os
import sys
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.data.adapters.databento import DatabentoAdapter

# Output directory
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))


def fetch_and_save(date: str, start_time: str = "09:30", end_time: str = "16:00"):
    """Fetch ticks from Databento and save to JSON cache."""

    adapter = DatabentoAdapter()

    # Get front month contract for MES
    contract = DatabentoAdapter.get_front_month_contract("MES", date)
    print(f"Fetching {contract} for {date} ({start_time} - {end_time} ET)...")

    # Fetch ticks
    ticks = adapter.get_session_ticks(
        contract=contract,
        date=date,
        start_time=start_time,
        end_time=end_time,
    )

    if not ticks:
        print("No ticks returned!")
        return None

    print(f"Fetched {len(ticks):,} ticks from Databento")

    # Save to JSON for comparison
    safe_start = start_time.replace(":", "")
    safe_end = end_time.replace(":", "")
    output_path = os.path.join(OUTPUT_DIR, f"databento_{contract}_{date}_{safe_start}_{safe_end}.json")

    tick_data = []
    for tick in ticks:
        tick_data.append({
            "timestamp": tick.timestamp.isoformat(),
            "price": tick.price,
            "volume": tick.volume,
            "side": tick.side,
            "symbol": tick.symbol,
        })

    with open(output_path, "w") as f:
        json.dump(tick_data, f)

    print(f"Saved to {output_path}")

    # Print summary stats
    prices = [t.price for t in ticks]
    print(f"\nSummary:")
    print(f"  Ticks: {len(ticks):,}")
    print(f"  First: {ticks[0].timestamp}")
    print(f"  Last:  {ticks[-1].timestamp}")
    print(f"  High:  {max(prices):.2f}")
    print(f"  Low:   {min(prices):.2f}")
    print(f"  Open:  {prices[0]:.2f}")
    print(f"  Close: {prices[-1]:.2f}")

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Fetch Databento MES ticks for comparison")
    parser.add_argument("--date", required=True, help="Date to fetch (YYYY-MM-DD)")
    parser.add_argument("--start", default="09:30", help="Start time ET (default: 09:30)")
    parser.add_argument("--end", default="16:00", help="End time ET (default: 16:00)")
    args = parser.parse_args()

    fetch_and_save(args.date, args.start, args.end)


if __name__ == "__main__":
    main()
