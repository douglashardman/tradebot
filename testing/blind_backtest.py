#!/usr/bin/env python3
"""
Blind Backtest - Random period selection with 2-tick slippage.

Runs a 30-day tier progression test starting at $2,500 on MES,
using existing cached tick data.

The dates are selected randomly and not revealed until results.
"""

import asyncio
import json
import logging
import os
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("blind_backtest")

# Cache directory
CACHE_DIR = Path(__file__).parent.parent / "data" / "tick_cache"

# 2-TICK SLIPPAGE (doubled from default 1)
SLIPPAGE_TICKS = 2


def get_available_dates() -> List[str]:
    """Get all dates with cached data."""
    dates = set()
    for f in CACHE_DIR.glob("*.json"):
        name = f.stem
        if name.endswith("_recap"):
            continue
        parts = name.split("_")
        if len(parts) >= 2:
            date = parts[1]
            if date.startswith("2025-"):
                dates.add(date)
    return sorted(dates)


def get_consecutive_periods(dates: List[str], min_length: int = 30) -> List[List[str]]:
    """Find consecutive date periods of at least min_length days."""
    if not dates:
        return []

    periods = []
    current_period = [dates[0]]

    for i in range(1, len(dates)):
        prev = datetime.strptime(dates[i-1], "%Y-%m-%d")
        curr = datetime.strptime(dates[i], "%Y-%m-%d")

        # Allow up to 3-day gaps (weekends + holiday)
        if (curr - prev).days <= 4:
            current_period.append(dates[i])
        else:
            if len(current_period) >= min_length:
                periods.append(current_period)
            current_period = [dates[i]]

    if len(current_period) >= min_length:
        periods.append(current_period)

    return periods


def load_cached_ticks(contract: str, date: str) -> Optional[List[Tick]]:
    """Load ticks from cache."""
    cache_file = CACHE_DIR / f"{contract}_{date}_0930_1600.json"

    if not cache_file.exists():
        return None

    with open(cache_file) as f:
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


def get_contract_for_date(symbol: str, date_str: str) -> str:
    """Get the front-month contract for a symbol and date."""
    return DatabentoAdapter.get_front_month_contract(symbol, date_str)


def find_cached_contract(symbol: str, date: str) -> Optional[str]:
    """Find which contract we have cached for this symbol/date."""
    # Try the calculated front-month first
    contract = get_contract_for_date(symbol, date)
    cache_file = CACHE_DIR / f"{contract}_{date}_0930_1600.json"
    if cache_file.exists():
        return contract

    # Search for any matching contract
    for f in CACHE_DIR.glob(f"{symbol}*_{date}_0930_1600.json"):
        return f.stem.split("_")[0]

    return None


