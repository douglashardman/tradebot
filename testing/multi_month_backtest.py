#!/usr/bin/env python3
"""
Multi-Month Limit Order Backtest

Tests limit order strategy across diverse market conditions:
- August 2024: VIX spike, volatility
- October 2024: Pre-election uncertainty
- February 2025: Mid-winter, choppy
- June 2025: Summer, trending or dead

All tests use ES (Tier 2+) with limit orders at signal price.
"""

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
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.data.adapters.databento import DatabentoAdapter

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("multi_month")


@dataclass
class PendingOrder:
    """A limit order waiting to be filled."""
    signal: Signal
    limit_price: float
    direction: str
    size: int
    stop_price: float
    target_price: float
    created_at: datetime
    pattern: str
    _created_bar: int = 0


@dataclass
class Position:
    """An open position."""
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
    exit_reason: str
    pnl: float
    pnl_ticks: int
    pattern: str
    date: str


class MonthBacktester:
    """Backtest a single month with limit orders."""

    def __init__(
        self,
        stop_ticks: int = 16,
        target_ticks: int = 24,
        tick_size: float = 0.25,
        tick_value: float = 12.50,  # ES
        max_pending_bars: int = 6,
    ):
        self.stop_ticks = stop_ticks
        self.target_ticks = target_ticks
        self.tick_size = tick_size
        self.tick_value = tick_value
        self.max_pending_bars = max_pending_bars

        self.reset()

    def reset(self):
        """Reset state for new month."""
        self.pending_orders: List[PendingOrder] = []
        self.open_position: Optional[Position] = None
        self.completed_trades: List[TradeResult] = []
        self.expired_orders: int = 0
        self.filled_orders: int = 0

        self.engine: Optional[OrderFlowEngine] = None
        self.router: Optional[StrategyRouter] = None

        self._current_bar_close: Optional[float] = None
        self._current_bar_count: int = 0
        self._current_date: str = ""

        self.pattern_stats: Dict[str, Dict] = defaultdict(
            lambda: {"signals": 0, "filled": 0, "expired": 0, "wins": 0, "losses": 0, "pnl": 0.0}
        )
        self.daily_results: List[Dict] = []

    def _setup_day(self, date: str, symbol: str) -> None:
        self._current_date = date
        self._current_bar_count = 0
        self.pending_orders = []

        self.engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})
        self.router = StrategyRouter({})

        self.engine.on_bar(self._on_bar)
        self.engine.on_signal(self._on_signal)

    def _on_bar(self, bar: FootprintBar) -> None:
        self._current_bar_close = bar.close_price
        self._current_bar_count += 1

        if self.router:
            self.router.on_bar(bar)

        self._expire_old_orders()

    def _on_signal(self, signal: Signal) -> None:
        if not self.router or self.open_position:
            return

        signal = self.router.evaluate_signal(signal)

        if not signal.approved:
            return

        limit_price = signal.price
        direction = signal.direction

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
            size=1,
            stop_price=stop_price,
            target_price=target_price,
            created_at=signal.timestamp,
            pattern=pattern,
            _created_bar=self._current_bar_count,
        )

        self.pending_orders.append(order)
        self.pattern_stats[pattern]["signals"] += 1

    def _expire_old_orders(self) -> None:
        expired = []
        for order in self.pending_orders:
            bars_pending = self._current_bar_count - order._created_bar
            if bars_pending >= self.max_pending_bars:
                expired.append(order)
                self.expired_orders += 1
                self.pattern_stats[order.pattern]["expired"] += 1

        for order in expired:
            self.pending_orders.remove(order)

    def _process_tick(self, tick: Tick) -> None:
        price = tick.price

        if not self.open_position and self.pending_orders:
            self._check_limit_fills(tick)

        if self.open_position:
            self._check_position_exit(tick)

        if self.engine:
            self.engine.process_tick(tick)

    def _check_limit_fills(self, tick: Tick) -> None:
        price = tick.price

        for order in list(self.pending_orders):
            filled = False

            if order.direction == "LONG":
                if price <= order.limit_price:
                    filled = True
            else:
                if price >= order.limit_price:
                    filled = True

            if filled:
                if order.direction == "LONG":
                    fill_price = order.limit_price + self.tick_size
                    stop = fill_price - (self.stop_ticks * self.tick_size)
                    target = fill_price + (self.target_ticks * self.tick_size)
                else:
                    fill_price = order.limit_price - self.tick_size
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
                for other in self.pending_orders:
                    self.pattern_stats[other.pattern]["expired"] += 1
                    self.expired_orders += 1
                self.pending_orders = []

                self.filled_orders += 1
                self.pattern_stats[order.pattern]["filled"] += 1
                break

    def _check_position_exit(self, tick: Tick) -> None:
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
        else:
            if price >= pos.stop_price:
                exit_price = pos.stop_price
                exit_reason = "STOP"
            elif price <= pos.target_price:
                exit_price = pos.target_price
                exit_reason = "TARGET"

        if exit_price:
            self._close_position(exit_price, exit_reason, tick.timestamp)

    def _close_position(self, exit_price: float, reason: str, exit_time: datetime) -> None:
        pos = self.open_position
        if not pos:
            return

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
            date=self._current_date,
        )

        self.completed_trades.append(trade)

        if pnl > 0:
            self.pattern_stats[pos.pattern]["wins"] += 1
        else:
            self.pattern_stats[pos.pattern]["losses"] += 1
        self.pattern_stats[pos.pattern]["pnl"] += pnl

        self.open_position = None

    def _end_day(self, date: str) -> Dict:
        if self.open_position and self._current_bar_close:
            self._close_position(self._current_bar_close, "FLATTEN", datetime.now())

        for order in self.pending_orders:
            self.expired_orders += 1
            self.pattern_stats[order.pattern]["expired"] += 1
        self.pending_orders = []

        day_trades = [t for t in self.completed_trades if t.date == date]
        day_pnl = sum(t.pnl for t in day_trades)
        day_wins = sum(1 for t in day_trades if t.pnl > 0)

        result = {
            "date": date,
            "trades": len(day_trades),
            "wins": day_wins,
            "losses": len(day_trades) - day_wins,
            "pnl": day_pnl,
        }

        self.daily_results.append(result)
        return result

    def get_summary(self) -> Dict:
        """Get summary statistics."""
        total_signals = sum(s["signals"] for s in self.pattern_stats.values())
        total_filled = sum(s["filled"] for s in self.pattern_stats.values())
        total_trades = len(self.completed_trades)
        wins = sum(1 for t in self.completed_trades if t.pnl > 0)
        gross_pnl = sum(t.pnl for t in self.completed_trades)
        gross_profit = sum(t.pnl for t in self.completed_trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.completed_trades if t.pnl < 0))

        return {
            "signals": total_signals,
            "filled": total_filled,
            "fill_rate": total_filled / total_signals * 100 if total_signals > 0 else 0,
            "trades": total_trades,
            "wins": wins,
            "losses": total_trades - wins,
            "win_rate": wins / total_trades * 100 if total_trades > 0 else 0,
            "gross_pnl": gross_pnl,
            "gross_profit": gross_profit,
            "gross_loss": gross_loss,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else 0,
            "winning_days": sum(1 for d in self.daily_results if d["pnl"] > 0),
            "losing_days": sum(1 for d in self.daily_results if d["pnl"] < 0),
            "pattern_stats": dict(self.pattern_stats),
        }


