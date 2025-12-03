#!/usr/bin/env python3
"""
Nov 20th Validation - Compare bar-level vs tick-level stop checking.

This validates that we correctly identified the Bishop trading discrepancy:
- Bar-level: How Bishop WAS trading (check stops at bar close only)
- Tick-level: How Bishop SHOULD trade (check stops on every tick)

Usage:
    PYTHONPATH=. python testing/nov20_validation.py --mode bar
    PYTHONPATH=. python testing/nov20_validation.py --mode tick
"""

import argparse
import json
import os
import sys
from datetime import datetime, time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Tick, Signal, FootprintBar
from src.core.capital import TierManager
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession

TICK_FILE = "/home/faded-vibes/tradebot/data/tick_cache/ESZ5_2025-11-20_0930_1600.json"


def load_ticks():
    """Load tick data from JSON cache."""
    print(f"Loading ticks from {TICK_FILE}...")
    with open(TICK_FILE) as f:
        data = json.load(f)

    ticks = []
    for d in data:
        ticks.append(Tick(
            timestamp=datetime.fromisoformat(d["timestamp"]),
            price=d["price"],
            volume=d["volume"],
            side=d["side"],
            symbol=d.get("symbol", "ES")
        ))

    print(f"Loaded {len(ticks):,} ticks")
    return ticks


class ValidationBacktest:
    """Run backtest with configurable stop checking mode."""

    def __init__(self, mode: str, starting_balance: float = 2500.0):
        self.mode = mode  # "bar" or "tick"
        self.starting_balance = starting_balance
        self.trades = []
        self.signals_detected = []
        self.signals_approved = []

        self.engine = None
        self.router = None
        self.session = None
        self.manager = None
        self.tier_manager = None
        self._current_bar_signals = []

    def setup(self, symbol: str = "MES"):
        """Initialize components."""
        # Tier manager with fresh state
        state_file = Path(f"/tmp/nov20_{self.mode}_state.json")
        state_file.unlink(missing_ok=True)

        self.tier_manager = TierManager(
            starting_balance=self.starting_balance,
            state_file=state_file,
        )
        tier_config = self.tier_manager.start_session()

        print(f"\nMode: {self.mode.upper()}-LEVEL stop checking")
        print(f"Starting balance: ${self.starting_balance:,.2f}")
        print(f"Tier: {tier_config['tier_name']}")

        # Session
        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=1000000,
            daily_loss_limit=tier_config["daily_loss_limit"],
            max_position_size=tier_config["max_contracts"],
            stop_loss_ticks=16,
            take_profit_ticks=24,
            paper_starting_balance=self.starting_balance,
            bypass_trading_hours=True,
        )

        # Execution manager
        self.manager = ExecutionManager(self.session)
        self.manager.on_trade(self._on_trade)

        # Engine and router
        self.engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})
        self.router = StrategyRouter({})

        # Wire callbacks
        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)
        self._current_bar_signals = []

    def _on_bar(self, bar: FootprintBar):
        """Handle completed bar."""
        self._current_bar_signals = []

        if self.router:
            self.router.on_bar(bar)

        # BAR-LEVEL: Check stops at bar close (OLD behavior)
        if self.mode == "bar":
            if bar.close_price and self.manager and self.manager.open_positions:
                self.manager.update_prices(bar.close_price)

    def _on_signal(self, signal: Signal):
        """Handle signal."""
        if not self.router or not self.manager:
            return

        self.signals_detected.append(signal)
        self._current_bar_signals.append(signal)

        signal = self.router.evaluate_signal(signal)

        if signal.approved:
            self.signals_approved.append(signal)

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

            order = self.manager.on_signal(signal, absolute_size=position_size)

            if order:
                print(f"  ORDER: {order.side} {order.size}x @ {order.entry_price:.2f} ({signal.pattern.name})")

    def _on_trade(self, trade):
        """Handle completed trade."""
        self.tier_manager.record_trade(trade.pnl)

        self.trades.append({
            "side": trade.side,
            "size": trade.size,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "pnl": trade.pnl,
            "exit_reason": trade.exit_reason,
            "entry_time": trade.entry_time.isoformat() if trade.entry_time else None,
            "exit_time": trade.exit_time.isoformat() if trade.exit_time else None,
        })

        emoji = "+" if trade.pnl >= 0 else ""
        print(f"  TRADE: {trade.side} {trade.size}x | {trade.entry_price:.2f} -> {trade.exit_price:.2f} | {emoji}${trade.pnl:.2f} ({trade.exit_reason})")

    def run(self, ticks):
        """Run the backtest."""
        flatten_time = time(15, 55)
        flattened = False

        for i, tick in enumerate(ticks):
            # Check flatten time
            tick_time = tick.timestamp.time() if hasattr(tick.timestamp, 'time') else None
            if tick_time and tick_time >= flatten_time and not flattened:
                if self.manager and self.manager.open_positions:
                    print("  FLATTEN at 3:55 PM")
                    self.manager.close_all_positions(tick.price, "AUTO_FLATTEN")
                flattened = True

            if flattened:
                continue

            # Process tick through engine (builds bars, detects patterns)
            self.engine.process_tick(tick)

            # TICK-LEVEL: Check stops on every tick (NEW behavior)
            if self.mode == "tick":
                if self.manager and self.manager.open_positions:
                    self.manager.update_prices(tick.price)

            # Check halt
            if self.manager.is_halted:
                print(f"  HALTED: {self.manager.halt_reason}")
                break

            # Progress
            if i > 0 and i % 200000 == 0:
                pct = i / len(ticks) * 100
                print(f"  Progress: {pct:.0f}%")

        # Close remaining positions
        if self.manager.open_positions:
            last_price = ticks[-1].price if ticks else 0
            self.manager.close_all_positions(last_price, "END_OF_DAY")

        # Results
        gross_pnl = sum(t["pnl"] for t in self.trades)
        wins = sum(1 for t in self.trades if t["pnl"] > 0)
        losses = len(self.trades) - wins

        return {
            "mode": self.mode,
            "ticks": len(ticks),
            "trades": len(self.trades),
            "wins": wins,
            "losses": losses,
            "gross_pnl": gross_pnl,
            "signals_detected": len(self.signals_detected),
            "signals_approved": len(self.signals_approved),
            "trade_details": self.trades,
        }


def main():
    parser = argparse.ArgumentParser(description="Nov 20th Validation Backtest")
    parser.add_argument("--mode", required=True, choices=["bar", "tick"],
                        help="Stop checking mode: 'bar' (old) or 'tick' (new)")
    parser.add_argument("--balance", type=float, default=2500.0, help="Starting balance")
    args = parser.parse_args()

    ticks = load_ticks()

    backtest = ValidationBacktest(mode=args.mode, starting_balance=args.balance)
    backtest.setup(symbol="MES")

    results = backtest.run(ticks)

    print("\n" + "=" * 60)
    print(f"NOV 20th VALIDATION - {args.mode.upper()}-LEVEL")
    print("=" * 60)
    print(f"Ticks processed: {results['ticks']:,}")
    print(f"Signals detected: {results['signals_detected']}")
    print(f"Signals approved: {results['signals_approved']}")
    print(f"Trades: {results['trades']}")
    print(f"Wins/Losses: {results['wins']}/{results['losses']}")
    win_rate = (results['wins'] / results['trades'] * 100) if results['trades'] > 0 else 0
    print(f"Win Rate: {win_rate:.1f}%")
    print(f"Gross P&L: ${results['gross_pnl']:+,.2f}")
    print("=" * 60)


if __name__ == "__main__":
    main()
