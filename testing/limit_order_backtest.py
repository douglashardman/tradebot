#!/usr/bin/env python3
"""
Limit Order Backtest - Tests if patterns have predictive value when filled at signal price.

Logic:
1. Signal fires at bar close with price = bar.low (long) or bar.high (short)
2. Place limit order at signal.price
3. Only fill if subsequent tick touches that price
4. If price never returns, order expires (no trade)

This answers: Of trades where price actually returns to the pattern level, what's the win rate?
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass, field
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
from src.data.adapters.databento import DatabentoAdapter

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("limit_backtest")


@dataclass
class PendingOrder:
    """A limit order waiting to be filled."""
    signal: Signal
    limit_price: float
    direction: str  # LONG or SHORT
    size: int
    stop_price: float
    target_price: float
    created_at: datetime
    pattern: str
    expires_at: Optional[datetime] = None  # Expire after N bars or end of day


@dataclass
class Position:
    """An open position after limit order filled."""
    direction: str
    size: int
    entry_price: float
    entry_time: datetime
    stop_price: float
    target_price: float
    pattern: str


@dataclass
class TradeResult:
    """Completed trade."""
    direction: str
    size: int
    entry_price: float
    entry_time: datetime
    exit_price: float
    exit_time: datetime
    exit_reason: str  # TARGET, STOP, FLATTEN
    pnl: float
    pnl_ticks: int
    pattern: str


class LimitOrderBacktester:
    """Backtest with limit order fills only."""

    def __init__(
        self,
        starting_balance: float = 2500.0,
        stop_ticks: int = 16,
        target_ticks: int = 24,
        tick_size: float = 0.25,
        tick_value: float = 1.25,  # MES: $1.25 per tick per contract
        max_pending_bars: int = 6,  # Expire unfilled orders after N bars
    ):
        self.starting_balance = starting_balance
        self.balance = starting_balance
        self.stop_ticks = stop_ticks
        self.target_ticks = target_ticks
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.max_pending_bars = max_pending_bars

        # State
        self.pending_orders: List[PendingOrder] = []
        self.open_position: Optional[Position] = None
        self.completed_trades: List[TradeResult] = []
        self.expired_orders: int = 0
        self.filled_orders: int = 0

        # Components
        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None

        # Current state
        self._current_bar_close: Optional[float] = None
        self._current_bar_count: int = 0
        self._current_date: str = ""

        # Stats by pattern
        self.pattern_stats: Dict[str, Dict] = defaultdict(
            lambda: {"signals": 0, "filled": 0, "expired": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        )

        # Daily stats
        self.daily_results: List[Dict] = []

    def _setup_day(self, date: str, symbol: str) -> None:
        """Set up for a new trading day."""
        self._current_date = date
        self._current_bar_count = 0
        self.pending_orders = []

        # Don't reset position - could carry overnight (but we'll flatten EOD)

        self.engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})
        self.router = StrategyRouter({})

        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

    def _on_bar(self, bar: FootprintBar) -> None:
        """Handle bar completion."""
        self._current_bar_close = bar.close_price
        self._current_bar_count += 1

        if self.router:
            self.router.on_bar(bar)

        # Expire old pending orders
        self._expire_old_orders()

    def _on_signal(self, signal: Signal) -> None:
        """Handle signal - create pending limit order."""
        if not self.router:
            return

        # Don't take new signals if we have an open position
        if self.open_position:
            return

        # Evaluate through router
        signal = self.router.evaluate_signal(signal)

        if not signal.approved:
            return

        # Create pending limit order at signal price (bar.low or bar.high)
        limit_price = signal.price
        direction = signal.direction

        # Calculate stop and target from the LIMIT price (where we'd actually enter)
        if direction == "LONG":
            stop_price = limit_price - (self.stop_ticks * self.tick_size)
            target_price = limit_price + (self.target_ticks * self.tick_size)
        else:
            stop_price = limit_price + (self.stop_ticks * self.tick_size)
            target_price = limit_price - (self.target_ticks * self.tick_size)

        pattern = signal.pattern.value if hasattr(signal.pattern, 'value') else str(signal.pattern)

        order = PendingOrder(
            signal=signal,
            limit_price=limit_price,
            direction=direction,
            size=1,  # Fixed size for simplicity
            stop_price=stop_price,
            target_price=target_price,
            created_at=signal.timestamp,
            pattern=pattern,
            expires_at=None,  # We'll expire based on bar count
        )

        # Track pending order's bar count for expiration
        order._created_bar = self._current_bar_count

        self.pending_orders.append(order)
        self.pattern_stats[pattern]["signals"] += 1

        logger.debug(f"Pending {direction} limit @ {limit_price} ({pattern})")

    def _expire_old_orders(self) -> None:
        """Expire orders that have been pending too long."""
        expired = []
        for order in self.pending_orders:
            bars_pending = self._current_bar_count - order._created_bar
            if bars_pending >= self.max_pending_bars:
                expired.append(order)
                self.expired_orders += 1
                self.pattern_stats[order.pattern]["expired"] += 1
                logger.debug(f"Expired {order.direction} limit @ {order.limit_price} after {bars_pending} bars")

        for order in expired:
            self.pending_orders.remove(order)

    def _process_tick(self, tick: Tick) -> None:
        """Process tick - check for limit fills and stop/target hits."""
        price = tick.price

        # First, check if any pending limit orders get filled
        if not self.open_position and self.pending_orders:
            self._check_limit_fills(tick)

        # Then, check stop/target on open position
        if self.open_position:
            self._check_position_exit(tick)

        # Process through engine for bar building and signal detection
        if self.engine:
            self.engine.process_tick(tick)

    def _check_limit_fills(self, tick: Tick) -> None:
        """Check if tick price fills any pending limit orders."""
        price = tick.price

        for order in list(self.pending_orders):
            filled = False

            if order.direction == "LONG":
                # Long limit fills when price drops to or below limit
                if price <= order.limit_price:
                    filled = True
            else:
                # Short limit fills when price rises to or above limit
                if price >= order.limit_price:
                    filled = True

            if filled:
                # Apply 1 tick slippage (conservative)
                if order.direction == "LONG":
                    fill_price = order.limit_price + self.tick_size
                else:
                    fill_price = order.limit_price - self.tick_size

                # Recalculate stop/target from actual fill price
                if order.direction == "LONG":
                    stop = fill_price - (self.stop_ticks * self.tick_size)
                    target = fill_price + (self.target_ticks * self.tick_size)
                else:
                    stop = fill_price + (self.stop_ticks * self.tick_size)
                    target = fill_price - (self.target_ticks * self.tick_size)

                self.open_position = Position(
                    direction=order.direction,
                    size=order.size,
                    entry_price=fill_price,
                    entry_time=tick.timestamp,
                    stop_price=stop,
                    target_price=target,
                    pattern=order.pattern,
                )

                self.pending_orders.remove(order)
                # Clear other pending orders - we have a position now
                for other in self.pending_orders:
                    self.pattern_stats[other.pattern]["expired"] += 1
                    self.expired_orders += 1
                self.pending_orders = []

                self.filled_orders += 1
                self.pattern_stats[order.pattern]["filled"] += 1

                logger.debug(f"FILLED {order.direction} @ {fill_price} (limit was {order.limit_price})")
                break  # Only one fill at a time

    def _check_position_exit(self, tick: Tick) -> None:
        """Check if position hits stop or target."""
        if not self.open_position:
            return

        pos = self.open_position
        price = tick.price
        exit_price = None
        exit_reason = None

        if pos.direction == "LONG":
            if price <= pos.stop_price:
                exit_price = pos.stop_price
                exit_reason = "STOP"
            elif price >= pos.target_price:
                exit_price = pos.target_price
                exit_reason = "TARGET"
        else:  # SHORT
            if price >= pos.stop_price:
                exit_price = pos.stop_price
                exit_reason = "STOP"
            elif price <= pos.target_price:
                exit_price = pos.target_price
                exit_reason = "TARGET"

        if exit_price:
            self._close_position(exit_price, exit_reason, tick.timestamp)

    def _close_position(self, exit_price: float, reason: str, exit_time: datetime) -> None:
        """Close the open position."""
        pos = self.open_position
        if not pos:
            return

        # Calculate P&L
        if pos.direction == "LONG":
            pnl_ticks = int((exit_price - pos.entry_price) / self.tick_size)
        else:
            pnl_ticks = int((pos.entry_price - exit_price) / self.tick_size)

        pnl = pnl_ticks * self.tick_value * pos.size

        trade = TradeResult(
            direction=pos.direction,
            size=pos.size,
            entry_price=pos.entry_price,
            entry_time=pos.entry_time,
            exit_price=exit_price,
            exit_time=exit_time,
            exit_reason=reason,
            pnl=pnl,
            pnl_ticks=pnl_ticks,
            pattern=pos.pattern,
        )

        self.completed_trades.append(trade)
        self.balance += pnl

        # Update pattern stats
        if pnl > 0:
            self.pattern_stats[pos.pattern]["wins"] += 1
        else:
            self.pattern_stats[pos.pattern]["losses"] += 1
        self.pattern_stats[pos.pattern]["pnl"] += pnl

        emoji = "+" if pnl >= 0 else ""
        logger.info(f"Trade: {pos.direction} | Entry: {pos.entry_price} -> Exit: {exit_price} | {emoji}${pnl:.2f} ({reason}) | {pos.pattern}")

        self.open_position = None

    def _end_day(self, date: str) -> Dict:
        """End trading day - flatten positions, record stats."""
        # Flatten any open position
        if self.open_position and self._current_bar_close:
            self._close_position(self._current_bar_close, "FLATTEN", datetime.now())

        # Expire remaining pending orders
        for order in self.pending_orders:
            self.expired_orders += 1
            self.pattern_stats[order.pattern]["expired"] += 1
        self.pending_orders = []

        # Calculate day's P&L from today's trades
        day_trades = [t for t in self.completed_trades if t.entry_time.strftime("%Y-%m-%d") == date]
        day_pnl = sum(t.pnl for t in day_trades)
        day_wins = sum(1 for t in day_trades if t.pnl > 0)

        result = {
            "date": date,
            "trades": len(day_trades),
            "wins": day_wins,
            "losses": len(day_trades) - day_wins,
            "pnl": day_pnl,
            "balance": self.balance,
        }

        self.daily_results.append(result)

        logger.info(f"Day {date}: {len(day_trades)} trades | {day_wins}W/{len(day_trades)-day_wins}L | ${day_pnl:+.2f} | Balance: ${self.balance:.2f}")

        return result

    async def run_day(self, date: str, symbol: str = "MES") -> Dict:
        """Run backtest for a single day."""
        contract = f"{symbol}U4"  # August 2024 uses September contracts

        logger.info(f"\n{'='*60}")
        logger.info(f"DAY: {date} | Symbol: {contract}")
        logger.info(f"{'='*60}")

        self._setup_day(date, symbol)

        # Load ticks
        ticks = load_cached_ticks(contract, date)
        if not ticks:
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

        # Flatten time
        flatten_time = time(15, 55)
        flattened = False

        for tick in ticks:
            tick_time = tick.timestamp.time() if hasattr(tick.timestamp, 'time') else None

            if tick_time and tick_time >= flatten_time and not flattened:
                if self.open_position:
                    self._close_position(tick.price, "FLATTEN", tick.timestamp)
                # Expire pending orders
                for order in self.pending_orders:
                    self.expired_orders += 1
                    self.pattern_stats[order.pattern]["expired"] += 1
                self.pending_orders = []
                flattened = True
                continue

            if flattened:
                continue

            self._process_tick(tick)

        return self._end_day(date)

    def print_summary(self) -> None:
        """Print comprehensive summary."""
        total_signals = sum(s["signals"] for s in self.pattern_stats.values())
        total_filled = sum(s["filled"] for s in self.pattern_stats.values())
        total_expired = sum(s["expired"] for s in self.pattern_stats.values())

        total_trades = len(self.completed_trades)
        wins = sum(1 for t in self.completed_trades if t.pnl > 0)
        losses = total_trades - wins

        gross_profit = sum(t.pnl for t in self.completed_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.completed_trades if t.pnl < 0))

        print("\n" + "=" * 70)
        print("LIMIT ORDER BACKTEST RESULTS")
        print("=" * 70)

        print("\n--- FILL STATISTICS ---")
        print(f"  Total Signals:     {total_signals:>6}")
        print(f"  Filled:            {total_filled:>6} ({total_filled/total_signals*100:.1f}%)" if total_signals > 0 else "  Filled:            0")
        print(f"  Expired:           {total_expired:>6} ({total_expired/total_signals*100:.1f}%)" if total_signals > 0 else "  Expired:           0")

        print("\n--- PERFORMANCE (Filled Trades Only) ---")
        print(f"  Starting Balance:  ${self.starting_balance:>10,.2f}")
        print(f"  Ending Balance:    ${self.balance:>10,.2f}")
        print(f"  Total P&L:         ${self.balance - self.starting_balance:>+10,.2f}")

        print(f"\n  Total Trades:      {total_trades:>6}")
        print(f"  Wins:              {wins:>6}")
        print(f"  Losses:            {losses:>6}")
        print(f"  Win Rate:          {wins/total_trades*100:>6.1f}%" if total_trades > 0 else "  Win Rate:          N/A")
        print(f"  Profit Factor:     {gross_profit/gross_loss:>6.2f}" if gross_loss > 0 else "  Profit Factor:     N/A")

        print("\n--- PATTERN BREAKDOWN ---")
        print(f"  {'Pattern':<30} | Signals | Filled | Expired | Trades | Win% | P&L")
        print("-" * 90)

        for pattern, stats in sorted(self.pattern_stats.items(), key=lambda x: x[1]['pnl'], reverse=True):
            filled = stats["filled"]
            wins = stats["wins"]
            losses = stats["losses"]
            trades = wins + losses
            wr = (wins / trades * 100) if trades > 0 else 0
            fill_rate = (filled / stats["signals"] * 100) if stats["signals"] > 0 else 0

            print(f"  {pattern:<30} | {stats['signals']:>7} | {filled:>6} | {stats['expired']:>7} | {trades:>6} | {wr:>4.0f}% | ${stats['pnl']:>+8.2f}")

        print("\n--- DAILY BREAKDOWN ---")
        winning_days = sum(1 for d in self.daily_results if d["pnl"] > 0)
        losing_days = sum(1 for d in self.daily_results if d["pnl"] < 0)
        print(f"  Winning Days:      {winning_days}")
        print(f"  Losing Days:       {losing_days}")
        print(f"  Win Day Rate:      {winning_days/len(self.daily_results)*100:.1f}%" if self.daily_results else "  Win Day Rate:      N/A")

        print("\n" + "=" * 70)


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
    """Load ticks from cache."""
    from src.core.types import Tick

    safe_start = start_time.replace(":", "")
    safe_end = end_time.replace(":", "")
    cache_path = os.path.join(CACHE_DIR, f"{contract}_{date}_{safe_start}_{safe_end}.json")

    if not os.path.exists(cache_path):
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


def save_ticks_to_cache(ticks, contract: str, date: str, start_time: str = "09:30", end_time: str = "16:00"):
    """Save ticks to cache."""
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


async def main():
    parser = argparse.ArgumentParser(description="Limit Order Backtest")
    parser.add_argument("--days", type=int, default=22, help="Number of trading days")
    parser.add_argument("--start", type=str, default="2024-08-01", help="Start date")
    parser.add_argument("--expire-bars", type=int, default=6, help="Expire unfilled orders after N bars")
    args = parser.parse_args()

    backtester = LimitOrderBacktester(
        starting_balance=2500.0,
        max_pending_bars=args.expire_bars,
    )

    trading_days = get_trading_days(args.start, args.days)

    logger.info(f"\n{'='*60}")
    logger.info("LIMIT ORDER BACKTEST")
    logger.info(f"Testing: If price returns to signal level, what's the win rate?")
    logger.info(f"Days: {len(trading_days)} | Expire after: {args.expire_bars} bars")
    logger.info(f"{'='*60}\n")

    for date in trading_days:
        await backtester.run_day(date, symbol="MES")

    backtester.print_summary()


if __name__ == "__main__":
    asyncio.run(main())
