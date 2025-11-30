#!/usr/bin/env python3
"""
Batch backtest multiple dates and summarize results.
"""

import argparse
import os
import sys
from datetime import datetime, time

from src.core.types import Tick, Signal, FootprintBar
from src.data.adapters.polygon import PolygonAdapter
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession


class BacktestRunner:
    """Run backtest for a single date."""

    def __init__(self, symbol: str = "ES", api_key: str = None):
        self.symbol = symbol
        self.api_key = api_key or os.getenv("POLYGON_API_KEY")
        self.results = {}

    def run_date(self, date: str) -> dict:
        """Run backtest for a specific date."""
        print(f"\n{'='*60}")
        print(f"Backtesting {self.symbol} on {date}")
        print(f"{'='*60}")

        # Polygon adapter
        adapter = PolygonAdapter(self.api_key)

        # Engine with 5-minute bars
        engine = OrderFlowEngine({
            "symbol": self.symbol,
            "timeframe": 300,
        })

        # Router with slightly lower thresholds for backtesting
        router = StrategyRouter({
            "min_signal_strength": 0.60,  # Lower from 0.70
            "min_regime_confidence": 0.50,  # Lower from 0.60
            "session_open": time(9, 30),
            "session_close": time(16, 0),
            "regime": {
                "min_regime_score": 3.5,  # Lower from 4.0
                "adx_trend_threshold": 25,
            },
        })

        # Session - use 100 shares for ETFs to get meaningful P&L
        # For SPY: 100 shares * $0.50 stop = $50 risk per trade
        is_etf = self.symbol in ["SPY", "QQQ", "IWM"]
        position_size = 100 if is_etf else 1
        stop_ticks = 50 if is_etf else 5  # $0.50 for ETFs, 5 ticks for futures
        target_ticks = 100 if is_etf else 4  # $1.00 for ETFs, 4 ticks for futures

        session = TradingSession(
            mode="paper",
            symbol=self.symbol,
            daily_profit_target=500.0,
            daily_loss_limit=-300.0,
            max_position_size=position_size,
            max_concurrent_trades=1,
            stop_loss_ticks=stop_ticks,
            take_profit_ticks=target_ticks,
            trading_start=time(9, 30),
            trading_end=time(16, 0),
        )
        session.started_at = datetime.now()

        # Execution manager
        manager = ExecutionManager(session)

        # Override trading hours check for backtesting (we're running outside market hours)
        session.is_within_trading_hours = lambda: True

        # Stats
        tick_count = 0
        signals_generated = 0
        signals_approved = 0
        trades_executed = 0

        # Track signals
        all_signals = []

        def on_tick(tick: Tick):
            nonlocal tick_count
            tick_count += 1
            engine.process_tick(tick)

        def on_bar(bar: FootprintBar):
            router.on_bar(bar)
            if bar.close_price:
                manager.update_prices(bar.close_price)

        def on_signal(signal: Signal):
            nonlocal signals_generated, signals_approved, trades_executed
            signals_generated += 1

            signal = router.evaluate_signal(signal)
            all_signals.append({
                "pattern": signal.pattern.value if hasattr(signal.pattern, 'value') else str(signal.pattern),
                "direction": signal.direction,
                "strength": signal.strength,
                "price": signal.price,
                "approved": signal.approved,
                "rejection_reason": signal.rejection_reason,
                "regime": signal.regime,
            })

            if signal.approved:
                signals_approved += 1

                # Check cooldown and position
                if not manager.pending_orders and not manager.open_positions:
                    if signal.strength >= 0.60:
                        multiplier = router.get_position_size_multiplier()
                        order = manager.on_signal(signal, multiplier)
                        if order:
                            trades_executed += 1
                            print(f"  TRADE: {order.side} @ {order.entry_price:.2f}")

        # Wire callbacks
        engine.on_bar(on_bar)
        engine.on_signal(on_signal)
        adapter.register_callback(on_tick)

        # Get data and replay
        bars = adapter.get_minute_bars(self.symbol, date, "09:30", "16:00")
        if not bars:
            print(f"  No data found for {date}")
            return None

        ticks = adapter.bars_to_ticks(bars, self.symbol)
        print(f"  Loaded {len(bars)} bars, {len(ticks)} ticks")

        # Fast replay (no delays)
        for tick in ticks:
            on_tick(tick)

        # Get results
        stats = manager.get_statistics()
        state = manager.get_state()

        result = {
            "date": date,
            "symbol": self.symbol,
            "bars": len(bars),
            "ticks": len(ticks),
            "signals_generated": signals_generated,
            "signals_approved": signals_approved,
            "trades": stats.get("total_trades", 0),
            "win_rate": stats.get("win_rate", 0),
            "pnl": state.get("daily_pnl", 0),
            "signals": all_signals,
        }

        print(f"\n  Results:")
        print(f"    Bars: {result['bars']}")
        print(f"    Signals: {result['signals_generated']} generated, {result['signals_approved']} approved")
        print(f"    Trades: {result['trades']}")
        print(f"    Win Rate: {result['win_rate']:.1%}")
        print(f"    P&L: ${result['pnl']:.2f}")

        # Show signal breakdown
        if all_signals:
            print(f"\n  Signal Breakdown:")
            approved = [s for s in all_signals if s['approved']]
            rejected = [s for s in all_signals if not s['approved']]

            if approved:
                print(f"    Approved ({len(approved)}):")
                for s in approved[:5]:
                    print(f"      {s['pattern']} {s['direction']} @ {s['price']:.2f} (strength: {s['strength']:.2f})")

            if rejected:
                reasons = {}
                for s in rejected:
                    reason = s['rejection_reason'] or "Unknown"
                    reasons[reason] = reasons.get(reason, 0) + 1
                print(f"    Rejected ({len(rejected)}):")
                for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:5]:
                    print(f"      {reason}: {count}")

        return result


