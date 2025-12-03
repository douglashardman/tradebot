#!/usr/bin/env python3
"""
August 2024 Backtest - BAR-LEVEL STOP CHECKING VERSION

This version matches how Bishop LIVE actually works:
- Stops/targets are ONLY checked at bar close (every 5 minutes)
- NOT checked on every tick

This gives realistic results that match live trading performance.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, time
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
logger = logging.getLogger("august_bar_level")


def get_trading_days(start_date: str, num_days: int) -> List[str]:
    """Generate list of trading days (skip weekends)."""
    days = []
    current = datetime.strptime(start_date, "%Y-%m-%d")

    while len(days) < num_days:
        if current.weekday() < 5:
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
        self.max_drawdown = 0.0
        self.max_drawdown_pct = 0.0
        self.max_drawdown_date = ""
        self.current_drawdown = 0.0
        self.current_win_streak = 0
        self.current_loss_streak = 0
        self.max_win_streak = 0
        self.max_loss_streak = 0
        self.current_winning_day_streak = 0
        self.current_losing_day_streak = 0
        self.max_winning_day_streak = 0
        self.max_losing_day_streak = 0
        self.max_winning_day_streak_dates = []
        self.max_losing_day_streak_dates = []
        self.all_trades: List[Dict] = []
        self.daily_results: List[Dict] = []
        self.pattern_stats: Dict[str, Dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
        self.regime_stats: Dict[str, Dict] = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
        self.tier_changes: List[Dict] = []
        self._temp_losing_days: List[str] = []
        self._temp_winning_days: List[str] = []

    def record_trade(self, trade: Dict):
        """Record a trade and update stats."""
        self.all_trades.append(trade)
        pnl = trade.get("pnl", 0)
        self.current_balance += pnl

        if self.current_balance > self.peak_balance:
            self.peak_balance = self.current_balance
            self.current_drawdown = 0.0
        else:
            self.current_drawdown = self.peak_balance - self.current_balance
            if self.current_drawdown > self.max_drawdown:
                self.max_drawdown = self.current_drawdown
                self.max_drawdown_pct = (self.max_drawdown / self.peak_balance) * 100
                self.max_drawdown_date = trade.get("date", "")

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

        pattern = trade.get("pattern", "UNKNOWN")
        self.pattern_stats[pattern]["trades"] += 1
        self.pattern_stats[pattern]["pnl"] += pnl
        if pnl > 0:
            self.pattern_stats[pattern]["wins"] += 1

        regime = trade.get("regime", "UNKNOWN")
        self.regime_stats[regime]["trades"] += 1
        self.regime_stats[regime]["pnl"] += pnl
        if pnl > 0:
            self.regime_stats[regime]["wins"] += 1

    def record_day(self, result: Dict):
        """Record daily result."""
        self.daily_results.append(result)
        date = result.get("date", "")
        pnl = result.get("pnl", 0)

        if pnl > 0:
            self.current_winning_day_streak += 1
            self._temp_winning_days.append(date)
            if self.current_losing_day_streak > 0:
                if self.current_losing_day_streak > self.max_losing_day_streak:
                    self.max_losing_day_streak = self.current_losing_day_streak
                    self.max_losing_day_streak_dates = self._temp_losing_days.copy()
            self.current_losing_day_streak = 0
            self._temp_losing_days = []
            if self.current_winning_day_streak > self.max_winning_day_streak:
                self.max_winning_day_streak = self.current_winning_day_streak
                self.max_winning_day_streak_dates = self._temp_winning_days.copy()
        elif pnl < 0:
            self.current_losing_day_streak += 1
            self._temp_losing_days.append(date)
            if self.current_winning_day_streak > 0:
                if self.current_winning_day_streak > self.max_winning_day_streak:
                    self.max_winning_day_streak = self.current_winning_day_streak
                    self.max_winning_day_streak_dates = self._temp_winning_days.copy()
            self.current_winning_day_streak = 0
            self._temp_winning_days = []
            if self.current_losing_day_streak > self.max_losing_day_streak:
                self.max_losing_day_streak = self.current_losing_day_streak
                self.max_losing_day_streak_dates = self._temp_losing_days.copy()

    def record_tier_change(self, change: Dict):
        self.tier_changes.append(change)

    def get_summary(self) -> Dict:
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
            "max_losing_day_streak": self.max_losing_day_streak,
            "tier_changes": len(self.tier_changes),
            "pattern_stats": dict(self.pattern_stats),
            "regime_stats": dict(self.regime_stats),
        }


class August2024BarLevelBacktester:
    """
    August 2024 backtest with BAR-LEVEL stop checking.

    KEY DIFFERENCE: Stops/targets are ONLY checked when bars complete,
    NOT on every tick. This matches how Bishop LIVE actually works.
    """

    def __init__(self, starting_balance: float = 2500.0):
        self.starting_balance = starting_balance
        self.stats = DetailedStats(starting_balance)

        state_file = Path("data/august_bar_level_state.json")
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
        old_tier = TIERS[change["from_tier"]]
        new_tier = TIERS[change["to_tier"]]
        direction = "UP" if change["to_tier"] > change["from_tier"] else "DOWN"

        logger.info(f"\n{'='*60}")
        logger.info(f"TIER CHANGE {direction}!")
        logger.info(f"  {old_tier['name']} -> {new_tier['name']}")
        logger.info(f"  Balance: ${change['balance']:,.2f}")
        logger.info(f"{'='*60}\n")

        self.stats.record_tier_change({
            "date": self._current_date,
            "direction": direction,
            "from": old_tier["name"],
            "to": new_tier["name"],
            "balance": change["balance"],
        })

    def _setup_day(self, date: str) -> None:
        self._current_date = date
        tier_config = self.tier_manager.start_session()
        symbol = tier_config["instrument"]

        logger.info(f"\n{'='*60}")
        logger.info(f"DAY: {date}")
        logger.info(f"Tier: {tier_config['tier_name']}")
        logger.info(f"Symbol: {symbol}")
        logger.info(f"Balance: ${tier_config['balance']:,.2f}")
        logger.info(f"{'='*60}\n")

        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=1000000,
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
        """
        Handle completed bar - THIS IS WHERE STOPS ARE CHECKED.

        This matches run_headless.py behavior exactly.
        """
        self._current_bar_signals = []

        if self.router:
            self.router.on_bar(bar)

        # KEY: Only check stops/targets at bar close, not on every tick
        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal) -> None:
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
                "tier": self.tier_manager.state.tier_name,
                "balance_before": self.tier_manager.state.balance,
            }

            order = self.manager.on_signal(signal, absolute_size=position_size)

            if order:
                self._pending_trade_context["entry_price"] = order.entry_price
                self._pending_trade_context["stop_price"] = order.stop_price
                self._pending_trade_context["target_price"] = order.target_price

    def _on_trade(self, trade) -> None:
        ctx = getattr(self, '_pending_trade_context', {})
        balance_before = ctx.get("balance_before", self.tier_manager.state.balance)
        self.tier_manager.record_trade(trade.pnl)
        balance_after = self.tier_manager.state.balance

        tick_value = 12.50
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
            "pnl": trade.pnl,
            "pnl_ticks": pnl_ticks,
            "exit_reason": trade.exit_reason,
            "pattern": ctx.get("pattern", "UNKNOWN"),
            "regime": ctx.get("regime", "UNKNOWN"),
            "tier": ctx.get("tier", "UNKNOWN"),
            "balance_before": balance_before,
            "balance_after": balance_after,
            "instrument": "MES" if "MES" in ctx.get("tier", "") else "ES",
        }

        self.stats.record_trade(trade_record)

        emoji = "+" if trade.pnl >= 0 else ""
        logger.info(f"Trade: {trade.side} {trade.size}x | P&L: {emoji}${trade.pnl:,.2f} | Balance: ${self.tier_manager.state.balance:,.2f}")

    def _end_day(self, date: str) -> Dict:
        if not self.manager:
            return {}

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
        self._setup_day(date)
        symbol = force_symbol or self.tier_manager.state.instrument
        contract = f"{symbol}U4"

        logger.info(f"Loading tick data for {contract} on {date}...")

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

        logger.info(f"Processing {len(ticks):,} ticks (BAR-LEVEL stop checking)...")

        flatten_time = time(15, 55)
        flattened = False

        for i, tick in enumerate(ticks):
            tick_time = tick.timestamp.time() if hasattr(tick.timestamp, 'time') else None
            if tick_time and tick_time >= flatten_time and not flattened:
                if self.manager and self.manager.open_positions:
                    logger.info(f"Flattening at 3:55 PM ET")
                    self.manager.close_all_positions(tick.price, "FLATTEN")
                flattened = True

            if flattened:
                continue

            # Process tick through engine (builds bars, detects signals)
            self.engine.process_tick(tick)

            # KEY DIFFERENCE: We do NOT call update_prices on every tick!
            # Stops are only checked in _on_bar when bars complete.

            if self.manager.is_halted:
                logger.info(f"Session halted: {self.manager.halt_reason}")
                break

            if i > 0 and i % 100000 == 0:
                pct = i / len(ticks) * 100
                logger.info(f"  Progress: {pct:.0f}%")

        return self._end_day(date)

    async def run_august(self, mcs_days: int = 5) -> None:
        trading_days = get_trading_days("2024-08-01", 22)

        logger.info(f"\n{'='*60}")
        logger.info("AUGUST 2024 BACKTEST (BAR-LEVEL STOP CHECKING)")
        logger.info("=" * 60)
        logger.info("This version matches how Bishop LIVE actually works:")
        logger.info("- Stops/targets checked at BAR CLOSE only (every 5 min)")
        logger.info("- NOT checked on every tick")
        logger.info("=" * 60)
        logger.info(f"Starting balance: ${self.starting_balance:,.2f}")
        logger.info(f"MES days: {mcs_days}")
        logger.info(f"Trading days: {len(trading_days)}")
        logger.info(f"Date range: {trading_days[0]} to {trading_days[-1]}")
        logger.info(f"{'='*60}\n")

        for i, date in enumerate(trading_days):
            if i < mcs_days:
                await self.run_day(date, force_symbol="MES")
            else:
                await self.run_day(date)

        self._print_summary()

    def _print_summary(self) -> None:
        s = self.stats.get_summary()

        print("\n" + "=" * 70)
        print("AUGUST 2024 BACKTEST RESULTS (BAR-LEVEL STOPS)")
        print("=" * 70)

        print("\n--- PERFORMANCE OVERVIEW ---")
        print(f"  Starting Balance:  ${s['starting_balance']:>12,.2f}")
        print(f"  Ending Balance:    ${s['ending_balance']:>12,.2f}")
        print(f"  Total P&L:         ${s['total_pnl']:>+12,.2f} ({s['total_pnl_pct']:+.1f}%)")

        print("\n--- TRADE STATISTICS ---")
        print(f"  Total Trades:      {s['total_trades']:>8}")
        print(f"  Wins:              {s['wins']:>8}")
        print(f"  Losses:            {s['losses']:>8}")
        print(f"  Win Rate:          {s['win_rate']:>8.1f}%")
        print(f"  Profit Factor:     {s['profit_factor']:>8.2f}")
        print(f"  Avg Win:           ${s['avg_win']:>8,.2f}")
        print(f"  Avg Loss:          ${s['avg_loss']:>8,.2f}")

        print("\n--- DRAWDOWN ---")
        print(f"  Max Drawdown:      ${s['max_drawdown']:>8,.2f} ({s['max_drawdown_pct']:.1f}%)")
        print(f"  Max DD Date:       {s['max_drawdown_date']}")

        print("\n--- DAILY PERFORMANCE ---")
        print(f"  Trading Days:      {s['total_days']}")
        print(f"  Winning Days:      {s['winning_days']}")
        print(f"  Losing Days:       {s['losing_days']}")
        print(f"  Win Day Rate:      {s['win_day_rate']:.1f}%")

        print("\n--- DAILY BREAKDOWN ---")
        for r in self.stats.daily_results:
            emoji = "+" if r["pnl"] >= 0 else ""
            print(f"  {r['date']}: {emoji}${r['pnl']:>8,.0f} | {r['trades']:>2}T ({r['win_rate']:>5.0f}% WR) | ${r['balance']:>10,.0f} | {r['instrument']}")

        if self.stats.tier_changes:
            print("\n--- TIER PROGRESSION ---")
            for tc in self.stats.tier_changes:
                print(f"  {tc['date']}: {tc['direction']} | {tc['from']} -> {tc['to']} | ${tc['balance']:,.2f}")

        print("\n" + "=" * 70)
        print("END OF REPORT")
        print("=" * 70)


async def main():
    parser = argparse.ArgumentParser(description="August 2024 Backtest (Bar-Level Stops)")
    parser.add_argument("--balance", type=float, default=2500.0, help="Starting balance")
    parser.add_argument("--mes-days", type=int, default=5, help="Days to force MES trading")
    args = parser.parse_args()

    backtester = August2024BarLevelBacktester(starting_balance=args.balance)
    await backtester.run_august(mcs_days=args.mes_days)


if __name__ == "__main__":
    asyncio.run(main())
