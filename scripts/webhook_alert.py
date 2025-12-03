#!/usr/bin/env python3
"""
Webhook Alert Gateway - Fire-and-forget notification handler.

The trading bot calls this script to send notifications to external services.
This decouples alerting from trading - if this script hangs or fails,
the trading bot doesn't care and continues doing its job.

Usage:
    python scripts/webhook_alert.py <event_type> '<json_data>'

Examples:
    python scripts/webhook_alert.py session_start '{"balance": 2500, "tier": "Tier 1"}'
    python scripts/webhook_alert.py position_opened '{"direction": "LONG", "size": 1, "symbol": "MES", "entry_price": 6035.50}'
    python scripts/webhook_alert.py position_closed '{"direction": "LONG", "pnl": 57.50, "exit_type": "TARGET"}'
    python scripts/webhook_alert.py alert '{"title": "Test", "message": "Hello", "type": "info"}'

Environment:
    DISCORD_WEBHOOK_URL - Discord webhook for notifications
    DASHBOARD_WEBHOOK_URL - Local dashboard endpoint (optional)
"""

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Load environment from .env if available
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# Try to use aiohttp, fall back to requests
try:
    import aiohttp
    USE_ASYNC = True
except ImportError:
    import urllib.request
    import urllib.error
    USE_ASYNC = False


# Webhook URLs from environment
DISCORD_URL = os.getenv("DISCORD_WEBHOOK_URL")
DASHBOARD_URL = os.getenv("DASHBOARD_WEBHOOK_URL")


# === Discord Formatting ===

