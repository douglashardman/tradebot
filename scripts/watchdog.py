#!/usr/bin/env python3
"""
Trading System Watchdog - Monitors health and alerts on issues only.

Runs as a separate process watching the main trading system.
Only sends Discord alerts when something needs attention.

Alert Tiers:
- Tier 1 (CRITICAL): Immediate alert - system down, can't connect, position stuck
- Tier 2 (WARNING): Alert after sustained issue - no ticks, high memory, reconnects
- Tier 3 (DAILY): One summary at market close

Silent: Individual trades, tier changes, routine operations
"""

import asyncio
import json
import logging
import os
import sys
import signal
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Optional

import aiohttp
import psutil
import pytz

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.scheduler import is_trading_day, get_market_close_time

# Configure logging
LOG_DIR = os.getenv("LOG_DIR", os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs"))
os.makedirs(LOG_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(LOG_DIR, "watchdog.log"), mode="a"),
    ],
)
logger = logging.getLogger("watchdog")

# Timezone
ET = pytz.timezone("America/New_York")

# Paths
DATA_DIR = Path(os.path.dirname(os.path.dirname(__file__))) / "data"
HEARTBEAT_FILE = DATA_DIR / "heartbeat.json"
STATE_FILE = DATA_DIR / "tier_state.json"


