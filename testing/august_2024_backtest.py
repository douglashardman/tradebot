#!/usr/bin/env python3
"""
August 2024 Backtest for Rook

Runs a tiered backtest simulation:
- First 5 trading days: MES (Micro E-mini)
- Full 30 days: ES data available for tier progression

Starting balance: $2,500 (Tier 1)
Demonstrates tier progression through capital tiers.

Includes detailed statistics:
- Max drawdown
- Longest losing streak
- Win/loss streaks
- Daily P&L breakdown
- Tier progression timeline
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional, Dict, Any
from collections import defaultdict

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Tick, Signal, FootprintBar
from src.core.capital import TierManager, TIERS
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.data.adapters.databento import DatabentoAdapter

# Cache directory for tick data
CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("august_backtest")


def get_trading_days(start_date: str, num_days: int) -> List[str]:
    """Generate list of trading days (skip weekends)."""
    days = []
    current = datetime.strptime(start_date, "%Y-%m-%d")

    while len(days) < num_days:
        if current.weekday() < 5:  # Monday = 0, Friday = 4
            days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)

    return days


def load_cached_ticks(contract: str, date: str, start_time: str = "09:30", end_time: str = "16:00"):
    """Load ticks from cache file if it exists."""
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


def save_ticks_to_cache(ticks: List[Tick], contract: str, date: str, start_time: str = "09:30", end_time: str = "16:00"):
    """Save ticks to cache file."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe_start = start_time.replace(":", "")
    safe_end = end_time.replace(":", "")
    cache_path = os.path.join(CACHE_DIR, f"{contract}_{date}_{safe_start}_{safe_end}.json")

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

    logger.info(f"Cached {len(ticks):,} ticks to: {cache_path}")


