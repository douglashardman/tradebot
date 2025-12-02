#!/usr/bin/env python3
"""
Daily Recap Generator

Generates a daily summary of trading activity for the dev server.
Includes session stats, signals detected, trades taken, and key events.

Run after market close or alongside tick export at 11:01 PM ET.
"""

import json
import os
import subprocess
import sys
from datetime import datetime, date
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.data.live_db import (
    get_session_by_date,
    get_trades_for_session,
    get_orders_for_session,
)

# Configuration
TRADEBOT_DIR = Path("/opt/tradebot")
DATA_DIR = TRADEBOT_DIR / "data"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"
RECAP_DIR = DATA_DIR / "recaps"
EXPORT_HOST = os.environ.get("TICK_EXPORT_HOST", "99.69.168.225")
EXPORT_USER = os.environ.get("TICK_EXPORT_USER", "faded-vibes")
EXPORT_PATH = os.environ.get("TICK_EXPORT_PATH", "/home/faded-vibes/tradebot/data/tick_cache")
EXPORT_KEY = os.environ.get("TICK_EXPORT_KEY", "/home/tradebot/.ssh/tradebot_sync")


def get_signal_log(today_str: str) -> list:
    """Extract signals from journalctl for today."""
    signals = []
    try:
        result = subprocess.run(
            ["journalctl", "-u", "tradebot", "--since", f"{today_str} 00:00:00",
             "--until", f"{today_str} 23:59:59", "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=30
        )
        for line in result.stdout.split("\n"):
            if "Signal detected:" in line:
                signals.append(line.strip())
            elif "Signal rejected:" in line:
                signals.append(line.strip())
    except Exception as e:
        signals.append(f"Error reading signals: {e}")
    return signals


def get_bar_log(today_str: str) -> list:
    """Extract completed bars from journalctl for today."""
    bars = []
    try:
        result = subprocess.run(
            ["journalctl", "-u", "tradebot", "--since", f"{today_str} 00:00:00",
             "--until", f"{today_str} 23:59:59", "--no-pager", "-o", "cat"],
            capture_output=True,
            text=True,
            timeout=30
        )
        for line in result.stdout.split("\n"):
            if "Bar complete:" in line:
                bars.append(line.strip())
    except Exception as e:
        bars.append(f"Error reading bars: {e}")
    return bars


def get_errors_warnings(today_str: str) -> list:
    """Extract errors and warnings from journalctl for today."""
    issues = []
    try:
        result = subprocess.run(
            ["journalctl", "-u", "tradebot", "--since", f"{today_str} 00:00:00",
             "--until", f"{today_str} 23:59:59", "--no-pager", "-o", "cat",
             "-p", "warning"],
            capture_output=True,
            text=True,
            timeout=30
        )
        for line in result.stdout.split("\n"):
            if line.strip():
                issues.append(line.strip())
    except Exception as e:
        issues.append(f"Error reading logs: {e}")
    return issues[:50]  # Limit to 50 entries


def get_restarts(today_str: str) -> dict:
    """Extract system restart events from journalctl for today.

    Returns dict with:
    - events: list of all startup/shutdown/resume events with timestamps
    - gaps: list of restart gaps showing downtime periods
    """
    events = []
    try:
        result = subprocess.run(
            ["journalctl", "-u", "tradebot", "--since", f"{today_str} 00:00:00",
             "--until", f"{today_str} 23:59:59", "--no-pager"],
            capture_output=True,
            text=True,
            timeout=30
        )
        for line in result.stdout.split("\n"):
            # Capture startup and shutdown events
            if "HEADLESS TRADING SYSTEM STARTUP" in line:
                # Extract timestamp from journalctl line (format: "Dec 02 15:07:31")
                parts = line.split()
                if len(parts) >= 3:
                    time_str = parts[2]  # e.g., "15:07:31"
                    events.append({"time": time_str, "event": "STARTUP"})
            elif "Received shutdown signal" in line:
                parts = line.split()
                if len(parts) >= 3:
                    time_str = parts[2]
                    events.append({"time": time_str, "event": "SHUTDOWN"})
            elif "Resumed session:" in line:
                parts = line.split()
                if len(parts) >= 3:
                    time_str = parts[2]
                    # Extract P&L and trade count from the line
                    pnl_str = None
                    trades_str = None
                    if "P&L=" in line:
                        idx = line.find("P&L=")
                        pnl_str = line[idx:].split(",")[0].replace("P&L=", "")
                    if "trades" in line:
                        # Extract "X trades"
                        import re
                        match = re.search(r'(\d+)\s+trades', line)
                        if match:
                            trades_str = match.group(1)
                    events.append({
                        "time": time_str,
                        "event": "RESUMED",
                        "pnl": pnl_str,
                        "trades": trades_str
                    })
    except Exception as e:
        events.append({"error": str(e)})

    # Calculate gaps between shutdown and next startup
    gaps = []
    last_shutdown = None
    for event in events:
        if event.get("event") == "SHUTDOWN":
            last_shutdown = event.get("time")
        elif event.get("event") == "STARTUP" and last_shutdown:
            # Calculate gap duration
            try:
                from datetime import datetime
                fmt = "%H:%M:%S"
                t1 = datetime.strptime(last_shutdown, fmt)
                t2 = datetime.strptime(event.get("time"), fmt)
                gap_seconds = (t2 - t1).total_seconds()
                gaps.append({
                    "shutdown": last_shutdown,
                    "startup": event.get("time"),
                    "gap_seconds": int(gap_seconds)
                })
            except:
                gaps.append({
                    "shutdown": last_shutdown,
                    "startup": event.get("time"),
                    "gap_seconds": None
                })
            last_shutdown = None

    return {
        "events": events,
        "gaps": gaps,
        "total_restarts": len([e for e in events if e.get("event") == "STARTUP"]),
        "total_downtime_seconds": sum(g.get("gap_seconds", 0) or 0 for g in gaps)
    }


def get_heartbeat() -> dict:
    """Read current heartbeat file."""
    try:
        with open(HEARTBEAT_FILE) as f:
            return json.load(f)
    except Exception as e:
        return {"error": str(e)}


def generate_recap(target_date: date = None) -> dict:
    """Generate complete daily recap."""
    if target_date is None:
        target_date = date.today()

    today_str = target_date.strftime("%Y-%m-%d")

    # Get session from database
    session = get_session_by_date(today_str)

    # Get trades if session exists
    trades = []
    orders = []
    if session:
        trades = get_trades_for_session(session["id"])
        orders = get_orders_for_session(session["id"])

    # Get heartbeat
    heartbeat = get_heartbeat()

    # Get logs
    signals = get_signal_log(today_str)
    bars = get_bar_log(today_str)
    errors = get_errors_warnings(today_str)
    restarts = get_restarts(today_str)

    # Build recap
    recap = {
        "generated_at": datetime.now().isoformat(),
        "date": today_str,
        "server": "production",

        "session": {
            "id": session["id"] if session else None,
            "mode": session["mode"] if session else None,
            "symbol": session["symbol"] if session else None,
            "tier_name": session["tier_name"] if session else None,
            "starting_balance": session["starting_balance"] if session else None,
            "status": session["status"] if session else "NO_SESSION",
        },

        "heartbeat": {
            "tick_count": heartbeat.get("tick_count", 0),
            "bar_count": heartbeat.get("bar_count", 0),
            "signal_count": heartbeat.get("signal_count", 0),
            "trade_count": heartbeat.get("trade_count", 0),
            "daily_pnl": heartbeat.get("daily_pnl", 0.0),
            "open_positions": heartbeat.get("open_positions", 0),
            "feed_connected": heartbeat.get("feed_connected", False),
            "reconnect_count": heartbeat.get("reconnect_count", 0),
            "is_halted": heartbeat.get("is_halted", False),
            "halt_reason": heartbeat.get("halt_reason"),
        },

        "summary": {
            "total_bars": len(bars),
            "total_signals": len(signals),
            "total_trades": len(trades),
            "total_orders": len(orders),
            "errors_warnings": len(errors),
            "restarts": restarts.get("total_restarts", 0),
            "total_downtime_seconds": restarts.get("total_downtime_seconds", 0),
        },

        "restarts": restarts,
        "trades": trades,
        "orders": orders,
        "signals": signals,
        "bars": bars,
        "errors": errors[:20],  # Limit errors in recap
    }

    return recap


def save_recap(recap: dict, target_date: date = None) -> Path:
    """Save recap to local file."""
    if target_date is None:
        target_date = date.today()

    RECAP_DIR.mkdir(parents=True, exist_ok=True)

    filename = f"{target_date.strftime('%Y-%m-%d')}_recap.json"
    filepath = RECAP_DIR / filename

    with open(filepath, "w") as f:
        json.dump(recap, f, indent=2, default=str)

    print(f"Saved recap to {filepath}")
    return filepath


def export_recap(filepath: Path) -> bool:
    """SCP recap to home server."""
    dest = f"{EXPORT_USER}@{EXPORT_HOST}:{EXPORT_PATH}/"

    cmd = ["scp", "-i", EXPORT_KEY, str(filepath), dest]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            print(f"Exported recap to {dest}")
            return True
        else:
            print(f"Export failed: {result.stderr}")
            return False
    except Exception as e:
        print(f"Export error: {e}")
        return False


def print_summary(recap: dict):
    """Print human-readable summary."""
    print("\n" + "=" * 60)
    print(f"DAILY RECAP - {recap['date']}")
    print("=" * 60)

    hb = recap["heartbeat"]
    print(f"\nSession: {recap['session']['status']}")
    print(f"Mode: {recap['session']['mode']} | Symbol: {recap['session']['symbol']}")
    print(f"Tier: {recap['session']['tier_name']}")

    print(f"\nActivity:")
    print(f"  Ticks:    {hb['tick_count']:,}")
    print(f"  Bars:     {hb['bar_count']}")
    print(f"  Signals:  {hb['signal_count']}")
    print(f"  Trades:   {hb['trade_count']}")
    print(f"  P&L:      ${hb['daily_pnl']:.2f}")

    if recap["trades"]:
        print(f"\nTrades:")
        for t in recap["trades"]:
            print(f"  #{t.get('trade_num')}: {t.get('direction')} @ {t.get('entry_price')} -> {t.get('exit_price')} | P&L: ${t.get('pnl', 0):.2f}")

    if recap["errors"]:
        print(f"\nErrors/Warnings: {len(recap['errors'])}")
        for e in recap["errors"][:5]:
            print(f"  - {e[:80]}...")

    print("\n" + "=" * 60)


def main():
    """Generate and export daily recap."""
    import argparse

    parser = argparse.ArgumentParser(description="Generate daily trading recap")
    parser.add_argument("--date", help="Date to recap (YYYY-MM-DD), default today")
    parser.add_argument("--no-export", action="store_true", help="Don't SCP to home server")
    parser.add_argument("--quiet", action="store_true", help="Suppress output")
    args = parser.parse_args()

    target_date = date.today()
    if args.date:
        target_date = datetime.strptime(args.date, "%Y-%m-%d").date()

    # Generate recap
    recap = generate_recap(target_date)

    # Save locally
    filepath = save_recap(recap, target_date)

    # Print summary
    if not args.quiet:
        print_summary(recap)

    # Export to home server
    if not args.no_export:
        export_recap(filepath)

    return recap


if __name__ == "__main__":
    main()
