# Backtesting Results & Analysis

This document tracks all backtesting experiments, parameters, results, and actionable findings for the Order Flow Trading System.

## Overview

| Attribute | Value |
|-----------|-------|
| **Data Source** | Databento ES futures tick data |
| **Test Period** | July 1, 2025 - November 28, 2025 |
| **Trading Days** | 110 |
| **Total Ticks** | ~82.5 million |
| **Cache Size** | 3.3 GB |
| **Contracts** | ESU5 (Sep), ESZ5 (Dec) |
| **Session** | RTH 09:30-16:00 ET |

---

## Table of Contents

### Foundation Tests (1-3)
- [Test 1: Original Backtest (Optimistic Fills)](#test-1-original-backtest-optimistic-fills)
- [Test 2: Conservative Fills](#test-2-conservative-fills)
- [Test 3: Daily Loss Limit Comparison](#test-3-daily-loss-limit-comparison)

### Position Sizing Tests (4-6)
- [Test 4: Volatility-Based Sizing](#test-4-volatility-based-sizing)
- [Test 5: Regime-Based Sizing](#test-5-regime-based-sizing)
- [Test 6: Win Streak Sizing](#test-6-win-streak-sizing)

### Drawdown & Trade Limits (7-8)
- [Test 7: First Hour Loss Stop](#test-7-first-hour-loss-stop)
- [Test 8: Trade Count Limits](#test-8-trade-count-limits)

### Pattern Combinations (9-10)
- [Test 9: Stacked Signals](#test-9-stacked-signals)
- [Test 10: Pattern Sequences](#test-10-pattern-sequences)

### Time Window Tests (11-13)
- [Test 11: First Hour Only](#test-11-first-hour-only)
- [Test 12: Skip First 30 Minutes](#test-12-skip-first-30-minutes)
- [Test 13: Afternoon Only](#test-13-afternoon-only)

### Edge Case Analysis (14-15)
- [Test 14: Monday After Big Friday](#test-14-monday-after-big-friday)
- [Test 15: Contract Rollover Weeks](#test-15-contract-rollover-weeks)

### Combined Strategies (16-19)
- [Test 16: Combined Strategy Stack](#test-16-combined-strategy-stack)
- [Test 17: Scaled Loss Limits](#test-17-scaled-loss-limits)
- [Test 18: Capital Simulation](#test-18-capital-simulation)
- [Test 19: Worst Case Stress Test](#test-19-worst-case-stress-test)

### Summary
- [Results Summary (All Tests)](#results-summary-all-tests)
- [Recommended Configuration](#recommended-configuration)

---

## Baseline Configuration

All tests use these default parameters unless otherwise specified:

| Parameter | Value |
|-----------|-------|
| Stop Loss | 16 ticks (4 points, $200) |
| Take Profit | 24 ticks (6 points, $300) |
| Daily Loss Limit | $500 |
| Position Size | 1 contract |
| Max Concurrent Trades | 1 |
| Fill Assumption | Conservative |
| Bar Size | 5-minute footprint bars |

---

# Foundation Tests (1-3)

## Test 1: Original Backtest (Optimistic Fills)

| | |
|---|---|
| **Date** | November 2025 |
| **Script** | `scripts/run_databento_backtest.py` |

### Hypothesis

Establish baseline performance with optimistic fill assumptions (fill when price touches target).

### Parameters

| Parameter | Value |
|-----------|-------|
| Fill Assumption | Optimistic (touch = fill) |
| Daily Loss Limit | $400 |
| Position Size | 1 contract |

### Results

| Metric | Value |
|--------|-------|
| Total P&L | $187,900 |
| Avg Daily P&L | $1,708 |
| Winning Days | 94 (85.5%) |
| Losing Days | 14 (12.7%) |
| Flat Days | 2 (1.8%) |
| Total Trades | ~1,500 |
| Trade Win Rate | 68.0% |
| Max Drawdown | ~$1,200 |

### Finding

Strong baseline: 85% winning days with 68% trade win rate. However, optimistic fills may overstate real-world performance.

**Verdict: BASELINE**

---

## Test 2: Conservative Fills

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/run_conservative_backtest.py` |

### Hypothesis

Most backtests are overly optimistic about limit order fills. Require price to trade THROUGH target (not just touch it) to simulate being last in queue.

### Parameters

| Parameter | Value |
|-----------|-------|
| Fill Assumption | Conservative (price must exceed target by 1 tick) |
| Daily Loss Limit | $400 |
| Position Size | 1 contract |

### Results

| Metric | Optimistic | Conservative | Change |
|--------|------------|--------------|--------|
| Total P&L | $187,900 | $182,200 | -$5,700 (-3.0%) |
| Winning Days | 94 (85.5%) | 94 (85.5%) | Same |
| Losing Days | 14 | 14 | Same |
| Trade Win Rate | 68.0% | 69.9% | +1.9% |
| Total Trades | ~1,500 | 1,265 | -235 |

### Finding

Only 3% P&L reduction with realistic fills. The 235 "lost" trades were ones where price only touched (didn't penetrate) target. Higher win rate confirms trades that DO fill are higher quality.

**Verdict: ADOPTED** - Conservative fills used for all future tests.

---

## Test 3: Daily Loss Limit Comparison

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/test_loss_limit.py` |

### Hypothesis

A tighter daily loss limit ($300) may cut off profitable recovery opportunities. Test $300 vs $400 vs $500 limits.

### Parameters

| Variant | Daily Loss Limit |
|---------|------------------|
| 3A | $300 |
| 3B | $400 |
| 3C | $500 |

### Results

| Metric | $300 Limit | $400 Limit | $500 Limit |
|--------|------------|------------|------------|
| Total P&L | $181,500 | $182,200 | $193,400 |
| Winning Days | 93 (84.5%) | 94 (85.5%) | 101 (91.8%) |
| Losing Days | 15 (13.6%) | 14 (12.7%) | 7 (6.4%) |
| Days Hit Limit | 15 | 14 | 7 |
| Max Losing Streak | 2 days | 2 days | 2 days |

### Finding

$500 limit is optimal: 8 days that hit $300 limit recovered to profit with more room. The system recovers from early losses - cutting it off leaves money on the table.

**Verdict: $500 LIMIT ADOPTED**

---

# Position Sizing Tests (4-6)

## Test 4: Volatility-Based Sizing

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 4` |

### Hypothesis

Trade larger on calm days, smaller on volatile days. Low ATR = 2 contracts, High ATR = 1 contract.

### Parameters

| Parameter | Value |
|-----------|-------|
| Position Size | 2 contracts if ATR < 3 points, else 1 |
| Daily Loss Limit | $500 |

### Results

| Metric | Baseline | Volatility Sizing | Change |
|--------|----------|-------------------|--------|
| Total P&L | $190,700 | $228,300 | +$37,600 (+19.7%) |
| Winning Days | 100 (90.9%) | 98 (89.1%) | -2 |
| Losing Days | 7 (6.4%) | 10 (9.1%) | +3 |
| Trade Win Rate | 69.6% | 69.5% | Same |
| Total Trades | 1,339 | 1,326 | -13 |

### Finding

Low-volatility days are safer for larger positions. 19.7% improvement without proportional risk increase.

**Verdict: WINNER (+19.7%)**

---

## Test 5: Regime-Based Sizing

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 5` |

### Hypothesis

Trade larger in trending markets where win rate is higher. TRENDING = 2 contracts, RANGING = 1 contract.

### Parameters

| Parameter | Value |
|-----------|-------|
| Position Size | 2 contracts in TRENDING_UP/DOWN, else 1 |
| Daily Loss Limit | $500 |

### Results

| Metric | Baseline | Regime Sizing | Change |
|--------|----------|---------------|--------|
| Total P&L | $190,700 | $281,900 | +$91,200 (+47.8%) |
| Winning Days | 100 (90.9%) | 97 (88.2%) | -3 |
| Losing Days | 7 (6.4%) | 11 (10.0%) | +4 |
| Trade Win Rate | 69.6% | 69.5% | Same |
| Total Trades | 1,339 | 1,322 | -17 |

### Finding

Trending regimes justify larger positions. Nearly 48% improvement - the biggest single-strategy gain.

**Verdict: BIG WINNER (+47.8%)**

---

## Test 6: Win Streak Sizing

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 6` |

### Hypothesis

Hot hands predict future success. After 3 wins: +1 contract. After 2 losses: -1 contract.

### Parameters

| Parameter | Value |
|-----------|-------|
| Position Size | Base 1, +1 after 3 wins, -1 after 2 losses |
| Daily Loss Limit | $500 |

### Results

| Metric | Baseline | Streak Sizing | Change |
|--------|----------|---------------|--------|
| Total P&L | $190,700 | $228,800 | +$38,100 (+20.0%) |
| Winning Days | 100 (90.9%) | 100 (90.9%) | Same |
| Losing Days | 7 (6.4%) | 7 (6.4%) | Same |
| Trade Win Rate | 69.6% | 69.6% | Same |
| Total Trades | 1,339 | 1,339 | Same |

### Finding

Momentum in your own performance matters. 20% improvement with identical win rate.

**Verdict: WINNER (+20.0%)**

---

# Drawdown & Trade Limits (7-8)

## Test 7: First Hour Loss Stop

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 7` |

### Hypothesis

If down $200 in the first hour, the day is probably bad. Stop early to limit damage.

### Parameters

| Parameter | Value |
|-----------|-------|
| First Hour Loss Threshold | $200 |
| Action | Stop trading for the day |

### Results

| Metric | Baseline | First Hour Stop | Change |
|--------|----------|-----------------|--------|
| Total P&L | $190,700 | $185,900 | -$4,800 (-2.5%) |
| Winning Days | 100 (90.9%) | 98 (89.1%) | -2 |
| Losing Days | 7 (6.4%) | 9 (8.2%) | +2 |
| Trade Win Rate | 69.6% | 69.6% | Same |
| Total Trades | 1,339 | 1,187 | -152 |

### Finding

Early losses don't predict the rest of the day. The system recovers. Stopping early costs money.

**Verdict: LOSER (-2.5%) - Don't stop early**

---

## Test 8: Trade Count Limits

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 8` |

### Hypothesis

Is there a point where more trades = worse returns due to overtrading?

### Parameters

| Variant | Max Trades/Day |
|---------|----------------|
| 8A | 5 |
| 8B | 10 |
| 8C | Unlimited |

### Results

| Metric | 5 Trades | 10 Trades | Unlimited |
|--------|----------|-----------|-----------|
| Total P&L | $67,800 | $123,800 | $190,700 |
| Winning Days | 89 (80.9%) | 100 (90.9%) | 100 (90.9%) |
| Losing Days | 19 (17.3%) | 7 (6.4%) | 7 (6.4%) |
| vs Baseline | -64.5% | -35.1% | Baseline |

### Finding

More trades = better returns. No evidence of overtrading. Let the system trade freely.

**Verdict: LOSER - Trade limits hurt performance**

---

# Pattern Combinations (9-10)

## Test 9: Stacked Signals

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 9` |

### Hypothesis

When 2+ patterns fire simultaneously in the same bar, double position size for higher conviction.

### Parameters

| Parameter | Value |
|-----------|-------|
| Stacked Signal Threshold | 2+ patterns same bar |
| Position Size Multiplier | 2x |

### Results

| Metric | Baseline | Stacked Signals | Change |
|--------|----------|-----------------|--------|
| Total P&L | $190,700 | $309,400 | +$118,700 (+62.2%) |
| Winning Days | 100 (90.9%) | 93 (84.5%) | -7 |
| Losing Days | 7 (6.4%) | 15 (13.6%) | +8 |
| Trade Win Rate | 69.6% | 69.7% | Same |
| Total Trades | 1,339 | 1,243 | -96 |

### Finding

Stacked signals (multiple patterns in same bar) are high-conviction setups. 62% improvement - major discovery.

**Verdict: BIG WINNER (+62.2%)**

---

## Test 10: Pattern Sequences

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 10` |

### Hypothesis

ABSORPTION followed by EXHAUSTION (in same direction) = high conviction setup. Double position size.

### Parameters

| Parameter | Value |
|-----------|-------|
| Sequence | ABSORPTION → EXHAUSTION |
| Position Size Multiplier | 2x |

### Results

| Metric | Baseline | Pattern Sequence | Change |
|--------|----------|------------------|--------|
| Total P&L | $190,700 | $205,100 | +$14,400 (+7.5%) |
| Winning Days | 100 (90.9%) | 96 (87.3%) | -4 |
| Losing Days | 7 (6.4%) | 11 (10.0%) | +4 |
| Trade Win Rate | 69.6% | 69.6% | Same |
| Total Trades | 1,339 | 1,325 | -14 |

### Finding

Pattern sequences help but not as much as stacked signals. The ABSORPTION→EXHAUSTION sequence is less common.

**Verdict: WINNER (+7.5%)**

---

# Time Window Tests (11-13)

## Test 11: First Hour Only

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 11` |

### Hypothesis

How much of daily P&L comes from the opening hour (9:30-10:30 ET)?

### Parameters

| Parameter | Value |
|-----------|-------|
| Trading Window | 09:30-10:30 ET only |

### Results

| Metric | Full Day | First Hour Only | Change |
|--------|----------|-----------------|--------|
| Total P&L | $190,700 | $50,100 | -$140,600 (-73.7%) |
| % of Full Day P&L | 100% | 26.3% | - |
| Trade Win Rate | 69.6% | 76.8% | +7.2% |
| Total Trades | 1,339 | 364 | -975 |

### Finding

First hour has HIGHEST win rate (76.8%) but only captures 26% of P&L. High quality but limited opportunity.

**Verdict: INFORMATIONAL - First hour is best quality**

---

## Test 12: Skip First 30 Minutes

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 12` |

### Hypothesis

Avoid opening chaos. Does skipping 9:30-10:00 improve results?

### Parameters

| Parameter | Value |
|-----------|-------|
| Trading Window | 10:00-16:00 ET (skip first 30 min) |

### Results

| Metric | Full Day | Skip First 30 | Change |
|--------|----------|---------------|--------|
| Total P&L | $190,700 | $151,000 | -$39,700 (-20.8%) |
| Winning Days | 100 (90.9%) | 95 (86.4%) | -5 |
| Trade Win Rate | 69.6% | 66.6% | -3.0% |
| Total Trades | 1,339 | 1,089 | -250 |

### Finding

The first 30 minutes are valuable. Lower win rate when skipping them.

**Verdict: LOSER (-20.8%) - Don't skip the open**

---

## Test 13: Afternoon Only

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 13` |

### Hypothesis

Trade only 14:00-16:00 ET (afternoon session).

### Parameters

| Parameter | Value |
|-----------|-------|
| Trading Window | 14:00-16:00 ET only |

### Results

| Metric | Full Day | Afternoon Only | Change |
|--------|----------|----------------|--------|
| Total P&L | $190,700 | $43,900 | -$146,800 (-77.0%) |
| % of Full Day P&L | 100% | 23.0% | - |
| Winning Days | 100 (90.9%) | 77 (70.0%) | -23 |
| Trade Win Rate | 69.6% | 64.2% | -5.4% |

### Finding

Afternoon is the WEAKEST session. Only 23% of P&L with lower win rate.

**Verdict: AVOID - Afternoon underperforms**

---

# Edge Case Analysis (14-15)

## Test 14: Monday After Big Friday

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 14` |

### Hypothesis

Does Friday's P&L predict Monday's performance? Mean reversion vs momentum.

### Parameters

| Category | Definition |
|----------|------------|
| Big Win Friday | P&L >= $1,000 |
| Big Loss Friday | P&L <= -$300 |
| Normal Friday | -$300 < P&L < $1,000 |

### Results

| Friday Type | Count | Monday Avg P&L | Monday Win Rate |
|-------------|-------|----------------|-----------------|
| Big Win (>=$1000) | 17 | $1,153 | 88% |
| Big Loss (<=-$300) | 0 | N/A | N/A |
| Normal | 4 | $1,100 | 100% |

### Key Data Points

| Friday | Friday P&L | Monday | Monday P&L |
|--------|------------|--------|------------|
| 2025-08-01 | +$4,300 | 2025-08-04 | +$1,000 |
| 2025-10-17 | +$4,100 | 2025-10-20 | +$1,400 |
| 2025-10-31 | +$3,700 | 2025-11-03 | +$2,600 |
| 2025-11-07 | +$4,300 | 2025-11-10 | +$2,800 |
| 2025-11-21 | +$5,800 | 2025-11-24 | +$2,200 |

### Finding

1. **No big losing Fridays exist** - the system never loses $300+ on Friday
2. **Momentum continues** - after big Friday, Monday wins 88%
3. **No mean reversion** - DON'T reduce Monday trading

**Verdict: INFORMATIONAL - Momentum continues into Monday**

---

## Test 15: Contract Rollover Weeks

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 15` |

### Hypothesis

ES futures rollover week (third week of Mar/Jun/Sep/Dec) may have different behavior.

### Parameters

| Parameter | Value |
|-----------|-------|
| Rollover Week | September 15-19, 2025 |
| Contract Transition | ESU5 → ESZ5 |

### Results

| Metric | Rollover Week | Normal Weeks |
|--------|---------------|--------------|
| Days | 5 | 105 |
| Total P&L | $6,000 | $184,700 |
| Avg Daily P&L | $1,200 | $1,759 |
| Win Days | 5/5 (100%) | 95/105 (90%) |

### Daily Breakdown (Rollover Week)

| Date | P&L |
|------|-----|
| 2025-09-15 | +$200 |
| 2025-09-16 | +$100 |
| 2025-09-17 | +$500 |
| 2025-09-18 | +$2,900 |
| 2025-09-19 | +$2,300 |

### Finding

Rollover week underperforms by ~$560/day but is still profitable (100% win days). Sample size is small (1 week).

**Verdict: INFORMATIONAL - Still profitable, consider smaller size**

---

# Combined Strategies (16-19)

## Test 16: Combined Strategy Stack

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 16` |

### Hypothesis

Combine all winning strategies additively:
- Base: 1 contract
- Stacked signals (2+): +1 contract
- Trending regime: +1 contract
- Win streak (3+): +1 contract
- Loss streak (2+): -1 contract
- Max cap: 4 contracts

### Parameters

| Parameter | Value |
|-----------|-------|
| Sizing Mode | Additive (strategies stack) |
| Max Position | 4 contracts |
| Daily Loss Limit | $500 |

### Results

| Metric | Baseline | Combined | Change |
|--------|----------|----------|--------|
| Total P&L | $190,700 | $416,500 | +$225,800 (+118.4%) |
| Winning Days | 100 (90.9%) | 88 (80.0%) | -12 |
| Losing Days | 7 (6.4%) | 18 (16.4%) | +11 |
| Trade Win Rate | 69.6% | 69.7% | Same |
| Total Trades | 1,339 | 1,230 | -109 |

### Risk Metrics

| Metric | Value |
|--------|-------|
| Max Position Used | 4 contracts |
| Max Daily Loss | $1,000 |
| Max Drawdown | $1,400 |

### Finding

Strategies COMPOUND rather than overlap. More than doubles P&L. However, $500 limit gets hit with larger positions.

**Verdict: MASSIVE WINNER (+118.4%)**

---

## Test 17: Scaled Loss Limits

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 17` |

### Hypothesis

With larger positions, fixed $500 loss limit gets hit too fast. Scale limit with position size: $300 × max contracts.

### Parameters

| Max Contracts | Fixed Limit | Scaled Limit |
|---------------|-------------|--------------|
| 1 | $500 | $300 |
| 2 | $500 | $600 |
| 3 | $500 | $900 |
| 4 | $500 | $1,200 |

### Results

| Metric | Fixed $500 | Scaled $300×N | Change |
|--------|------------|---------------|--------|
| Total P&L | $416,500 | $432,800 | +$16,300 (+3.9%) |
| Winning Days | 88 | 92 | +4 |
| Days Hit Limit | 17 | 13 | -4 |

### Finding

Scaled limits reduce days hitting limit from 17 to 13 and add $16,300 in profit. Larger positions need proportionally larger breathing room.

**Verdict: WINNER (+3.9% on top of combined) - BEST OVERALL**

---

## Test 18: Capital Simulation

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 18` |

### Hypothesis

Starting with $2,500, simulate realistic account growth with tier-based position sizing.

### Position Sizing Tiers

| Account Balance | Instrument | Max Contracts | Daily Limit |
|-----------------|------------|---------------|-------------|
| $0 - $5,000 | MES | 1 | $50 |
| $5,000 - $10,000 | ES | 1 | $500 |
| $10,000 - $20,000 | ES | 2 | $1,000 |
| $20,000 - $40,000 | ES | 3 | $1,500 |
| $40,000+ | ES | 4 | $2,000 |

### Results

| Metric | Value |
|--------|-------|
| Starting Balance | $2,500 |
| Ending Balance | $351,080 |
| Total Gain | +$348,580 (+13,943%) |
| Peak Balance | $351,680 |
| Max Drawdown | $1,100 (6.1%) |

### Tier Progression

| Tier | Day | Date | Balance |
|------|-----|------|---------|
| MES (1) | 1 | 2025-07-01 | $2,500 |
| ES (1) | 38 | 2025-08-21 | $5,280 |
| ES (2) | 44 | 2025-08-29 | $10,480 |
| ES (3) | 48 | 2025-09-04 | $20,480 |
| ES (4) | 59 | 2025-09-19 | $44,880 |

### Finding

Account NEVER dropped a tier. Smooth progression from $2,500 to $351,080 in 110 days with only 6.1% max drawdown.

**Verdict: ROBUST - Tier system provides natural risk management**

---

## Test 19: Worst Case Stress Test

| | |
|---|---|
| **Date** | November 30, 2025 |
| **Script** | `scripts/advanced_backtest.py --test 19` |

### Hypothesis

What if we started trading on the worst possible day? Would a $2,500 account survive?

### Worst 5-Day Stretch Identified

| Date | P&L |
|------|-----|
| 2025-07-01 | -$500 |
| 2025-07-02 | -$100 |
| 2025-07-03 | +$100 |
| 2025-07-04 | +$300 |
| 2025-07-07 | +$1,900 |
| **Total** | **+$1,700** |

**Note**: Even the "worst" 5-day stretch is NET POSITIVE (+$1,700)!

### Simulation Results (Starting on Worst Day with $2,500)

| Metric | Value |
|--------|-------|
| Starting Balance | $2,500 |
| Lowest Balance | $2,460 (Day 2) |
| Max Drawdown | $40 (1.6%) |
| Days to Recover | 4 |
| Balance at Day 30 | $4,530 |
| Survived | YES |

### Daily Log (First 10 Days)

| Day | Date | P&L | Balance | Tier |
|-----|------|-----|---------|------|
| 1 | 2025-07-01 | -$20 | $2,480 | MES |
| 2 | 2025-07-02 | -$20 | $2,460 | MES |
| 3 | 2025-07-03 | +$10 | $2,470 | MES |
| 4 | 2025-07-04 | +$30 | $2,500 | MES |
| 5 | 2025-07-07 | +$190 | $2,690 | MES |
| 6 | 2025-07-08 | +$110 | $2,800 | MES |
| 7 | 2025-07-09 | +$130 | $2,930 | MES |
| 8 | 2025-07-10 | +$80 | $3,010 | MES |
| 9 | 2025-07-11 | +$100 | $3,110 | MES |
| 10 | 2025-07-14 | +$50 | $3,160 | MES |

### Finding

Even starting on the absolute worst day:
- Max drawdown only 1.6% ($40)
- Recovered within 4 days
- By day 30: +81% ($4,530)

The MES tier provides excellent downside protection.

**Verdict: EXTREMELY ROBUST**

---

# Results Summary (All Tests)

## Performance Ranking

| Rank | Test | Total P&L | vs Baseline | Verdict |
|------|------|-----------|-------------|---------|
| 1 | **17: Scaled Loss Limits** | $432,800 | **+126.9%** | **BEST** |
| 2 | **16: Combined Stack** | $416,500 | +118.4% | Winner |
| 3 | **9: Stacked Signals** | $309,400 | +62.2% | Winner |
| 4 | **5: Regime Sizing** | $281,900 | +47.8% | Winner |
| 5 | **6: Streak Sizing** | $228,800 | +20.0% | Winner |
| 6 | **4: Volatility Sizing** | $228,300 | +19.7% | Winner |
| 7 | **10: Pattern Sequence** | $205,100 | +7.5% | Winner |
| 8 | Baseline | $190,700 | - | Reference |
| 9 | 7: First Hour Stop | $185,900 | -2.5% | Skip |
| 10 | 12: Skip First 30 | $151,000 | -20.8% | Skip |
| 11 | 8B: Max 10 Trades | $123,800 | -35.1% | Avoid |
| 12 | 8A: Max 5 Trades | $67,800 | -64.5% | Avoid |
| 13 | 11: First Hour Only | $50,100 | -73.7% | Limited |
| 14 | 13: Afternoon Only | $43,900 | -77.0% | Avoid |

## Key Findings

1. **Combined strategies + scaled limits = +126.9%** - strategies compound, not overlap
2. **Stacked signals (+62.2%)** is the single biggest improvement
3. **Regime sizing (+47.8%)** capitalizes on trending markets
4. **$2,500 → $351,080 in 110 days** with tier-based sizing
5. **Even worst 5-day stretch is net positive** (+$1,700)
6. **Account never dropped a tier** in capital simulation
7. **Don't limit trades** - more trades = more profit
8. **Trade all day** - first hour highest quality, but all periods contribute
9. **Momentum continues** - big Fridays predict big Mondays (88% win rate)

---

# Recommended Configuration

## Optimal Strategy Stack

| Component | Setting | Rationale |
|-----------|---------|-----------|
| Stacked Signals | +1 contract when 2+ patterns | High conviction |
| Regime Sizing | +1 contract in TRENDING | Higher win rate |
| Streak Sizing | +1 after 3 wins, -1 after 2 losses | Momentum |
| Max Position | 4 contracts | Risk cap |
| Loss Limit | $300 × max contracts | Scaled breathing room |
| Time Restrictions | None | Trade all day |
| Trade Limits | Unlimited | More = better |

## Capital Growth Tiers

| Balance | Instrument | Contracts | Daily Limit |
|---------|------------|-----------|-------------|
| $0 - $5,000 | MES | 1 | $50 |
| $5,000 - $10,000 | ES | 1 | $500 |
| $10,000 - $20,000 | ES | 2 | $1,000 |
| $20,000 - $40,000 | ES | 3 | $1,500 |
| $40,000+ | ES | 4 | $2,000 |

## Expected Performance

| Metric | Conservative | Optimistic |
|--------|--------------|------------|
| Daily P&L (1 ES) | $1,650 | $1,760 |
| Monthly P&L (1 ES) | $33,000 | $35,000 |
| Winning Days | 85% | 92% |
| Max Losing Streak | 2 days | 2 days |
| Max Drawdown | 6% | 6% |

---

## Test Scripts Reference

| Script | Purpose |
|--------|---------|
| `scripts/run_databento_backtest.py` | Single day or batch backtest |
| `scripts/run_conservative_backtest.py` | Conservative fills test |
| `scripts/test_loss_limit.py` | Loss limit comparison |
| `scripts/advanced_backtest.py` | All advanced tests (4-19) |

---

*Last Updated: November 30, 2025*
