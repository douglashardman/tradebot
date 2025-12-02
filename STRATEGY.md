# Trading Strategy Guide

> Comprehensive documentation of the order flow trading strategies employed by this system.
> This document explains *what* we trade, *when* we trade it, and *how* position sizing works.

---

## Table of Contents

1. [Core Concept: Order Flow Analysis](#core-concept-order-flow-analysis)
2. [Footprint Charts Explained](#footprint-charts-explained)
3. [Primary Strategies](#primary-strategies)
4. [Secondary Strategies](#secondary-strategies)
5. [Market Regime Detection](#market-regime-detection)
6. [Strategy-Regime Matrix](#strategy-regime-matrix)
7. [Position Sizing & Capital Tiers](#position-sizing--capital-tiers)
8. [Risk Management](#risk-management)

---

## Core Concept: Order Flow Analysis

Traditional price charts show *what* happened. Order flow shows *why* it happened.

We analyze every tick of data to see:
- **Who is aggressive**: Buyers lifting offers vs. sellers hitting bids
- **Where volume clusters**: Price levels with heavy activity
- **Imbalances**: Directional pressure at specific prices
- **Absorption**: Large passive orders absorbing aggressive flow

This gives us an edge: we see the footprints of institutional activity *before* it shows up on price charts.

---

## Footprint Charts Explained

A footprint chart shows volume at each price level within a bar, split by aggressor side.

### Reading the Footprint

```
                    FOOTPRINT BAR (5-min)
    ┌─────────────────────────────────────────────┐
    │  Price    │  Bid Vol  │  Ask Vol  │  Delta  │
    ├───────────┼───────────┼───────────┼─────────┤
    │  5765.00  │     45    │    312    │  +267   │ ← High (Ask dominated = buyers)
    │  5764.75  │     89    │    156    │   +67   │
    │  5764.50  │    203    │    198    │    -5   │ ← Balance
    │  5764.25  │    167    │     88    │   -79   │
    │  5764.00  │    289    │     34    │  -255   │ ← Low (Bid dominated = sellers)
    └───────────┴───────────┴───────────┴─────────┘

    Bar Delta: +267 + 67 - 5 - 79 - 255 = -5 (neutral)

    Legend:
    • Bid Volume = Sellers hitting the bid (aggressive selling)
    • Ask Volume = Buyers lifting the ask (aggressive buying)
    • Delta = Ask Vol - Bid Vol (positive = buying pressure)
```

### Key Footprint Concepts

| Term | Definition |
|------|------------|
| **Delta** | Ask volume minus bid volume. Positive = net buying. |
| **Imbalance** | 300%+ ratio between adjacent price levels (diagonal) |
| **POC** | Point of Control - price with highest total volume |
| **Value Area** | Price range containing 70% of volume |
| **Unfinished** | Price extreme with very low volume (will likely revisit) |

---

## Primary Strategies

These are high-probability setups that form the core of our edge.

### 1. Volume Imbalances

**What it is**: Extreme directional pressure at a price level, measured diagonally across the footprint.

**Detection**: When ask volume at one price is 300%+ of bid volume at the price below (or vice versa).

```
    BUY IMBALANCE EXAMPLE               SELL IMBALANCE EXAMPLE

    Price    │ Bid │ Ask │              Price    │ Bid │ Ask │
    ─────────┼─────┼─────┤              ─────────┼─────┼─────┤
    5765.00  │  20 │ 180 │ ←──┐         5765.00  │ 180 │  15 │ ←──┐
    5764.75  │  45 │  52 │    │         5764.75  │  50 │  48 │    │
    5764.50  │  30 │  88 │    │ 180/30  5764.50  │  35 │  90 │    │ 180/90
             ↑              = 6.0x              ↑              = 2.0x
          Compare                            Compare          (not enough)
          diagonally                         diagonally

    Buy Imbalance: Ask[5765] / Bid[5764.50] = 180/30 = 6.0x (SIGNAL!)
    Threshold: 3.0x (300%) minimum
```

**Stacked Imbalances**: 3+ consecutive imbalances in the same direction.

```
    STACKED BUY IMBALANCES (Strong Bullish)

    Price    │ Bid │ Ask │ Imbalance
    ─────────┼─────┼─────┼──────────
    5766.00  │  15 │ 210 │ 210/25 = 8.4x  ✓
    5765.75  │  25 │ 185 │ 185/30 = 6.2x  ✓
    5765.50  │  30 │ 156 │ 156/45 = 3.5x  ✓  ← 3 stacked = STRONG SIGNAL
    5765.25  │  45 │  88 │
    5765.00  │  60 │  45 │

    Interpretation: Aggressive buyers are overwhelming sellers
    at multiple consecutive levels. Strong buying conviction.
```

**When to trade**:
- Single imbalances → Ranging markets (mean reversion)
- Stacked imbalances → Volatile markets (momentum)
- **DISABLED** in trending markets (0% win rate - trend already established)

---

### 2. Exhaustion Patterns

**What it is**: Declining aggression at price extremes, signaling a potential reversal.

**Detection**: 3+ consecutive price levels showing declining volume at the bar's high or low.

```
    BUYING EXHAUSTION (Short Signal)

    Price reaches new highs but buying pressure is declining:

    Price    │ Bid │ Ask │ Analysis
    ─────────┼─────┼─────┼─────────────────────
    5768.00  │  20 │  45 │ ← Weakest buying (HIGH)
    5767.75  │  25 │  78 │ ← Declining
    5767.50  │  30 │ 112 │ ← Declining
    5767.25  │  35 │ 156 │ ← Strong buying here
    5767.00  │  40 │ 145 │

    Ask volume: 156 → 112 → 78 → 45 (declining 28%, 30%, 42%)

    Interpretation: Buyers are losing steam. They pushed price
    higher but with decreasing conviction. SHORT signal.
```

```
    SELLING EXHAUSTION (Long Signal)

    Price makes new lows but selling pressure is fading:

    Price    │ Bid │ Ask │ Analysis
    ─────────┼─────┼─────┼─────────────────────
    5762.00  │ 145 │  38 │
    5761.75  │ 160 │  35 │ ← Strong selling here
    5761.50  │ 118 │  30 │ ← Declining
    5761.25  │  82 │  25 │ ← Declining
    5761.00  │  48 │  20 │ ← Weakest selling (LOW)

    Bid volume: 160 → 118 → 82 → 48 (declining 26%, 31%, 41%)

    Interpretation: Sellers are exhausted. They pushed price
    lower but commitment is waning. LONG signal.
```

**Minimum requirements**:
- 3 consecutive levels of decline
- 30%+ total decline from strongest to weakest level

---

### 3. Absorption Patterns

**What it is**: Large passive orders (limit orders) absorbing aggressive flow without moving price.

**Detection**: High volume concentration at extremes with price rejecting that level.

```
    BUYING ABSORPTION (Long Signal)

    Heavy selling absorbed at lows, price closes in upper half:

    Price    │ Bid │ Ask │ Analysis
    ─────────┼─────┼─────┼────────────────────────
    5764.00  │  45 │  88 │ ← Close (upper half)
    5763.75  │  67 │  56 │
    5763.50  │  89 │  45 │
    5763.25  │ 234 │  30 │ ← ABSORPTION ZONE
    5763.00  │ 312 │  25 │ ← 60%+ of bid vol here
    5762.75  │ 278 │  22 │ ← Heavy passive buying

    Bottom 3 levels: 312 + 278 + 234 = 824 bid volume
    Total bar bid volume: 1,025
    Concentration: 824/1025 = 80% (> 60% threshold)

    Close position: Upper half of bar ✓

    Interpretation: Big passive buyer absorbed all the selling.
    Price rejected the lows. LONG signal.
```

```
    SELLING ABSORPTION (Short Signal)

    Heavy buying absorbed at highs, price closes in lower half:

    Price    │ Bid │ Ask │ Analysis
    ─────────┼─────┼─────┼────────────────────────
    5768.00  │  25 │ 298 │ ← Heavy passive selling
    5767.75  │  30 │ 267 │ ← ABSORPTION ZONE
    5767.50  │  35 │ 223 │ ← 60%+ of ask vol here
    5767.25  │  45 │  78 │
    5767.00  │  56 │  65 │ ← Close (lower half)

    Top 3 levels: 298 + 267 + 223 = 788 ask volume
    Total bar ask volume: 931
    Concentration: 788/931 = 85% (> 60% threshold)

    Close position: Lower half of bar ✓

    Interpretation: Big passive seller absorbed all the buying.
    Price rejected the highs. SHORT signal.
```

**Requirements**:
- 60%+ of volume concentrated at top/bottom 3 levels
- Price must close in opposite half of bar (rejection)
- Minimum volume threshold (150 for ES, 30 for MES)

---

### 4. Delta Divergence

**What it is**: Price makes new highs/lows but delta (buying pressure) diverges, suggesting hidden weakness.

**Detection**: Compare price structure vs. delta structure over 5 bars.

```
    BEARISH DELTA DIVERGENCE (Short Signal)

    Bar   │ High    │ Cumulative Delta │ Analysis
    ──────┼─────────┼──────────────────┼─────────────────
    Bar 1 │ 5765.00 │    +2,450        │
    Bar 2 │ 5766.50 │    +3,100        │ Price ↑, Delta ↑
    Bar 3 │ 5768.00 │    +2,800        │ Price ↑, Delta ↓ ←
    Bar 4 │ 5769.25 │    +2,200        │ Price ↑, Delta ↓ ←
    Bar 5 │ 5770.00 │    +1,500        │ Price ↑, Delta ↓ ←

    Price: Higher highs (bullish structure)
    Delta: Lower highs (bearish divergence)

    Interpretation: Price keeps rising but buying pressure is
    declining. "Smart money" may be distributing. SHORT signal.
```

```
    BULLISH DELTA DIVERGENCE (Long Signal)

    Bar   │ Low     │ Cumulative Delta │ Analysis
    ──────┼─────────┼──────────────────┼─────────────────
    Bar 1 │ 5762.00 │    -2,800        │
    Bar 2 │ 5760.50 │    -3,500        │ Price ↓, Delta ↓
    Bar 3 │ 5759.00 │    -3,100        │ Price ↓, Delta ↑ ←
    Bar 4 │ 5757.75 │    -2,600        │ Price ↓, Delta ↑ ←
    Bar 5 │ 5756.50 │    -2,000        │ Price ↓, Delta ↑ ←

    Price: Lower lows (bearish structure)
    Delta: Higher lows (bullish divergence)

    Interpretation: Price keeps falling but selling pressure is
    declining. Accumulation may be occurring. LONG signal.
```

---

## Secondary Strategies

### Unfinished Business

**What it is**: Price extremes (high/low) with minimal volume, suggesting the market will revisit.

```
    UNFINISHED HIGH (Future Resistance)

    Price    │ Bid │ Ask │ Analysis
    ─────────┼─────┼─────┼──────────────
    5770.00  │   2 │   3 │ ← UNFINISHED (Vol ≤ 5)
    5769.75  │  45 │  78 │ ← Normal volume
    5769.50  │  89 │ 112 │

    Bar's high touched 5770 but almost no one traded there.
    This level is "unfinished" - price will likely return.

    UNFINISHED LOW (Future Support)

    Price    │ Bid │ Ask │ Analysis
    ─────────┼─────┼─────┼──────────────
    5755.50  │  92 │  56 │
    5755.25  │  67 │  34 │ ← Normal volume
    5755.00  │   4 │   1 │ ← UNFINISHED (Vol ≤ 5)
```

The system tracks up to 50 unfinished levels and generates signals when price revisits them.

---

## Market Regime Detection

The system classifies market conditions into four regimes using a scoring system based on 19 inputs:

### Regime Scoring Inputs

| Category | Indicators |
|----------|------------|
| **Trend** | ADX (14-period), EMA crossover (9/21), Price vs VWAP |
| **Structure** | Higher highs/lows, Lower highs/lows, Range-bound bars |
| **Volatility** | ATR percentile, Average bar width, Volume vs average |
| **Delta** | Cumulative delta, Delta slope, Delta momentum |

### The Four Regimes

```
    ┌─────────────────────────────────────────────────────────────┐
    │                      MARKET REGIMES                         │
    ├──────────────┬──────────────┬───────────────┬───────────────┤
    │  TRENDING UP │ TRENDING DN  │    RANGING    │   VOLATILE    │
    ├──────────────┼──────────────┼───────────────┼───────────────┤
    │ ADX > 25     │ ADX > 25     │ ADX < 20      │ ATR > 85%ile  │
    │ EMA9 > EMA21 │ EMA9 < EMA21 │ Price @ VWAP  │ Wide bars     │
    │ Price > VWAP │ Price < VWAP │ No HH/LL      │ High volume   │
    │ HH + HL      │ LH + LL      │ Range-bound   │ Rapid delta   │
    │ Delta > 0    │ Delta < 0    │ Neutral delta │ ADX declining │
    └──────────────┴──────────────┴───────────────┴───────────────┘
```

### Regime Scoring Example

```
    TRENDING UP CALCULATION:

    Input                          │ Condition Met? │ Score
    ───────────────────────────────┼────────────────┼───────
    ADX = 28                       │ > 25? YES      │ +2.0
    EMA9 > EMA21                   │ YES            │ +1.5
    Price > VWAP                   │ YES            │ +1.0
    Higher highs + Higher lows     │ YES            │ +2.0
    Cumulative delta > 0           │ YES            │ +0.5
    Delta + Delta slope > 0        │ YES            │ +1.5
    ADX slope > 0                  │ YES            │ +0.5
    ───────────────────────────────┴────────────────┴───────
    TOTAL SCORE:                                      9.0

    Minimum to classify: 4.0 ✓

    Confidence = (Winner Score - Runner Up) / Winner Score
               = (9.0 - 3.5) / 9.0 = 61%
```

### NO_TRADE Overrides

Certain conditions force NO_TRADE regardless of scoring:

| Condition | Reason |
|-----------|--------|
| < 15 min to close | End-of-day volatility |
| News window active | Event-driven moves |
| < 5 min since open | Opening range chaos |
| Volume < 30% of average | Low liquidity |

---

## Strategy-Regime Matrix

**Critical insight**: Not all strategies work in all regimes. This matrix is the key to our edge.

```
┌────────────────────────┬──────────┬──────────┬─────────┬──────────┐
│ STRATEGY               │ TREND UP │ TREND DN │ RANGING │ VOLATILE │
├────────────────────────┼──────────┼──────────┼─────────┼──────────┤
│ Buy Imbalance          │    ✗     │    ✗     │    ✓    │    ✗     │
│ Sell Imbalance         │    ✗     │    ✗     │    ✓    │    ✗     │
│ Stacked Buy Imbalance  │    ✗     │    ✗     │    ✗    │    ✓     │
│ Stacked Sell Imbalance │    ✗     │    ✗     │    ✗    │    ✓     │
│ Buying Exhaustion      │    ✗     │    ✓     │    ✓    │    ✗     │
│ Selling Exhaustion     │    ✓     │    ✗     │    ✓    │    ✗     │
│ Buying Absorption      │    ✓     │    ✗     │    ✓    │    ✗     │
│ Selling Absorption     │    ✗     │    ✓     │    ✓    │    ✗     │
│ Bullish Divergence     │    ✓     │    ✗     │    -    │    -     │
│ Bearish Divergence     │    ✗     │    ✓     │    -    │    -     │
├────────────────────────┼──────────┴──────────┴─────────┴──────────┤
│ Position Multiplier    │   1.0x   │   1.0x   │  0.75x  │   0.5x   │
│ Directional Bias       │   LONG   │  SHORT   │  NONE   │   NONE   │
└────────────────────────┴──────────────────────────────────────────┘

✓ = Enabled    ✗ = Disabled    - = Not applicable
```

### Why This Matrix Matters

1. **Trending markets**: Imbalances are *disabled* because trend is already established. Instead, we look for pullback entries (exhaustion, absorption) and divergence for tops/bottoms.

2. **Ranging markets**: Most strategies enabled. Single imbalances work for mean reversion. Exhaustion and absorption work at range extremes.

3. **Volatile markets**: Only stacked imbalances enabled. Need strong momentum (3+ consecutive imbalances) to trade through the chop.

### Directional Bias Filter

In trending regimes, signals must align with the trend:

```
    TRENDING UP:  Only LONG signals accepted
    TRENDING DN:  Only SHORT signals accepted
    RANGING:      Both directions accepted
    VOLATILE:     Both directions accepted
```

---

## Position Sizing & Capital Tiers

### The Tier System

Position size and instrument scale with account balance:

```
┌───────┬────────────────┬────────────┬──────────┬─────────┬────────────┐
│ TIER  │ BALANCE RANGE  │ INSTRUMENT │ BASE QTY │ MAX QTY │ LOSS LIMIT │
├───────┼────────────────┼────────────┼──────────┼─────────┼────────────┤
│   1   │ $0 - $3,500    │    MES     │    1     │    3    │   -$100    │
│   2   │ $3,500 - $5K   │    ES      │    1     │    1    │   -$400    │
│   3   │ $5,000 - $7.5K │    ES      │    1     │    2    │   -$400    │
│   4   │ $7,500 - $10K  │    ES      │    1     │    3    │   -$500    │
│   5   │ $10,000+       │    ES      │    1     │    3    │   -$500    │
└───────┴────────────────┴────────────┴──────────┴─────────┴────────────┘
```

### Scaling Into Larger Positions

Position size adjusts based on confluence and market conditions:

```
    POSITION SIZING FORMULA:

    Base Position:           1 contract

    + Stacked Signals:      +1 if 2+ patterns fire simultaneously
    + Trending Regime:      +1 if TRENDING_UP or TRENDING_DOWN
    + Win Streak Bonus:     +1 if 3+ consecutive wins
    - Loss Streak Penalty:  -1 if 2+ consecutive losses

    Final Size = max(1, min(calculated, tier.max_contracts))
```

### Scaling Examples

```
    EXAMPLE 1: Maximum Size (Tier 1)

    Account: $2,500 (Tier 1 - MES, max 3 contracts)
    Regime: TRENDING_UP
    Signals: Buying absorption + Selling exhaustion (stacked)
    Streak: 4 wins in a row

    Size = 1 (base) + 1 (stacked) + 1 (trending) + 1 (streak)
         = 4 contracts
         = min(4, 3) = 3 contracts ← capped at tier max


    EXAMPLE 2: Reduced Size

    Account: $6,000 (Tier 3 - ES, max 2 contracts)
    Regime: RANGING (0.75x multiplier)
    Signals: Single buy imbalance
    Streak: 2 losses in a row

    Size = 1 (base) + 0 (single signal) + 0 (not trending) - 1 (losses)
         = 0 contracts
         = max(1, 0) = 1 contract ← minimum 1

    (Note: Regime multiplier 0.75x applied to risk, not qty)


    EXAMPLE 3: Tier Transition

    Account: $3,450 → Win $100 → $3,550

    Before: Tier 1 (MES, 3 max)
    After:  Tier 2 (ES, 1 max) ← Instrument upgrade!

    System auto-detects tier change and switches instrument.
```

### Instrument Value Comparison

```
    MES (Micro E-mini S&P 500):
    • Tick size: 0.25 points
    • Tick value: $1.25
    • 16 tick stop = $20 risk

    ES (E-mini S&P 500):
    • Tick size: 0.25 points
    • Tick value: $12.50
    • 16 tick stop = $200 risk

    ES = 10x the value of MES
```

---

## Risk Management

### Fixed Bracket Orders

Every trade uses a predefined bracket with fixed risk/reward:

```
    LONG ENTRY @ 5765.00

    ┌─────────────────────────────────────┐
    │           TAKE PROFIT               │
    │        5771.00 (+24 ticks)          │  +$30.00 (MES)
    │        +6.00 points                 │  +$300.00 (ES)
    ├─────────────────────────────────────┤
    │             ENTRY                   │
    │           5765.00                   │
    ├─────────────────────────────────────┤
    │           STOP LOSS                 │
    │        5761.00 (-16 ticks)          │  -$20.00 (MES)
    │        -4.00 points                 │  -$200.00 (ES)
    └─────────────────────────────────────┘

    Risk:Reward = 16:24 = 1:1.5

    Break-even win rate needed: 40%
    Actual backtest win rate: 55-60%
```

### Daily Loss Limits

Trading halts automatically when daily P&L hits the limit:

| Tier | Daily Loss Limit | Example |
|------|------------------|---------|
| 1 (MES) | -$100 | 5 losing trades |
| 2-3 (ES) | -$400 | 2 losing trades |
| 4-5 (ES) | -$500 | 2-3 losing trades |

### Session Boundaries

```
    Trading Window: 9:30 AM - 3:45 PM ET

    NO trades within:
    • First 5 minutes (opening chaos)
    • Last 15 minutes (closing volatility)
    • News windows (configurable)

    Futures Daily Halt: 5:00 PM - 6:00 PM ET
    (No trading possible - exchange closed)
```

---

## Signal Flow Summary

```
┌─────────────────────────────────────────────────────────────────────┐
│                        SIGNAL FLOW                                  │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│   TICK DATA (Databento)                                             │
│        │                                                            │
│        ▼                                                            │
│   ┌─────────────────────┐                                           │
│   │ Footprint Aggregator │  ─────►  5-minute bars with             │
│   │  (300 second bars)   │          bid/ask at each price          │
│   └─────────────────────┘                                           │
│        │                                                            │
│        ▼                                                            │
│   ┌─────────────────────┐                                           │
│   │   Pattern Detectors  │  ─────►  Imbalance, Exhaustion,         │
│   │   (5 types)          │          Absorption, Divergence,        │
│   └─────────────────────┘          Unfinished                      │
│        │                                                            │
│        ▼                                                            │
│   ┌─────────────────────┐                                           │
│   │   Regime Detector    │  ─────►  TRENDING_UP, TRENDING_DOWN,    │
│   │   (19 inputs scored) │          RANGING, VOLATILE, NO_TRADE    │
│   └─────────────────────┘                                           │
│        │                                                            │
│        ▼                                                            │
│   ┌─────────────────────┐                                           │
│   │   Strategy Router    │  ─────►  Filter signals by regime       │
│   │   (regime matrix)    │          Check directional bias         │
│   └─────────────────────┘          Validate strength ≥ 0.5         │
│        │                                                            │
│        ▼                                                            │
│   ┌─────────────────────┐                                           │
│   │  Execution Manager   │  ─────►  Check daily limits             │
│   │  (risk management)   │          Calculate position size        │
│   └─────────────────────┘          Create bracket order            │
│        │                                                            │
│        ▼                                                            │
│   ┌─────────────────────┐                                           │
│   │   Order Execution    │  ─────►  Entry + Stop + Target          │
│   │   (Rithmic API)      │          Track P&L, update tier         │
│   └─────────────────────┘                                           │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Key Takeaways

1. **Order flow reveals intent** - We see aggressive buyers/sellers before price moves.

2. **Regime context is everything** - The same pattern can be a winner or loser depending on market conditions.

3. **Imbalances fail in trends** - Don't fight momentum. Wait for pullbacks or reversals.

4. **Confluence increases size** - Multiple signals + trending regime = larger position.

5. **Fixed risk per trade** - Every trade risks 16 ticks, targets 24 ticks (1:1.5).

6. **Tier system preserves capital** - Start small (MES), scale up (ES) as account grows.

7. **Hard stops protect capital** - Daily loss limits halt trading before drawdowns compound.

---

*Last updated: 2025-12-01*
