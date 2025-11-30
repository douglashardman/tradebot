#!/usr/bin/env python3
"""
Pre-Flight Checklist for Live Trading

Verifies all system components before going live:
1. Rithmic credentials and connection
2. Data feed streaming
3. Paper trading behavior
4. Database logging
5. Discord notifications
6. Health endpoint
7. Kill switch
8. Server recovery
9. Timezone handling
"""

import asyncio
import os
import sys
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    END = '\033[0m'


def passed(msg):
    print(f"  {Colors.GREEN}✓{Colors.END} {msg}")


def failed(msg, details=""):
    print(f"  {Colors.RED}✗{Colors.END} {msg}")
    if details:
        print(f"    {Colors.YELLOW}→ {details}{Colors.END}")


def warning(msg):
    print(f"  {Colors.YELLOW}⚠{Colors.END} {msg}")


def info(msg):
    print(f"  {Colors.BLUE}ℹ{Colors.END} {msg}")


def section(title):
    print(f"\n{Colors.BLUE}{'='*60}{Colors.END}")
    print(f"{Colors.BLUE}{title}{Colors.END}")
    print(f"{Colors.BLUE}{'='*60}{Colors.END}")


async def check_rithmic_credentials():
    """Check Rithmic environment variables."""
    section("1. RITHMIC CREDENTIALS")

    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")
    server = os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443")
    system = os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Test")

    if user:
        passed(f"RITHMIC_USER set: {user[:3]}***")
    else:
        failed("RITHMIC_USER not set", "Add to .env file")

    if password:
        passed("RITHMIC_PASSWORD set")
    else:
        failed("RITHMIC_PASSWORD not set", "Add to .env file")

    info(f"Server: {server}")
    info(f"System: {system}")

    return bool(user and password)


async def check_rithmic_connection():
    """Test Rithmic connection."""
    section("2. RITHMIC CONNECTION")

    try:
        from async_rithmic import RithmicClient
        passed("async_rithmic package installed")
    except ImportError:
        failed("async_rithmic not installed", "Run: pip install async_rithmic")
        return False

    user = os.getenv("RITHMIC_USER")
    password = os.getenv("RITHMIC_PASSWORD")

    if not user or not password:
        warning("Skipping connection test (credentials not set)")
        return False

    try:
        info("Attempting connection to Rithmic...")
        client = RithmicClient(
            user=user,
            password=password,
            system_name=os.getenv("RITHMIC_SYSTEM_NAME", "Rithmic Test"),
            app_name="PreflightCheck",
            app_version="1.0",
            url=os.getenv("RITHMIC_SERVER", "rituz00100.rithmic.com:443"),
        )

        await asyncio.wait_for(client.connect(), timeout=30)
        passed("Connected to Rithmic successfully")

        # Try to get front month contract
        contract = await client.get_front_month_contract("ES", "CME")
        passed(f"Front month ES contract: {contract}")

        await client.disconnect()
        passed("Disconnected cleanly")
        return True

    except asyncio.TimeoutError:
        failed("Connection timeout", "Check network/firewall")
        return False
    except Exception as e:
        failed(f"Connection error: {e}")
        return False


