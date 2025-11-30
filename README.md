# Order Flow Trading System

A headless, regime-adaptive trading system for real-time order flow analysis and automated execution on futures markets.

## Features

- **Real-time Order Flow Analysis**
  - Footprint chart construction from tick data
  - Pattern detection: imbalances, exhaustion, absorption, delta divergence, unfinished business
  - Cumulative delta tracking and volume profiling

- **Regime Detection**
  - Automatic market regime classification (trending, ranging, volatile)
  - Technical indicators: ADX(14), ATR(14), EMA(9/21), VWAP
  - Market structure analysis (higher highs/lows)
  - 5-bar lookback for regime scoring

- **Strategy Routing**
  - Signal filtering based on current regime
  - Pattern enablement/disablement per regime
  - Position sizing multipliers by regime confidence

- **Paper Trading**
  - Bracket order execution (entry, stop, target)
  - Real-time P&L tracking with correct tick values per symbol
  - Daily profit target and loss limit enforcement
  - Dynamic symbol switching with automatic tick value updates

- **Historical Replay**
  - Polygon.io integration for historical market data
  - Replay any trading day at configurable speed
  - Simulated tick generation from minute bars
  - Full system integration (signals, trades, dashboard)

- **Web Dashboard**
  - Real-time WebSocket updates
  - Market regime visualization
  - Trade and signal history
  - Session management and settings

## Quick Start

### Installation

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Configuration

Create a `.env` file with your API keys:

```bash
cp .env.example .env
# Edit .env with your keys
```

Required environment variables:
- `POLYGON_API_KEY` - For historical replay (free tier available)
- `DATABENTO_API_KEY` - For live data (optional)

### Run Demo Mode

Demo mode uses simulated market data to demonstrate the system:

```bash
PYTHONPATH=. python scripts/run_demo.py
```

Then open http://localhost:8000/dashboard in your browser.

### Run Historical Replay

Replay real market data from any trading day using Polygon.io:

```bash
# Replay ES futures from a specific date at 100x speed
PYTHONPATH=. python scripts/run_replay.py --date 2024-11-13 --symbol ES --speed 100

# Options:
#   --date      Date to replay (YYYY-MM-DD)
#   --symbol    Symbol to replay (ES, SPY, etc.)
#   --speed     Replay speed multiplier (1=realtime, 100=100x faster)
#   --api-key   Polygon API key (or set POLYGON_API_KEY env var)
```

### Run with Live Data (Databento)

```bash
# Set Databento API key
export DATABENTO_API_KEY=your_key_here

# Start the system
PYTHONPATH=. python main.py --symbol MES --mode paper
```

## Command Line Options

### main.py (Live Trading)

```
python main.py [options]

Options:
  --symbol, -s      Trading symbol (default: MES)
  --mode, -m        Trading mode: paper or live (default: paper)
  --port, -p        Dashboard port (default: 8000)
  --timeframe, -t   Footprint bar timeframe in seconds (default: 300)
  --profit-target   Daily profit target in $ (default: 500)
  --loss-limit      Daily loss limit in $ (default: -300)
  --position-size   Max position size (default: 2)
  --replay          Replay historical data: CONTRACT START END
  --speed           Replay speed multiplier (default: 10.0)
  --no-data         Dashboard only mode
```

### run_replay.py (Historical Replay)

```
python scripts/run_replay.py [options]

Options:
  --date        Date to replay (YYYY-MM-DD, default: 2024-11-13)
  --symbol      Symbol to replay (default: ES)
  --speed       Replay speed multiplier (default: 50.0)
  --api-key     Polygon API key (or use POLYGON_API_KEY env var)
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | Health check |
| `/dashboard` | GET | Web dashboard |
| `/api/session/start` | POST | Start trading session |
| `/api/session/stop` | POST | Stop trading session |
| `/api/session/status` | GET | Get session status |
| `/api/session/halt` | POST | Halt trading |
| `/api/session/resume` | POST | Resume trading |
| `/api/trades` | GET | Get recent trades |
| `/api/positions` | GET | Get open positions |
| `/api/signals` | GET | Get signal history |
| `/api/statistics` | GET | Get trading statistics |
| `/api/settings` | POST | Update session settings |
| `/ws` | WebSocket | Real-time updates |

## Architecture

```
┌─────────────────┐
│   Data Feed     │  Polygon (replay) / Databento (live) / Demo (simulated)
│   Adapter       │
└────────┬────────┘
         │ Tick
         ▼
┌─────────────────┐
│   OrderFlow     │  Footprint bars, pattern detection
│   Engine        │
└────────┬────────┘
         │ Signal
         ▼
