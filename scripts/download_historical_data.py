#!/usr/bin/env python3
"""
Download historical tick data from Databento for backtesting.

Downloads 60 days of ESM5 data (March-May 2025), with MES for first 5 days.
"""

import os
import sys
import json
from datetime import datetime, timedelta

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.data.adapters.databento import DatabentoAdapter

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")


def get_trading_days(start_date: str, num_days: int) -> list[str]:
    """Generate list of trading days (skip weekends)."""
    days = []
    current = datetime.strptime(start_date, "%Y-%m-%d")

    while len(days) < num_days:
        # Skip weekends
        if current.weekday() < 5:  # Monday = 0, Friday = 4
            days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return days


def download_session(contract: str, date: str, start_time: str = "09:30", end_time: str = "16:00"):
    """Download a single session and cache it."""
    os.makedirs(CACHE_DIR, exist_ok=True)

    safe_start = start_time.replace(":", "")
    safe_end = end_time.replace(":", "")
    cache_path = os.path.join(CACHE_DIR, f"{contract}_{date}_{safe_start}_{safe_end}.json")

    # Check if already cached
    if os.path.exists(cache_path):
        print(f"  CACHED: {contract} {date} - skipping")
        return True

    print(f"  DOWNLOADING: {contract} {date} {start_time}-{end_time}...")

    try:
        adapter = DatabentoAdapter()
        ticks = adapter.get_session_ticks(
            contract=contract,
            date=date,
            start_time=start_time,
            end_time=end_time
        )

        if not ticks:
            print(f"    WARNING: No data returned for {contract} {date}")
            return False

        # Cache the data
        data = [
            {
                "timestamp": t.timestamp.isoformat(),
                "price": t.price,
                "volume": t.volume,
                "side": t.side,
                "symbol": t.symbol
            }
            for t in ticks
        ]

        with open(cache_path, "w") as f:
            json.dump(data, f)

        print(f"    Cached {len(ticks):,} ticks")
        return True

    except Exception as e:
        print(f"    ERROR: {e}")
        return False


def main():
    print("=" * 60)
    print("Historical Data Download - ESM5 (June 2025 contract)")
    print("=" * 60)
    print()

    # Generate 60 trading days starting March 10, 2025
    # ESM5 is the June contract, valid mid-contract from ~March 10 to ~June 6
    start_date = "2025-03-10"
    trading_days = get_trading_days(start_date, 60)

    print(f"Date range: {trading_days[0]} to {trading_days[-1]}")
    print(f"Total trading days: {len(trading_days)}")
    print()

    # First 5 days: Download both ES and MES
    es_contract = "ESM5"
    mes_contract = "MESM5"

    print("Phase 1: First 5 days with BOTH ES and MES")
    print("-" * 40)

    for date in trading_days[:5]:
        download_session(es_contract, date)
        download_session(mes_contract, date)

    print()
    print("Phase 2: Remaining 55 days with ES only")
    print("-" * 40)

    for date in trading_days[5:]:
        download_session(es_contract, date)

    print()
    print("=" * 60)
    print("Download complete!")
    print("=" * 60)

    # Show what we downloaded
    print()
    print("Summary:")
    print(f"  ES days: {len(trading_days)} (ESM5)")
    print(f"  MES days: 5 (MESM5)")
    print(f"  Date range: {trading_days[0]} to {trading_days[-1]}")


if __name__ == "__main__":
    main()
