#!/usr/bin/env python3
"""
Advanced backtesting framework for position sizing, timing, and pattern tests.

Supports multiple test configurations:
- Volatility-based sizing
- Regime-based sizing
- Win streak sizing
- Time-based stops
- Trade count limits
- Time window restrictions
- And more...
"""

import os
import sys
import re
import json
from datetime import datetime, time, timedelta
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Callable
from enum import Enum

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Tick, Signal, FootprintBar
from src.data.adapters.databento import DatabentoAdapter
from src.analysis.engine import OrderFlowEngine
from src.regime.router import StrategyRouter
from src.execution.manager import ExecutionManager
from src.execution.session import TradingSession
from src.core.constants import TICK_VALUES

CACHE_DIR = os.path.join(os.path.dirname(__file__), "../data/tick_cache")


@dataclass
class TestConfig:
    """Configuration for a backtest run."""
    name: str
    description: str

    # Base parameters (defaults from optimal settings)
    daily_loss_limit: float = -500.0
    conservative_fills: bool = True
    base_position_size: int = 1
    stop_loss_ticks: int = 16
    take_profit_ticks: int = 24

    # Position sizing modifiers
    volatility_sizing: bool = False  # Use ATR to adjust size
    regime_sizing: bool = False      # Use regime to adjust size
    streak_sizing: bool = False      # Use win/loss streak to adjust size

    # Time restrictions
    start_time: Optional[time] = None   # None = 09:30
    end_time: Optional[time] = None     # None = 16:00
    skip_first_minutes: int = 0         # Skip opening N minutes

    # Trade limits
    max_trades_per_day: int = 100       # Effectively unlimited

    # Early stop conditions
    first_hour_loss_stop: Optional[float] = None  # Stop if down this much in first hour

    # Pattern filters
    require_stacked_signals: bool = False  # Only trade when 2+ patterns fire
    stacked_signals_sizing: bool = False   # Double size when 2+ signals in same bar
    pattern_sequence_sizing: bool = False  # ABSORPTION followed by EXHAUSTION = extra conviction

    # Combined strategy options
    combine_sizing: bool = False           # If True, sizing strategies are ADDITIVE
    max_position_size: int = 4             # Maximum contracts when combining strategies
    scaled_loss_limit: bool = False        # Scale daily loss limit with position size

    def __str__(self):
        return f"{self.name}: {self.description}"


