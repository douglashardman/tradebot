#!/usr/bin/env python3
"""
Comprehensive stress tests for the order flow trading system.

Tests include:
1. Slippage stress test (1-2 ticks per trade)
2. Time-of-day analysis (hourly breakdown)
3. Day-of-week analysis
4. Losing streak analysis
5. Drawdown duration analysis
6. Monte Carlo simulation (1000 randomizations)
"""

import argparse
import random
from datetime import datetime
from typing import List, Dict, Tuple
from collections import defaultdict

from src.data.backtest_db import get_connection


def get_all_trades() -> List[Dict]:
    """Get all completed trades from database."""
    conn = get_connection()
    rows = conn.execute("""
        SELECT
            t.*, b.date as backtest_date
        FROM trades t
        JOIN backtests b ON t.backtest_id = b.id
        WHERE t.exit_price IS NOT NULL
        ORDER BY b.date, t.entry_time
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def slippage_test(trades: List[Dict], slippage_ticks: float = 1.5) -> Dict:
    """
    Test impact of slippage on performance.

    Adds slippage to both entry and exit (worst case).
    For ES: 1 tick = $12.50, so 1.5 ticks = $18.75 per side = $37.50 round trip
    """
    print("\n" + "=" * 70)
    print(f"SLIPPAGE STRESS TEST ({slippage_ticks} ticks each way)")
    print("=" * 70)

    # ES tick value
    tick_value = 12.50
    slippage_per_trade = slippage_ticks * tick_value * 2  # Entry + exit

    original_pnl = sum(t['pnl'] for t in trades)
    total_slippage = len(trades) * slippage_per_trade
    adjusted_pnl = original_pnl - total_slippage

    # Recalculate metrics
    adjusted_trades = []
    for t in trades:
        adj_pnl = t['pnl'] - slippage_per_trade
        adjusted_trades.append({**t, 'adj_pnl': adj_pnl})

    wins = sum(1 for t in adjusted_trades if t['adj_pnl'] > 0)
    losses = sum(1 for t in adjusted_trades if t['adj_pnl'] < 0)
    gross_profit = sum(t['adj_pnl'] for t in adjusted_trades if t['adj_pnl'] > 0)
    gross_loss = abs(sum(t['adj_pnl'] for t in adjusted_trades if t['adj_pnl'] < 0))

    print(f"\nOriginal Performance:")
    print(f"  Net P&L: ${original_pnl:,.0f}")
    print(f"  Trades: {len(trades)}")

    print(f"\nSlippage Impact:")
    print(f"  Slippage per trade: ${slippage_per_trade:.2f}")
    print(f"  Total slippage: ${total_slippage:,.0f}")

    print(f"\nAdjusted Performance:")
    print(f"  Net P&L: ${adjusted_pnl:,.0f}")
    print(f"  Win Rate: {wins/len(trades):.1%}")
    print(f"  Profit Factor: {gross_profit/gross_loss:.2f}" if gross_loss > 0 else "  Profit Factor: inf")
    print(f"  Avg Daily (98 days): ${adjusted_pnl/98:,.0f}")

    survived = adjusted_pnl > 0
    print(f"\n{'PASSED' if survived else 'FAILED'}: System {'survives' if survived else 'fails'} with {slippage_ticks}-tick slippage")

    return {
        "original_pnl": original_pnl,
        "total_slippage": total_slippage,
        "adjusted_pnl": adjusted_pnl,
        "adjusted_win_rate": wins/len(trades),
        "adjusted_pf": gross_profit/gross_loss if gross_loss > 0 else float('inf'),
        "survived": survived
    }


def time_of_day_analysis(trades: List[Dict]) -> Dict:
    """Break down performance by hour of day."""
    print("\n" + "=" * 70)
    print("TIME-OF-DAY ANALYSIS")
    print("=" * 70)

    hourly = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0})

    for t in trades:
        try:
            # Parse entry time
            entry_time = t['entry_time']
            if isinstance(entry_time, str):
                if 'T' in entry_time:
                    hour = int(entry_time.split('T')[1].split(':')[0])
                else:
                    hour = int(entry_time.split(':')[0])
            else:
                hour = entry_time.hour

            hourly[hour]["trades"] += 1
            if t['pnl'] > 0:
                hourly[hour]["wins"] += 1
            hourly[hour]["pnl"] += t['pnl']
        except:
            continue

    print(f"\n{'Hour':<8} {'Trades':>8} {'Win%':>8} {'Net P&L':>12} {'Avg P&L':>10}")
    print("-" * 50)

    results = {}
    for hour in sorted(hourly.keys()):
        data = hourly[hour]
        win_rate = data["wins"] / data["trades"] if data["trades"] > 0 else 0
        avg_pnl = data["pnl"] / data["trades"] if data["trades"] > 0 else 0

        # Format hour range
        hour_range = f"{hour:02d}:00-{hour+1:02d}:00"
        print(f"{hour_range:<8} {data['trades']:>8} {win_rate:>7.0%} ${data['pnl']:>10,.0f} ${avg_pnl:>8,.0f}")

        results[hour] = {
            "trades": data["trades"],
            "win_rate": win_rate,
            "pnl": data["pnl"],
            "avg_pnl": avg_pnl
        }

    # Find best and worst hours
    best_hour = max(hourly.keys(), key=lambda h: hourly[h]["pnl"])
    worst_hour = min(hourly.keys(), key=lambda h: hourly[h]["pnl"])

    print(f"\nBest hour: {best_hour:02d}:00 (${hourly[best_hour]['pnl']:,.0f})")
    print(f"Worst hour: {worst_hour:02d}:00 (${hourly[worst_hour]['pnl']:,.0f})")

    # Check for consistently losing hours
    losing_hours = [h for h in hourly.keys() if hourly[h]["pnl"] < 0]
    if losing_hours:
        print(f"\nLosing hours to consider skipping: {[f'{h:02d}:00' for h in losing_hours]}")

    return results


def day_of_week_analysis(trades: List[Dict]) -> Dict:
    """Break down performance by day of week."""
    print("\n" + "=" * 70)
    print("DAY-OF-WEEK ANALYSIS")
    print("=" * 70)

    day_names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    daily = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0, "days": set()})

    for t in trades:
        try:
            date_str = t['backtest_date']
            date = datetime.strptime(date_str, "%Y-%m-%d")
            dow = date.weekday()

            daily[dow]["trades"] += 1
            if t['pnl'] > 0:
                daily[dow]["wins"] += 1
            daily[dow]["pnl"] += t['pnl']
            daily[dow]["days"].add(date_str)
        except:
            continue

    print(f"\n{'Day':<12} {'Days':>6} {'Trades':>8} {'Win%':>8} {'Net P&L':>12} {'Avg/Day':>10}")
    print("-" * 60)

    results = {}
    for dow in range(5):  # Mon-Fri only
        data = daily[dow]
        num_days = len(data["days"])
        win_rate = data["wins"] / data["trades"] if data["trades"] > 0 else 0
        avg_day = data["pnl"] / num_days if num_days > 0 else 0

        print(f"{day_names[dow]:<12} {num_days:>6} {data['trades']:>8} {win_rate:>7.0%} ${data['pnl']:>10,.0f} ${avg_day:>8,.0f}")

        results[day_names[dow]] = {
            "days": num_days,
            "trades": data["trades"],
            "win_rate": win_rate,
            "pnl": data["pnl"],
            "avg_day": avg_day
        }

    # Find best and worst days
    best_dow = max(range(5), key=lambda d: daily[d]["pnl"])
    worst_dow = min(range(5), key=lambda d: daily[d]["pnl"])

    print(f"\nBest day: {day_names[best_dow]} (${daily[best_dow]['pnl']:,.0f})")
    print(f"Worst day: {day_names[worst_dow]} (${daily[worst_dow]['pnl']:,.0f})")

    return results


def losing_streak_analysis(trades: List[Dict]) -> Dict:
    """Analyze consecutive losing trades."""
    print("\n" + "=" * 70)
    print("LOSING STREAK ANALYSIS")
    print("=" * 70)

    # Find all streaks
    current_streak = 0
    max_streak = 0
    max_streak_loss = 0
    current_loss = 0
    streaks = []

    for t in trades:
        if t['pnl'] < 0:
            current_streak += 1
            current_loss += t['pnl']
            if current_streak > max_streak:
                max_streak = current_streak
                max_streak_loss = current_loss
        else:
            if current_streak > 0:
                streaks.append((current_streak, current_loss))
            current_streak = 0
            current_loss = 0

    # Final streak
    if current_streak > 0:
        streaks.append((current_streak, current_loss))

    print(f"\nLongest losing streak: {max_streak} trades")
    print(f"Max streak loss: ${max_streak_loss:,.0f}")

    # Distribution of streaks
    streak_dist = defaultdict(int)
    for length, _ in streaks:
        streak_dist[length] += 1

    print(f"\nStreak distribution:")
    for length in sorted(streak_dist.keys()):
        print(f"  {length} losses in a row: {streak_dist[length]} times")

    # Calculate probability of N losses in a row given win rate
    total_trades = len(trades)
    wins = sum(1 for t in trades if t['pnl'] > 0)
    loss_rate = 1 - (wins / total_trades)

    print(f"\nWith {loss_rate:.1%} loss rate:")
    for n in [3, 5, 7, 10]:
        prob = loss_rate ** n
        expected = total_trades * prob
        print(f"  Probability of {n} losses in a row: {prob:.2%} (expected {expected:.1f} occurrences)")

    return {
        "max_streak": max_streak,
        "max_streak_loss": max_streak_loss,
        "streak_distribution": dict(streak_dist),
        "loss_rate": loss_rate
    }


def drawdown_duration_analysis(trades: List[Dict]) -> Dict:
    """Analyze how long drawdowns last."""
    print("\n" + "=" * 70)
    print("DRAWDOWN DURATION ANALYSIS")
    print("=" * 70)

    # Build equity curve with timestamps
    equity = 0
    peak = 0
    drawdown_start = None
    current_dd = 0
    max_dd = 0
    max_dd_duration = 0
    max_dd_start = None
    max_dd_end = None

    drawdowns = []

    for i, t in enumerate(trades):
        equity += t['pnl']

        if equity > peak:
            # New high - end any drawdown
            if drawdown_start is not None:
                duration = i - drawdown_start
                drawdowns.append({
                    "depth": current_dd,
                    "duration_trades": duration,
                    "start_idx": drawdown_start,
                    "end_idx": i
                })
            peak = equity
            drawdown_start = None
            current_dd = 0
        else:
            # In drawdown
            dd = peak - equity
            if drawdown_start is None:
                drawdown_start = i
            current_dd = dd

            if dd > max_dd:
                max_dd = dd
                max_dd_start = drawdown_start

    # Handle final drawdown if still in one
    if drawdown_start is not None:
        duration = len(trades) - drawdown_start
        drawdowns.append({
            "depth": current_dd,
            "duration_trades": duration,
            "start_idx": drawdown_start,
            "end_idx": len(trades)
        })

    print(f"\nMax drawdown: ${max_dd:,.0f}")
    print(f"Total drawdown events: {len(drawdowns)}")

    if drawdowns:
        avg_duration = sum(d["duration_trades"] for d in drawdowns) / len(drawdowns)
        max_duration = max(d["duration_trades"] for d in drawdowns)

        print(f"Average drawdown duration: {avg_duration:.1f} trades")
        print(f"Longest drawdown: {max_duration} trades")

        # Show top 5 drawdowns
        print(f"\nTop 5 drawdowns:")
        sorted_dd = sorted(drawdowns, key=lambda x: x["depth"], reverse=True)[:5]
        for i, dd in enumerate(sorted_dd):
            print(f"  {i+1}. ${dd['depth']:,.0f} over {dd['duration_trades']} trades")

    return {
        "max_drawdown": max_dd,
        "total_drawdowns": len(drawdowns),
        "drawdowns": drawdowns
    }


def monte_carlo_simulation(trades: List[Dict], iterations: int = 1000) -> Dict:
    """
    Randomize trade order to see distribution of outcomes.

    This tests whether the results are robust or dependent on specific sequencing.
    """
    print("\n" + "=" * 70)
    print(f"MONTE CARLO SIMULATION ({iterations} iterations)")
    print("=" * 70)

    pnls = [t['pnl'] for t in trades]
    original_pnl = sum(pnls)

    results = []
    max_drawdowns = []

    for i in range(iterations):
        # Shuffle trades
        shuffled = pnls.copy()
        random.shuffle(shuffled)

        # Calculate equity curve and max drawdown
        equity = 0
        peak = 0
        max_dd = 0

        for pnl in shuffled:
            equity += pnl
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

        results.append(equity)  # Final equity
        max_drawdowns.append(max_dd)

    results.sort()
    max_drawdowns.sort()

    # Calculate percentiles
    def percentile(data, p):
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data)-1)]

    print(f"\nFinal Equity Distribution:")
    print(f"  Worst case (0th): ${results[0]:,.0f}")
    print(f"  5th percentile: ${percentile(results, 5):,.0f}")
    print(f"  25th percentile: ${percentile(results, 25):,.0f}")
    print(f"  Median (50th): ${percentile(results, 50):,.0f}")
    print(f"  75th percentile: ${percentile(results, 75):,.0f}")
    print(f"  95th percentile: ${percentile(results, 95):,.0f}")
    print(f"  Best case (100th): ${results[-1]:,.0f}")
    print(f"  Original: ${original_pnl:,.0f}")

    print(f"\nMax Drawdown Distribution:")
    print(f"  Best case (lowest DD): ${max_drawdowns[0]:,.0f}")
    print(f"  5th percentile: ${percentile(max_drawdowns, 5):,.0f}")
    print(f"  Median: ${percentile(max_drawdowns, 50):,.0f}")
    print(f"  95th percentile: ${percentile(max_drawdowns, 95):,.0f}")
    print(f"  Worst case: ${max_drawdowns[-1]:,.0f}")

    # Risk of ruin calculation (equity going below -$5000)
    ruin_threshold = -5000
    ruin_count = 0
    for _ in range(iterations):
        shuffled = pnls.copy()
        random.shuffle(shuffled)

        equity = 0
        hit_ruin = False
        for pnl in shuffled:
            equity += pnl
            if equity < ruin_threshold:
                hit_ruin = True
                break
        if hit_ruin:
            ruin_count += 1

    ruin_probability = ruin_count / iterations
    print(f"\nRisk of equity below ${ruin_threshold:,}: {ruin_probability:.1%}")

    return {
        "percentiles": {
            "p5": percentile(results, 5),
            "p25": percentile(results, 25),
            "p50": percentile(results, 50),
            "p75": percentile(results, 75),
            "p95": percentile(results, 95)
        },
        "worst_case": results[0],
        "best_case": results[-1],
        "median_dd": percentile(max_drawdowns, 50),
        "worst_dd": max_drawdowns[-1],
        "ruin_probability": ruin_probability
    }


def run_all_tests():
    """Run all stress tests."""
    print("=" * 70)
    print("ORDER FLOW TRADING SYSTEM - STRESS TEST SUITE")
    print("=" * 70)

    trades = get_all_trades()
    print(f"\nLoaded {len(trades)} trades for analysis")

    results = {}

    # 1. Slippage tests
    results["slippage_1tick"] = slippage_test(trades, 1.0)
    results["slippage_2tick"] = slippage_test(trades, 2.0)

    # 2. Time of day
    results["time_of_day"] = time_of_day_analysis(trades)

    # 3. Day of week
    results["day_of_week"] = day_of_week_analysis(trades)

    # 4. Losing streaks
    results["losing_streaks"] = losing_streak_analysis(trades)

    # 5. Drawdown duration
    results["drawdown"] = drawdown_duration_analysis(trades)

    # 6. Monte Carlo
    results["monte_carlo"] = monte_carlo_simulation(trades, 1000)

    # Summary
    print("\n" + "=" * 70)
    print("STRESS TEST SUMMARY")
    print("=" * 70)

    print(f"\n1. SLIPPAGE ROBUSTNESS:")
    print(f"   1-tick slippage: {'PASS' if results['slippage_1tick']['survived'] else 'FAIL'} (${results['slippage_1tick']['adjusted_pnl']:,.0f})")
    print(f"   2-tick slippage: {'PASS' if results['slippage_2tick']['survived'] else 'FAIL'} (${results['slippage_2tick']['adjusted_pnl']:,.0f})")

    print(f"\n2. TIME CONSISTENCY:")
    losing_hours = [h for h, d in results['time_of_day'].items() if d['pnl'] < 0]
    print(f"   Hours with negative P&L: {losing_hours if losing_hours else 'None'}")

    print(f"\n3. DAY CONSISTENCY:")
    losing_days = [d for d, data in results['day_of_week'].items() if data['pnl'] < 0]
    print(f"   Days with negative P&L: {losing_days if losing_days else 'None'}")

    print(f"\n4. PSYCHOLOGICAL RESILIENCE:")
    print(f"   Max losing streak: {results['losing_streaks']['max_streak']} trades (${results['losing_streaks']['max_streak_loss']:,.0f})")

    print(f"\n5. MONTE CARLO:")
    print(f"   Worst case equity: ${results['monte_carlo']['worst_case']:,.0f}")
    print(f"   Worst case drawdown: ${results['monte_carlo']['worst_dd']:,.0f}")
    print(f"   Risk of ruin: {results['monte_carlo']['ruin_probability']:.1%}")

    return results


def main():
    parser = argparse.ArgumentParser(description="Run stress tests on trading system")
    parser.add_argument("--slippage", action="store_true", help="Run slippage test only")
    parser.add_argument("--time-of-day", action="store_true", help="Run time-of-day analysis only")
    parser.add_argument("--day-of-week", action="store_true", help="Run day-of-week analysis only")
    parser.add_argument("--losing-streaks", action="store_true", help="Run losing streak analysis only")
    parser.add_argument("--drawdown", action="store_true", help="Run drawdown duration analysis only")
    parser.add_argument("--monte-carlo", action="store_true", help="Run Monte Carlo simulation only")
    parser.add_argument("--iterations", type=int, default=1000, help="Monte Carlo iterations")

    args = parser.parse_args()

    trades = get_all_trades()
    print(f"Loaded {len(trades)} trades")

    # Run specific test or all
    if args.slippage:
        slippage_test(trades, 1.5)
    elif args.time_of_day:
        time_of_day_analysis(trades)
    elif args.day_of_week:
        day_of_week_analysis(trades)
    elif args.losing_streaks:
        losing_streak_analysis(trades)
    elif args.drawdown:
        drawdown_duration_analysis(trades)
    elif args.monte_carlo:
        monte_carlo_simulation(trades, args.iterations)
    else:
        run_all_tests()


if __name__ == "__main__":
    main()
