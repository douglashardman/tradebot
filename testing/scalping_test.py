#!/usr/bin/env python3
"""
Scalping Strategy Backtest

Tests tight scalping parameters:
- Take Profit: 4 ticks (1 point, $50/ES)
- Stop Loss: 2 ticks (0.5 points, $25/ES)

This is a 2:1 reward/risk but inverted from normal strategies.
Win rate needs to be >33% to break even (before slippage/commissions).

Usage:
    PYTHONPATH=. python scripts/scalping_test.py
    PYTHONPATH=. python scripts/scalping_test.py --days 10
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Tick, Signal, FootprintBar
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter

# Cache directory for tick data
CACHE_DIR = Path(__file__).parent.parent / "data" / "tick_cache"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("scalping_test")


@dataclass
class ScalpTrade:
    """A single scalp trade."""
    entry_time: datetime
    entry_price: float
    direction: str  # "LONG" or "SHORT"
    stop_price: float
    target_price: float
    exit_time: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    pnl: float = 0.0
    pnl_ticks: int = 0


@dataclass
class ScalpingBacktester:
    """Simple backtester for scalping strategy."""

    # Scalping parameters
    take_profit_ticks: int = 4  # 1 point
    stop_loss_ticks: int = 2    # 0.5 points
    entry_slippage_ticks: int = 1  # 1 tick slippage on entry

    # ES tick value
    tick_size: float = 0.25
    tick_value: float = 12.50  # $12.50 per tick for ES

    # State
    open_trade: Optional[ScalpTrade] = None
    completed_trades: List[ScalpTrade] = field(default_factory=list)
    daily_pnl: float = 0.0

    # Components
    engine: Optional[OrderFlowEngine] = None
    router: Optional[StrategyRouter] = None

    # Signal buffer for current bar
    _current_bar_signals: List[Signal] = field(default_factory=list)
    _last_price: float = 0.0

    def setup(self):
        """Initialize components."""
        self.engine = OrderFlowEngine({"symbol": "ES", "timeframe": 300})
        self.router = StrategyRouter({})

        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

        self.completed_trades = []
        self.open_trade = None
        self.daily_pnl = 0.0
        self._current_bar_signals = []

    def _on_bar(self, bar: FootprintBar):
        """Handle completed bar."""
        self._current_bar_signals = []
        if self.router:
            self.router.on_bar(bar)

    def _on_signal(self, signal: Signal):
        """Handle signal - enter trade if approved and no open position."""
        if not self.router:
            return

        self._current_bar_signals.append(signal)
        signal = self.router.evaluate_signal(signal)

        if signal.approved and not self.open_trade:
            self._enter_trade(signal)

    def _enter_trade(self, signal: Signal):
        """Enter a scalp trade."""
        direction = signal.direction
        entry_price = self._last_price

        # Apply slippage (worse fill)
        if direction == "LONG":
            entry_price += self.entry_slippage_ticks * self.tick_size
            stop_price = entry_price - (self.stop_loss_ticks * self.tick_size)
            target_price = entry_price + (self.take_profit_ticks * self.tick_size)
        else:
            entry_price -= self.entry_slippage_ticks * self.tick_size
            stop_price = entry_price + (self.stop_loss_ticks * self.tick_size)
            target_price = entry_price - (self.take_profit_ticks * self.tick_size)

        self.open_trade = ScalpTrade(
            entry_time=signal.timestamp,
            entry_price=entry_price,
            direction=direction,
            stop_price=stop_price,
            target_price=target_price,
        )

    def _check_exit(self, tick: Tick):
        """Check if open trade should exit."""
        if not self.open_trade:
            return

        trade = self.open_trade
        price = tick.price

        exit_price = None
        exit_reason = None

        if trade.direction == "LONG":
            # Check stop first (worse case)
            if price <= trade.stop_price:
                exit_price = trade.stop_price
                exit_reason = "STOP_LOSS"
            # Then check target
            elif price >= trade.target_price:
                exit_price = trade.target_price
                exit_reason = "TAKE_PROFIT"
        else:  # SHORT
            if price >= trade.stop_price:
                exit_price = trade.stop_price
                exit_reason = "STOP_LOSS"
            elif price <= trade.target_price:
                exit_price = trade.target_price
                exit_reason = "TAKE_PROFIT"

        if exit_price:
            self._close_trade(tick.timestamp, exit_price, exit_reason)

    def _close_trade(self, exit_time: datetime, exit_price: float, exit_reason: str):
        """Close the open trade."""
        trade = self.open_trade
        trade.exit_time = exit_time
        trade.exit_price = exit_price
        trade.exit_reason = exit_reason

        # Calculate P&L
        if trade.direction == "LONG":
            trade.pnl_ticks = int((exit_price - trade.entry_price) / self.tick_size)
        else:
            trade.pnl_ticks = int((trade.entry_price - exit_price) / self.tick_size)

        trade.pnl = trade.pnl_ticks * self.tick_value
        self.daily_pnl += trade.pnl

        self.completed_trades.append(trade)
        self.open_trade = None

    def process_tick(self, tick: Tick):
        """Process a single tick."""
        self._last_price = tick.price

        # Check exits first
        self._check_exit(tick)

        # Then process for new signals
        self.engine.process_tick(tick)

    def close_open_position(self, price: float, time: datetime):
        """Force close any open position at end of day."""
        if self.open_trade:
            self._close_trade(time, price, "END_OF_DAY")


def load_cached_ticks(contract: str, date: str) -> Optional[List[Tick]]:
    """Load ticks from cache."""
    cache_path = CACHE_DIR / f"{contract}_{date}_0930_1600.json"

    if not cache_path.exists():
        return None

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


def get_available_dates(contract: str = "ESH5") -> List[str]:
    """Get list of available dates from cache."""
    dates = []
    for f in CACHE_DIR.glob(f"{contract}_*_0930_1600.json"):
        # Extract date from filename: ESH5_2025-01-13_0930_1600.json
        parts = f.stem.split("_")
        if len(parts) >= 2:
            dates.append(parts[1])
    return sorted(dates)


def run_backtest(dates: List[str], contract: str = "ESH5"):
    """Run the scalping backtest."""

    print("\n" + "="*70)
    print("SCALPING STRATEGY BACKTEST")
    print("="*70)
    print(f"Take Profit: 4 ticks (1 point, $50)")
    print(f"Stop Loss: 2 ticks (0.5 points, $25)")
    print(f"Entry Slippage: 1 tick")
    print(f"Reward/Risk: 2:1")
    print(f"Break-even win rate: ~33% (before commissions)")
    print(f"Contract: {contract}")
    print(f"Days: {len(dates)}")
    print("="*70 + "\n")

    all_trades = []
    daily_results = []

    for date in dates:
        logger.info(f"Processing {date}...")

        # Load ticks
        ticks = load_cached_ticks(contract, date)
        if not ticks:
            logger.warning(f"No data for {date}, skipping")
            continue

        # Run day
        bt = ScalpingBacktester()
        bt.setup()

        for tick in ticks:
            bt.process_tick(tick)

        # Close any open position
        if ticks:
            bt.close_open_position(ticks[-1].price, ticks[-1].timestamp)

        # Collect results
        trades = len(bt.completed_trades)
        wins = sum(1 for t in bt.completed_trades if t.pnl > 0)
        losses = trades - wins
        win_rate = (wins / trades * 100) if trades > 0 else 0

        daily_results.append({
            "date": date,
            "pnl": bt.daily_pnl,
            "trades": trades,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
        })

        all_trades.extend(bt.completed_trades)

        emoji = "+" if bt.daily_pnl >= 0 else ""
        logger.info(f"  {date}: {emoji}${bt.daily_pnl:,.2f} | {trades}T ({wins}W/{losses}L) | {win_rate:.0f}% WR")

    # Print summary
    print("\n" + "="*70)
    print("RESULTS SUMMARY")
    print("="*70)

    total_pnl = sum(r["pnl"] for r in daily_results)
    total_trades = sum(r["trades"] for r in daily_results)
    total_wins = sum(r["wins"] for r in daily_results)
    total_losses = total_trades - total_wins
    overall_win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

    winning_days = sum(1 for r in daily_results if r["pnl"] > 0)
    losing_days = sum(1 for r in daily_results if r["pnl"] < 0)
    flat_days = sum(1 for r in daily_results if r["pnl"] == 0)

    print(f"\nTotal P&L: ${total_pnl:+,.2f}")
    print(f"Total Trades: {total_trades}")
    print(f"Wins/Losses: {total_wins}W / {total_losses}L")
    print(f"Overall Win Rate: {overall_win_rate:.1f}%")
    print(f"\nWinning Days: {winning_days} ({winning_days/len(daily_results)*100:.0f}%)")
    print(f"Losing Days: {losing_days} ({losing_days/len(daily_results)*100:.0f}%)")
    print(f"Flat Days: {flat_days}")

    if daily_results:
        avg_daily_pnl = total_pnl / len(daily_results)
        best_day = max(daily_results, key=lambda x: x["pnl"])
        worst_day = min(daily_results, key=lambda x: x["pnl"])

        print(f"\nAvg Daily P&L: ${avg_daily_pnl:+,.2f}")
        print(f"Best Day: {best_day['date']} (${best_day['pnl']:+,.2f})")
        print(f"Worst Day: {worst_day['date']} (${worst_day['pnl']:+,.2f})")

    # Exit reason breakdown
    print("\n--- Exit Reasons ---")
    tp_count = sum(1 for t in all_trades if t.exit_reason == "TAKE_PROFIT")
    sl_count = sum(1 for t in all_trades if t.exit_reason == "STOP_LOSS")
    eod_count = sum(1 for t in all_trades if t.exit_reason == "END_OF_DAY")

    print(f"Take Profit: {tp_count} ({tp_count/total_trades*100:.1f}%)" if total_trades else "")
    print(f"Stop Loss: {sl_count} ({sl_count/total_trades*100:.1f}%)" if total_trades else "")
    print(f"End of Day: {eod_count} ({eod_count/total_trades*100:.1f}%)" if total_trades else "")

    # P&L breakdown by exit reason
    if all_trades:
        tp_pnl = sum(t.pnl for t in all_trades if t.exit_reason == "TAKE_PROFIT")
        sl_pnl = sum(t.pnl for t in all_trades if t.exit_reason == "STOP_LOSS")
        eod_pnl = sum(t.pnl for t in all_trades if t.exit_reason == "END_OF_DAY")

        print(f"\nP&L by Exit Reason:")
        print(f"  Take Profit: ${tp_pnl:+,.2f}")
        print(f"  Stop Loss: ${sl_pnl:+,.2f}")
        print(f"  End of Day: ${eod_pnl:+,.2f}")

    # Compare to baseline (16 SL / 24 TP)
    print("\n--- Comparison to Baseline (16 SL / 24 TP) ---")
    print("Baseline Jan-Feb 2025: +$71,741 (208 trades, 77% winning days)")
    print(f"Scalping 4TP/2SL:      ${total_pnl:+,.2f} ({total_trades} trades, {winning_days/len(daily_results)*100:.0f}% winning days)")

    print("\n" + "="*70)

    # Daily breakdown table
    print("\n--- Daily Breakdown ---")
    print(f"{'Date':<12} {'P&L':>10} {'Trades':>8} {'W/L':>8} {'WR%':>6}")
    print("-" * 50)
    for r in daily_results:
        pnl_str = f"${r['pnl']:+,.0f}"
        wl_str = f"{r['wins']}/{r['losses']}"
        print(f"{r['date']:<12} {pnl_str:>10} {r['trades']:>8} {wl_str:>8} {r['win_rate']:>5.0f}%")

    print("-" * 50)
    print(f"{'TOTAL':<12} ${total_pnl:+,.0f}")


def main():
    parser = argparse.ArgumentParser(description="Scalping Strategy Backtest")
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of days to backtest (default: 30)",
    )
    parser.add_argument(
        "--contract",
        default="ESH5",
        help="Contract to test (default: ESH5)",
    )
    args = parser.parse_args()

    # Get available dates
    dates = get_available_dates(args.contract)

    if not dates:
        print(f"No cached data found for {args.contract}")
        print(f"Cache directory: {CACHE_DIR}")
        return

    # Limit to requested days
    dates = dates[:args.days]

    print(f"Found {len(dates)} days of data for {args.contract}")

    run_backtest(dates, args.contract)


if __name__ == "__main__":
    main()