class AdvancedBacktester:
    """Run backtests with advanced configuration options."""

    def __init__(self, config: TestConfig):
        self.config = config
        self.results = []

    def get_cached_sessions(self) -> List[Dict]:
        """Get all cached sessions."""
        sessions = []
        for filename in os.listdir(CACHE_DIR):
            if not filename.endswith('.json'):
                continue
            match = re.match(r'(\w+)_(\d{4}-\d{2}-\d{2})_(\d{4})_(\d{4})\.json', filename)
            if match:
                contract, date, start, end = match.groups()
                sessions.append({
                    'contract': contract,
                    'date': date,
                    'start_time': f"{start[:2]}:{start[2:]}",
                    'end_time': f"{end[:2]}:{end[2:]}",
                })
        sessions.sort(key=lambda x: x['date'])
        return sessions

    def load_ticks(self, session: Dict) -> List[Tick]:
        """Load ticks from cache."""
        filename = f"{session['contract']}_{session['date']}_{session['start_time'].replace(':', '')}_{session['end_time'].replace(':', '')}.json"
        cache_path = os.path.join(CACHE_DIR, filename)

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

    def calculate_atr(self, ticks: List[Tick], period: int = 14) -> float:
        """Calculate ATR from tick data."""
        if len(ticks) < 100:
            return 0

        # Build 5-min bars
        bars = []
        current_bar_start = None
        high, low, close = 0, float('inf'), 0

        for tick in ticks:
            bar_start = tick.timestamp.replace(second=0, microsecond=0)
            bar_start = bar_start.replace(minute=(bar_start.minute // 5) * 5)

            if current_bar_start is None:
                current_bar_start = bar_start
                high = low = close = tick.price
            elif bar_start != current_bar_start:
                bars.append({'high': high, 'low': low, 'close': close})
                current_bar_start = bar_start
                high = low = close = tick.price
            else:
                high = max(high, tick.price)
                low = min(low, tick.price)
                close = tick.price

        if len(bars) < period + 1:
            return sum(b['high'] - b['low'] for b in bars) / len(bars) if bars else 0

        # Calculate ATR
        trs = []
        for i in range(1, len(bars)):
            tr = max(
                bars[i]['high'] - bars[i]['low'],
                abs(bars[i]['high'] - bars[i-1]['close']),
                abs(bars[i]['low'] - bars[i-1]['close'])
            )
            trs.append(tr)

        return sum(trs[-period:]) / period

    def get_position_size(self, atr: float, regime: str, win_streak: int, loss_streak: int, stacked_count: int = 1) -> int:
        """Calculate position size based on config.

        When combine_sizing is True, strategies are ADDITIVE:
        - Base: 1 contract
        - Stacked signals (2+): +1 contract
        - Trending regime: +1 contract
        - Win streak (3+): +1 contract
        - Loss streak (2+): -1 contract

        Otherwise, strategies override each other (legacy behavior).
        """
        if getattr(self.config, 'combine_sizing', False):
            # COMBINED mode: strategies are additive
            size = self.config.base_position_size

            # Stacked signals bonus
            if self.config.stacked_signals_sizing and stacked_count >= 2:
                size += 1

            # Regime bonus
            if self.config.regime_sizing:
                if regime in ['TRENDING_UP', 'TRENDING_DOWN']:
                    size += 1

            # Streak adjustment
            if self.config.streak_sizing:
                if win_streak >= 3:
                    size += 1
                elif loss_streak >= 2:
                    size -= 1

            # Apply cap
            max_size = getattr(self.config, 'max_position_size', 4)
            return max(1, min(size, max_size))

        # LEGACY mode: strategies override each other
        size = self.config.base_position_size

        if self.config.volatility_sizing:
            # Low ATR (< 3 points) = 2 contracts, High ATR = 1 contract
            if atr < 3.0:
                size = 2
            else:
                size = 1

        if self.config.regime_sizing:
            if regime in ['TRENDING_UP', 'TRENDING_DOWN']:
                size = 2
            else:
                size = 1

        if self.config.streak_sizing:
            if win_streak >= 3:
                size = min(size + 1, 3)  # Cap at 3
            elif loss_streak >= 2:
                size = max(size - 1, 1)  # Min 1

        return size

    def should_skip_time(self, tick_time: datetime) -> bool:
        """Check if we should skip this time based on config.

        Note: tick_time is in UTC. Session starts at 14:30 UTC (9:30 ET).
        """
        t = tick_time.time()

        # Skip first N minutes (session starts at 14:30 UTC = 9:30 ET)
        if self.config.skip_first_minutes > 0:
            session_start_utc = time(14, 30)  # 9:30 ET = 14:30 UTC
            skip_until = (datetime.combine(datetime.today(), session_start_utc) +
                         timedelta(minutes=self.config.skip_first_minutes)).time()
            if t < skip_until:
                return True

        # Custom start time (already in UTC from config)
        if self.config.start_time and t < self.config.start_time:
            return True

        # Custom end time (already in UTC from config)
        if self.config.end_time and t > self.config.end_time:
            return True

        return False

    def run_single_day(self, session: Dict) -> Dict:
        """Run backtest for a single day."""
        ticks = self.load_ticks(session)
        if not ticks:
            return None

        symbol = session['contract'][:2]
        tick_value = TICK_VALUES.get(symbol, 12.50)

        # Calculate day's ATR for volatility sizing
        atr = self.calculate_atr(ticks) if self.config.volatility_sizing else 0

        # Setup components
        engine = OrderFlowEngine({"symbol": symbol, "timeframe": 300})

        router = StrategyRouter({
            "min_signal_strength": 0.60,
            "min_regime_confidence": 0.50,
            "session_open": time(14, 30),
            "session_close": time(21, 0),
        })

        trading_session = TradingSession(
            mode="paper",
            symbol=symbol,
            daily_profit_target=100000.0,
            daily_loss_limit=self.config.daily_loss_limit,
            max_position_size=self.config.base_position_size,
            max_concurrent_trades=1,
            stop_loss_ticks=self.config.stop_loss_ticks,
            take_profit_ticks=self.config.take_profit_ticks,
            conservative_fills=self.config.conservative_fills,
        )
        trading_session.is_within_trading_hours = lambda: True

        manager = ExecutionManager(trading_session)

        # Tracking
        trades = []
        signals_generated = 0
        win_streak = 0
        loss_streak = 0
        first_hour_pnl = 0
        first_hour_end = None
        stopped_early = False

        # For stacked signals (Test 9) - track signals per bar
        current_bar_signals = []
        current_bar_time = None

        # For pattern sequences (Test 10) - track last pattern
        last_pattern = None
        last_pattern_direction = None

        def on_bar(bar: FootprintBar):
            nonlocal current_bar_signals, current_bar_time
            # Reset signal tracking on new bar
            if bar.start_time != current_bar_time:
                current_bar_signals = []
                current_bar_time = bar.start_time
            router.on_bar(bar)
            if bar.close_price:
                manager.update_prices(bar.close_price)

        def on_signal(signal: Signal):
            nonlocal signals_generated, win_streak, loss_streak, stopped_early
            nonlocal current_bar_signals, last_pattern, last_pattern_direction
            signals_generated += 1

            if stopped_early:
                return

            if len(trades) >= self.config.max_trades_per_day:
                return

            signal = router.evaluate_signal(signal)

            # Get pattern name for tracking
            pattern_name = signal.pattern.value if hasattr(signal.pattern, 'value') else str(signal.pattern)

            # Track signal for stacked detection
            current_bar_signals.append({
                'pattern': pattern_name,
                'direction': signal.direction,
                'strength': signal.strength
            })

            if signal.approved and signal.strength >= 0.60:
                if not manager.pending_orders and not manager.open_positions:
                    # Get current regime
                    regime_state = router.get_state()
                    regime = regime_state.get("current_regime", "UNKNOWN")

                    # Count same-direction signals in this bar
                    same_direction_count = sum(1 for s in current_bar_signals
                                               if s['direction'] == signal.direction)

                    # Calculate position size (pass stacked count for combined mode)
                    size = self.get_position_size(atr, regime, win_streak, loss_streak, same_direction_count)

                    # Stacked signals sizing (Test 9 - legacy mode, not combined)
                    if self.config.stacked_signals_sizing and not self.config.combine_sizing:
                        if same_direction_count >= 2:
                            size = min(size * 2, 4)  # Double, cap at 4

                    # Pattern sequence sizing (Test 10)
                    if self.config.pattern_sequence_sizing:
                        # ABSORPTION followed by EXHAUSTION = high conviction
                        is_absorption = 'ABSORPTION' in pattern_name
                        is_exhaustion = 'EXHAUSTION' in pattern_name
                        was_absorption = last_pattern and 'ABSORPTION' in last_pattern

                        if is_exhaustion and was_absorption and last_pattern_direction == signal.direction:
                            size = min(size * 2, 4)  # Double, cap at 4

                    # Temporarily update session max size
                    original_size = trading_session.max_position_size
                    trading_session.max_position_size = size

                    order = manager.on_signal(signal, 1.0)

                    trading_session.max_position_size = original_size

                    if order:
                        trades.append({
                            "entry_time": current_tick.timestamp if current_tick else datetime.now(),
                            "pattern": pattern_name,
                            "direction": signal.direction,
                            "entry_price": order.entry_price,
                            "size": size,
                            "regime": regime,
                            "atr": atr,
                            "pnl": 0,
                            "exit_reason": None,
                            "stacked_count": len(current_bar_signals),
                        })

                    # Track pattern for sequence detection (Test 10)
                    last_pattern = pattern_name
                    last_pattern_direction = signal.direction

        engine.on_bar(on_bar)
        engine.on_signal(on_signal)

        # Process ticks
        current_tick = None
        last_pnl = 0

        for tick in ticks:
            current_tick = tick

            # Check time restrictions
            if self.should_skip_time(tick.timestamp):
                continue

            # Track first hour for early stop
            if first_hour_end is None:
                first_hour_end = tick.timestamp + timedelta(hours=1)

            engine.process_tick(tick)

            # Track P&L changes
            state = manager.get_state()
            current_pnl = state.get("daily_pnl", 0)

            if current_pnl != last_pnl and trades:
                pnl_change = current_pnl - last_pnl

                # Update streak tracking
                if pnl_change > 0:
                    win_streak += 1
                    loss_streak = 0
                else:
                    loss_streak += 1
                    win_streak = 0

                # Find and update trade
                for trade in reversed(trades):
                    if trade["pnl"] == 0:
                        trade["pnl"] = pnl_change
                        trade["exit_reason"] = "target" if pnl_change > 0 else "stop"
                        break

                # Track first hour P&L
                if tick.timestamp < first_hour_end:
                    first_hour_pnl = current_pnl

                last_pnl = current_pnl

            # Check first hour stop condition
            if (self.config.first_hour_loss_stop and
                tick.timestamp >= first_hour_end and
                first_hour_pnl <= self.config.first_hour_loss_stop and
                not stopped_early):
                stopped_early = True

        # Calculate results
        total_pnl = sum(t["pnl"] for t in trades)
        wins = sum(1 for t in trades if t["pnl"] > 0)
        losses = sum(1 for t in trades if t["pnl"] < 0)

        return {
            "date": session['date'],
            "contract": session['contract'],
            "trades": len(trades),
            "wins": wins,
            "losses": losses,
            "pnl": total_pnl,
            "atr": atr,
            "stopped_early": stopped_early,
            "first_hour_pnl": first_hour_pnl,
            "trade_details": trades,
        }

    def run_all(self, quiet: bool = True) -> Dict:
        """Run backtest on all cached sessions."""
        sessions = self.get_cached_sessions()

        print(f"\n{'='*70}")
        print(f"TEST: {self.config.name}")
        print(f"{self.config.description}")
        print(f"{'='*70}")
        print(f"Running {len(sessions)} days...")

        results = []
        for i, session in enumerate(sessions):
            if not quiet:
                print(f"[{i+1}/{len(sessions)}] {session['date']}...", end=" ", flush=True)

            try:
                result = self.run_single_day(session)
                if result:
                    results.append(result)
                    if not quiet:
                        status = "WIN" if result['pnl'] > 0 else "LOSS" if result['pnl'] < 0 else "FLAT"
                        print(f"{result['trades']} trades, ${result['pnl']:+,.0f} [{status}]")
            except Exception as e:
                if not quiet:
                    print(f"ERROR: {e}")

        self.results = results
        return self.summarize()

    def summarize(self) -> Dict:
        """Summarize backtest results."""
        if not self.results:
            return {}

        total_pnl = sum(r['pnl'] for r in self.results)
        winning_days = sum(1 for r in self.results if r['pnl'] > 0)
        losing_days = sum(1 for r in self.results if r['pnl'] < 0)
        flat_days = sum(1 for r in self.results if r['pnl'] == 0)
        total_trades = sum(r['trades'] for r in self.results)
        total_wins = sum(r['wins'] for r in self.results)
        total_losses = sum(r['losses'] for r in self.results)

        summary = {
            "config": self.config.name,
            "days_tested": len(self.results),
            "total_pnl": total_pnl,
            "avg_daily_pnl": total_pnl / len(self.results),
            "winning_days": winning_days,
            "winning_days_pct": winning_days / len(self.results) * 100,
            "losing_days": losing_days,
            "losing_days_pct": losing_days / len(self.results) * 100,
            "flat_days": flat_days,
            "total_trades": total_trades,
            "avg_trades_per_day": total_trades / len(self.results),
            "trade_win_rate": total_wins / (total_wins + total_losses) * 100 if (total_wins + total_losses) > 0 else 0,
        }

        # Print summary
        print(f"\n{'='*70}")
        print(f"RESULTS: {self.config.name}")
        print(f"{'='*70}")
        print(f"Days tested:      {summary['days_tested']}")
        print(f"Total P&L:        ${summary['total_pnl']:,.0f}")
        print(f"Avg Daily P&L:    ${summary['avg_daily_pnl']:,.0f}")
        print(f"Winning days:     {summary['winning_days']} ({summary['winning_days_pct']:.1f}%)")
        print(f"Losing days:      {summary['losing_days']} ({summary['losing_days_pct']:.1f}%)")
        print(f"Total trades:     {summary['total_trades']}")
        print(f"Trade win rate:   {summary['trade_win_rate']:.1f}%")

        return summary


def run_test(config: TestConfig, quiet: bool = True) -> Dict:
    """Convenience function to run a test."""
    tester = AdvancedBacktester(config)
    return tester.run_all(quiet=quiet)


# Pre-defined test configurations
BASELINE = TestConfig(
    name="Baseline",
    description="Current optimal settings: $500 loss limit, conservative fills, 1 contract"
)

TEST_4_VOLATILITY_SIZING = TestConfig(
    name="Test 4: Volatility-Based Sizing",
    description="Low ATR days: 2 contracts, High ATR days: 1 contract",
    volatility_sizing=True,
)

TEST_5_REGIME_SIZING = TestConfig(
    name="Test 5: Regime-Based Sizing",
    description="TRENDING: 2 contracts, RANGING: 1 contract",
    regime_sizing=True,
)

TEST_6_STREAK_SIZING = TestConfig(
    name="Test 6: Win Streak Sizing",
    description="After 3 wins: +1 contract, After 2 losses: -1 contract",
    streak_sizing=True,
)

TEST_7_FIRST_HOUR_STOP = TestConfig(
    name="Test 7: First Hour Stop",
    description="If down $200 in first hour, stop for day",
    first_hour_loss_stop=-200.0,
)

TEST_8A_MAX_5_TRADES = TestConfig(
    name="Test 8A: Max 5 Trades",
    description="Maximum 5 trades per day",
    max_trades_per_day=5,
)

TEST_8B_MAX_10_TRADES = TestConfig(
    name="Test 8B: Max 10 Trades",
    description="Maximum 10 trades per day",
    max_trades_per_day=10,
)

TEST_11_FIRST_HOUR_ONLY = TestConfig(
    name="Test 11: First Hour Only",
    description="Trade 9:30-10:30 ET only (14:30-15:30 UTC)",
    end_time=time(15, 30),  # 10:30 ET = 15:30 UTC
)

TEST_12_SKIP_FIRST_30 = TestConfig(
    name="Test 12: Skip First 30 Min",
    description="Skip 9:30-10:00 ET, trade 10:00-16:00 ET",
    skip_first_minutes=30,
)

TEST_13_AFTERNOON_ONLY = TestConfig(
    name="Test 13: Afternoon Only",
    description="Trade 14:00-16:00 ET only (19:00-21:00 UTC)",
    start_time=time(19, 0),   # 14:00 ET = 19:00 UTC
    end_time=time(21, 0),     # 16:00 ET = 21:00 UTC
)

TEST_9_STACKED_SIGNALS = TestConfig(
    name="Test 9: Stacked Signals",
    description="2+ patterns in same bar = double position size",
    stacked_signals_sizing=True,
)

TEST_10_PATTERN_SEQUENCE = TestConfig(
    name="Test 10: Pattern Sequence",
    description="ABSORPTION followed by EXHAUSTION = double size",
    pattern_sequence_sizing=True,
)


def run_test_14_monday_after_friday():
    """
    Test 14: Monday after big Friday.

    Hypothesis: After a big winning Friday (>$1000 profit), Monday might mean-revert.
    After a big losing Friday (<-$300), Monday might continue or bounce.

    Tests whether to adjust Monday trading based on Friday's performance.
    """
    from datetime import datetime as dt

    print("\n" + "=" * 70)
    print("TEST 14: Monday After Big Friday")
    print("Analyze if Friday P&L predicts Monday behavior")
    print("=" * 70)

    # Run baseline to get daily results
    tester = AdvancedBacktester(BASELINE)
    sessions = tester.get_cached_sessions()

    print(f"Running {len(sessions)} days for baseline...")

    results_by_date = {}
    for session in sessions:
        result = tester.run_single_day(session)
        if result:
            results_by_date[session['date']] = result

    # Find Friday→Monday pairs
    friday_monday_pairs = []
    dates = sorted(results_by_date.keys())

    for i, date in enumerate(dates):
        dt_obj = dt.strptime(date, '%Y-%m-%d')
        if dt_obj.weekday() == 4:  # Friday
            # Find next Monday (3 days later)
            monday_date = (dt_obj + timedelta(days=3)).strftime('%Y-%m-%d')
            if monday_date in results_by_date:
                friday_monday_pairs.append({
                    'friday_date': date,
                    'friday_pnl': results_by_date[date]['pnl'],
                    'monday_date': monday_date,
                    'monday_pnl': results_by_date[monday_date]['pnl'],
                })

    print(f"\nFound {len(friday_monday_pairs)} Friday→Monday pairs")

    # Analyze patterns
    big_win_fridays = [p for p in friday_monday_pairs if p['friday_pnl'] >= 1000]
    big_loss_fridays = [p for p in friday_monday_pairs if p['friday_pnl'] <= -300]
    normal_fridays = [p for p in friday_monday_pairs if -300 < p['friday_pnl'] < 1000]

    print(f"\n--- Big Winning Fridays (>=$1000) ---")
    print(f"Count: {len(big_win_fridays)}")
    if big_win_fridays:
        avg_monday = sum(p['monday_pnl'] for p in big_win_fridays) / len(big_win_fridays)
        win_mondays = sum(1 for p in big_win_fridays if p['monday_pnl'] > 0)
        print(f"Avg Monday P&L: ${avg_monday:,.0f}")
        print(f"Monday Win Rate: {100*win_mondays/len(big_win_fridays):.0f}%")
        for p in big_win_fridays:
            arrow = "↑" if p['monday_pnl'] > 0 else "↓"
            print(f"  {p['friday_date']} (${p['friday_pnl']:+,.0f}) → {p['monday_date']} (${p['monday_pnl']:+,.0f}) {arrow}")

    print(f"\n--- Big Losing Fridays (<=-$300) ---")
    print(f"Count: {len(big_loss_fridays)}")
    if big_loss_fridays:
        avg_monday = sum(p['monday_pnl'] for p in big_loss_fridays) / len(big_loss_fridays)
        win_mondays = sum(1 for p in big_loss_fridays if p['monday_pnl'] > 0)
        print(f"Avg Monday P&L: ${avg_monday:,.0f}")
        print(f"Monday Win Rate: {100*win_mondays/len(big_loss_fridays):.0f}%")
        for p in big_loss_fridays:
            arrow = "↑" if p['monday_pnl'] > 0 else "↓"
            print(f"  {p['friday_date']} (${p['friday_pnl']:+,.0f}) → {p['monday_date']} (${p['monday_pnl']:+,.0f}) {arrow}")

    print(f"\n--- Normal Fridays (-$300 to $1000) ---")
    print(f"Count: {len(normal_fridays)}")
    if normal_fridays:
        avg_monday = sum(p['monday_pnl'] for p in normal_fridays) / len(normal_fridays)
        win_mondays = sum(1 for p in normal_fridays if p['monday_pnl'] > 0)
        print(f"Avg Monday P&L: ${avg_monday:,.0f}")
        print(f"Monday Win Rate: {100*win_mondays/len(normal_fridays):.0f}%")

    # Summary
    print("\n" + "=" * 70)
    print("FINDINGS")
    print("=" * 70)

    all_mondays_pnl = sum(p['monday_pnl'] for p in friday_monday_pairs)
    big_win_mondays_pnl = sum(p['monday_pnl'] for p in big_win_fridays) if big_win_fridays else 0
    big_loss_mondays_pnl = sum(p['monday_pnl'] for p in big_loss_fridays) if big_loss_fridays else 0

    print(f"Total Monday P&L: ${all_mondays_pnl:,.0f}")
    print(f"Mondays after big win Friday: ${big_win_mondays_pnl:,.0f}")
    print(f"Mondays after big loss Friday: ${big_loss_mondays_pnl:,.0f}")

    return {
        'pairs': len(friday_monday_pairs),
        'big_win_fridays': len(big_win_fridays),
        'big_loss_fridays': len(big_loss_fridays),
        'monday_after_big_win_avg': sum(p['monday_pnl'] for p in big_win_fridays) / len(big_win_fridays) if big_win_fridays else 0,
        'monday_after_big_loss_avg': sum(p['monday_pnl'] for p in big_loss_fridays) / len(big_loss_fridays) if big_loss_fridays else 0,
    }


def run_test_15_rollover_weeks():
    """
    Test 15: Contract rollover weeks.

    ES futures roll on the third Friday of March, June, September, December.
    In our data (July-November 2025), September 2025 rollover applies.

    Third Friday of September 2025 = September 19, 2025
    Rollover week = September 15-19, 2025

    Tests whether we should trade differently during rollover week.
    """
    from datetime import datetime as dt

    print("\n" + "=" * 70)
    print("TEST 15: Contract Rollover Weeks")
    print("Analyze performance during ES futures rollover week")
    print("=" * 70)

    # September 2025 rollover: week of Sep 15-19
    # The rollover actually happens Thursday before 3rd Friday
    rollover_start = dt(2025, 9, 15)
    rollover_end = dt(2025, 9, 19)

    print(f"Rollover week: {rollover_start.date()} to {rollover_end.date()}")

    # Run baseline
    tester = AdvancedBacktester(BASELINE)
    sessions = tester.get_cached_sessions()

    print(f"Running {len(sessions)} days for baseline...")

    rollover_results = []
    normal_results = []

    for session in sessions:
        result = tester.run_single_day(session)
        if result:
            date = dt.strptime(session['date'], '%Y-%m-%d')
            if rollover_start <= date <= rollover_end:
                rollover_results.append(result)
            else:
                normal_results.append(result)

    print(f"\n--- Rollover Week ({len(rollover_results)} days) ---")
    if rollover_results:
        total_pnl = sum(r['pnl'] for r in rollover_results)
        avg_pnl = total_pnl / len(rollover_results)
        win_days = sum(1 for r in rollover_results if r['pnl'] > 0)
        print(f"Total P&L: ${total_pnl:,.0f}")
        print(f"Avg Daily P&L: ${avg_pnl:,.0f}")
        print(f"Win Days: {win_days}/{len(rollover_results)} ({100*win_days/len(rollover_results):.0f}%)")
        for r in rollover_results:
            print(f"  {r['date']}: ${r['pnl']:+,.0f}")
    else:
        print("No rollover week data found in cached sessions")

    print(f"\n--- Normal Weeks ({len(normal_results)} days) ---")
    if normal_results:
        total_pnl = sum(r['pnl'] for r in normal_results)
        avg_pnl = total_pnl / len(normal_results)
        win_days = sum(1 for r in normal_results if r['pnl'] > 0)
        print(f"Total P&L: ${total_pnl:,.0f}")
        print(f"Avg Daily P&L: ${avg_pnl:,.0f}")
        print(f"Win Days: {win_days}/{len(normal_results)} ({100*win_days/len(normal_results):.0f}%)")

    # Compare
    print("\n" + "=" * 70)
    print("FINDINGS")
    print("=" * 70)

    if rollover_results and normal_results:
        rollover_avg = sum(r['pnl'] for r in rollover_results) / len(rollover_results)
        normal_avg = sum(r['pnl'] for r in normal_results) / len(normal_results)
        diff = rollover_avg - normal_avg
        print(f"Rollover week avg: ${rollover_avg:,.0f}/day")
        print(f"Normal weeks avg: ${normal_avg:,.0f}/day")
        print(f"Difference: ${diff:+,.0f}/day")

        if abs(diff) < 200:
            print("→ Rollover week performance is similar to normal weeks")
        elif diff > 0:
            print("→ Rollover week OUTPERFORMS normal weeks")
        else:
            print("→ Rollover week UNDERPERFORMS normal weeks")

    return {
        'rollover_days': len(rollover_results),
        'rollover_pnl': sum(r['pnl'] for r in rollover_results) if rollover_results else 0,
        'normal_days': len(normal_results),
        'normal_pnl': sum(r['pnl'] for r in normal_results) if normal_results else 0,
    }


# Test 16: Combined Strategy Stack
TEST_16_COMBINED = TestConfig(
    name="Test 16: Combined Strategy Stack",
    description="Stacked + Regime + Streak sizing combined (additive)",
    stacked_signals_sizing=True,
    regime_sizing=True,
    streak_sizing=True,
    combine_sizing=True,
    max_position_size=4,
    daily_loss_limit=-500.0,
    conservative_fills=True,
)


def run_test_16_combined():
    """
    Test 16: Combined Strategy Stack.

    Combine all winning strategies to see if they compound or overlap:
    - Stacked signals: +1 contract when 2+ patterns fire
    - Regime sizing: +1 contract in TRENDING
    - Streak sizing: +1 after 3 wins, -1 after 2 losses
    - Max cap: 4 contracts
    """
    print("\n" + "=" * 70)
    print("TEST 16: Combined Strategy Stack")
    print("Stacked + Regime + Streak sizing (additive, max 4 contracts)")
    print("=" * 70)

    tester = AdvancedBacktester(TEST_16_COMBINED)
    sessions = tester.get_cached_sessions()

    print(f"Running {len(sessions)} days...")

    results = []
    max_position_used = 0
    max_daily_loss = 0
    running_balance = 10000
    peak_balance = 10000
    max_drawdown = 0

    for session in sessions:
        result = tester.run_single_day(session)
        if result:
            results.append(result)

            # Track max position used
            for trade in result.get('trade_details', []):
                max_position_used = max(max_position_used, trade.get('size', 1))

            # Track max daily loss
            if result['pnl'] < 0:
                max_daily_loss = min(max_daily_loss, result['pnl'])

            # Track drawdown
            running_balance += result['pnl']
            peak_balance = max(peak_balance, running_balance)
            current_drawdown = peak_balance - running_balance
            max_drawdown = max(max_drawdown, current_drawdown)

    # Calculate summary
    total_pnl = sum(r['pnl'] for r in results)
    winning_days = sum(1 for r in results if r['pnl'] > 0)
    losing_days = sum(1 for r in results if r['pnl'] < 0)
    total_trades = sum(r['trades'] for r in results)
    total_wins = sum(r['wins'] for r in results)
    total_losses = sum(r['losses'] for r in results)

    print(f"\n{'='*70}")
    print("RESULTS: Test 16 - Combined Strategy Stack")
    print(f"{'='*70}")
    print(f"Days tested:       {len(results)}")
    print(f"Total P&L:         ${total_pnl:,.0f}")
    print(f"Avg Daily P&L:     ${total_pnl/len(results):,.0f}")
    print(f"Winning days:      {winning_days} ({100*winning_days/len(results):.1f}%)")
    print(f"Losing days:       {losing_days} ({100*losing_days/len(results):.1f}%)")
    print(f"Total trades:      {total_trades}")
    print(f"Trade win rate:    {100*total_wins/(total_wins+total_losses):.1f}%")
    print(f"\n--- Risk Metrics ---")
    print(f"Max position used: {max_position_used} contracts")
    print(f"Max daily loss:    ${max_daily_loss:,.0f}")
    print(f"Max drawdown:      ${max_drawdown:,.0f}")
    print(f"Final balance:     ${running_balance:,.0f} (started $10,000)")

    return {
        'total_pnl': total_pnl,
        'winning_days': winning_days,
        'losing_days': losing_days,
        'max_position_used': max_position_used,
        'max_daily_loss': max_daily_loss,
        'max_drawdown': max_drawdown,
        'results': results,
    }


def run_test_17_scaled_limits():
    """
    Test 17: Scaled Loss Limits.

    Scale daily loss limit with position size:
    - Base limit: $300 per contract allowed
    - If max 3 contracts that day: $900 limit
    """
    print("\n" + "=" * 70)
    print("TEST 17: Scaled Loss Limits")
    print("Daily loss limit = $300 × max contracts that day")
    print("=" * 70)

    # We need to run this manually to track per-day scaling
    base_config = TestConfig(
        name="Test 17: Scaled Loss Limits",
        description="Loss limit scales with position size",
        stacked_signals_sizing=True,
        regime_sizing=True,
        streak_sizing=True,
        combine_sizing=True,
        max_position_size=4,
        conservative_fills=True,
    )

    tester = AdvancedBacktester(base_config)
    sessions = tester.get_cached_sessions()

    print(f"Running {len(sessions)} days...")

    # Run Test 16 (fixed $500 limit) for comparison
    fixed_results = []
    scaled_results = []

    # For fixed limit
    fixed_config = TEST_16_COMBINED
    fixed_tester = AdvancedBacktester(fixed_config)

    # For scaled limit, we'll manually adjust per-day
    days_hit_fixed = 0
    days_hit_scaled = 0

    for session in sessions:
        # Run with fixed $500 limit
        fixed_result = fixed_tester.run_single_day(session)
        if fixed_result:
            fixed_results.append(fixed_result)
            if fixed_result['pnl'] <= -450:  # Within $50 of limit
                days_hit_fixed += 1

        # For scaled test, determine max position for day and scale limit
        # This is approximate - we use the max position from fixed run
        if fixed_result:
            max_pos = 1
            for trade in fixed_result.get('trade_details', []):
                max_pos = max(max_pos, trade.get('size', 1))

            scaled_limit = -300 * max_pos

            # Create config with scaled limit
            scaled_config = TestConfig(
                name="Scaled",
                description="",
                stacked_signals_sizing=True,
                regime_sizing=True,
                streak_sizing=True,
                combine_sizing=True,
                max_position_size=4,
                daily_loss_limit=scaled_limit,
                conservative_fills=True,
            )
            scaled_tester = AdvancedBacktester(scaled_config)
            scaled_result = scaled_tester.run_single_day(session)
            if scaled_result:
                scaled_results.append(scaled_result)
                if scaled_result['pnl'] <= scaled_limit + 50:
                    days_hit_scaled += 1

    # Compare results
    fixed_pnl = sum(r['pnl'] for r in fixed_results)
    scaled_pnl = sum(r['pnl'] for r in scaled_results)
    fixed_wins = sum(1 for r in fixed_results if r['pnl'] > 0)
    scaled_wins = sum(1 for r in scaled_results if r['pnl'] > 0)

    print(f"\n{'='*70}")
    print("COMPARISON: Fixed vs Scaled Loss Limits")
    print(f"{'='*70}")
    print(f"{'Metric':<25} {'Fixed $500':<15} {'Scaled $300×N':<15}")
    print("-" * 55)
    print(f"{'Total P&L':<25} ${fixed_pnl:>13,.0f} ${scaled_pnl:>13,.0f}")
    print(f"{'Winning Days':<25} {fixed_wins:>13} {scaled_wins:>13}")
    print(f"{'Days Hit Limit':<25} {days_hit_fixed:>13} {days_hit_scaled:>13}")
    print(f"{'Difference':<25} {'Baseline':>13} ${scaled_pnl - fixed_pnl:>+12,.0f}")

    return {
        'fixed_pnl': fixed_pnl,
        'scaled_pnl': scaled_pnl,
        'days_hit_fixed': days_hit_fixed,
        'days_hit_scaled': days_hit_scaled,
    }


def run_test_18_capital_simulation():
    """
    Test 18: Capital Simulation.

    Starting with $2,500, simulate account growth with tier-based sizing:
    - $2,500-$5,000: 1 MES ($50 daily limit)
    - $5,000-$10,000: 1 ES ($500 daily limit)
    - $10,000-$20,000: 2 ES ($1,000 daily limit)
    - $20,000-$40,000: 3 ES ($1,500 daily limit)
    - $40,000+: 4 ES ($2,000 daily limit)
    """
    print("\n" + "=" * 70)
    print("TEST 18: Capital Simulation")
    print("Starting balance: $2,500 with tier-based position sizing")
    print("=" * 70)

    # MES = 1/10 of ES, so we'll scale P&L by 0.1 for MES trades
    tiers = [
        {'min': 0, 'max': 5000, 'contracts': 1, 'is_mes': True, 'limit': 50},      # Under $5K = MES
        {'min': 5000, 'max': 10000, 'contracts': 1, 'is_mes': False, 'limit': 500},
        {'min': 10000, 'max': 20000, 'contracts': 2, 'is_mes': False, 'limit': 1000},
        {'min': 20000, 'max': 40000, 'contracts': 3, 'is_mes': False, 'limit': 1500},
        {'min': 40000, 'max': float('inf'), 'contracts': 4, 'is_mes': False, 'limit': 2000},
    ]

    def get_tier(balance):
        for tier in tiers:
            if tier['min'] <= balance < tier['max']:
                return tier
        return tiers[-1]  # Max tier for very high balances

    # Get baseline results for reference (1 ES contract)
    baseline_tester = AdvancedBacktester(TEST_16_COMBINED)
    sessions = baseline_tester.get_cached_sessions()

    print(f"Simulating {len(sessions)} days...")

    balance = 2500
    peak_balance = balance
    max_drawdown = 0
    max_drawdown_pct = 0
    tier_transitions = []
    current_tier_idx = 0

    daily_log = []

    for session in sessions:
        tier = get_tier(balance)
        tier_idx = tiers.index(tier)

        # Track tier transitions
        if tier_idx != current_tier_idx:
            tier_transitions.append({
                'date': session['date'],
                'from_tier': current_tier_idx,
                'to_tier': tier_idx,
                'balance': balance,
            })
            current_tier_idx = tier_idx

        # Run backtest with appropriate sizing
        config = TestConfig(
            name="Tier",
            description="",
            stacked_signals_sizing=True,
            regime_sizing=True,
            streak_sizing=True,
            combine_sizing=True,
            max_position_size=tier['contracts'],
            daily_loss_limit=-tier['limit'],
            conservative_fills=True,
        )
        tester = AdvancedBacktester(config)
        result = tester.run_single_day(session)

        if result:
            # Scale P&L for MES
            pnl = result['pnl']
            if tier['is_mes']:
                pnl = pnl * 0.1  # MES is 1/10 of ES

            balance += pnl
            peak_balance = max(peak_balance, balance)
            drawdown = peak_balance - balance
            max_drawdown = max(max_drawdown, drawdown)
            if peak_balance > 0:
                dd_pct = drawdown / peak_balance * 100
                max_drawdown_pct = max(max_drawdown_pct, dd_pct)

            daily_log.append({
                'date': session['date'],
                'pnl': pnl,
                'balance': balance,
                'tier': tier_idx,
                'contracts': tier['contracts'],
                'is_mes': tier['is_mes'],
            })

    # Find days to reach each tier
    tier_reached = {}
    for i, log in enumerate(daily_log):
        if log['tier'] not in tier_reached:
            tier_reached[log['tier']] = {'day': i + 1, 'date': log['date']}

    print(f"\n{'='*70}")
    print("RESULTS: Capital Simulation")
    print(f"{'='*70}")
    print(f"Starting balance:  $2,500")
    print(f"Ending balance:    ${balance:,.0f}")
    print(f"Total gain:        ${balance - 2500:,.0f} ({(balance/2500 - 1)*100:.0f}%)")
    print(f"Peak balance:      ${peak_balance:,.0f}")
    print(f"Max drawdown:      ${max_drawdown:,.0f} ({max_drawdown_pct:.1f}%)")

    print(f"\n--- Tier Progression ---")
    tier_names = ['MES (1)', 'ES (1)', 'ES (2)', 'ES (3)', 'ES (4)']
    for tier_idx, info in sorted(tier_reached.items()):
        print(f"  {tier_names[tier_idx]:<10}: Day {info['day']:3d} ({info['date']})")

    print(f"\n--- Tier Transitions ---")
    for t in tier_transitions:
        direction = "↑" if t['to_tier'] > t['from_tier'] else "↓"
        print(f"  {t['date']}: {tier_names[t['from_tier']]} → {tier_names[t['to_tier']]} {direction} (${t['balance']:,.0f})")

    # Check if we ever dropped a tier
    dropped_tiers = [t for t in tier_transitions if t['to_tier'] < t['from_tier']]
    if dropped_tiers:
        print(f"\n⚠️  Account dropped tier {len(dropped_tiers)} time(s)")
    else:
        print(f"\n✓ Account never dropped a tier")

    return {
        'starting_balance': 2500,
        'ending_balance': balance,
        'max_drawdown': max_drawdown,
        'max_drawdown_pct': max_drawdown_pct,
        'tier_transitions': tier_transitions,
        'tier_reached': tier_reached,
        'daily_log': daily_log,
    }


def run_test_19_worst_case():
    """
    Test 19: Worst Case Stress Test.

    Find the worst 5-day stretch and simulate starting on day 1 of that stretch
    with $2,500 account using tier-based sizing.
    """
    print("\n" + "=" * 70)
    print("TEST 19: Worst Case Stress Test")
    print("What if we started on the worst possible day?")
    print("=" * 70)

    # First, run baseline to find worst 5-day stretch
    baseline_tester = AdvancedBacktester(BASELINE)
    sessions = baseline_tester.get_cached_sessions()

    print(f"Analyzing {len(sessions)} days to find worst 5-day stretch...")

    # Get all daily P&Ls
    daily_pnls = []
    for session in sessions:
        result = baseline_tester.run_single_day(session)
        if result:
            daily_pnls.append({'date': session['date'], 'pnl': result['pnl'], 'session': session})

    # Find worst 5-day window
    worst_window_start = 0
    worst_window_pnl = float('inf')

    for i in range(len(daily_pnls) - 4):
        window_pnl = sum(d['pnl'] for d in daily_pnls[i:i+5])
        if window_pnl < worst_window_pnl:
            worst_window_pnl = window_pnl
            worst_window_start = i

    worst_5_days = daily_pnls[worst_window_start:worst_window_start+5]

    print(f"\n--- Worst 5-Day Stretch ---")
    print(f"Dates: {worst_5_days[0]['date']} to {worst_5_days[-1]['date']}")
    print(f"Combined P&L: ${worst_window_pnl:,.0f}")
    for d in worst_5_days:
        print(f"  {d['date']}: ${d['pnl']:+,.0f}")

    # Now simulate starting on this day with $2,500
    print(f"\n--- Simulation: Starting ${2500:,} on {worst_5_days[0]['date']} ---")

    # Reorder sessions to start from worst day
    reordered_sessions = [d['session'] for d in daily_pnls[worst_window_start:]]
    reordered_sessions.extend([d['session'] for d in daily_pnls[:worst_window_start]])

    # Use tier-based sizing from Test 18
    tiers = [
        {'min': 0, 'max': 5000, 'contracts': 1, 'is_mes': True, 'limit': 50},      # Under $5K = MES
        {'min': 5000, 'max': 10000, 'contracts': 1, 'is_mes': False, 'limit': 500},
        {'min': 10000, 'max': 20000, 'contracts': 2, 'is_mes': False, 'limit': 1000},
        {'min': 20000, 'max': float('inf'), 'contracts': 4, 'is_mes': False, 'limit': 2000},
    ]

    def get_tier(balance):
        if balance <= 0:
            return {'contracts': 0, 'is_mes': True, 'limit': 0}  # Wiped out
        for tier in tiers:
            if tier['min'] <= balance < tier['max']:
                return tier
        return tiers[-1]

    balance = 2500
    lowest_balance = balance
    lowest_date = worst_5_days[0]['date']
    survived = True
    recovery_day = None

    for i, session in enumerate(reordered_sessions[:30]):  # First 30 days
        tier = get_tier(balance)

        if tier['contracts'] == 0:
            print(f"  💀 ACCOUNT WIPED OUT on day {i+1}")
            survived = False
            break

        config = TestConfig(
            name="Stress",
            description="",
            stacked_signals_sizing=True,
            regime_sizing=True,
            streak_sizing=True,
            combine_sizing=True,
            max_position_size=tier['contracts'],
            daily_loss_limit=-tier['limit'],
            conservative_fills=True,
        )
        tester = AdvancedBacktester(config)
        result = tester.run_single_day(session)

        if result:
            pnl = result['pnl']
            if tier['is_mes']:
                pnl = pnl * 0.1

            balance += pnl

            if balance < lowest_balance:
                lowest_balance = balance
                lowest_date = session['date']

            status = "WIN" if pnl > 0 else "LOSS"
            tier_name = "MES" if tier['is_mes'] else f"ES×{tier['contracts']}"
            print(f"  Day {i+1:2d} ({session['date']}): ${pnl:+6,.0f} [{status}] → ${balance:,.0f} ({tier_name})")

            # Check if recovered past starting balance
            if recovery_day is None and balance >= 2500 and i > 0:
                recovery_day = i + 1

    print(f"\n{'='*70}")
    print("FINDINGS")
    print(f"{'='*70}")
    print(f"Survived worst stretch: {'YES ✓' if survived else 'NO ✗'}")
    print(f"Lowest balance:         ${lowest_balance:,.0f} on {lowest_date}")
    print(f"Max drawdown:           ${2500 - lowest_balance:,.0f} ({(1 - lowest_balance/2500)*100:.1f}%)")
    if recovery_day:
        print(f"Days to recover:        {recovery_day}")
    else:
        print(f"Days to recover:        Did not recover in 30 days" if survived else "N/A")

    return {
        'worst_5_days': worst_5_days,
        'survived': survived,
        'lowest_balance': lowest_balance,
        'recovery_day': recovery_day,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Advanced backtesting framework")
    parser.add_argument("--test", type=int, help="Run specific test number (4-13)")
    parser.add_argument("--baseline", action="store_true", help="Run baseline test")
    parser.add_argument("--all", action="store_true", help="Run all tests")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-day output")

    args = parser.parse_args()

    tests = {
        4: TEST_4_VOLATILITY_SIZING,
        5: TEST_5_REGIME_SIZING,
        6: TEST_6_STREAK_SIZING,
        7: TEST_7_FIRST_HOUR_STOP,
        "8a": TEST_8A_MAX_5_TRADES,
        "8b": TEST_8B_MAX_10_TRADES,
        9: TEST_9_STACKED_SIGNALS,
        10: TEST_10_PATTERN_SEQUENCE,
        11: TEST_11_FIRST_HOUR_ONLY,
        12: TEST_12_SKIP_FIRST_30,
        13: TEST_13_AFTERNOON_ONLY,
    }

    if args.baseline:
        run_test(BASELINE, quiet=args.quiet)
    elif args.test:
        if args.test in tests:
            run_test(tests[args.test], quiet=args.quiet)
        elif args.test == 8:
            run_test(TEST_8A_MAX_5_TRADES, quiet=args.quiet)
            run_test(TEST_8B_MAX_10_TRADES, quiet=args.quiet)
        elif args.test == 14:
            run_test_14_monday_after_friday()
        elif args.test == 15:
            run_test_15_rollover_weeks()
        elif args.test == 16:
            run_test_16_combined()
        elif args.test == 17:
            run_test_17_scaled_limits()
        elif args.test == 18:
            run_test_18_capital_simulation()
        elif args.test == 19:
            run_test_19_worst_case()
        else:
            print(f"Unknown test number: {args.test}")
    elif args.all:
        results = []
        results.append(run_test(BASELINE, quiet=True))
        for key, config in tests.items():
            results.append(run_test(config, quiet=True))

        # Print comparison
        print(f"\n{'='*70}")
        print("COMPARISON SUMMARY")
        print(f"{'='*70}")
        print(f"{'Test':<35} {'P&L':>12} {'Win Days':>10} {'Trades':>8}")
        print("-" * 70)
        for r in results:
            if r:
                print(f"{r['config']:<35} ${r['total_pnl']:>10,.0f} {r['winning_days_pct']:>9.1f}% {r['total_trades']:>8}")
    else:
        parser.print_help()