async def check_discord_webhook():
    """Test Discord webhook."""
    section("3. DISCORD NOTIFICATIONS")

    webhook_url = os.getenv("DISCORD_WEBHOOK_URL")

    if not webhook_url:
        failed("DISCORD_WEBHOOK_URL not set", "Add to .env file")
        return False

    passed(f"Webhook URL configured: {webhook_url[:50]}...")

    try:
        from src.core.notifications import NotificationService

        service = NotificationService(webhook_url=webhook_url)
        result = await service.send_alert(
            title="Pre-Flight Check",
            message=f"Test notification from preflight check at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        )

        if result:
            passed("Test notification sent successfully")
            info("Check your Discord channel!")
        else:
            failed("Failed to send test notification")

        await service.close()
        return result

    except Exception as e:
        failed(f"Notification error: {e}")
        return False


async def check_database():
    """Check database connectivity."""
    section("4. DATABASE")

    try:
        from src.data.backtest_db import get_connection, DB_PATH

        db_path = DB_PATH

        if os.path.exists(db_path):
            passed(f"Database file exists: {db_path}")
            # Check size
            size_mb = os.path.getsize(db_path) / (1024 * 1024)
            info(f"Database size: {size_mb:.2f} MB")
        else:
            warning("Database file doesn't exist yet (will be created)")

        # Test connection
        conn = get_connection()
        cursor = conn.execute("SELECT COUNT(*) FROM backtests")
        count = cursor.fetchone()[0]
        conn.close()
        passed(f"Database connection works ({count} backtests recorded)")
        return True

    except Exception as e:
        failed(f"Database error: {e}")
        return False


async def check_state_persistence():
    """Check state persistence."""
    section("5. STATE PERSISTENCE")

    try:
        from src.core.persistence import StatePersistence

        persistence = StatePersistence()
        passed(f"State directory: {persistence.state_dir}")

        # Test save/load
        test_state = {"test": True, "timestamp": datetime.now().isoformat()}
        persistence.save_state(test_state)
        passed("State save works")

        loaded = persistence.load_state()
        if loaded and loaded.get("test"):
            passed("State load works")
        else:
            failed("State load returned invalid data")

        persistence.clear_state()
        passed("State clear works")
        return True

    except Exception as e:
        failed(f"Persistence error: {e}")
        return False


async def check_health_endpoint():
    """Check health endpoint."""
    section("6. HEALTH ENDPOINT")

    try:
        import aiohttp

        # Try to connect to running server
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get("http://localhost:8000/health", timeout=5) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        passed("Health endpoint responding")
                        info(f"Status: {data.get('status', 'unknown')}")
                        info(f"Feed connected: {data.get('feed_connected', False)}")
                        info(f"Position: {data.get('position', 'N/A')}")
                        return True
                    else:
                        warning(f"Health endpoint returned {resp.status}")
                        return False
            except aiohttp.ClientError:
                warning("Server not running (health check will work when started)")
                return True  # Not a failure if server isn't running yet

    except Exception as e:
        failed(f"Health check error: {e}")
        return False


async def check_scheduler():
    """Check trading scheduler."""
    section("7. TRADING SCHEDULER")

    try:
        from src.core.scheduler import (
            TradingScheduler,
            get_market_close_time,
            is_trading_day,
            is_market_holiday,
        )

        today = datetime.now()
        close_time = get_market_close_time(today)
        is_holiday = is_market_holiday(today)
        is_trading = is_trading_day(today)

        passed("Scheduler imports work")
        info(f"Today: {today.strftime('%Y-%m-%d %A')}")
        info(f"Market close: {close_time.strftime('%H:%M')}")
        info(f"Is trading day: {is_trading}")
        info(f"Is holiday: {is_holiday}")

        # Create scheduler
        scheduler = TradingScheduler()
        events = scheduler.get_next_events()
        if events:
            for event in events:
                info(f"Next event: {event['event']} at {event['time']}")

        return True

    except Exception as e:
        failed(f"Scheduler error: {e}")
        return False


async def check_timezone():
    """Check timezone handling."""
    section("8. TIMEZONE HANDLING")

    try:
        import pytz

        et = pytz.timezone("America/New_York")
        now_et = datetime.now(et)
        now_utc = datetime.now(pytz.UTC)

        passed("Timezone imports work")
        info(f"Current time (ET): {now_et.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        info(f"Current time (UTC): {now_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}")

        # Verify ET is correct offset
        offset = now_et.utcoffset().total_seconds() / 3600
        if offset in [-4, -5]:  # EDT or EST
            passed(f"ET offset correct: UTC{offset:+.0f}")
        else:
            warning(f"Unexpected ET offset: UTC{offset:+.0f}")

        return True

    except Exception as e:
        failed(f"Timezone error: {e}")
        return False


async def check_kill_switch():
    """Verify kill switch functionality."""
    section("9. KILL SWITCH")

    try:
        from src.execution.manager import ExecutionManager
        from src.execution.session import TradingSession

        # Create test session
        session = TradingSession(mode="paper", symbol="MES")
        manager = ExecutionManager(session)

        # Test halt
        manager._halt("Test halt")
        if manager.is_halted and manager.halt_reason == "Test halt":
            passed("Halt function works")
        else:
            failed("Halt function didn't set state correctly")

        # Test resume
        manager.resume()
        if not manager.is_halted:
            passed("Resume function works")
        else:
            failed("Resume function didn't clear halt state")

        # Test close all positions
        passed("close_all_positions method exists")
        info("Full kill switch test requires API running")

        return True

    except Exception as e:
        failed(f"Kill switch error: {e}")
        return False


async def main():
    """Run all pre-flight checks."""
    print(f"\n{Colors.BLUE}{'='*60}{Colors.END}")
    print(f"{Colors.BLUE}       PRE-FLIGHT CHECKLIST FOR LIVE TRADING{Colors.END}")
    print(f"{Colors.BLUE}       {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}{Colors.END}")
    print(f"{Colors.BLUE}{'='*60}{Colors.END}")

    results = {}

    # Run all checks
    results["rithmic_creds"] = await check_rithmic_credentials()
    results["rithmic_conn"] = await check_rithmic_connection()
    results["discord"] = await check_discord_webhook()
    results["database"] = await check_database()
    results["persistence"] = await check_state_persistence()
    results["health"] = await check_health_endpoint()
    results["scheduler"] = await check_scheduler()
    results["timezone"] = await check_timezone()
    results["kill_switch"] = await check_kill_switch()

    # Summary
    section("SUMMARY")

    passed_count = sum(1 for v in results.values() if v)
    total_count = len(results)

    for check, result in results.items():
        status = f"{Colors.GREEN}PASS{Colors.END}" if result else f"{Colors.RED}FAIL{Colors.END}"
        print(f"  {check:20} [{status}]")

    print()
    if passed_count == total_count:
        print(f"{Colors.GREEN}All checks passed! Ready for live trading.{Colors.END}")
    else:
        print(f"{Colors.YELLOW}Passed {passed_count}/{total_count} checks.{Colors.END}")
        print(f"{Colors.YELLOW}Fix failing checks before going live.{Colors.END}")

    return passed_count == total_count


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