def format_discord_embed(event_type: str, data: Dict[str, Any]) -> Dict[str, Any]:
    """Format event data as a Discord embed."""

    test_mode = data.pop("_test_mode", False)
    test_prefix = "[TEST] " if test_mode else ""

    embed = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if event_type == "session_start":
        embed["title"] = f"{test_prefix}Trading Session Started"
        embed["color"] = 0x00FF00  # Green
        embed["fields"] = [
            {"name": "Balance", "value": f"${data.get('balance', 0):,.2f}", "inline": True},
            {"name": "Tier", "value": data.get("tier", "Unknown"), "inline": True},
            {"name": "Symbol", "value": data.get("symbol", "MES"), "inline": True},
            {"name": "Mode", "value": data.get("mode", "paper"), "inline": True},
        ]
        if data.get("profit_target"):
            embed["fields"].append({"name": "Profit Target", "value": f"${data['profit_target']:,.0f}", "inline": True})
        if data.get("loss_limit"):
            embed["fields"].append({"name": "Loss Limit", "value": f"${abs(data['loss_limit']):,.0f}", "inline": True})

    elif event_type == "position_opened":
        direction = data.get("direction", "LONG")
        emoji = "📈" if direction == "LONG" else "📉"
        embed["title"] = f"{test_prefix}{emoji} Position Opened"
        embed["color"] = 0x3498DB  # Blue
        embed["fields"] = [
            {"name": "Direction", "value": direction, "inline": True},
            {"name": "Size", "value": str(data.get("size", 1)), "inline": True},
            {"name": "Symbol", "value": data.get("symbol", "MES"), "inline": True},
            {"name": "Entry", "value": f"{data.get('entry_price', 0):.2f}", "inline": True},
        ]
        if data.get("stop_price"):
            embed["fields"].append({"name": "Stop", "value": f"{data['stop_price']:.2f}", "inline": True})
        if data.get("target_price"):
            embed["fields"].append({"name": "Target", "value": f"{data['target_price']:.2f}", "inline": True})

    elif event_type == "position_closed":
        pnl = data.get("pnl", 0)
        emoji = "✅" if pnl >= 0 else "❌"
        embed["title"] = f"{test_prefix}{emoji} Trade Closed"
        embed["color"] = 0x00FF00 if pnl >= 0 else 0xFF0000  # Green or Red
        pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
        embed["fields"] = [
            {"name": "Direction", "value": data.get("direction", ""), "inline": True},
            {"name": "Size", "value": str(data.get("size", 1)), "inline": True},
            {"name": "Symbol", "value": data.get("symbol", "MES"), "inline": True},
            {"name": "Entry", "value": f"{data.get('entry_price', 0):.2f}", "inline": True},
            {"name": "Exit", "value": f"{data.get('exit_price', 0):.2f}", "inline": True},
            {"name": "P&L", "value": pnl_str, "inline": True},
            {"name": "Exit Type", "value": data.get("exit_type", "MANUAL"), "inline": True},
        ]
        if data.get("daily_pnl") is not None:
            daily = data["daily_pnl"]
            daily_str = f"+${daily:,.2f}" if daily >= 0 else f"-${abs(daily):,.2f}"
            embed["fields"].append({"name": "Daily P&L", "value": daily_str, "inline": True})

    elif event_type == "alert":
        embed["title"] = f"{test_prefix}{data.get('title', 'Alert')}"
        embed["description"] = data.get("message", "")
        alert_type = data.get("type", "info").lower()
        color_map = {
            "success": 0x00FF00,
            "warning": 0xFFA500,
            "error": 0xFF0000,
            "info": 0x3498DB,
        }
        embed["color"] = color_map.get(alert_type, 0x3498DB)

    elif event_type == "session_halted":
        embed["title"] = f"{test_prefix}⚠️ Trading Halted"
        embed["color"] = 0xFFA500  # Orange
        embed["description"] = data.get("reason", "Loss limit reached")
        if data.get("daily_pnl") is not None:
            embed["fields"] = [
                {"name": "Daily P&L", "value": f"${data['daily_pnl']:+,.2f}", "inline": True},
            ]

    elif event_type == "tier_change":
        direction = "📈" if data.get("to_tier", 0) > data.get("from_tier", 0) else "📉"
        embed["title"] = f"{test_prefix}{direction} Tier Change"
        embed["color"] = 0x9B59B6  # Purple
        embed["fields"] = [
            {"name": "From", "value": data.get("from_tier_name", ""), "inline": True},
            {"name": "To", "value": data.get("to_tier_name", ""), "inline": True},
            {"name": "Balance", "value": f"${data.get('balance', 0):,.2f}", "inline": True},
            {"name": "Instrument", "value": f"{data.get('from_instrument', '')} → {data.get('to_instrument', '')}", "inline": True},
        ]

    elif event_type == "daily_digest":
        pnl = data.get("pnl", 0)
        emoji = "📊"
        embed["title"] = f"{test_prefix}{emoji} Daily Trading Summary"
        embed["color"] = 0x00FF00 if pnl >= 0 else 0xFF0000
        pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"
        embed["fields"] = [
            {"name": "Net P&L", "value": pnl_str, "inline": True},
            {"name": "Trades", "value": str(data.get("trade_count", 0)), "inline": True},
            {"name": "Win Rate", "value": f"{data.get('win_rate', 0):.0f}%", "inline": True},
        ]
        if data.get("ending_balance"):
            embed["fields"].append({"name": "Balance", "value": f"${data['ending_balance']:,.2f}", "inline": True})

    else:
        # Generic event
        embed["title"] = f"{test_prefix}{event_type.replace('_', ' ').title()}"
        embed["color"] = 0x3498DB
        embed["description"] = json.dumps(data, indent=2)[:2000]  # Discord limit

    bot_name = os.getenv("BOT_NAME", "TradeBot")
    return {"username": bot_name, "embeds": [embed]}


# === Dashboard Formatting ===

def format_dashboard_payload(event_type: str, data: Dict[str, Any]) -> tuple[str, Dict[str, Any]]:
    """Format event data for dashboard webhook. Returns (endpoint, payload)."""

    if event_type == "session_start":
        return "/webhook/session/start", {
            "starting_balance": data.get("balance", 0),
            "tier": data.get("tier", "Unknown"),
        }

    elif event_type == "position_opened":
        return "/webhook/position/opened", {
            "direction": data.get("direction", "LONG"),
            "size": data.get("size", 1),
            "symbol": data.get("symbol", "MES"),
            "entry_price": data.get("entry_price", 0),
            "stop_price": data.get("stop_price"),
            "target_price": data.get("target_price"),
            "timestamp": data.get("timestamp"),
        }

    elif event_type == "position_closed":
        return "/webhook/position/closed", {
            "direction": data.get("direction", "LONG"),
            "size": data.get("size", 1),
            "symbol": data.get("symbol", "MES"),
            "entry_price": data.get("entry_price", 0),
            "exit_price": data.get("exit_price", 0),
            "pnl": data.get("pnl", 0),
            "exit_type": data.get("exit_type", "MANUAL"),
            "timestamp": data.get("timestamp"),
        }

    elif event_type == "session_halted":
        # Hit daily loss limit - empty payload, dashboard shows "Stopped Out"
        return "/webhook/session/halted", {}

    elif event_type == "session_end":
        # EOD or normal shutdown - empty payload, dashboard shows "Session Closed"
        return "/webhook/session/end", {}

    else:
        # Other events go to a generic endpoint
        return "/webhook/event", {
            "event_type": event_type,
            "data": data,
        }


