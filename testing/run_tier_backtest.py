#!/usr/bin/env python3
"""
Tier Progression Backtest

Runs a multi-day backtest starting at $2,500 with MES,
simulating tier progression through the capital tiers.

Sends Discord notifications for:
- Tier changes
- Daily digests
- Weekly digest (Friday)
- Problems/errors

Usage:
    PYTHONPATH=. python scripts/run_tier_backtest.py --week 2025-10-22
    PYTHONPATH=. python scripts/run_tier_backtest.py --dates 2025-10-22,2025-10-23,2025-10-24
    PYTHONPATH=. python scripts/run_tier_backtest.py --dates 2025-10-22 --discord
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

import databento as db

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Tick, Signal, FootprintBar
from src.core.capital import TierManager, TIERS
from src.core.notifications import NotificationService, DailyDigest, AlertType
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.data.adapters.databento import DatabentoAdapter
from src.data.backtest_db import log_backtest, log_trade, update_backtest, get_connection

# Cache directory for tick data
CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")


def load_cached_ticks(contract: str, date: str, start_time: str = "09:30", end_time: str = "16:00"):
    """Load ticks from cache file if it exists."""
    import json
    from src.core.types import Tick

    safe_start = start_time.replace(":", "")
    safe_end = end_time.replace(":", "")
    cache_path = os.path.join(CACHE_DIR, f"{contract}_{date}_{safe_start}_{safe_end}.json")

    if not os.path.exists(cache_path):
        return None

    logger.info(f"Loading from cache: {cache_path}")
    with open(cache_path) as f:
        data = json.load(f)

    ticks = []
    for d in data:
        ticks.append(Tick(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            price=d["price"],
            volume=d["volume"],
            side=d["side"],
            symbol=d["symbol"]
        ))
    return ticks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("tier_backtest")


class TierBacktester:
    """Run backtests with tier progression."""

    def __init__(
        self,
        starting_balance: float = 2500.0,
        speed_multiplier: float = 10.0,  # 1 min per hour = 60x
        send_discord: bool = False,
    ):
        self.starting_balance = starting_balance
        self.speed_multiplier = speed_multiplier
        self.send_discord = send_discord

        # Discord notifications
        self.notifier: Optional[NotificationService] = None
        if send_discord:
            webhook_url = os.getenv("DISCORD_WEBHOOK_URL")
            if webhook_url:
                self.notifier = NotificationService(webhook_url)
                logger.info("Discord notifications enabled")
            else:
                logger.warning("DISCORD_WEBHOOK_URL not set, notifications disabled")

        # Track results (must be initialized BEFORE TierManager since callback may fire)
        self.daily_results = []
        self.tier_changes = []
        self.all_trades = []
        self._current_date: str = ""  # Also needed by callback

        # Initialize tier manager
        state_file = Path("data/tier_backtest_state.json")
        state_file.unlink(missing_ok=True)  # Clean slate

        self.tier_manager = TierManager(
            starting_balance=starting_balance,
            state_file=state_file,
            on_tier_change=self._on_tier_change,
        )

        # Track week start balance for accurate weekly digest
        self._week_start_balance: float = starting_balance

        # Current day components
        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None
        self.session: Optional[TradingSession] = None
        self.manager: Optional[ExecutionManager] = None

        # Signal stacking
        self._current_bar_signals: List[Signal] = []

        # Database tracking
        self._current_backtest_id: Optional[int] = None
        self._trade_count: int = 0
        self._pending_trade_context: dict = {}  # Context for trade being executed

    def _on_tier_change(self, change: dict):
        """Handle tier change - log and send Discord notification."""
        old_tier = TIERS[change["from_tier"]]
        new_tier = TIERS[change["to_tier"]]
        direction = "UP" if change["to_tier"] > change["from_tier"] else "DOWN"

        logger.info(
            f"\n{'='*60}\n"
            f"TIER CHANGE {direction}!\n"
            f"  {old_tier['name']} -> {new_tier['name']}\n"
            f"  Balance: ${change['balance']:,.2f}\n"
            f"  Instrument: {change['from_instrument']} -> {change['to_instrument']}\n"
            f"{'='*60}\n"
        )

        self.tier_changes.append({
            "direction": direction,
            "from": old_tier["name"],
            "to": new_tier["name"],
            "balance": change["balance"],
            "instrument_change": f"{change['from_instrument']} -> {change['to_instrument']}",
            "date": self._current_date,
        })

        # Send Discord notification
        if self.notifier:
            asyncio.create_task(self._send_tier_change_notification(change, old_tier, new_tier, direction))

    async def _send_tier_change_notification(self, change: dict, old_tier: dict, new_tier: dict, direction: str):
        """Send tier change to Discord."""
        emoji = "🎉" if direction == "UP" else "⚠️"
        await self.notifier.send_alert(
            title=f"{emoji} Tier Change: {direction}!",
            message=(
                f"**{old_tier['name']}** → **{new_tier['name']}**\n\n"
                f"**Date:** {self._current_date}\n"
                f"**Balance:** ${change['balance']:,.2f}\n"
                f"**Instrument:** {change['from_instrument']} → {change['to_instrument']}\n"
                f"**Max Contracts:** {old_tier['max_contracts']} → {new_tier['max_contracts']}\n"
                f"**Loss Limit:** ${abs(old_tier['daily_loss_limit'])} → ${abs(new_tier['daily_loss_limit'])}"
            ),
            alert_type=AlertType.SUCCESS if direction == "UP" else AlertType.WARNING,
        )

    def _setup_day(self, date: str) -> None:
        """Set up components for a trading day."""
        self._current_date = date

        # Get tier settings
        tier_config = self.tier_manager.start_session()
        symbol = tier_config["instrument"]

        logger.info(f"\n{'='*60}")
        logger.info(f"DAY: {date}")
        logger.info(f"Tier: {tier_config['tier_name']}")
        logger.info(f"Symbol: {symbol}")
        logger.info(f"Balance: ${tier_config['balance']:,.2f}")
        logger.info(f"Max contracts: {tier_config['max_contracts']}")
        logger.info(f"Loss limit: ${abs(tier_config['daily_loss_limit'])}")
        logger.info(f"{'='*60}\n")

        # Create session
        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=5000,  # High limit for backtest
            daily_loss_limit=tier_config["daily_loss_limit"],
            max_position_size=tier_config["max_contracts"],
            stop_loss_ticks=16,
            take_profit_ticks=24,
            paper_starting_balance=tier_config["balance"],
            bypass_trading_hours=True,  # Backtest uses tick timestamps, not wall clock
        )

        # Create execution manager
        self.manager = ExecutionManager(self.session)
        self.manager.on_trade(self._on_trade)

        # Create engine and router
        self.engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})
        self.router = StrategyRouter({})

        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

        self._current_bar_signals = []
        self._trade_count = 0
        self._current_backtest_id = None

    def _on_bar(self, bar: FootprintBar) -> None:
        """Handle completed bar."""
        self._current_bar_signals = []

        if self.router:
            self.router.on_bar(bar)

        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal with tier-based sizing."""
        if not self.router or not self.manager:
            return

        self._current_bar_signals.append(signal)
        signal = self.router.evaluate_signal(signal)

        if signal.approved:
            # Count stacked signals
            stacked_count = sum(
                1 for s in self._current_bar_signals
                if s.direction == signal.direction
            )

            # Get position size from tier manager
            current_regime = self.router.current_regime if self.router else "UNKNOWN"
            position_size = self.tier_manager.get_position_size(
                regime=current_regime,
                stacked_count=stacked_count,
                use_streaks=True,
            )

            # Capture context BEFORE executing (for database logging)
            self._pending_trade_context = {
                "pattern": signal.pattern,
                "signal_strength": getattr(signal, "strength", 0),
                "regime": current_regime,
                "regime_score": getattr(self.router, "regime_score", None),
                "stacked_count": stacked_count,
                "tier_index": self.tier_manager.state.tier_index,
                "tier_name": self.tier_manager.state.tier_name,
                "instrument": self.tier_manager.state.instrument,
                "win_streak": self.tier_manager.state.win_streak,
                "loss_streak": self.tier_manager.state.loss_streak,
                "balance_before": self.tier_manager.state.balance,
            }

            order = self.manager.on_signal(signal, absolute_size=position_size)

            if order:
                logger.debug(
                    f"Order: {order.side} {order.size}x @ {order.entry_price} "
                    f"(stacked={stacked_count}, regime={current_regime})"
                )

    def _on_trade(self, trade) -> None:
        """Handle completed trade."""
        # Get context captured at signal time
        ctx = self._pending_trade_context or {}

        # Update tier manager (updates balance and streaks)
        self.tier_manager.record_trade(trade.pnl)
        balance_after = self.tier_manager.state.balance

        self._trade_count += 1

        # Track in memory
        self.all_trades.append({
            "side": trade.side,
            "size": trade.size,
            "entry": trade.entry_price,
            "exit": trade.exit_price,
            "pnl": trade.pnl,
            "exit_reason": trade.exit_reason,
            "tier_name": ctx.get("tier_name"),
            "instrument": ctx.get("instrument"),
        })

        # Log to database
        if self._current_backtest_id:
            # Convert enums to strings if needed
            pattern = ctx.get("pattern", "UNKNOWN")
            if hasattr(pattern, 'name'):
                pattern = pattern.name
            elif hasattr(pattern, 'value'):
                pattern = str(pattern.value)

            regime = ctx.get("regime", "UNKNOWN")
            if hasattr(regime, 'name'):
                regime = regime.name
            elif hasattr(regime, 'value'):
                regime = str(regime.value)

            log_trade(
                backtest_id=self._current_backtest_id,
                trade_num=self._trade_count,
                entry_time=trade.entry_time,
                exit_time=trade.exit_time,
                pattern=pattern,
                direction=trade.side,
                regime=regime,
                regime_score=ctx.get("regime_score"),
                signal_strength=ctx.get("signal_strength", 0),
                entry_price=trade.entry_price,
                exit_price=trade.exit_price,
                stop_price=getattr(trade, "stop_price", None),
                target_price=getattr(trade, "target_price", None),
                size=trade.size,
                pnl=trade.pnl,
                pnl_ticks=trade.pnl_ticks,
                exit_reason=trade.exit_reason,
                running_equity=balance_after - self.starting_balance,
                tier_index=ctx.get("tier_index"),
                tier_name=ctx.get("tier_name"),
                instrument=ctx.get("instrument"),
                stacked_count=ctx.get("stacked_count", 1),
                win_streak=ctx.get("win_streak", 0),
                loss_streak=ctx.get("loss_streak", 0),
                balance_before=ctx.get("balance_before"),
                balance_after=balance_after,
            )

        # Clear pending context
        self._pending_trade_context = {}

        emoji = "+" if trade.pnl >= 0 else ""
        logger.info(
            f"Trade: {trade.side} {trade.size}x | "
            f"P&L: {emoji}${trade.pnl:,.2f} | "
            f"Balance: ${self.tier_manager.state.balance:,.2f}"
        )

    async def _end_day(self, date: str, contract: str = "", tick_count: int = 0) -> dict:
        """End trading day and return results."""
        if not self.manager:
            return {}

        # Close any open positions
        if self.manager.open_positions:
            for pos in self.manager.open_positions:
                price = pos.current_price or pos.entry_price
            self.manager.close_all_positions(price, "END_OF_DAY")

        # Get stats
        daily_pnl = self.manager.daily_pnl
        trades = len(self.manager.completed_trades)
        wins = sum(1 for t in self.manager.completed_trades if t.pnl > 0)
        losses = trades - wins

        # End tier session
        session_result = self.tier_manager.end_session(daily_pnl)

        # Update backtest record with final stats
        if self._current_backtest_id:
            update_backtest(
                backtest_id=self._current_backtest_id,
                trades=trades,
                wins=wins,
                losses=losses,
                pnl=daily_pnl,
                notes=f"Tier: {self.tier_manager.state.tier_name} | "
                      f"Balance: ${self.tier_manager.state.balance:,.2f} | "
                      f"Contract: {contract}",
            )
            logger.info(f"Updated backtest #{self._current_backtest_id} with {trades} trades, ${daily_pnl:+,.2f} P&L")

        result = {
            "date": date,
            "pnl": daily_pnl,
            "trades": trades,
            "wins": wins,
            "win_rate": wins / trades * 100 if trades > 0 else 0,
            "tier": self.tier_manager.state.tier_name,
            "balance": self.tier_manager.state.balance,
            "instrument": self.tier_manager.state.instrument,
            "tier_changed": session_result.get("tier_changed", False),
            "backtest_id": self._current_backtest_id,
        }

        self.daily_results.append(result)

        logger.info(f"\nDay {date} Summary:")
        logger.info(f"  P&L: ${daily_pnl:+,.2f}")
        logger.info(f"  Trades: {trades} ({wins}W/{losses}L)")
        logger.info(f"  Balance: ${self.tier_manager.state.balance:,.2f}")
        logger.info(f"  Tier: {self.tier_manager.state.tier_name}")

        # Send daily digest to Discord
        if self.notifier:
            await self._send_daily_digest(result)

        return result

    async def _send_daily_digest(self, result: dict):
        """Send daily digest to Discord."""
        # Build trades detail (last 5)
        trades_detail = []
        for trade in self.manager.completed_trades[-5:]:
            trades_detail.append({
                "side": trade.side,
                "entry_price": trade.entry_price,
                "exit_price": trade.exit_price,
                "exit_reason": trade.exit_reason,
                "pnl": trade.pnl,
                "entry_time": trade.entry_time.strftime("%H:%M") if trade.entry_time else "",
            })

        # Get starting balance for the day
        day_start_balance = result["balance"] - result["pnl"]

        digest = DailyDigest(
            date=result["date"],
            session_start="09:30",
            session_end="16:00",
            status=f"COMPLETED | {result['tier']}",
            starting_balance=day_start_balance,
            ending_balance=result["balance"],
            day_pnl=result["pnl"],
            trades=result["trades"],
            wins=result["wins"],
            losses=result["trades"] - result["wins"],
            win_rate=result["win_rate"],
            trades_detail=trades_detail,
            regime_breakdown={},
            current_position="FLAT",
            account_balance=result["balance"],
        )

        await self.notifier.send_daily_digest(digest)

    async def run_day(self, date: str) -> dict:
        """Run backtest for a single day."""
        self._setup_day(date)

        # Get tick data
        symbol = self.tier_manager.state.instrument
        contract = DatabentoAdapter.get_front_month_contract(symbol, date)

        logger.info(f"Loading tick data for {contract} on {date}...")

        # Try cache first (FREE!)
        ticks = load_cached_ticks(contract, date)

        if ticks:
            logger.info(f"Loaded {len(ticks):,} ticks from cache (no Databento cost)")
        else:
            # Fall back to Databento (costs money)
            logger.warning(f"Cache miss for {contract} {date} - fetching from Databento...")
            adapter = DatabentoAdapter()
            ticks = adapter.get_session_ticks(
                contract=contract,
                date=date,
                start_time="09:30",
                end_time="16:00",
            )

        if not ticks:
            logger.warning(f"No tick data for {date}")
            return await self._end_day(date, contract, 0)

        # Create backtest record to get ID for trade logging
        self._current_backtest_id = log_backtest(
            symbol=symbol,
            contract=contract,
            date=date,
            start_time="09:30",
            end_time="16:00",
            ticks=len(ticks),
            notes=f"Tier backtest: {self.tier_manager.state.tier_name}",
            from_cache=True,  # Tick data is cached, don't count as spending
        )
        logger.info(f"Created backtest record #{self._current_backtest_id}")

        logger.info(f"Processing {len(ticks):,} ticks at {self.speed_multiplier}x speed...")

        # Process ticks
        tick_delay = 1.0 / self.speed_multiplier / 1000  # Approximate

        for i, tick in enumerate(ticks):
            self.engine.process_tick(tick)

            # Update position prices
            if self.manager and self.manager.open_positions:
                self.manager.update_prices(tick.price)

            # Check halt conditions
            if self.manager.is_halted:
                logger.info(f"Session halted: {self.manager.halt_reason}")
                break

            # Progress indicator
            if i > 0 and i % 50000 == 0:
                pct = i / len(ticks) * 100
                logger.info(f"  Progress: {pct:.0f}% ({i:,}/{len(ticks):,} ticks)")

            # Small delay for speed simulation
            if tick_delay > 0 and i % 1000 == 0:
                await asyncio.sleep(tick_delay)

        return await self._end_day(date, contract, len(ticks))

    async def run_week(self, dates: List[str]) -> None:
        """Run backtest for multiple days."""
        logger.info(f"\n{'='*60}")
        logger.info(f"TIER PROGRESSION BACKTEST")
        logger.info(f"Starting balance: ${self.starting_balance:,.2f}")
        logger.info(f"Days: {len(dates)}")
        logger.info(f"Speed: {self.speed_multiplier}x")
        logger.info(f"Discord: {'enabled' if self.notifier else 'disabled'}")
        logger.info(f"{'='*60}\n")

        week_start_idx = 0  # Track start of current week for weekly digest
        self._week_start_balance = self.starting_balance  # Start of first week

        # For short runs (5 days or less), skip Friday checks and send one digest at end
        is_single_week = len(dates) <= 5

        for i, date in enumerate(dates):
            await self.run_day(date)

            # Only check for Friday in multi-week runs
            if not is_single_week:
                date_obj = datetime.strptime(date, "%Y-%m-%d")
                if date_obj.weekday() == 4:  # Friday
                    if self.notifier:
                        await self._send_weekly_digest(dates[week_start_idx:i+1], self._week_start_balance)
                    week_start_idx = i + 1  # Next week starts after this
                    # Update week start balance for next week
                    self._week_start_balance = self.tier_manager.state.balance

        # Send final weekly digest (always for single week, or if didn't end on Friday)
        if dates and self.notifier:
            if is_single_week:
                # Single week - send one digest with all days
                await self._send_weekly_digest(dates, self._week_start_balance)
            elif week_start_idx < len(dates):
                # Multi-week that didn't end on Friday
                last_date = datetime.strptime(dates[-1], "%Y-%m-%d")
                if last_date.weekday() != 4:
                    await self._send_weekly_digest(dates[week_start_idx:], self._week_start_balance)

        self._print_summary()

        # Close notifier
        if self.notifier:
            await self.notifier.close()

    async def _send_weekly_digest(self, week_dates: List[str], week_start_balance: float):
        """Send weekly digest to Discord."""
        if not week_dates:
            return

        # Get results for this week
        week_results = [r for r in self.daily_results if r["date"] in week_dates]
        if not week_results:
            return

        # Calculate week stats - use actual start/end balance for accuracy
        end_balance = week_results[-1]["balance"]
        week_pnl = end_balance - week_start_balance  # Accurate P&L from balance change
        week_trades = sum(r["trades"] for r in week_results)
        week_wins = sum(r["wins"] for r in week_results)
        week_win_rate = week_wins / week_trades * 100 if week_trades > 0 else 0
        winning_days = sum(1 for r in week_results if r["pnl"] > 0)

        # Get tier changes this week
        week_tier_changes = [tc for tc in self.tier_changes if tc.get("date") in week_dates]

        # Build daily breakdown
        daily_lines = []
        for r in week_results:
            emoji = "+" if r["pnl"] >= 0 else ""
            daily_lines.append(f"{r['date']}: {emoji}${r['pnl']:,.0f} | {r['trades']}T | {r['instrument']}")

        # Build tier changes section
        tier_section = ""
        if week_tier_changes:
            tier_lines = [f"  {tc['direction']}: {tc['from']} → {tc['to']}" for tc in week_tier_changes]
            tier_section = f"\n**Tier Changes:**\n" + "\n".join(tier_lines)

        await self.notifier.send_alert(
            title=f"Weekly Summary: {week_dates[0]} to {week_dates[-1]}",
            message=(
                f"**Week P&L:** ${week_pnl:+,.2f}\n"
                f"**Balance:** ${week_start_balance:,.2f} → ${end_balance:,.2f}\n"
                f"**Trades:** {week_trades} ({week_win_rate:.0f}% WR)\n"
                f"**Winning Days:** {winning_days}/{len(week_results)}\n"
                f"**Current Tier:** {week_results[-1]['tier']}\n\n"
                f"**Daily Breakdown:**\n" + "\n".join(daily_lines) +
                tier_section
            ),
            alert_type=AlertType.SUCCESS if week_pnl >= 0 else AlertType.WARNING,
        )

    def _print_summary(self) -> None:
        """Print final summary."""
        print("\n" + "="*60)
        print("WEEK SUMMARY")
        print("="*60)

        print(f"\nStarting: ${self.starting_balance:,.2f} on MES")
        print(f"Ending:   ${self.tier_manager.state.balance:,.2f} on {self.tier_manager.state.instrument}")
        print(f"Total P&L: ${self.tier_manager.state.balance - self.starting_balance:+,.2f}")

        print("\n--- Daily Breakdown ---")
        for r in self.daily_results:
            tier_flag = " *TIER CHANGE*" if r["tier_changed"] else ""
            print(
                f"{r['date']}: ${r['pnl']:+,.0f} | "
                f"{r['trades']}T ({r['win_rate']:.0f}% WR) | "
                f"${r['balance']:,.0f} | {r['instrument']}{tier_flag}"
            )

        if self.tier_changes:
            print("\n--- Tier Changes ---")
            for tc in self.tier_changes:
                print(f"  {tc['direction']}: {tc['from']} -> {tc['to']} @ ${tc['balance']:,.2f}")

        print("\n--- Position Sizing Stats ---")
        sizes = [t.get("size", 1) for t in self.all_trades]
        if sizes:
            print(f"  Total trades: {len(sizes)}")
            print(f"  Avg size: {sum(sizes)/len(sizes):.1f} contracts")
            print(f"  Max size: {max(sizes)} contracts")
            print(f"  1 contract: {sizes.count(1)} trades")
            print(f"  2 contracts: {sizes.count(2)} trades")
            print(f"  3 contracts: {sizes.count(3)} trades")

        print("="*60)


