"""Notification service for alerts and daily digests via Discord webhook."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional, Any

import aiohttp

logger = logging.getLogger(__name__)


class AlertType(Enum):
    """Types of alerts that can be sent."""
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    SUCCESS = "success"
    TRADE_OPEN = "trade_open"
    TRADE_CLOSE = "trade_close"
    SESSION_HALT = "session_halt"
    CONNECTION_LOST = "connection_lost"
    CONNECTION_RESTORED = "connection_restored"
    DAILY_DIGEST = "daily_digest"


# Discord embed colors
ALERT_COLORS = {
    AlertType.INFO: 0x3498DB,        # Blue
    AlertType.WARNING: 0xF39C12,     # Orange
    AlertType.ERROR: 0xE74C3C,       # Red
    AlertType.SUCCESS: 0x2ECC71,     # Green
    AlertType.TRADE_OPEN: 0x9B59B6,  # Purple
    AlertType.TRADE_CLOSE: 0x2ECC71, # Green
    AlertType.SESSION_HALT: 0xE74C3C, # Red
    AlertType.CONNECTION_LOST: 0xE74C3C, # Red
    AlertType.CONNECTION_RESTORED: 0x2ECC71, # Green
    AlertType.DAILY_DIGEST: 0x3498DB, # Blue
}


@dataclass
class DailyDigest:
    """Data for daily trading summary."""
    date: str
    session_start: str
    session_end: str
    status: str  # "COMPLETED", "STOPPED EARLY (reason)"
    starting_balance: float
    ending_balance: float
    day_pnl: float
    trades: int
    wins: int
    losses: int
    win_rate: float
    trades_detail: List[Dict[str, Any]]
    regime_breakdown: Dict[str, int]
    current_position: str  # "FLAT" or position description
    account_balance: float


class NotificationService:
    """
    Service for sending notifications via Discord webhook.

    Supports:
    - Real-time alerts (connection status, errors, trades)
    - Daily trading digest (4pm summary)
    - Configurable alert levels
    """

    def __init__(
        self,
        webhook_url: Optional[str] = None,
        bot_name: str = "TradeBot",
        bot_avatar: Optional[str] = None,
        alert_on_trades: bool = False,  # Can be noisy
        alert_on_connection: bool = True,
        alert_on_limits: bool = True,
        alert_on_errors: bool = True,
    ):
        """
        Initialize notification service.

        Args:
            webhook_url: Discord webhook URL. If None, reads from DISCORD_WEBHOOK_URL env var.
            bot_name: Name to display for bot messages.
            bot_avatar: URL for bot avatar image.
            alert_on_trades: Send alerts for each trade (can be noisy).
            alert_on_connection: Send alerts for connection status changes.
            alert_on_limits: Send alerts when daily limits are hit.
            alert_on_errors: Send alerts for system errors.
        """
        self.webhook_url = webhook_url or os.getenv("DISCORD_WEBHOOK_URL")
        self.bot_name = bot_name
        self.bot_avatar = bot_avatar
        self.alert_on_trades = alert_on_trades
        self.alert_on_connection = alert_on_connection
        self.alert_on_limits = alert_on_limits
        self.alert_on_errors = alert_on_errors

        self._session: Optional[aiohttp.ClientSession] = None
        self._enabled = bool(self.webhook_url)

        if not self._enabled:
            logger.warning(
                "Discord webhook URL not configured. "
                "Set DISCORD_WEBHOOK_URL environment variable to enable notifications."
            )

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def _send_webhook(self, payload: dict) -> bool:
        """
        Send a webhook payload to Discord.

        Args:
            payload: Discord webhook payload.

        Returns:
            True if sent successfully, False otherwise.
        """
        if not self._enabled:
            logger.debug("Notifications disabled, skipping send")
            return False

        try:
            session = await self._get_session()
            async with session.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status == 204:
                    return True
                elif response.status == 429:
                    # Rate limited
                    retry_after = float(response.headers.get("Retry-After", 1))
                    logger.warning(f"Discord rate limited, retry after {retry_after}s")
                    await asyncio.sleep(retry_after)
                    return await self._send_webhook(payload)
                else:
                    text = await response.text()
                    logger.error(f"Discord webhook error: {response.status} - {text}")
                    return False
        except Exception as e:
            logger.error(f"Failed to send Discord notification: {e}")
            return False

    async def send_alert(
        self,
        title: str,
        message: str,
        alert_type: AlertType = AlertType.INFO,
        fields: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """
        Send an alert notification.

        Args:
            title: Alert title.
            message: Alert message/description.
            alert_type: Type of alert (affects color/icon).
            fields: Optional list of embed fields.

        Returns:
            True if sent successfully.
        """
        # Emoji prefixes for alert types
        emoji_map = {
            AlertType.INFO: "ℹ️",
            AlertType.WARNING: "⚠️",
            AlertType.ERROR: "❌",
            AlertType.SUCCESS: "✅",
            AlertType.TRADE_OPEN: "📈",
            AlertType.TRADE_CLOSE: "✅",
            AlertType.SESSION_HALT: "🛑",
            AlertType.CONNECTION_LOST: "🔴",
            AlertType.CONNECTION_RESTORED: "🟢",
            AlertType.DAILY_DIGEST: "📊",
        }

        emoji = emoji_map.get(alert_type, "")
        color = ALERT_COLORS.get(alert_type, 0x3498DB)

        embed = {
            "title": f"{emoji} {title}",
            "description": message,
            "color": color,
            "timestamp": datetime.utcnow().isoformat(),
        }

        if fields:
            embed["fields"] = fields

        payload = {
            "username": self.bot_name,
            "embeds": [embed],
        }

        if self.bot_avatar:
            payload["avatar_url"] = self.bot_avatar

        return await self._send_webhook(payload)

    # === Specific Alert Methods ===

    async def alert_daily_loss_limit(self, pnl: float) -> bool:
        """Alert when daily loss limit is hit."""
        if not self.alert_on_limits:
            return False

        return await self.send_alert(
            title="Session Halted: Daily Loss Limit",
            message=f"Trading stopped. Daily P&L: **${pnl:,.2f}**",
            alert_type=AlertType.SESSION_HALT,
        )

    async def alert_daily_profit_target(self, pnl: float) -> bool:
        """Alert when daily profit target is hit."""
        if not self.alert_on_limits:
            return False

        return await self.send_alert(
            title="Session Complete: Profit Target Hit",
            message=f"Daily profit target reached! P&L: **${pnl:,.2f}**",
            alert_type=AlertType.SUCCESS,
        )

    async def alert_connection_lost(self, details: str = "") -> bool:
        """Alert when data feed connection is lost."""
        if not self.alert_on_connection:
            return False

        timestamp = datetime.now().strftime("%H:%M:%S ET")
        return await self.send_alert(
            title="Data Feed Disconnected",
            message=f"Connection lost at {timestamp}. {details}",
            alert_type=AlertType.CONNECTION_LOST,
        )

    async def alert_connection_restored(self, details: str = "") -> bool:
        """Alert when data feed connection is restored."""
        if not self.alert_on_connection:
            return False

        timestamp = datetime.now().strftime("%H:%M:%S ET")
        return await self.send_alert(
            title="Data Feed Reconnected",
            message=f"Connection restored at {timestamp}. {details}",
            alert_type=AlertType.CONNECTION_RESTORED,
        )

    async def alert_system_error(self, error: str, details: str = "") -> bool:
        """Alert for system errors."""
        if not self.alert_on_errors:
            return False

        return await self.send_alert(
            title="System Error",
            message=f"**Error:** {error}\n{details}",
            alert_type=AlertType.ERROR,
        )

    async def alert_trade_opened(
        self,
        side: str,
        size: int,
        price: float,
        symbol: str = "MES",
    ) -> bool:
        """Alert when a trade is opened."""
        if not self.alert_on_trades:
            return False

        emoji = "📈" if side.upper() == "LONG" else "📉"
        return await self.send_alert(
            title=f"Position Opened",
            message=f"{emoji} **{side.upper()}** {size} {symbol} @ **{price:.2f}**",
            alert_type=AlertType.TRADE_OPEN,
        )

    async def alert_trade_closed(
        self,
        side: str,
        size: int,
        entry_price: float,
        exit_price: float,
        pnl: float,
        exit_reason: str,
        symbol: str = "MES",
    ) -> bool:
        """Alert when a trade is closed."""
        if not self.alert_on_trades:
            return False

        emoji = "✅" if pnl >= 0 else "❌"
        pnl_str = f"+${pnl:,.2f}" if pnl >= 0 else f"-${abs(pnl):,.2f}"

        return await self.send_alert(
            title=f"Position Closed",
            message=(
                f"{emoji} **{side.upper()}** {size} {symbol}\n"
                f"Entry: {entry_price:.2f} → Exit: {exit_price:.2f}\n"
                f"P&L: **{pnl_str}** ({exit_reason})"
            ),
            alert_type=AlertType.TRADE_CLOSE,
        )

    async def send_daily_digest(self, digest: DailyDigest) -> bool:
        """
        Send the daily trading summary.

        Args:
            digest: DailyDigest data object.

        Returns:
            True if sent successfully.
        """
        # Build trades detail section
        trades_text = ""
        for i, trade in enumerate(digest.trades_detail[:10], 1):  # Limit to 10 trades
            direction = trade.get("side", "?")
            entry = trade.get("entry_price", 0)
            exit_price = trade.get("exit_price", 0)
            exit_reason = trade.get("exit_reason", "?")
            pnl = trade.get("pnl", 0)
            time_str = trade.get("entry_time", "")[:5] if trade.get("entry_time") else "?"

            pnl_str = f"+${pnl:,.0f}" if pnl >= 0 else f"-${abs(pnl):,.0f}"
            trades_text += f"{i}. {time_str} {direction} @ {entry:.2f} → {exit_price:.2f} ({exit_reason}) {pnl_str}\n"

        if not trades_text:
            trades_text = "No trades today"

        # Build regime breakdown
        regime_text = ""
        for regime, count in digest.regime_breakdown.items():
            regime_text += f"• {regime}: {count} trades\n"
        if not regime_text:
            regime_text = "No regime data"

        # Build embed
        fields = [
            {"name": "Starting Balance", "value": f"${digest.starting_balance:,.2f}", "inline": True},
            {"name": "Ending Balance", "value": f"${digest.ending_balance:,.2f}", "inline": True},
            {"name": "Day P&L", "value": f"${digest.day_pnl:+,.2f}", "inline": True},
            {"name": "Trades", "value": str(digest.trades), "inline": True},
            {"name": "Wins/Losses", "value": f"{digest.wins}/{digest.losses}", "inline": True},
            {"name": "Win Rate", "value": f"{digest.win_rate:.1f}%", "inline": True},
            {"name": "Trade Details", "value": trades_text[:1024], "inline": False},
            {"name": "Regime Breakdown", "value": regime_text[:1024], "inline": False},
            {"name": "Current Position", "value": digest.current_position, "inline": True},
            {"name": "Account Balance", "value": f"${digest.account_balance:,.2f}", "inline": True},
        ]

        return await self.send_alert(
            title=f"Daily Trading Summary - {digest.date}",
            message=f"**Session:** {digest.session_start} - {digest.session_end} ET\n**Status:** {digest.status}",
            alert_type=AlertType.DAILY_DIGEST,
            fields=fields,
        )


# Global notification service instance
_notification_service: Optional[NotificationService] = None


def get_notification_service() -> NotificationService:
    """Get the global notification service instance."""
    global _notification_service
    if _notification_service is None:
        _notification_service = NotificationService()
    return _notification_service


def configure_notifications(
    webhook_url: Optional[str] = None,
    **kwargs,
) -> NotificationService:
    """
    Configure the global notification service.

    Args:
        webhook_url: Discord webhook URL.
        **kwargs: Additional configuration options.

    Returns:
        Configured NotificationService instance.
    """
    global _notification_service
    _notification_service = NotificationService(webhook_url=webhook_url, **kwargs)
    return _notification_service