class WatchdogMonitor:
    """
    Monitors trading system health and alerts on issues.
    """

    def __init__(self):
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
        if not self.webhook_url:
            logger.error("DISCORD_WEBHOOK_URL not set!")
            sys.exit(1)

        self._running = False
        self._session: Optional[aiohttp.ClientSession] = None

        # Thresholds
        self.heartbeat_stale_minutes = int(os.getenv("HEARTBEAT_STALE_MINUTES", "5"))
        self.memory_warning_percent = int(os.getenv("MEMORY_WARNING_PERCENT", "85"))
        self.disk_critical_mb = int(os.getenv("DISK_CRITICAL_MB", "500"))
        self.check_interval_seconds = int(os.getenv("WATCHDOG_CHECK_INTERVAL", "60"))

        # State tracking
        self._last_alert_time: dict = {}  # Prevent alert spam
        self._alert_cooldown_minutes = 15
        self._issues_today: list = []
        self._sent_preflight = False
        self._sent_session_start = False
        self._sent_eod_summary = False
        self._consecutive_heartbeat_failures = 0
        self._process_name = "run_headless.py"

    async def start(self):
        """Start the watchdog."""
        logger.info("=" * 50)
        logger.info("WATCHDOG MONITOR STARTING")
        logger.info("=" * 50)
        logger.info(f"Check interval: {self.check_interval_seconds}s")
        logger.info(f"Heartbeat stale threshold: {self.heartbeat_stale_minutes} min")
        logger.info(f"Memory warning: {self.memory_warning_percent}%")
        logger.info(f"Disk critical: {self.disk_critical_mb} MB")

        self._session = aiohttp.ClientSession()
        self._running = True

        try:
            while self._running:
                now_et = datetime.now(ET)

                # Only monitor during trading days/hours
                if is_trading_day(now_et):
                    market_open = time(9, 25)  # Start 5 min before open
                    market_close = get_market_close_time(now_et)
                    close_with_buffer = time(market_close.hour, market_close.minute + 15)

                    current_time = now_et.time()

                    # Reset daily flags at midnight
                    if current_time < time(0, 5):
                        self._sent_preflight = False
                        self._sent_session_start = False
                        self._sent_eod_summary = False
                        self._issues_today = []

                    # During market hours
                    if market_open <= current_time <= close_with_buffer:
                        # Send preflight check at 9:25 (5 min before RTH)
                        if not self._sent_preflight and current_time >= time(9, 25):
                            await self._send_preflight_check()
                            self._sent_preflight = True

                        # Send session start at 9:30
                        if not self._sent_session_start and current_time >= time(9, 30):
                            await self._send_session_start()
                            self._sent_session_start = True

                        # Run health checks
                        await self._run_health_checks()

                        # Send EOD summary
                        if not self._sent_eod_summary and current_time >= market_close:
                            await self._send_eod_summary()
                            self._sent_eod_summary = True

                await asyncio.sleep(self.check_interval_seconds)

        except asyncio.CancelledError:
            logger.info("Watchdog cancelled")
        finally:
            if self._session:
                await self._session.close()

    async def stop(self):
        """Stop the watchdog."""
        self._running = False

    async def _run_health_checks(self):
        """Run all health checks."""
        issues = []

        # Check 1: Is the trading process running?
        process_running = self._check_process_running()
        if not process_running:
            issues.append(("CRITICAL", "Trading process not running"))

        # Check 2: Is heartbeat fresh?
        heartbeat_ok, heartbeat_msg = self._check_heartbeat()
        if not heartbeat_ok:
            issues.append(("CRITICAL" if self._consecutive_heartbeat_failures > 2 else "WARNING", heartbeat_msg))

        # Check 3: System resources
        memory_ok, memory_msg = self._check_memory()
        if not memory_ok:
            issues.append(("WARNING", memory_msg))

        disk_ok, disk_msg = self._check_disk()
        if not disk_ok:
            issues.append(("CRITICAL", disk_msg))

        # Check 4: Connection status (from heartbeat data)
        connection_ok, connection_msg = self._check_connection_status()
        if not connection_ok:
            issues.append(("WARNING", connection_msg))

        # Alert on issues
        for severity, message in issues:
            await self._alert(severity, message)

    def _check_process_running(self) -> bool:
        """Check if the trading process is running."""
        for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
            try:
                cmdline = proc.info.get('cmdline', [])
                if cmdline and any(self._process_name in arg for arg in cmdline):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False

    def _check_heartbeat(self) -> tuple[bool, str]:
        """Check if heartbeat file is fresh."""
        if not HEARTBEAT_FILE.exists():
            self._consecutive_heartbeat_failures += 1
            return False, "No heartbeat file found"

        try:
            with open(HEARTBEAT_FILE) as f:
                data = json.load(f)

            last_beat = datetime.fromisoformat(data.get("timestamp", ""))
            age_seconds = (datetime.now() - last_beat).total_seconds()
            age_minutes = age_seconds / 60

            if age_minutes > self.heartbeat_stale_minutes:
                self._consecutive_heartbeat_failures += 1
                return False, f"Heartbeat stale ({age_minutes:.1f} min old)"

            # Reset failure counter on success
            self._consecutive_heartbeat_failures = 0
            return True, "OK"

        except Exception as e:
            self._consecutive_heartbeat_failures += 1
            return False, f"Heartbeat read error: {e}"

    def _check_memory(self) -> tuple[bool, str]:
        """Check system memory usage."""
        memory = psutil.virtual_memory()
        if memory.percent > self.memory_warning_percent:
            return False, f"High memory usage: {memory.percent:.1f}%"
        return True, "OK"

    def _check_disk(self) -> tuple[bool, str]:
        """Check disk space."""
        disk = psutil.disk_usage("/")
        free_mb = disk.free / (1024 * 1024)
        if free_mb < self.disk_critical_mb:
            return False, f"Low disk space: {free_mb:.0f} MB free"
        return True, "OK"

    def _check_connection_status(self) -> tuple[bool, str]:
        """Check broker connection status from heartbeat."""
        if not HEARTBEAT_FILE.exists():
            return True, "OK"  # Can't check without heartbeat

        try:
            with open(HEARTBEAT_FILE) as f:
                data = json.load(f)

            if not data.get("feed_connected", True):
                return False, "Data feed disconnected"

            reconnect_count = data.get("reconnect_count", 0)
            if reconnect_count > 3:
                return False, f"Multiple reconnects today ({reconnect_count})"

            return True, "OK"

        except Exception:
            return True, "OK"  # Don't alert on read errors here

    async def _alert(self, severity: str, message: str):
        """Send alert to Discord if not in cooldown."""
        alert_key = f"{severity}:{message}"

        # Check cooldown
        last_alert = self._last_alert_time.get(alert_key)
        if last_alert:
            minutes_since = (datetime.now() - last_alert).total_seconds() / 60
            if minutes_since < self._alert_cooldown_minutes:
                logger.debug(f"Alert in cooldown: {message}")
                return

        # Track issue for daily summary
        self._issues_today.append({
            "time": datetime.now().strftime("%H:%M"),
            "severity": severity,
            "message": message,
        })

        # Send alert
        color = 0xFF0000 if severity == "CRITICAL" else 0xFFA500  # Red or Orange
        emoji = "🚨" if severity == "CRITICAL" else "⚠️"

        embed = {
            "title": f"{emoji} {severity}: Trading System Alert",
            "description": message,
            "color": color,
            "timestamp": datetime.now(pytz.UTC).isoformat(),
            "footer": {"text": "Watchdog Monitor"},
        }

        await self._send_discord(embed)
        self._last_alert_time[alert_key] = datetime.now()
        logger.warning(f"ALERT [{severity}]: {message}")

    async def _send_preflight_check(self):
        """Send preflight check 5 minutes before RTH opens."""
        # Gather all system checks
        process_ok = self._check_process_running()
        heartbeat_ok, heartbeat_msg = self._check_heartbeat()
        memory_ok, memory_msg = self._check_memory()
        disk_ok, disk_msg = self._check_disk()
        connection_ok, connection_msg = self._check_connection_status()

        # Get detailed stats from heartbeat
        bars = 0
        ticks = 0
        signals = 0
        regime = "Unknown"
        mode = "unknown"
        symbol = "?"

        if HEARTBEAT_FILE.exists():
            try:
                with open(HEARTBEAT_FILE) as f:
                    data = json.load(f)
                bars = data.get("bar_count", 0)
                ticks = data.get("tick_count", 0)
                signals = data.get("signal_count", 0)
                mode = data.get("mode", "unknown")
                symbol = data.get("symbol", "?")
            except Exception:
                pass

        # Get tier info
        tier_name = "Unknown"
        balance = 0
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                tier_name = state.get("tier_name", "Unknown")
                balance = state.get("balance", 0)
            except Exception:
                pass

        # Build checklist
        checks = []
        checks.append(f"{'✅' if process_ok else '❌'} Trading process")
        checks.append(f"{'✅' if heartbeat_ok else '❌'} Heartbeat fresh")
        checks.append(f"{'✅' if connection_ok else '❌'} Data feed connected")
        checks.append(f"{'✅' if bars >= 21 else '⚠️'} Regime ready ({bars}/21 bars)")
        checks.append(f"{'✅' if memory_ok else '⚠️'} Memory OK")
        checks.append(f"{'✅' if disk_ok else '❌'} Disk space OK")

        # Determine overall status
        critical_fail = not process_ok or not heartbeat_ok or not disk_ok
        warning = not connection_ok or not memory_ok or bars < 21

        if critical_fail:
            status = "❌ NOT READY - Intervention Required"
            color = 0xFF0000  # Red
        elif warning:
            status = "⚠️ READY WITH WARNINGS"
            color = 0xFFA500  # Orange
        else:
            status = "✅ ALL SYSTEMS GO"
            color = 0x00FF00  # Green

        checklist_text = "\n".join(checks)

        embed = {
            "title": f"Pre-Flight Check - {datetime.now(ET).strftime('%A, %B %d')}",
            "description": (
                f"**{status}**\n\n"
                f"**Checklist:**\n{checklist_text}\n\n"
                f"**Session Info:**\n"
                f"• Mode: {mode.upper()}\n"
                f"• Symbol: {symbol}\n"
                f"• Tier: {tier_name}\n"
                f"• Balance: ${balance:,.2f}\n\n"
                f"**Pre-Market Stats:**\n"
                f"• Ticks: {ticks:,}\n"
                f"• Bars: {bars}\n"
                f"• Signals: {signals}\n\n"
                f"RTH opens in 5 minutes."
            ),
            "color": color,
            "timestamp": datetime.now(pytz.UTC).isoformat(),
            "footer": {"text": "Watchdog Monitor | Next update at market close"},
        }

        await self._send_discord(embed)
        logger.info(f"Sent preflight check: {status}")

    async def _send_session_start(self):
        """Send session started message at 9:30."""
        process_ok = self._check_process_running()
        heartbeat_ok, _ = self._check_heartbeat()

        if process_ok and heartbeat_ok:
            status = "✅ Trading Session Started"
            color = 0x00FF00  # Green
        else:
            status = "🚨 Session Started - Issues Detected"
            color = 0xFF0000  # Red

        embed = {
            "title": status,
            "description": "RTH session is now open. Monitoring for signals.",
            "color": color,
            "timestamp": datetime.now(pytz.UTC).isoformat(),
            "footer": {"text": "Watchdog Monitor"},
        }

        await self._send_discord(embed)
        logger.info("Sent session start message")

    async def _send_eod_summary(self):
        """Send end-of-day summary."""
        # Get final state
        tier_info = ""
        day_pnl = 0
        trades = 0

        # Get authoritative trade data from database
        try:
            from src.data.live_db import get_session_by_date, get_trades_for_session
            today = datetime.now(ET).strftime("%Y-%m-%d")
            session = get_session_by_date(today)
            if session:
                db_trades = get_trades_for_session(session["id"])
                completed = [t for t in db_trades if t.get("exit_price")]
                trades = len(completed)
                day_pnl = sum(t.get("pnl_net", 0) or 0 for t in completed)
        except Exception as e:
            logger.warning(f"Could not get trades from DB: {e}")
            # Fallback to heartbeat
            if HEARTBEAT_FILE.exists():
                try:
                    with open(HEARTBEAT_FILE) as f:
                        data = json.load(f)
                    day_pnl = data.get("daily_pnl", 0)
                    trades = data.get("trade_count", 0)
                except Exception:
                    pass

        # Get tier info
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE) as f:
                    state = json.load(f)
                tier_name = state.get("tier_name", "Unknown")
                balance = state.get("balance", 0)
                tier_info = f"**Tier:** {tier_name}\n**Balance:** ${balance:,.2f}\n"
            except Exception:
                pass

        # Build issues summary
        issues_text = "None"
        if self._issues_today:
            issues_text = "\n".join(
                f"• {i['time']} [{i['severity']}] {i['message']}"
                for i in self._issues_today[-5:]  # Last 5 issues
            )
            if len(self._issues_today) > 5:
                issues_text += f"\n... and {len(self._issues_today) - 5} more"

        # Determine overall status
        critical_count = sum(1 for i in self._issues_today if i["severity"] == "CRITICAL")
        warning_count = sum(1 for i in self._issues_today if i["severity"] == "WARNING")

        if critical_count > 0:
            status = "🚨 Issues Detected"
            color = 0xFF0000
        elif warning_count > 0:
            status = "⚠️ Warnings"
            color = 0xFFA500
        else:
            status = "✅ No Issues"
            color = 0x00FF00

        pnl_emoji = "📈" if day_pnl >= 0 else "📉"

        embed = {
            "title": f"End of Day Summary - {datetime.now(ET).strftime('%A, %B %d')}",
            "description": (
                f"**Status:** {status}\n\n"
                f"{tier_info}"
                f"{pnl_emoji} **Day P&L:** ${day_pnl:+,.2f}\n"
                f"**Trades:** {trades}\n\n"
                f"**Issues Today:**\n{issues_text}"
            ),
            "color": color,
            "timestamp": datetime.now(pytz.UTC).isoformat(),
            "footer": {"text": "Watchdog Monitor | See you tomorrow"},
        }

        await self._send_discord(embed)
        logger.info("Sent EOD summary")

    async def _send_discord(self, embed: dict):
        """Send embed to Discord."""
        if not self._session:
            return

        payload = {"embeds": [embed]}

        try:
            async with self._session.post(self.webhook_url, json=payload) as resp:
                if resp.status not in (200, 204):
                    logger.error(f"Discord webhook failed: {resp.status}")
        except Exception as e:
            logger.error(f"Discord send error: {e}")


async def main():
    """Entry point."""
    monitor = WatchdogMonitor()

    # Handle shutdown signals
    loop = asyncio.get_event_loop()

    def signal_handler():
        logger.info("Shutdown signal received")
        asyncio.create_task(monitor.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, signal_handler)

    await monitor.start()


if __name__ == "__main__":
    asyncio.run(main())
