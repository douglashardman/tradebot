#!/usr/bin/env python3
"""
Comparison Backtest - Run against Databento official data.

This runs the same backtest logic against Databento's official tick data
for comparison with Bishop's recorded data and live results.

Usage:
    PYTHONPATH=. python testing/databento_comparison/run_comparison.py --date 2025-12-03
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, time
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.core.types import Tick, Signal, FootprintBar
from src.core.capital import TierManager, TIERS
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession

# Directory containing this script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("databento_comparison")


def load_databento_ticks(date: str) -> List[Tick]:
    """Load ticks from Databento JSON cache."""
    # Find the file
    pattern = f"databento_MES*_{date}_0930_1600.json"
    files = list(Path(SCRIPT_DIR).glob(pattern))

    if not files:
        logger.error(f"No Databento tick file found matching {pattern}")
        return []

    tick_file = files[0]
    logger.info(f"Loading ticks from {tick_file}")

    with open(tick_file) as f:
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

    logger.info(f"Loaded {len(ticks):,} ticks from Databento")
    return ticks


def load_warmup_bars(db_path: str, symbol: str, before_time: str, limit: int = 50) -> List[FootprintBar]:
    """Load warmup bars from Bishop's database."""
    import sqlite3
    from src.core.types import FootprintBar, PriceLevel

    if not os.path.exists(db_path):
        logger.warning(f"Warmup DB not found: {db_path}")
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT * FROM bars
        WHERE symbol = ? AND start_time < ?
        ORDER BY start_time DESC
        LIMIT ?
    """, (symbol, before_time, limit)).fetchall()
    conn.close()

    bars = []
    for row in reversed(rows):
        close_price = row["close_price"]
        synthetic_level = PriceLevel(
            price=close_price,
            bid_volume=row["sell_volume"],
            ask_volume=row["buy_volume"],
        )
        bar = FootprintBar(
            symbol=row["symbol"],
            start_time=datetime.fromisoformat(row["start_time"]),
            end_time=datetime.fromisoformat(row["end_time"]),
            timeframe=300,
            open_price=row["open_price"],
            high_price=row["high_price"],
            low_price=row["low_price"],
            close_price=close_price,
            levels={close_price: synthetic_level},
        )
        bars.append(bar)

    logger.info(f"Loaded {len(bars)} warmup bars from {db_path}")
    return bars


class ComparisonBacktest:
    """Run backtest against Databento data for comparison."""

    def __init__(self, starting_balance: float = 2527.50):
        self.starting_balance = starting_balance
        self.trades = []
        self.signals_detected = []
        self.signals_approved = []

        # Components
        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None
        self.session: Optional[TradingSession] = None
        self.manager: Optional[ExecutionManager] = None
        self.tier_manager: Optional[TierManager] = None

        # Signal stacking
        self._current_bar_signals: List[Signal] = []

    def setup(self, symbol: str = "MES", warmup_bars: List[FootprintBar] = None):
        """Initialize all components."""

        # Tier manager
        state_file = Path(SCRIPT_DIR) / "comparison_tier_state.json"
        state_file.unlink(missing_ok=True)

        self.tier_manager = TierManager(
            starting_balance=self.starting_balance,
            state_file=state_file,
        )

        tier_config = self.tier_manager.start_session()

        logger.info(f"Starting balance: ${self.starting_balance:,.2f}")
        logger.info(f"Tier: {tier_config['tier_name']}")
        logger.info(f"Symbol: {symbol}")

        # Session
        self.session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=1000000,  # Uncapped (match Bishop)
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

        # Warmup router with bars (match Bishop's state)
        if warmup_bars:
            for bar in warmup_bars:
                self.router.on_bar(bar)
            regime = self.router.current_regime.value if self.router.current_regime else "UNKNOWN"
            logger.info(f"Warmup complete: {len(warmup_bars)} bars, regime={regime}")

        # Wire up callbacks
        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

        self._current_bar_signals = []

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
                logger.info(
                    f"Order: {order.side} {order.size}x @ {order.entry_price:.2f} "
                    f"({signal.pattern.name})"
                )

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
        logger.info(
            f"Trade closed: {trade.side} {trade.size}x | "
            f"{trade.entry_price:.2f} -> {trade.exit_price:.2f} | "
            f"P&L: {emoji}${trade.pnl:.2f} ({trade.exit_reason})"
        )

    def run(self, ticks: List[Tick]) -> dict:
        """Run backtest on ticks."""

        # Flatten time: 3:55 PM ET (match Bishop)
        flatten_time = time(15, 55)
        flattened = False

        for i, tick in enumerate(ticks):
            # Check for flatten
            tick_time = tick.timestamp.time() if hasattr(tick.timestamp, 'time') else None
            if tick_time and tick_time >= flatten_time and not flattened:
                if self.manager and self.manager.open_positions:
                    logger.info("Flattening at 3:55 PM ET")
                    self.manager.close_all_positions(tick.price, "AUTO_FLATTEN")
                flattened = True

            if flattened:
                continue

            # Process tick
            self.engine.process_tick(tick)

            # Update prices
            if self.manager and self.manager.open_positions:
                self.manager.update_prices(tick.price)

            # Check halt
            if self.manager.is_halted:
                logger.info(f"Session halted: {self.manager.halt_reason}")
                break

            # Progress
            if i > 0 and i % 50000 == 0:
                pct = i / len(ticks) * 100
                logger.info(f"Progress: {pct:.0f}% ({i:,}/{len(ticks):,} ticks)")

        # Close any remaining positions
        if self.manager.open_positions:
            last_price = ticks[-1].price if ticks else 0
            self.manager.close_all_positions(last_price, "END_OF_DAY")

        # Results
        gross_pnl = sum(t["pnl"] for t in self.trades)
        wins = sum(1 for t in self.trades if t["pnl"] > 0)
        losses = len(self.trades) - wins

        return {
            "ticks": len(ticks),
            "trades": len(self.trades),
            "wins": wins,
            "losses": losses,
            "gross_pnl": gross_pnl,
            "signals_detected": len(self.signals_detected),
            "signals_approved": len(self.signals_approved),
            "trade_details": self.trades,
        }


async def main():
    parser = argparse.ArgumentParser(description="Databento Comparison Backtest")
    parser.add_argument("--date", required=True, help="Date (YYYY-MM-DD)")
    parser.add_argument("--balance", type=float, default=2527.50, help="Starting balance")
    parser.add_argument("--warmup-db", type=str, help="Path to warmup bars.db")
    parser.add_argument("--warmup-before", type=str, default="2025-12-03T14:34:00",
                        help="Only load warmup bars before this time")
    args = parser.parse_args()

    # Load Databento ticks
    ticks = load_databento_ticks(args.date)
    if not ticks:
        print("No ticks found!")
        return

    # Load warmup bars
    warmup_bars = []
    if args.warmup_db:
        warmup_bars = load_warmup_bars(args.warmup_db, "MES", args.warmup_before)

    # Run backtest
    backtest = ComparisonBacktest(starting_balance=args.balance)
    backtest.setup(symbol="MES", warmup_bars=warmup_bars)

    results = backtest.run(ticks)

    # Print results
    print("\n" + "=" * 60)
    print("DATABENTO COMPARISON RESULTS")
    print("=" * 60)
    print(f"Ticks processed: {results['ticks']:,}")
    print(f"Signals detected: {results['signals_detected']}")
    print(f"Signals approved: {results['signals_approved']}")
    print(f"Trades: {results['trades']}")
    print(f"Wins/Losses: {results['wins']}/{results['losses']}")
    print(f"Gross P&L: ${results['gross_pnl']:+,.2f}")
    print("=" * 60)

    print("\nTrade Details:")
    for i, t in enumerate(results["trade_details"], 1):
        pnl_str = f"+${t['pnl']:.2f}" if t["pnl"] >= 0 else f"-${abs(t['pnl']):.2f}"
        print(f"  {i}. {t['side']} {t['size']}x | {t['entry_price']:.2f} -> {t['exit_price']:.2f} | {pnl_str} ({t['exit_reason']})")

    print("\n" + "=" * 60)
    print("COMPARE TO BISHOP:")
    print("  Expected: 10 trades, +$122.50 gross")
    print(f"  Got:      {results['trades']} trades, ${results['gross_pnl']:+,.2f} gross")
    match = "MATCH" if results['trades'] == 10 and abs(results['gross_pnl'] - 122.50) < 5 else "DIFFERS"
    print(f"  Result:   {match}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