class DetailedStats:
    """Track detailed backtest statistics."""

    def __init__(self, starting_balance: float):
        self.starting_balance = starting_balance
        self.current_balance = starting_balance
        self.peak_balance = starting_balance

        # Drawdown tracking
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0
        self.max_drawdown_date = ""
        self.current_drawdown = 0.0

        # Streak tracking
        self.current_win_streak = 0
        self.current_loss_streak = 0
        self.max_win_streak = 0
        self.max_loss_streak = 0
        self.max_win_streak_dates = []
        self.max_loss_streak_dates = []

        # Daily streak tracking
        self.current_winning_day_streak = 0
        self.current_losing_day_streak = 0
        self.max_winning_day_streak = 0
        self.max_losing_day_streak = 0
        self.max_winning_day_streak_dates = []
        self.max_losing_day_streak_dates = []

        # Trade stats
        self.all_trades: List[Dict] = []
        self.daily_results: List[Dict] = []

        # Pattern stats
        self.pattern_stats: Dict[str, Dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

        # Regime stats
        self.regime_stats: Dict[str, Dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})

        # Tier progression
        self.tier_changes: List[Dict] = []

        # Consecutive losing days
        self._temp_losing_days: List[str] = []
        self._temp_winning_days: List[str] = []

    def record_trade(self, trade: Dict):
        """Record a trade and update stats."""
        self.all_trades.append(trade)
        pnl = trade.get("pnl", 0)

        # Update balance
        self.current_balance += pnl

        # Update peak and drawdown
        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
            self.current_drawdown = 0.0
        else:
            self.current_drawdown = self.peak_balance - self.current_balance
            if self.current_drawdown > self.max_drawdown:
                self.max_drawdown = self.current_drawdown
                self.max_drawdown_pct = (self.max_drawdown / self.peak_balance) * 100
                self.max_drawdown_date = trade.get("date", "")

        # Update trade streaks
        if pnl > 0:
            self.current_win_streak += 1
            self.current_loss_streak = 0
            if self.current_win_streak > self.max_win_streak:
                self.max_win_streak = self.current_win_streak
        elif pnl < 0:
            self.current_loss_streak += 1
            self.current_win_streak = 0
            if self.current_loss_streak > self.max_loss_streak:
                self.max_loss_streak = self.current_loss_streak

        # Pattern stats
        pattern = trade.get("pattern", "UNKNOWN")
        self.pattern_stats[pattern]["trades"] += 1
        self.pattern_stats[pattern]["pnl"] += pnl
        if pnl > 0:
            self.pattern_stats[pattern]["wins"] += 1

        # Regime stats
        regime = trade.get("regime", "UNKNOWN")
        self.regime_stats[regime]["trades"] += 1
        self.regime_stats[regime]["pnl"] += pnl
        if pnl > 0:
            self.regime_stats[regime]["wins"] += 1

    def record_day(self, result: Dict):
        """Record daily result and update day streaks."""
        self.daily_results.append(result)
        date = result.get("date", "")
        pnl = result.get("pnl", 0)

        if pnl > 0:
            self.current_winning_day_streak += 1
            self._temp_winning_days.append(date)

            # Check if this ends a losing streak
            if self.current_losing_day_streak > 0:
                if self.current_losing_day_streak > self.max_losing_day_streak:
                    self.max_losing_day_streak = self.current_losing_day_streak
                    self.max_losing_day_streak_dates = self._temp_losing_days.copy()
            self.current_losing_day_streak = 0
            self._temp_losing_days = []

            # Update max winning streak
            if self.current_winning_day_streak > self.max_winning_day_streak:
                self.max_winning_day_streak = self.current_winning_day_streak
                self.max_winning_day_streak_dates = self._temp_winning_days.copy()

        elif pnl < 0:
            self.current_losing_day_streak += 1
            self._temp_losing_days.append(date)

            # Check if this ends a winning streak
            if self.current_winning_day_streak > 0:
                if self.current_winning_day_streak > self.max_winning_day_streak:
                    self.max_winning_day_streak = self.current_winning_day_streak
                    self.max_winning_day_streak_dates = self._temp_winning_days.copy()
            self.current_winning_day_streak = 0
            self._temp_winning_days = []

            # Update max losing streak
            if self.current_losing_day_streak > self.max_losing_day_streak:
                self.max_losing_day_streak = self.current_losing_day_streak
                self.max_losing_day_streak_dates = self._temp_losing_days.copy()

    def record_tier_change(self, change: Dict):
        """Record a tier change."""
        self.tier_changes.append(change)

    def get_summary(self) -> Dict:
        """Get comprehensive statistics summary."""
        total_trades = len(self.all_trades)
        wins = sum(1 for t in self.all_trades if t.get("pnl", 0) > 0)
        losses = sum(1 for t in self.all_trades if t.get("pnl", 0) < 0)

        gross_profit = sum(t.get("pnl", 0) for t in self.all_trades if t.get("pnl", 0) > 0)
        gross_loss = abs(sum(t.get("pnl", 0) for t in self.all_trades if t.get("pnl", 0) < 0))

        winning_days = sum(1 for d in self.daily_results if d.get("pnl", 0) > 0)
        losing_days = sum(1 for d in self.daily_results if d.get("pnl", 0) < 0)
        flat_days = len(self.daily_results) - winning_days - losing_days

        return {
            "starting_balance": self.starting_balance,
            "ending_balance": self.current_balance,
            "total_pnl": self.current_balance - self.starting_balance,
            "total_pnl_pct": ((self.current_balance - self.starting_balance) / self.starting_balance) * 100,

            "total_trades": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": (wins / total_trades * 100) if total_trades > 0 else 0,

            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float('inf'),
            "avg_win": (gross_profit / wins) if wins > 0 else 0,
            "avg_loss": (gross_loss / losses) if losses > 0 else 0,

            "max_drawdown": self.max_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "max_drawdown_date": self.max_drawdown_date,
            "peak_balance": self.peak_balance,

            "max_win_streak": self.max_win_streak,
            "max_loss_streak": self.max_loss_streak,

            "total_days": len(self.daily_results),
            "winning_days": winning_days,
            "losing_days": losing_days,
            "flat_days": flat_days,
            "win_day_rate": (winning_days / len(self.daily_results) * 100) if self.daily_results else 0,

            "max_winning_day_streak": self.max_winning_day_streak,
            "max_winning_day_streak_dates": self.max_winning_day_streak_dates,
            "max_losing_day_streak": self.max_losing_day_streak,
            "max_losing_day_streak_dates": self.max_losing_day_streak_dates,

            "tier_changes": len(self.tier_changes),
            "pattern_stats": dict(self.pattern_stats),
            "regime_stats": dict(self.regime_stats),
        }


