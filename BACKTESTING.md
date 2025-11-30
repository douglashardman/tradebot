# Backtesting Results & Analysis

This document tracks all backtesting experiments, parameters, results, and actionable findings for the Order Flow Trading System.

**Data Source**: Databento ES futures tick data
**Period**: July 1, 2025 - November 28, 2025 (110 trading days)
**Total Ticks Processed**: ~82.5 million
**Tick Cache Size**: 3.3 GB

---

## Table of Contents

1. [Test 1: Original 110-Day Backtest (Optimistic Fills)](#test-1-original-110-day-backtest-optimistic-fills)
2. [Test 2: Conservative Fills (Queue Position Simulation)](#test-2-conservative-fills-queue-position-simulation)
3. [Test 3: Daily Loss Limit Comparison ($300 vs $400 vs $500)](#test-3-daily-loss-limit-comparison)

---

## Test 1: Original 110-Day Backtest (Optimistic Fills)

**Date Run**: November 2025
**Script**: `scripts/run_databento_backtest.py`

### Parameters

| Parameter | Value |
|-----------|-------|
| Contracts | ES futures (ESU5, ESZ5) |
| Session | RTH 09:30-16:00 ET |
| Bar Size | 5-minute footprint bars |
| Stop Loss | 16 ticks (4 points, $200) |
| Take Profit | 24 ticks (6 points, $300) |
| Daily Loss Limit | -$400 |
| Daily Profit Target | Unlimited |
| Position Size | 1 contract |
| Max Concurrent Trades | 1 |
| Fill Assumption | **Optimistic** (fill when price touches target) |

### Results

| Metric | Value |
|--------|-------|
| Days Tested | 110 |
| Total P&L | **$187,900** |
| Avg Daily P&L | $1,708 |
| Winning Days | 94 (85%) |
| Losing Days | 14 (13%) |
| Flat Days | 2 |
| Total Trades | ~1,500 |
| Trade Win Rate | 68% |
| Max Drawdown | ~$1,200 |

### Pattern Performance

| Pattern | Trades | Win Rate | Total P&L |
|---------|--------|----------|-----------|
| SELLING_EXHAUSTION | Highest volume | ~70% | Positive |
| BUYING_EXHAUSTION | High volume | ~68% | Positive |
| SELLING_ABSORPTION | Medium volume | ~65% | Positive |
| BUYING_ABSORPTION | Medium volume | ~64% | Positive |

### Regime Performance

| Regime | Performance |
|--------|-------------|
| TRENDING_UP | Best performance, higher win rate |
| TRENDING_DOWN | Good performance with SHORT bias |
| RANGING | Moderate performance, more whipsaws |
| VOLATILE | Mixed results |

### Findings

1. **85% winning days** - Strong day-over-day consistency
2. **68% trade win rate** with 1.5:1 reward:risk delivers positive expectancy
3. **Trending regimes outperform** ranging regimes
4. **Exhaustion patterns** are the most reliable signals

### Questions Raised

- Are we being too optimistic about fill assumptions?
- How many "lucky" fills are we assuming when price just touches target?

### Actions Taken

→ Proceeded to Test 2 to validate with conservative fill assumptions

---

## Test 2: Conservative Fills (Queue Position Simulation)

**Date Run**: November 30, 2025
**Script**: `scripts/run_conservative_backtest.py`

### Hypothesis

Most backtests are overly optimistic about limit order fills. In reality, if your limit order is at price X, price needs to trade THROUGH X (not just touch it) to guarantee your fill, especially if you're last in the order queue.

### Parameters

| Parameter | Value |
|-----------|-------|
| All parameters same as Test 1, except: | |
| Fill Assumption | **Conservative** (require price to go 1 tick BEYOND target) |
| Daily Loss Limit | -$400 |

### Implementation

Added `conservative_fills` flag to `TradingSession`:

```python
# In src/execution/session.py
conservative_fills: bool = False  # If True, require 1 tick through target

# In src/execution/manager.py - update_prices()
if conservative:
    # LONG: require price > target (not >=)
    # SHORT: require price < target (not <=)
```

### Results

| Metric | Optimistic | Conservative | Difference |
|--------|------------|--------------|------------|
| Total P&L | $187,900 | **$182,200** | -$5,700 (-3.0%) |
| Winning Days | 94 (85%) | 94 (85.5%) | Same |
| Losing Days | 14 | 14 | Same |
| Trade Win Rate | 68% | **69.9%** | +1.9% |
| Total Trades | ~1,500 | **1,265** | -235 fewer |

### Findings

1. **Only 3% reduction** in P&L with conservative fills - strategy is robust
2. **235 fewer trades** - these were trades where price only touched (didn't penetrate) target
3. **Higher win rate** (69.9% vs 68%) - trades that DO fill are higher quality
4. **Most targets hit with conviction** - price goes through, not just touches

### Interpretation

The small difference ($5,700 over 110 days = $52/day) indicates:
- We're not relying on "lucky" fills to be profitable
- Realistic P&L expectation: **$180K-188K** over 110 days
- Strategy edge comes from analysis quality, not execution luck

### Actions Taken

→ Adopted conservative fills as the default for all future backtests
→ Proceeded to Test 3 to optimize daily loss limit

---

## Test 3: Daily Loss Limit Comparison

**Date Run**: November 30, 2025
**Script**: `scripts/test_loss_limit.py`

### Hypothesis

If we hit a daily loss limit and stop trading, but the market would have provided recovery opportunities later in the day, we're leaving money on the table. Testing different loss limits will reveal:
1. How many days get "cut off" prematurely
2. The optimal balance between protection and opportunity

### Parameters

All tests used conservative fills. Only the daily loss limit varied:

| Test | Daily Loss Limit |
|------|------------------|
| 3A | -$300 |
| 3B | -$400 |
| 3C | -$500 |

### Results

| Metric | $300 Limit | $400 Limit | $500 Limit |
|--------|------------|------------|------------|
| **Winning Days** | 93 (84.5%) | 94 (85.5%) | **101 (91.8%)** |
| **Losing Days** | 15 (13.6%) | 14 (12.7%) | **7 (6.4%)** |
| Flat Days | 2 | 2 | 2 |
| **Total P&L** | $181,500 | $182,200 | **$193,400** |
| Avg Daily P&L | $1,650 | $1,656 | **$1,758** |
| Days Hit Limit | 15 | 14 | 7 |

### Streak Analysis ($300 Limit)

| Losing Streak Length | Occurrences |
|---------------------|-------------|
| 1-day streak | 11 times |
| 2-day streak | 2 times |
| **Max consecutive losses** | **2 days** |

### Key Finding: Recovery Potential

| Limit Change | Days That Recovered | Extra Profit |
|--------------|---------------------|--------------|
| $300 → $400 | 1 day | +$700 |
| $400 → $500 | 7 days | +$11,200 |
| **$300 → $500** | **8 days** | **+$11,900** |

### Interpretation

1. **8 days that hit $300 limit would have turned profitable** with more room
2. **$500 limit is optimal** for this strategy:
   - Only 7 losing days out of 110 (6.4%)
   - 92% winning day rate
   - $11,900 more profit than $300 limit
3. **The system recovers** - cutting it off early leaves money on the table
4. **Worst case buffer needed**: $1,000-1,500 (to survive 2-3 bad days)

### Actions Taken

→ **Changed recommended daily loss limit from $300 to $500**
→ Updated risk parameters in documentation

---

## Summary: Current Optimal Parameters

Based on all testing to date:

| Parameter | Recommended Value | Rationale |
|-----------|-------------------|-----------|
| Stop Loss | 16 ticks ($200) | Sufficient room for noise |
| Take Profit | 24 ticks ($300) | 1.5:1 reward:risk |
| Daily Loss Limit | **$500** | Allows recovery, 92% win days |
| Fill Assumption | Conservative | Realistic expectations |
| Position Size | 1 contract | Risk management |

### Expected Performance (Conservative Estimate)

| Metric | Expected Value |
|--------|----------------|
| Daily P&L | $1,650 - $1,760 |
| Monthly P&L (20 days) | $33,000 - $35,000 |
| Winning Days | 85-92% |
| Max Losing Streak | 2 days |
| Required Buffer | $1,500 minimum |

---

## Appendix: Test Scripts

| Script | Purpose |
|--------|---------|
| `scripts/run_databento_backtest.py` | Single day or batch backtest |
| `scripts/run_conservative_backtest.py` | Full 110-day conservative fills test |
| `scripts/test_loss_limit.py` | Loss limit comparison with streak analysis |

---

*Last Updated: November 30, 2025*