┌─────────────────┐
│   Strategy      │  Regime detection, signal filtering
│   Router        │
└────────┬────────┘
         │ Approved Signal
         ▼
┌─────────────────┐
│   Execution     │  Order placement, position management
│   Manager       │
└────────┬────────┘
         │ Trade/Position updates
         ▼
┌─────────────────┐
│   FastAPI       │  REST API + WebSocket
│   Server        │
└─────────────────┘
```

## Order Flow Patterns

| Pattern | Direction | Description |
|---------|-----------|-------------|
| Buy Imbalance | LONG | Aggressive buying at price level (300%+ ratio) |
| Sell Imbalance | SHORT | Aggressive selling at price level |
| Stacked Buy Imbalance | LONG | 3+ consecutive buy imbalances |
| Stacked Sell Imbalance | SHORT | 3+ consecutive sell imbalances |
| Buying Exhaustion | SHORT | Declining buy volume at highs |
| Selling Exhaustion | LONG | Declining sell volume at lows |
| Buying Absorption | LONG | Large passive bids absorbing selling |
| Selling Absorption | SHORT | Large passive offers absorbing buying |
| Delta Divergence | Varies | Price/delta relationship mismatch |
| Unfinished Business | Varies | Incomplete auction at extreme |

## Market Regimes

| Regime | Description | Trading Style |
|--------|-------------|---------------|
| TRENDING_UP | Strong uptrend (ADX > 25, bullish structure) | Long-biased, trend following |
| TRENDING_DOWN | Strong downtrend (ADX > 25, bearish structure) | Short-biased, trend following |
| RANGING | Consolidation (ADX < 25) | Mean reversion at extremes |
| VOLATILE | Choppy conditions (high ATR) | Only strongest signals, reduced size |
| NO_TRADE | Avoid trading | Session edges, news, low volume |

## Supported Markets

| Symbol | Name | Tick Size | Tick Value |
|--------|------|-----------|------------|
| ES | E-mini S&P 500 | 0.25 | $12.50 |
| MES | Micro E-mini S&P 500 | 0.25 | $1.25 |
| NQ | E-mini Nasdaq-100 | 0.25 | $5.00 |
| MNQ | Micro E-mini Nasdaq-100 | 0.25 | $0.50 |
| CL | Crude Oil | 0.01 | $10.00 |
| GC | Gold | 0.10 | $10.00 |

## Project Structure

```
tradebot/
├── src/
│   ├── core/              # Types, constants, config
│   │   ├── types.py       # Data models (Tick, Signal, FootprintBar)
│   │   ├── constants.py   # Tick sizes, values, patterns
│   │   └── config.py      # Configuration management
│   ├── data/
│   │   ├── adapters/      # Data feed adapters
│   │   │   ├── polygon.py # Polygon.io historical replay
│   │   │   └── databento.py # Databento live feed
│   │   └── aggregator.py  # Tick to bar aggregation
│   ├── analysis/
│   │   ├── engine.py      # Order flow analysis engine
│   │   ├── indicators.py  # Technical indicators
│   │   └── detectors/     # Pattern detectors
│   │       ├── imbalance.py
│   │       ├── exhaustion.py
│   │       ├── absorption.py
│   │       ├── divergence.py
│   │       └── unfinished.py
│   ├── regime/
│   │   ├── detector.py    # Market regime detection
│   │   ├── router.py      # Strategy routing
│   │   └── inputs.py      # Regime calculation inputs
│   ├── execution/
│   │   ├── manager.py     # Execution management
│   │   ├── orders.py      # Order types
│   │   └── session.py     # Trading session
│   └── api/
│       └── server.py      # FastAPI server + WebSocket
├── scripts/
│   ├── run_demo.py        # Demo with simulated data
│   ├── run_replay.py      # Historical replay
│   └── simulate_trading.py # Trading simulation
├── static/                # Dashboard HTML/CSS/JS
├── tests/                 # Unit tests
├── main.py                # CLI entry point
├── requirements.txt       # Python dependencies
└── pyproject.toml         # Package configuration
```

## Testing

```bash
# Run unit tests
PYTHONPATH=. python tests/test_order_flow.py

# Run simulation test
PYTHONPATH=. python scripts/simulate_trading.py
```

## Data Sources

### Polygon.io (Historical Replay)
- Free tier available with delayed data
- Minute bar data converted to simulated ticks
- Sign up at https://polygon.io

### Databento (Live Data)
- Professional-grade tick data
- Real-time and historical
- Sign up at https://databento.com

## License

MIT