def main():
    parser = argparse.ArgumentParser(description="Batch backtest multiple dates")
    parser.add_argument(
        "--dates",
        type=str,
        nargs="+",
        default=[
            "2024-11-06",  # Wednesday (post-election)
            "2024-11-08",  # Friday
            "2024-11-11",  # Monday (Veterans Day - market open)
            "2024-11-18",  # Monday
            "2024-11-20",  # Wednesday
            "2024-10-15",  # Tuesday (mid-October)
            "2024-10-21",  # Monday
            "2024-10-30",  # Wednesday
        ],
        help="Dates to backtest"
    )
    parser.add_argument("--symbol", type=str, default="SPY", help="Symbol (SPY for Polygon free tier)")
    parser.add_argument("--api-key", type=str, default=None, help="Polygon API key")

    args = parser.parse_args()

    runner = BacktestRunner(symbol=args.symbol, api_key=args.api_key)

    all_results = []
    for date in args.dates:
        try:
            result = runner.run_date(date)
            if result:
                all_results.append(result)
        except Exception as e:
            print(f"  Error on {date}: {e}")

    # Summary
    print("\n" + "=" * 60)
    print("BACKTEST SUMMARY")
    print("=" * 60)

    if all_results:
        total_trades = sum(r["trades"] for r in all_results)
        total_signals = sum(r["signals_generated"] for r in all_results)
        total_approved = sum(r["signals_approved"] for r in all_results)
        total_pnl = sum(r["pnl"] for r in all_results)
        wins = sum(1 for r in all_results if r["pnl"] > 0)

        print(f"Dates tested: {len(all_results)}")
        print(f"Total signals: {total_signals} generated, {total_approved} approved")
        print(f"Total trades: {total_trades}")
        print(f"Total P&L: ${total_pnl:.2f}")
        print(f"Winning days: {wins}/{len(all_results)}")

        print(f"\nPer-day breakdown:")
        print(f"{'Date':<12} {'Signals':>8} {'Approved':>8} {'Trades':>7} {'P&L':>10}")
        print("-" * 50)
        for r in all_results:
            print(f"{r['date']:<12} {r['signals_generated']:>8} {r['signals_approved']:>8} {r['trades']:>7} ${r['pnl']:>9.2f}")
    else:
        print("No results collected")


if __name__ == "__main__":
    main()