async def main():
    parser = argparse.ArgumentParser(description="Tier Progression Backtest")
    parser.add_argument(
        "--week",
        help="Start date of week (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--dates",
        help="Comma-separated dates (YYYY-MM-DD,YYYY-MM-DD,...)",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=60.0,  # 1 min per hour
        help="Speed multiplier (default: 60 = 1min per hour)",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=2500.0,
        help="Starting balance (default: 2500)",
    )
    parser.add_argument(
        "--discord",
        action="store_true",
        help="Send notifications to Discord",
    )
    args = parser.parse_args()

    # Determine dates
    if args.dates:
        dates = [d.strip() for d in args.dates.split(",")]
    elif args.week:
        # Get 5 trading days starting from week start
        from src.data.backtest_db import get_connection
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT date FROM backtests WHERE date >= ? ORDER BY date LIMIT 5",
            (args.week,)
        )
        dates = [row[0] for row in cursor.fetchall()]
        conn.close()
    else:
        # Default: Oct 22-28, 2025
        dates = ["2025-10-22", "2025-10-23", "2025-10-24", "2025-10-27", "2025-10-28"]

    if not dates:
        print("No dates found!")
        return

    backtester = TierBacktester(
        starting_balance=args.balance,
        speed_multiplier=args.speed,
        send_discord=args.discord,
    )

    await backtester.run_week(dates)


if __name__ == "__main__":
    asyncio.run(main())