class BlindBacktester:
    """Run blind backtest with 2-tick slippage."""

    def __init__(self, starting_balance: float = 2500.0):
        self.starting_balance = starting_balance
        self.results = []
        self.all_trades = []
        self.tier_changes = []

        # Will be initialized per-run
        self.tier_manager = None
        self.engine = None
        self.router = None
        self.session = None
        self.manager = None
        self._current_bar_signals = []
        self._current_date = ""

    def _reset(self):
        """Reset for a new backtest run."""
        state_file = Path("data/blind_backtest_state.json")
        state_file.unlink(missing_ok=True)

        self.tier_manager = TierManager(
            starting_balance=self.starting_balance,
            state_file=state_file,
            on_tier_change=self._on_tier_change,
        )
        self.results = []
        self.all_trades = []
        self.tier_changes = []

    def _on_tier_change(self, change: dict):
        """Handle tier change."""
        old_tier = TIERS[change["from_tier"]]
        new_tier = TIERS[change["to_tier"]]
        direction = "UP" if change["to_tier"] > change["from_tier"] else "DOWN"

        self.tier_changes.append({
            "direction": direction,
            "from": old_tier["name"],
            "to": new_tier["name"],
            "balance": change["balance"],
            "date": self._current_date,
        })

        logger.info(f"  *** TIER {direction}: {old_tier['name']} -> {new_tier['name']} @ ${change['balance']:,.2f}")

    def _setup_day(self, date: str):
        """Set up for a trading day."""
        self._current_date = date

        tier_config = self.tier_manager.start_session()
        symbol = tier_config["instrument"]

        # Create session with 2-TICK SLIPPAGE
        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=5000,
            daily_loss_limit=tier_config["daily_loss_limit"],
            max_position_size=tier_config["max_contracts"],
            stop_loss_ticks=16,
            take_profit_ticks=24,
            paper_starting_balance=tier_config["balance"],
            paper_slippage_ticks=SLIPPAGE_TICKS,  # 2 ticks!
            bypass_trading_hours=True,
        )

        self.manager = ExecutionManager(self.session)
        self.manager.on_trade(self._on_trade)

        self.engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})
        self.router = StrategyRouter({})

        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

        self._current_bar_signals = []

        return tier_config

    def _on_bar(self, bar: FootprintBar):
        """Handle completed bar."""
        self._current_bar_signals = []
        if self.router:
            self.router.on_bar(bar)
        if bar.close_price and self.manager:
            self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal):
        """Handle signal."""
        if not self.router or not self.manager:
            return

        self._current_bar_signals.append(signal)
        signal = self.router.evaluate_signal(signal)

        if signal.approved:
            stacked_count = sum(
                1 for s in self._current_bar_signals
                if s.direction == signal.direction
            )

            current_regime = self.router.current_regime if self.router else "UNKNOWN"
            position_size = self.tier_manager.get_position_size(
                regime=current_regime,
                stacked_count=stacked_count,
                use_streaks=True,
            )

            self.manager.on_signal(signal, absolute_size=position_size)

    def _on_trade(self, trade):
        """Handle completed trade."""
        self.tier_manager.record_trade(trade.pnl)

        self.all_trades.append({
            "side": trade.side,
            "size": trade.size,
            "entry": trade.entry_price,
            "exit": trade.exit_price,
            "pnl": trade.pnl,
            "exit_reason": trade.exit_reason,
            "date": self._current_date,
        })

    async def run_day(self, date: str) -> Optional[dict]:
        """Run a single day."""
        tier_config = self._setup_day(date)
        symbol = tier_config["instrument"]

        # Find cached data for this symbol/date
        contract = find_cached_contract(symbol, date)
        if not contract:
            logger.warning(f"No cached data for {symbol} on {date}, skipping")
            return None

        ticks = load_cached_ticks(contract, date)
        if not ticks:
            logger.warning(f"Failed to load ticks for {contract} {date}")
            return None

        logger.info(f"Day {date} | {tier_config['tier_name']} | {symbol} ({contract}) | ${tier_config['balance']:,.2f} | {len(ticks):,} ticks")

        for tick in ticks:
            self.engine.process_tick(tick)
            if self.manager and self.manager.open_positions:
                self.manager.update_prices(tick.price)
            if self.manager.is_halted:
                break

        # Close any open positions
        if self.manager.open_positions:
            last_price = ticks[-1].price if ticks else 0
            self.manager.close_all_positions(last_price, "END_OF_DAY")

        daily_pnl = self.manager.daily_pnl
        trades = len(self.manager.completed_trades)
        wins = sum(1 for t in self.manager.completed_trades if t.pnl > 0)

        self.tier_manager.end_session(daily_pnl)

        result = {
            "date": date,
            "pnl": daily_pnl,
            "trades": trades,
            "wins": wins,
            "win_rate": wins / trades * 100 if trades > 0 else 0,
            "tier": self.tier_manager.state.tier_name,
            "balance": self.tier_manager.state.balance,
            "instrument": self.tier_manager.state.instrument,
        }
        self.results.append(result)

        emoji = "+" if daily_pnl >= 0 else ""
        logger.info(f"  -> {emoji}${daily_pnl:,.2f} | {trades}T ({wins}W) | Balance: ${self.tier_manager.state.balance:,.2f}")

        return result

    async def run_period(self, dates: List[str], label: str = ""):
        """Run a multi-day backtest."""
        self._reset()

        logger.info(f"\n{'='*60}")
        logger.info(f"BLIND BACKTEST: {label}")
        logger.info(f"Period: {dates[0]} to {dates[-1]} ({len(dates)} days)")
        logger.info(f"Starting: ${self.starting_balance:,.2f} on MES")
        logger.info(f"Slippage: {SLIPPAGE_TICKS} ticks")
        logger.info(f"{'='*60}\n")

        for date in dates:
            await self.run_day(date)

        self._print_summary(label)
        return self.results

    def _print_summary(self, label: str):
        """Print summary."""
        if not self.results:
            return

        total_pnl = self.tier_manager.state.balance - self.starting_balance
        total_trades = sum(r["trades"] for r in self.results)
        total_wins = sum(r["wins"] for r in self.results)
        win_rate = total_wins / total_trades * 100 if total_trades > 0 else 0
        winning_days = sum(1 for r in self.results if r["pnl"] > 0)
        max_drawdown = self._calculate_max_drawdown()

        print(f"\n{'='*60}")
        print(f"RESULTS: {label}")
        print(f"{'='*60}")
        print(f"Period: {self.results[0]['date']} to {self.results[-1]['date']}")
        print(f"Days: {len(self.results)} ({winning_days} profitable, {winning_days/len(self.results)*100:.0f}%)")
        print(f"Starting: ${self.starting_balance:,.2f}")
        print(f"Ending: ${self.tier_manager.state.balance:,.2f}")
        print(f"Total P&L: ${total_pnl:+,.2f} ({total_pnl/self.starting_balance*100:+.1f}%)")
        print(f"Trades: {total_trades} ({win_rate:.1f}% win rate)")
        print(f"Avg P&L/Day: ${total_pnl/len(self.results):+,.2f}")
        print(f"Max Drawdown: ${max_drawdown:,.2f}")

        if self.tier_changes:
            print(f"\nTier Changes ({len(self.tier_changes)}):")
            for tc in self.tier_changes:
                print(f"  {tc['date']}: {tc['direction']} to {tc['to']} @ ${tc['balance']:,.2f}")

        # Position sizing breakdown
        sizes = [t["size"] for t in self.all_trades]
        if sizes:
            print(f"\nPosition Sizing:")
            print(f"  1 contract: {sizes.count(1)} trades")
            print(f"  2 contracts: {sizes.count(2)} trades")
            print(f"  3 contracts: {sizes.count(3)} trades")
            print(f"  Avg size: {sum(sizes)/len(sizes):.1f}")

        print(f"{'='*60}\n")

    def _calculate_max_drawdown(self) -> float:
        """Calculate maximum drawdown from peak."""
        if not self.results:
            return 0.0

        peak = self.starting_balance
        max_dd = 0.0

        for r in self.results:
            if r["balance"] > peak:
                peak = r["balance"]
            dd = peak - r["balance"]
            if dd > max_dd:
                max_dd = dd

        return max_dd