def get_trading_days(start_date: str, num_days: int) -> List[str]:
    """Generate trading days (skip weekends)."""
    days = []
    current = datetime.strptime(start_date, "%Y-%m-%d")
    while len(days) < num_days:
        if current.weekday() < 5:
            days.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return days


def load_cached_ticks(contract: str, date: str):
    """Load ticks from cache."""
    from src.core.types import Tick

    cache_path = os.path.join(CACHE_DIR, f"{contract}_{date}_0930_1600.json")

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


def save_ticks_to_cache(ticks, contract: str, date: str):
    """Save ticks to cache."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    cache_path = os.path.join(CACHE_DIR, f"{contract}_{date}_0930_1600.json")

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


async def run_month(month_name: str, start_date: str, num_days: int, contract: str) -> Dict:
    """Run backtest for a single month."""
    logger.info(f"\n{'='*70}")
    logger.info(f"RUNNING: {month_name}")
    logger.info(f"Contract: {contract} | Start: {start_date} | Days: {num_days}")
    logger.info(f"{'='*70}")

    backtester = MonthBacktester()
    trading_days = get_trading_days(start_date, num_days)

    for date in trading_days:
        backtester._setup_day(date, "ES")

        ticks = load_cached_ticks(contract, date)
        if not ticks:
            logger.info(f"Fetching {contract} {date}...")
            try:
                adapter = DatabentoAdapter()
                ticks = adapter.get_session_ticks(
                    contract=contract,
                    date=date,
                    start_time="09:30",
                    end_time="16:00",
                )
                if ticks:
                    save_ticks_to_cache(ticks, contract, date)
            except Exception as e:
                logger.warning(f"Failed to fetch {date}: {e}")
                backtester._end_day(date)
                continue

        if not ticks:
            logger.warning(f"No data for {date}")
            backtester._end_day(date)
            continue

        logger.info(f"{date}: {len(ticks):,} ticks")

        flatten_time = time(15, 55)
        flattened = False

        for tick in ticks:
            tick_time = tick.timestamp.time() if hasattr(tick.timestamp, 'time') else None

            if tick_time and tick_time >= flatten_time and not flattened:
                if backtester.open_position:
                    backtester._close_position(tick.price, "FLATTEN", tick.timestamp)
                for order in backtester.pending_orders:
                    backtester.expired_orders += 1
                    backtester.pattern_stats[order.pattern]["expired"] += 1
                backtester.pending_orders = []
                flattened = True
                continue

            if flattened:
                continue

            backtester._process_tick(tick)

        result = backtester._end_day(date)
        pnl_str = f"+${result['pnl']:.2f}" if result['pnl'] >= 0 else f"${result['pnl']:.2f}"
        logger.info(f"  -> {result['trades']}T | {result['wins']}W/{result['losses']}L | {pnl_str}")

    summary = backtester.get_summary()
    summary["month"] = month_name
    summary["contract"] = contract
    summary["start_date"] = start_date
    summary["num_days"] = num_days
    summary["daily_results"] = backtester.daily_results

    return summary


def generate_markdown_report(results: List[Dict], output_path: str):
    """Generate comprehensive markdown report."""

    with open(output_path, "w") as f:
        f.write("# Multi-Month Limit Order Backtest Analysis\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write("**Strategy:** Limit orders at signal price (bar.low for longs, bar.high for shorts)\n")
        f.write("**Instrument:** ES (E-mini S&P 500) - $12.50 per tick\n")
        f.write("**Stop:** 16 ticks (4 points) | **Target:** 24 ticks (6 points)\n")
        f.write("**Order Expiry:** 6 bars (30 minutes)\n\n")

        f.write("---\n\n")

        # Executive Summary
        f.write("## Executive Summary\n\n")
        f.write("| Month | Trades | Win Rate | Gross P&L | After Comm | Profit Factor |\n")
        f.write("|-------|--------|----------|-----------|------------|---------------|\n")

        total_trades = 0
        total_wins = 0
        total_pnl = 0

        for r in results:
            commission = r["trades"] * 4.50
            net_pnl = r["gross_pnl"] - commission
            total_trades += r["trades"]
            total_wins += r["wins"]
            total_pnl += r["gross_pnl"]

            f.write(f"| {r['month']} | {r['trades']} | {r['win_rate']:.1f}% | ${r['gross_pnl']:+,.2f}")
            f.write(f" | ${net_pnl:+,.2f} | {r['profit_factor']:.2f} |\n")

        total_commission = total_trades * 4.50
        total_net = total_pnl - total_commission
        total_wr = total_wins / total_trades * 100 if total_trades > 0 else 0

        f.write(f"| **TOTAL** | **{total_trades}** | **{total_wr:.1f}%** | **${total_pnl:+,.2f}**")
        f.write(f" | **${total_net:+,.2f}** | - |\n\n")

        f.write("---\n\n")

        # Pattern Analysis Across All Months
        f.write("## Pattern Performance Across All Months\n\n")

        # Aggregate pattern stats
        pattern_totals = defaultdict(lambda: {
            "signals": 0, "filled": 0, "expired": 0,
            "wins": 0, "losses": 0, "pnl": 0.0,
            "by_month": {}
        })

        for r in results:
            month = r["month"]
            for pattern, stats in r["pattern_stats"].items():
                pattern_totals[pattern]["signals"] += stats["signals"]
                pattern_totals[pattern]["filled"] += stats["filled"]
                pattern_totals[pattern]["expired"] += stats["expired"]
                pattern_totals[pattern]["wins"] += stats["wins"]
                pattern_totals[pattern]["losses"] += stats["losses"]
                pattern_totals[pattern]["pnl"] += stats["pnl"]
                pattern_totals[pattern]["by_month"][month] = stats

        f.write("### Aggregate Performance\n\n")
        f.write("| Pattern | Signals | Fill% | Trades | Win% | Gross P&L | After Comm |\n")
        f.write("|---------|---------|-------|--------|------|-----------|------------|\n")

        sorted_patterns = sorted(pattern_totals.items(), key=lambda x: x[1]["pnl"], reverse=True)

        for pattern, stats in sorted_patterns:
            trades = stats["wins"] + stats["losses"]
            fill_rate = stats["filled"] / stats["signals"] * 100 if stats["signals"] > 0 else 0
            win_rate = stats["wins"] / trades * 100 if trades > 0 else 0
            commission = trades * 4.50
            net_pnl = stats["pnl"] - commission

            f.write(f"| {pattern} | {stats['signals']} | {fill_rate:.0f}% | {trades}")
            f.write(f" | {win_rate:.0f}% | ${stats['pnl']:+,.2f} | ${net_pnl:+,.2f} |\n")

        f.write("\n")

        # Pattern by Month Breakdown
        f.write("### Pattern Performance By Month\n\n")

        for pattern, stats in sorted_patterns:
            f.write(f"#### {pattern}\n\n")
            f.write("| Month | Signals | Filled | Win% | P&L |\n")
            f.write("|-------|---------|--------|------|-----|\n")

            for r in results:
                month = r["month"]
                if month in stats["by_month"]:
                    ms = stats["by_month"][month]
                    trades = ms["wins"] + ms["losses"]
                    wr = ms["wins"] / trades * 100 if trades > 0 else 0
                    f.write(f"| {month} | {ms['signals']} | {ms['filled']} | {wr:.0f}% | ${ms['pnl']:+,.2f} |\n")
                else:
                    f.write(f"| {month} | 0 | 0 | - | $0.00 |\n")

            f.write("\n")

        f.write("---\n\n")

        # Consistency Analysis
        f.write("## Pattern Consistency Analysis\n\n")
        f.write("Patterns are rated on consistency across all 4 months:\n\n")

        f.write("| Pattern | Profitable Months | Avg Win% | Verdict |\n")
        f.write("|---------|-------------------|----------|----------|\n")

        for pattern, stats in sorted_patterns:
            profitable_months = 0
            win_rates = []

            for month, ms in stats["by_month"].items():
                trades = ms["wins"] + ms["losses"]
                if trades > 0:
                    if ms["pnl"] > 0:
                        profitable_months += 1
                    win_rates.append(ms["wins"] / trades * 100)

            avg_wr = sum(win_rates) / len(win_rates) if win_rates else 0

            if profitable_months >= 3 and avg_wr >= 50:
                verdict = "✅ KEEP"
            elif profitable_months >= 2 and avg_wr >= 45:
                verdict = "⚠️ FILTER"
            else:
                verdict = "❌ DISABLE"

            f.write(f"| {pattern} | {profitable_months}/4 | {avg_wr:.0f}% | {verdict} |\n")

        f.write("\n---\n\n")

        # Monthly Details
        f.write("## Monthly Details\n\n")

        for r in results:
            f.write(f"### {r['month']}\n\n")
            f.write(f"**Contract:** {r['contract']} | **Period:** {r['start_date']} ({r['num_days']} days)\n\n")

            f.write("| Metric | Value |\n")
            f.write("|--------|-------|\n")
            f.write(f"| Signals | {r['signals']} |\n")
            f.write(f"| Fill Rate | {r['fill_rate']:.1f}% |\n")
            f.write(f"| Trades | {r['trades']} |\n")
            f.write(f"| Win Rate | {r['win_rate']:.1f}% |\n")
            f.write(f"| Gross P&L | ${r['gross_pnl']:+,.2f} |\n")
            commission = r['trades'] * 4.50
            f.write(f"| Commissions | ${commission:.2f} |\n")
            f.write(f"| Net P&L | ${r['gross_pnl'] - commission:+,.2f} |\n")
            f.write(f"| Profit Factor | {r['profit_factor']:.2f} |\n")
            f.write(f"| Winning Days | {r['winning_days']} |\n")
            f.write(f"| Losing Days | {r['losing_days']} |\n")
            f.write("\n")

            f.write("**Daily Breakdown:**\n\n")
            f.write("| Date | Trades | W/L | P&L |\n")
            f.write("|------|--------|-----|-----|\n")
            for d in r["daily_results"]:
                pnl_str = f"+${d['pnl']:.2f}" if d['pnl'] >= 0 else f"${d['pnl']:.2f}"
                f.write(f"| {d['date']} | {d['trades']} | {d['wins']}/{d['losses']} | {pnl_str} |\n")
            f.write("\n")

        f.write("---\n\n")

        # Recommendations
        f.write("## Recommendations\n\n")
        f.write("Based on cross-month analysis:\n\n")

        for pattern, stats in sorted_patterns:
            profitable_months = sum(1 for ms in stats["by_month"].values()
                                   if ms["wins"] + ms["losses"] > 0 and ms["pnl"] > 0)
            trades = stats["wins"] + stats["losses"]
            win_rate = stats["wins"] / trades * 100 if trades > 0 else 0

            if profitable_months >= 3 and win_rate >= 50:
                f.write(f"- **{pattern}**: ✅ Strong performer - keep enabled\n")
            elif profitable_months >= 2 and win_rate >= 45:
                f.write(f"- **{pattern}**: ⚠️ Marginal - consider regime filtering\n")
            elif profitable_months == 1:
                f.write(f"- **{pattern}**: ❌ Inconsistent - disable or heavy filtering\n")
            else:
                f.write(f"- **{pattern}**: ❌ Net loser - disable\n")

        f.write("\n---\n\n")
        f.write("*Report generated by Delta Trading System*\n")

    logger.info(f"Report saved to: {output_path}")


async def main():
    """Run all month backtests."""

    # Define test months
    # Contract months: H=March, M=June, U=September, Z=December
    months = [
        ("August 2024", "2024-08-01", 22, "ESU4"),      # Sep contract
        ("October 2024", "2024-10-01", 23, "ESZ4"),     # Dec contract
        ("February 2025", "2025-02-03", 19, "ESH5"),    # Mar contract
        ("June 2025", "2025-06-02", 21, "ESU5"),        # Sep contract
    ]

    results = []

    for month_name, start_date, num_days, contract in months:
        try:
            summary = await run_month(month_name, start_date, num_days, contract)
            results.append(summary)
        except Exception as e:
            logger.error(f"Failed to run {month_name}: {e}")
            import traceback
            traceback.print_exc()

    # Generate report
    report_path = os.path.join(os.path.dirname(__file__), "multi_month_analysis.md")
    generate_markdown_report(results, report_path)

    # Print summary to console
    print("\n" + "=" * 70)
    print("MULTI-MONTH BACKTEST SUMMARY")
    print("=" * 70)

    for r in results:
        commission = r["trades"] * 4.50
        net = r["gross_pnl"] - commission
        print(f"\n{r['month']}:")
        print(f"  Trades: {r['trades']} | Win Rate: {r['win_rate']:.1f}%")
        print(f"  Gross: ${r['gross_pnl']:+,.2f} | Net: ${net:+,.2f}")

    print("\n" + "=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