# === Async Sending (aiohttp) ===

async def send_async(discord_payload: Optional[Dict], dashboard_endpoint: Optional[str], dashboard_payload: Optional[Dict]) -> None:
    """Send webhooks asynchronously."""
    async with aiohttp.ClientSession() as session:
        tasks = []

        # Discord
        if DISCORD_URL and discord_payload:
            tasks.append(send_discord_async(session, discord_payload))

        # Dashboard
        if DASHBOARD_URL and dashboard_endpoint and dashboard_payload:
            url = f"{DASHBOARD_URL.rstrip('/')}{dashboard_endpoint}"
            tasks.append(send_dashboard_async(session, url, dashboard_payload))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


async def send_discord_async(session: aiohttp.ClientSession, payload: Dict) -> None:
    """Send to Discord webhook."""
    try:
        async with session.post(DISCORD_URL, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
            if resp.status not in (200, 204):
                print(f"Discord error: {resp.status}", file=sys.stderr)
    except Exception as e:
        print(f"Discord send failed: {e}", file=sys.stderr)


async def send_dashboard_async(session: aiohttp.ClientSession, url: str, payload: Dict) -> None:
    """Send to dashboard webhook."""
    try:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status != 200:
                print(f"Dashboard error: {resp.status}", file=sys.stderr)
    except Exception as e:
        # Dashboard might be offline - that's fine
        pass


# === Sync Sending (urllib fallback) ===

def send_sync(discord_payload: Optional[Dict], dashboard_endpoint: Optional[str], dashboard_payload: Optional[Dict]) -> None:
    """Send webhooks synchronously using urllib."""

    # Discord
    if DISCORD_URL and discord_payload:
        try:
            data = json.dumps(discord_payload).encode('utf-8')
            req = urllib.request.Request(DISCORD_URL, data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as resp:
                pass
        except Exception as e:
            print(f"Discord send failed: {e}", file=sys.stderr)

    # Dashboard
    if DASHBOARD_URL and dashboard_endpoint and dashboard_payload:
        try:
            url = f"{DASHBOARD_URL.rstrip('/')}{dashboard_endpoint}"
            data = json.dumps(dashboard_payload).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass
        except Exception:
            # Dashboard might be offline
            pass


# === Main ===

def main():
    if len(sys.argv) < 3:
        print("Usage: webhook_alert.py [--test] <event_type> '<json_data>'", file=sys.stderr)
        print("\nEvent types: session_start, position_opened, position_closed, alert, session_halted, tier_change, daily_digest", file=sys.stderr)
        print("\nFlags:")
        print("  --test    Prefix Discord messages with [TEST] to distinguish from live alerts", file=sys.stderr)
        sys.exit(1)

    # Check for --test flag
    test_mode = False
    args = sys.argv[1:]
    if args[0] == "--test":
        test_mode = True
        args = args[1:]

    if len(args) < 2:
        print("Usage: webhook_alert.py [--test] <event_type> '<json_data>'", file=sys.stderr)
        sys.exit(1)

    event_type = args[0]

    try:
        data = json.loads(args[1])
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    # Add test prefix if in test mode
    if test_mode:
        data["_test_mode"] = True

    # Format payloads
    discord_payload = format_discord_embed(event_type, data) if DISCORD_URL else None
    dashboard_endpoint, dashboard_payload = format_dashboard_payload(event_type, data) if DASHBOARD_URL else (None, None)

    # Send
    if USE_ASYNC:
        asyncio.run(send_async(discord_payload, dashboard_endpoint, dashboard_payload))
    else:
        send_sync(discord_payload, dashboard_endpoint, dashboard_payload)


if __name__ == "__main__":
    main()