async def main():
    """Run blind backtests."""

    # Get available cached dates
    available_dates = get_available_dates()
    logger.info(f"Found {len(available_dates)} cached dates")

    # Find consecutive periods of 30+ days
    periods = get_consecutive_periods(available_dates, min_length=30)
    logger.info(f"Found {len(periods)} consecutive periods of 30+ days")

    if not periods:
        logger.error("No consecutive 30-day periods available!")
        return

    # Seed random (don't reveal)
    random.seed(int(datetime.now().timestamp()))

    backtester = BlindBacktester(starting_balance=2500.0)

    # ===========================================
    # TEST 1: 30-day continuous period (BLIND)
    # ===========================================

    # Find periods that start with MES data available
    # MES data exists for: Jan 13-17 and Mar 10-14
    mes_start_dates = {"2025-01-13", "2025-03-10"}

    valid_starts = []
    for period in periods:
        for i, date in enumerate(period):
            if date in mes_start_dates and len(period) - i >= 30:
                valid_starts.append((period, i))

    if not valid_starts:
        logger.error("No valid 30-day periods with MES start data!")
        return

    # Pick randomly from valid starts
    chosen_period, start_idx = random.choice(valid_starts)
    test_dates = chosen_period[start_idx:start_idx + 30]

    logger.info("\n" + "="*60)
    logger.info("30-DAY BLIND BACKTEST")
    logger.info("Period will be revealed with results...")
    logger.info("="*60 + "\n")

    await backtester.run_period(test_dates, "30-Day Tier Progression (2-tick slippage)")

    # ===========================================
    # TEST 2: Random single days (stress test)
    # ===========================================

    logger.info("\n" + "="*60)
    logger.info("BONUS: Random single-day stress tests")
    logger.info("="*60 + "\n")

    # Pick 5 random days from different months
    months_seen = set()
    single_days = []

    random.shuffle(available_dates)
    for date in available_dates:
        month = date[:7]  # YYYY-MM
        if month not in months_seen:
            months_seen.add(month)
            single_days.append(date)
            if len(single_days) >= 5:
                break

    single_day_results = []

    for date in sorted(single_days):
        # Run each as a fresh $2,500 start on MES
        backtester._reset()
        result = await backtester.run_day(date)
        if result:
            single_day_results.append(result)

    # Summary of single days
    if single_day_results:
        print(f"\n{'='*60}")
        print("SINGLE-DAY STRESS TESTS (MES, $2,500 start each)")
        print(f"{'='*60}")

        total_pnl = sum(r["pnl"] for r in single_day_results)
        winning = sum(1 for r in single_day_results if r["pnl"] > 0)
        total_trades = sum(r["trades"] for r in single_day_results)

        for r in sorted(single_day_results, key=lambda x: x["date"]):
            emoji = "+" if r["pnl"] >= 0 else ""
            print(f"  {r['date']}: {emoji}${r['pnl']:,.2f} | {r['trades']}T | {r['win_rate']:.0f}% WR")

        print(f"\nCombined: ${total_pnl:+,.2f} across {len(single_day_results)} days ({winning} profitable)")
        print(f"Total trades: {total_trades}")
        print(f"{'='*60}\n")


if __name__ == "__main__":
    asyncio.run(main())