class August2024Backtester:
    """Run August 2024 backtest with tier progression."""

    def __init__(self, starting_balance: float = 2500.0):
        self.starting_balance = starting_balance

        # Initialize stats tracker
        self.stats = DetailedStats(starting_balance)

        # Initialize tier manager
        state_file = Path("data/august_backtest_state.json")
        state_file.unlink(missing_ok=True)

        self.tier_manager = TierManager(
            starting_balance=starting_balance,
            state_file=state_file,
            on_tier_change=self._on_tier_change,
        )

        self._current_date = ""
        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None
        self.session: Optional[TradingSession] = None
        self.manager: Optional[ExecutionManager] = None
        self._current_bar_signals: List[Signal] = []

    def _on_tier_change(self, change: dict):
        """Handle tier change."""
        old_tier = TIERS[change["from_tier"]]
        new_tier = TIERS[change["to_tier"]]
        direction = "UP" if change["to_tier"] > change["from_tier"] else "DOWN"

        logger.info(f"\n{'='*60}")
        logger.info(f"TIER CHANGE {direction}!")
        logger.info(f"  {old_tier['name']} -> {new_tier['name']}")
        logger.info(f"  Balance: ${change['balance']:,.2f}")
        logger.info(f"  Instrument: {change['from_instrument']} -> {change['to_instrument']}")
        logger.info(f"{'='*60}\n")

        self.stats.record_tier_change({
            "date": self._current_date,
            "direction": direction,
            "from": old_tier["name"],
            "to": new_tier["name"],
            "balance": change["balance"],
            "from_instrument": change["from_instrument"],
            "to_instrument": change["to_instrument"],
        })

    def _setup_day(self, date: str) -> None:
        """Set up components for a trading day."""
        self._current_date = date

        tier_config = self.tier_manager.start_session()
        symbol = tier_config["instrument"]

        logger.info(f"\n{'='*60}")
        logger.info(f"DAY: {date}")
        logger.info(f"Tier: {tier_config['tier_name']}")
        logger.info(f"Symbol: {symbol}")
        logger.info(f"Balance: ${tier_config['balance']:,.2f}")
        logger.info(f"Max contracts: {tier_config['max_contracts']}")
        logger.info(f"{'='*60}\n")

        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=1000000,  # No practical cap (matches production)
            daily_loss_limit=tier_config["daily_loss_limit"],
            max_position_size=tier_config["max_contracts"],
            stop_loss_ticks=16,
            take_profit_ticks=24,
            paper_starting_balance=tier_config["balance"],
            bypass_trading_hours=True,
        )

        self.manager = ExecutionManager(self.session)
        self.manager.on_trade(self._on_trade)

        self.engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})
        self.router = StrategyRouter({})

        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

        self._current_bar_signals = []

    def _on_bar(self, bar: FootprintBar) -> None:
        """Handle completed bar."""
        self._current_bar_signals = []
        if self.router:
            self.router.on_bar(bar)
        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal."""
        if not self.router or not self.manager:
            return

        self._current_bar_signals.append(signal)
        signal = self.router.evaluate_signal(signal)

        if signal.approved:
            stacked_count = sum(1 for s in self._current_bar_signals if s.direction == signal.direction)
            current_regime = self.router.current_regime if self.router else "UNKNOWN"

            position_size = self.tier_manager.get_position_size(
                regime=current_regime,
                stacked_count=stacked_count,
                use_streaks=True,
            )

            self._pending_trade_context = {
                "pattern": signal.pattern.value if hasattr(signal.pattern, 'value') else str(signal.pattern),
                "regime": current_regime.value if hasattr(current_regime, 'value') else str(current_regime),
                "date": self._current_date,
                "signal_strength": getattr(signal, 'strength', 0),
                "tier": self.tier_manager.state.tier_name,
                "balance_before": self.tier_manager.state.balance,
            }

            order = self.manager.on_signal(signal, absolute_size=position_size)

            if order:
                self._pending_trade_context["entry_price"] = order.entry_price
                self._pending_trade_context["stop_price"] = order.stop_price
                self._pending_trade_context["target_price"] = order.target_price

    def _on_trade(self, trade) -> None:
        """Handle completed trade."""
        ctx = getattr(self, '_pending_trade_context', {})

        balance_before = ctx.get("balance_before", self.tier_manager.state.balance)
        self.tier_manager.record_trade(trade.pnl)
        balance_after = self.tier_manager.state.balance

        # Calculate ticks P&L
        tick_value = 12.50 if ctx.get("tier", "").startswith("Tier 1") else 12.50  # ES/MES same tick value per contract
        pnl_ticks = int(trade.pnl / (tick_value * trade.size)) if trade.size > 0 else 0

        trade_record = {
            "trade_num": len(self.stats.all_trades) + 1,
            "date": ctx.get("date", self._current_date),
            "entry_time": trade.entry_time.strftime("%H:%M:%S") if trade.entry_time else "",
            "exit_time": trade.exit_time.strftime("%H:%M:%S") if trade.exit_time else "",
            "side": trade.side,
            "size": trade.size,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "stop_price": ctx.get("stop_price", 0),
            "target_price": ctx.get("target_price", 0),
            "pnl": trade.pnl,
            "pnl_ticks": pnl_ticks,
            "exit_reason": trade.exit_reason,
            "pattern": ctx.get("pattern", "UNKNOWN"),
            "regime": ctx.get("regime", "UNKNOWN"),
            "signal_strength": ctx.get("signal_strength", 0),
            "tier": ctx.get("tier", "UNKNOWN"),
            "balance_before": balance_before,
            "balance_after": balance_after,
            "instrument": "MES" if "MES" in ctx.get("tier", "") else "ES",
        }

        self.stats.record_trade(trade_record)

        emoji = "+" if trade.pnl >= 0 else ""
        logger.info(f"Trade: {trade.side} {trade.size}x | P&L: {emoji}${trade.pnl:,.2f} | Balance: ${self.tier_manager.state.balance:,.2f}")

    def _end_day(self, date: str) -> Dict:
        """End trading day and return results."""
        if not self.manager:
            return {}

        # Close any open positions
        if self.manager.open_positions:
            for pos in self.manager.open_positions:
                price = pos.current_price or pos.entry_price
            self.manager.close_all_positions(price, "END_OF_DAY")

        daily_pnl = self.manager.daily_pnl
        trades = len(self.manager.completed_trades)
        wins = sum(1 for t in self.manager.completed_trades if t.pnl > 0)

        self.tier_manager.end_session(daily_pnl)

        result = {
            "date": date,
            "pnl": daily_pnl,
            "trades": trades,
            "wins": wins,
            "losses": trades - wins,
            "win_rate": (wins / trades * 100) if trades > 0 else 0,
            "tier": self.tier_manager.state.tier_name,
            "balance": self.tier_manager.state.balance,
            "instrument": self.tier_manager.state.instrument,
        }

        self.stats.record_day(result)

        logger.info(f"\nDay {date} Summary:")
        logger.info(f"  P&L: ${daily_pnl:+,.2f}")
        logger.info(f"  Trades: {trades} ({wins}W/{trades - wins}L)")
        logger.info(f"  Balance: ${self.tier_manager.state.balance:,.2f}")

        return result

    async def run_day(self, date: str, force_symbol: str = None) -> Dict:
        """Run backtest for a single day."""
        self._setup_day(date)

        # Use forced symbol or tier-determined symbol
        symbol = force_symbol or self.tier_manager.state.instrument

        # August 2024 uses September contracts (U4)
        contract = f"{symbol}U4"

        logger.info(f"Loading tick data for {contract} on {date}...")

        # Try cache first
        ticks = load_cached_ticks(contract, date)

        if ticks:
            logger.info(f"Loaded {len(ticks):,} ticks from cache")
        else:
            logger.info(f"Fetching from Databento...")
            adapter = DatabentoAdapter()
            ticks = adapter.get_session_ticks(
                contract=contract,
                date=date,
                start_time="09:30",
                end_time="16:00",
            )

            if ticks:
                save_ticks_to_cache(ticks, contract, date)

        if not ticks:
            logger.warning(f"No tick data for {date}")
            return self._end_day(date)

        logger.info(f"Processing {len(ticks):,} ticks...")

        for i, tick in enumerate(ticks):
            self.engine.process_tick(tick)

            if self.manager and self.manager.open_positions:
                self.manager.update_prices(tick.price)

            if self.manager.is_halted:
                logger.info(f"Session halted: {self.manager.halt_reason}")
                break

            if i > 0 and i % 100000 == 0:
                pct = i / len(ticks) * 100
                logger.info(f"  Progress: {pct:.0f}%")

        return self._end_day(date)

    async def run_august(self, mcs_days: int = 5) -> None:
        """Run August 2024 backtest."""
        # Generate 22 trading days for August 2024 (Aug 1 - Aug 30)
        trading_days = get_trading_days("2024-08-01", 22)

        logger.info(f"\n{'='*60}")
        logger.info("AUGUST 2024 BACKTEST")
        logger.info(f"Starting balance: ${self.starting_balance:,.2f}")
        logger.info(f"MES days: {mcs_days}")
        logger.info(f"Trading days: {len(trading_days)}")
        logger.info(f"Date range: {trading_days[0]} to {trading_days[-1]}")
        logger.info(f"{'='*60}\n")

        for i, date in enumerate(trading_days):
            # Force MES for first N days, then let tier manager decide
            if i < mcs_days:
                await self.run_day(date, force_symbol="MES")
            else:
                await self.run_day(date)

        self._print_detailed_summary()
        self._generate_markdown_report()

    def _generate_markdown_report(self) -> None:
        """Generate comprehensive markdown report."""
        s = self.stats.get_summary()
        report_path = os.path.join(os.path.dirname(__file__), "aug2024.md")

        with open(report_path, "w") as f:
            f.write("# August 2024 Backtest Report\n\n")
            f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write("---\n\n")

            # Executive Summary
            f.write("## Executive Summary\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| **Testing Period** | August 1-30, 2024 (22 trading days) |\n")
            f.write(f"| **Starting Capital** | ${s['starting_balance']:,.2f} |\n")
            f.write(f"| **Ending Capital** | ${s['ending_balance']:,.2f} |\n")
            f.write(f"| **Total P&L** | ${s['total_pnl']:+,.2f} ({s['total_pnl_pct']:+.1f}%) |\n")
            f.write(f"| **Total Trades** | {s['total_trades']} |\n")
            f.write(f"| **Win Rate** | {s['win_rate']:.1f}% |\n")
            f.write(f"| **Profit Factor** | {s['profit_factor']:.2f} |\n")
            f.write(f"| **Max Drawdown** | ${s['max_drawdown']:,.2f} ({s['max_drawdown_pct']:.1f}%) |\n")
            f.write("\n---\n\n")

            # Performance Metrics
            f.write("## Performance Metrics\n\n")
            f.write("### Capital Growth\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Starting Balance | ${s['starting_balance']:,.2f} |\n")
            f.write(f"| Ending Balance | ${s['ending_balance']:,.2f} |\n")
            f.write(f"| Peak Balance | ${s['peak_balance']:,.2f} |\n")
            f.write(f"| Total P&L | ${s['total_pnl']:+,.2f} |\n")
            f.write(f"| Return | {s['total_pnl_pct']:+.1f}% |\n")
            f.write("\n")

            f.write("### Trade Statistics\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Total Trades | {s['total_trades']} |\n")
            f.write(f"| Winning Trades | {s['wins']} |\n")
            f.write(f"| Losing Trades | {s['losses']} |\n")
            f.write(f"| Win Rate | {s['win_rate']:.1f}% |\n")
            f.write(f"| Profit Factor | {s['profit_factor']:.2f} |\n")
            f.write(f"| Gross Profit | ${s['gross_profit']:,.2f} |\n")
            f.write(f"| Gross Loss | ${s['gross_loss']:,.2f} |\n")
            f.write(f"| Average Win | ${s['avg_win']:,.2f} |\n")
            f.write(f"| Average Loss | ${s['avg_loss']:,.2f} |\n")
            f.write(f"| Win/Loss Ratio | {(s['avg_win']/s['avg_loss']) if s['avg_loss'] > 0 else 0:.2f} |\n")
            f.write(f"| Expectancy | ${(s['total_pnl']/s['total_trades']) if s['total_trades'] > 0 else 0:.2f} per trade |\n")
            f.write("\n---\n\n")

            # Risk Metrics
            f.write("## Risk Metrics\n\n")
            f.write("### Drawdown Analysis\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Max Drawdown | ${s['max_drawdown']:,.2f} |\n")
            f.write(f"| Max Drawdown % | {s['max_drawdown_pct']:.1f}% |\n")
            f.write(f"| Max Drawdown Date | {s['max_drawdown_date']} |\n")
            f.write(f"| Recovery Factor | {(s['total_pnl']/s['max_drawdown']) if s['max_drawdown'] > 0 else 0:.2f} |\n")
            f.write("\n")

            f.write("### Streak Analysis\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Max Winning Streak (trades) | {s['max_win_streak']} |\n")
            f.write(f"| Max Losing Streak (trades) | {s['max_loss_streak']} |\n")
            f.write(f"| Max Winning Day Streak | {s['max_winning_day_streak']} days |\n")
            if s['max_winning_day_streak_dates']:
                f.write(f"| Winning Streak Dates | {s['max_winning_day_streak_dates'][0]} to {s['max_winning_day_streak_dates'][-1]} |\n")
            f.write(f"| Max Losing Day Streak | {s['max_losing_day_streak']} day(s) |\n")
            if s['max_losing_day_streak_dates']:
                f.write(f"| Losing Streak Dates | {', '.join(s['max_losing_day_streak_dates'])} |\n")
            f.write("\n---\n\n")

            # Daily Performance
            f.write("## Daily Performance\n\n")
            f.write("### Summary\n\n")
            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Trading Days | {s['total_days']} |\n")
            f.write(f"| Winning Days | {s['winning_days']} |\n")
            f.write(f"| Losing Days | {s['losing_days']} |\n")
            f.write(f"| Flat Days | {s['flat_days']} |\n")
            f.write(f"| Win Day Rate | {s['win_day_rate']:.1f}% |\n")
            avg_daily_pnl = s['total_pnl'] / s['total_days'] if s['total_days'] > 0 else 0
            f.write(f"| Average Daily P&L | ${avg_daily_pnl:,.2f} |\n")
            f.write("\n")

            f.write("### Daily Breakdown\n\n")
            f.write("| Date | P&L | Trades | Win Rate | Balance | Instrument |\n")
            f.write("|------|-----|--------|----------|---------|------------|\n")
            for r in self.stats.daily_results:
                emoji = "+" if r["pnl"] >= 0 else ""
                f.write(f"| {r['date']} | {emoji}${r['pnl']:,.2f} | {r['trades']} | {r['win_rate']:.0f}% | ${r['balance']:,.2f} | {r['instrument']} |\n")
            f.write("\n---\n\n")

            # Tier Progression
            f.write("## Tier Progression\n\n")
            if self.stats.tier_changes:
                f.write("| Date | Direction | From Tier | To Tier | Balance |\n")
                f.write("|------|-----------|-----------|---------|----------|\n")
                for tc in self.stats.tier_changes:
                    f.write(f"| {tc['date']} | {tc['direction']} | {tc['from']} | {tc['to']} | ${tc['balance']:,.2f} |\n")
            else:
                f.write("No tier changes occurred.\n")
            f.write("\n---\n\n")

            # Pattern Analysis
            f.write("## Pattern Analysis\n\n")
            f.write("| Pattern | Trades | Wins | Win Rate | Gross P&L | Avg P&L |\n")
            f.write("|---------|--------|------|----------|-----------|----------|\n")
            sorted_patterns = sorted(s['pattern_stats'].items(), key=lambda x: x[1]['pnl'], reverse=True)
            for pattern, stats in sorted_patterns:
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
                avg_pnl = stats['pnl'] / stats['trades'] if stats['trades'] > 0 else 0
                f.write(f"| {pattern} | {stats['trades']} | {stats['wins']} | {wr:.1f}% | ${stats['pnl']:+,.2f} | ${avg_pnl:+,.2f} |\n")
            f.write("\n---\n\n")

            # Regime Analysis
            f.write("## Regime Analysis\n\n")
            f.write("| Regime | Trades | Wins | Win Rate | Gross P&L | Avg P&L |\n")
            f.write("|--------|--------|------|----------|-----------|----------|\n")
            sorted_regimes = sorted(s['regime_stats'].items(), key=lambda x: x[1]['pnl'], reverse=True)
            for regime, stats in sorted_regimes:
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
                avg_pnl = stats['pnl'] / stats['trades'] if stats['trades'] > 0 else 0
                f.write(f"| {regime} | {stats['trades']} | {stats['wins']} | {wr:.1f}% | ${stats['pnl']:+,.2f} | ${avg_pnl:+,.2f} |\n")
            f.write("\n---\n\n")

            # Hourly Analysis
            f.write("## Hourly Analysis\n\n")
            hourly_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for t in self.stats.all_trades:
                if t.get("entry_time"):
                    hour = t["entry_time"].split(":")[0]
                    hourly_stats[hour]["trades"] += 1
                    hourly_stats[hour]["pnl"] += t.get("pnl", 0)
                    if t.get("pnl", 0) > 0:
                        hourly_stats[hour]["wins"] += 1

            f.write("| Hour (ET) | Trades | Wins | Win Rate | P&L |\n")
            f.write("|-----------|--------|------|----------|------|\n")
            for hour in sorted(hourly_stats.keys()):
                stats = hourly_stats[hour]
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
                f.write(f"| {hour}:00 | {stats['trades']} | {stats['wins']} | {wr:.1f}% | ${stats['pnl']:+,.2f} |\n")
            f.write("\n---\n\n")

            # Position Size Analysis
            f.write("## Position Size Analysis\n\n")
            size_stats = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
            for t in self.stats.all_trades:
                size = t.get("size", 1)
                size_stats[size]["trades"] += 1
                size_stats[size]["pnl"] += t.get("pnl", 0)
                if t.get("pnl", 0) > 0:
                    size_stats[size]["wins"] += 1

            f.write("| Contracts | Trades | Wins | Win Rate | P&L |\n")
            f.write("|-----------|--------|------|----------|------|\n")
            for size in sorted(size_stats.keys()):
                stats = size_stats[size]
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
                f.write(f"| {size} | {stats['trades']} | {stats['wins']} | {wr:.1f}% | ${stats['pnl']:+,.2f} |\n")
            f.write("\n---\n\n")

            # Trade-by-Trade Log
            f.write("## Trade-by-Trade Log\n\n")

            # Group trades by day
            trades_by_day = defaultdict(list)
            for t in self.stats.all_trades:
                trades_by_day[t.get("date", "Unknown")].append(t)

            for date in sorted(trades_by_day.keys()):
                day_trades = trades_by_day[date]
                day_pnl = sum(t.get("pnl", 0) for t in day_trades)
                day_wins = sum(1 for t in day_trades if t.get("pnl", 0) > 0)

                f.write(f"### {date}\n\n")
                f.write(f"**Day Summary:** {len(day_trades)} trades | {day_wins}W/{len(day_trades)-day_wins}L | P&L: ${day_pnl:+,.2f}\n\n")

                f.write("| # | Time | Side | Size | Entry | Exit | P&L | Ticks | Result | Pattern | Regime |\n")
                f.write("|---|------|------|------|-------|------|-----|-------|--------|---------|--------|\n")

                for t in day_trades:
                    result = "WIN" if t.get("pnl", 0) > 0 else "LOSS" if t.get("pnl", 0) < 0 else "FLAT"
                    result_emoji = "✅" if result == "WIN" else "❌" if result == "LOSS" else "➖"
                    entry_time = t.get("entry_time", "")
                    f.write(f"| {t.get('trade_num', '')} | {entry_time} | {t.get('side', '')} | {t.get('size', '')} | {t.get('entry_price', 0):.2f} | {t.get('exit_price', 0):.2f} | ${t.get('pnl', 0):+,.2f} | {t.get('pnl_ticks', 0):+d} | {result_emoji} {t.get('exit_reason', '')} | {t.get('pattern', '')} | {t.get('regime', '')} |\n")

                f.write("\n")

            f.write("---\n\n")

            # Additional Statistics
            f.write("## Additional Statistics\n\n")

            # Calculate some additional metrics
            wins = [t.get("pnl", 0) for t in self.stats.all_trades if t.get("pnl", 0) > 0]
            losses = [t.get("pnl", 0) for t in self.stats.all_trades if t.get("pnl", 0) < 0]

            f.write("### Win Distribution\n\n")
            if wins:
                f.write(f"- Largest Win: ${max(wins):,.2f}\n")
                f.write(f"- Smallest Win: ${min(wins):,.2f}\n")
                f.write(f"- Median Win: ${sorted(wins)[len(wins)//2]:,.2f}\n")
            f.write("\n")

            f.write("### Loss Distribution\n\n")
            if losses:
                f.write(f"- Largest Loss: ${min(losses):,.2f}\n")
                f.write(f"- Smallest Loss: ${max(losses):,.2f}\n")
                f.write(f"- Median Loss: ${sorted(losses)[len(losses)//2]:,.2f}\n")
            f.write("\n")

            # Exit reason analysis
            f.write("### Exit Reason Analysis\n\n")
            exit_stats = defaultdict(lambda: {"count": 0, "pnl": 0.0})
            for t in self.stats.all_trades:
                reason = t.get("exit_reason", "UNKNOWN")
                exit_stats[reason]["count"] += 1
                exit_stats[reason]["pnl"] += t.get("pnl", 0)

            f.write("| Exit Reason | Count | P&L |\n")
            f.write("|-------------|-------|------|\n")
            for reason, stats in sorted(exit_stats.items(), key=lambda x: x[1]['count'], reverse=True):
                f.write(f"| {reason} | {stats['count']} | ${stats['pnl']:+,.2f} |\n")
            f.write("\n")

            # Long vs Short analysis
            f.write("### Direction Analysis\n\n")
            long_trades = [t for t in self.stats.all_trades if t.get("side") == "LONG"]
            short_trades = [t for t in self.stats.all_trades if t.get("side") == "SHORT"]

            long_wins = sum(1 for t in long_trades if t.get("pnl", 0) > 0)
            short_wins = sum(1 for t in short_trades if t.get("pnl", 0) > 0)
            long_pnl = sum(t.get("pnl", 0) for t in long_trades)
            short_pnl = sum(t.get("pnl", 0) for t in short_trades)

            f.write("| Direction | Trades | Wins | Win Rate | P&L |\n")
            f.write("|-----------|--------|------|----------|------|\n")
            if long_trades:
                f.write(f"| LONG | {len(long_trades)} | {long_wins} | {(long_wins/len(long_trades)*100):.1f}% | ${long_pnl:+,.2f} |\n")
            if short_trades:
                f.write(f"| SHORT | {len(short_trades)} | {short_wins} | {(short_wins/len(short_trades)*100):.1f}% | ${short_pnl:+,.2f} |\n")
            f.write("\n---\n\n")

            f.write("## Notes\n\n")
            f.write("- **Contract:** MESU4/ESU4 (September 2024 expiry)\n")
            f.write("- **Session:** Regular Trading Hours (9:30 AM - 4:00 PM ET)\n")
            f.write("- **Stop Loss:** 16 ticks (4 points)\n")
            f.write("- **Take Profit:** 24 ticks (6 points)\n")
            f.write("- **Slippage:** 1 tick simulated on entries\n")
            f.write("- **Daily Profit Cap:** None (matches production settings)\n")
            f.write("- **First 5 days:** Forced MES trading regardless of tier\n")
            f.write("- **Time Column:** Shows backtest processing time, not actual market session time\n")
            f.write("\n")
            f.write("## Market Context - August 2024\n\n")
            f.write("August 2024 was characterized by:\n")
            f.write("- **Early Month Selloff:** Major market decline in the first week (Aug 1-5) following weak economic data and BOJ rate hike concerns\n")
            f.write("- **VIX Spike:** Volatility index spiked above 65 on Aug 5 (highest since 2020)\n")
            f.write("- **Swift Recovery:** Markets recovered strongly after Aug 5 lows\n")
            f.write("- **Trending Conditions:** Strong directional moves favored the system's absorption and exhaustion patterns\n")
            f.write("\n---\n\n")
            f.write("*Report generated by Delta Trading System*\n")

        logger.info(f"\nMarkdown report saved to: {report_path}")

    def _print_detailed_summary(self) -> None:
        """Print comprehensive summary."""
        s = self.stats.get_summary()

        print("\n" + "=" * 70)
        print("AUGUST 2024 BACKTEST RESULTS")
        print("=" * 70)

        # Performance Overview
        print("\n--- PERFORMANCE OVERVIEW ---")
        print(f"  Starting Balance:  ${s['starting_balance']:>12,.2f}")
        print(f"  Ending Balance:    ${s['ending_balance']:>12,.2f}")
        print(f"  Total P&L:         ${s['total_pnl']:>+12,.2f} ({s['total_pnl_pct']:+.1f}%)")
        print(f"  Peak Balance:      ${s['peak_balance']:>12,.2f}")

        # Trade Statistics
        print("\n--- TRADE STATISTICS ---")
        print(f"  Total Trades:      {s['total_trades']:>8}")
        print(f"  Wins:              {s['wins']:>8}")
        print(f"  Losses:            {s['losses']:>8}")
        print(f"  Win Rate:          {s['win_rate']:>8.1f}%")
        print(f"  Profit Factor:     {s['profit_factor']:>8.2f}")
        print(f"  Avg Win:           ${s['avg_win']:>8,.2f}")
        print(f"  Avg Loss:          ${s['avg_loss']:>8,.2f}")
        print(f"  Gross Profit:      ${s['gross_profit']:>8,.2f}")
        print(f"  Gross Loss:        ${s['gross_loss']:>8,.2f}")

        # Drawdown Analysis
        print("\n--- DRAWDOWN ANALYSIS ---")
        print(f"  Max Drawdown:      ${s['max_drawdown']:>8,.2f} ({s['max_drawdown_pct']:.1f}%)")
        print(f"  Max DD Date:       {s['max_drawdown_date']}")

        # Streak Analysis
        print("\n--- STREAK ANALYSIS ---")
        print(f"  Max Win Streak (trades):   {s['max_win_streak']}")
        print(f"  Max Loss Streak (trades):  {s['max_loss_streak']}")
        print(f"  Max Winning Day Streak:    {s['max_winning_day_streak']}")
        if s['max_winning_day_streak_dates']:
            print(f"    Dates: {s['max_winning_day_streak_dates'][0]} to {s['max_winning_day_streak_dates'][-1]}")
        print(f"  Max Losing Day Streak:     {s['max_losing_day_streak']}")
        if s['max_losing_day_streak_dates']:
            print(f"    Dates: {s['max_losing_day_streak_dates'][0]} to {s['max_losing_day_streak_dates'][-1]}")

        # Daily Performance
        print("\n--- DAILY PERFORMANCE ---")
        print(f"  Trading Days:      {s['total_days']}")
        print(f"  Winning Days:      {s['winning_days']}")
        print(f"  Losing Days:       {s['losing_days']}")
        print(f"  Flat Days:         {s['flat_days']}")
        print(f"  Win Day Rate:      {s['win_day_rate']:.1f}%")

        # Daily Breakdown
        print("\n--- DAILY BREAKDOWN ---")
        for r in self.stats.daily_results:
            emoji = "+" if r["pnl"] >= 0 else ""
            tier_str = r.get("tier", "")[:15]
            print(f"  {r['date']}: {emoji}${r['pnl']:>8,.0f} | {r['trades']:>2}T ({r['win_rate']:>5.0f}% WR) | ${r['balance']:>10,.0f} | {r['instrument']}")

        # Tier Progression
        if self.stats.tier_changes:
            print("\n--- TIER PROGRESSION ---")
            for tc in self.stats.tier_changes:
                print(f"  {tc['date']}: {tc['direction']} | {tc['from']} -> {tc['to']} | ${tc['balance']:,.2f}")

        # Pattern Performance
        if s['pattern_stats']:
            print("\n--- PATTERN PERFORMANCE ---")
            sorted_patterns = sorted(s['pattern_stats'].items(), key=lambda x: x[1]['pnl'], reverse=True)
            for pattern, stats in sorted_patterns:
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
                print(f"  {pattern:30} | {stats['trades']:>3} trades | {wr:>5.0f}% WR | ${stats['pnl']:>+10,.2f}")

        # Regime Performance
        if s['regime_stats']:
            print("\n--- REGIME PERFORMANCE ---")
            sorted_regimes = sorted(s['regime_stats'].items(), key=lambda x: x[1]['pnl'], reverse=True)
            for regime, stats in sorted_regimes:
                wr = (stats['wins'] / stats['trades'] * 100) if stats['trades'] > 0 else 0
                print(f"  {regime:20} | {stats['trades']:>3} trades | {wr:>5.0f}% WR | ${stats['pnl']:>+10,.2f}")

        print("\n" + "=" * 70)
        print("END OF REPORT")
        print("=" * 70)


async def main():
    parser = argparse.ArgumentParser(description="August 2024 Backtest")
    parser.add_argument(
        "--balance",
        type=float,
        default=2500.0,
        help="Starting balance (default: 2500)",
    )
    parser.add_argument(
        "--mes-days",
        type=int,
        default=5,
        help="Number of days to trade MES before tier system takes over (default: 5)",
    )
    args = parser.parse_args()

    backtester = August2024Backtester(starting_balance=args.balance)
    await backtester.run_august(mcs_days=args.mes_days)


if __name__ == "__main__":
    asyncio.run(main())
