# CLAUDE.md - Order Flow Trading System

> This file provides Claude with comprehensive context about the tradebot project.
> Last updated: 2025-12-01 (updated with watchdog, historical data downloader, extended backtests)

## Quick Reference

```
Location: /opt/tradebot
Repo: git@github.com:douglashardman/tradebot.git
Python: 3.10+ with venv at /opt/tradebot/venv
Primary Symbol: MES (Micro E-mini S&P 500)
Status: Production-ready, awaiting Rithmic credentials
```

## Project Purpose

Real-time order flow analysis system for automated futures trading. Analyzes tick-by-tick market data to identify high-probability trading signals based on volume imbalances, exhaustion, absorption, and delta divergence patterns. Executes bracket orders with automated risk management.

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.10+ |
| Data Processing | Nautilus Trader, Pandas, NumPy |
| API | FastAPI, Uvicorn, Pydantic |
| WebSocket | websockets library |
| Database | SQLAlchemy, SQLite (live), PostgreSQL (optional) |
| Live Data | Rithmic API (async_rithmic 1.2.0+) |
| Historical Data | Databento (ES futures), Polygon.io (ETFs) |
| Notifications | Discord webhooks |
| Logging | Structlog |

## Architecture Overview

```
Tick Data (Rithmic/Databento)
    ↓
FootprintAggregator (5-min bars)
    ↓
OrderFlowEngine (Pattern Detection)
    ↓
StrategyRouter (Regime Filtering)
    ↓
ExecutionManager (Risk Management)
    ↓
ExecutionBridge (Rithmic Orders)
    ↓
Position Tracking & P&L
    ↓
FastAPI Dashboard + Discord Alerts
```

## Directory Structure

```
/opt/tradebot/
├── src/
│   ├── core/           # Types, constants, config, notifications, persistence
│   │   ├── types.py        # Tick, Signal, FootprintBar, Regime dataclasses
│   │   ├── constants.py    # Tick sizes, tick values, symbol profiles
│   │   ├── config.py       # Configuration management
│   │   ├── notifications.py # Discord webhook service
│   │   ├── persistence.py  # State save/restore for crash recovery
│   │   ├── scheduler.py    # Auto-flatten, daily digest
│   │   ├── capital.py      # Tier progression system
│   │   └── operations.py   # Headless operations manager
│   ├── data/           # Data adapters and aggregation
│   │   ├── adapters/       # databento.py, polygon.py, rithmic.py
│   │   ├── aggregator.py   # Tick to footprint bar conversion
│   │   ├── live_db.py      # Live/paper trade history (SQLite)
│   │   └── backtest_db.py  # Backtest result tracking
│   ├── analysis/       # Pattern detection engine
│   │   ├── engine.py       # Orchestrates tick processing & detection
│   │   ├── indicators.py   # EMA, SMA, ATR, ADX
│   │   └── detectors/      # imbalance, exhaustion, absorption, divergence
│   ├── regime/         # Market regime classification
│   │   ├── detector.py     # TRENDING/RANGING/VOLATILE/NO_TRADE
│   │   ├── router.py       # Signal filtering + position sizing
│   │   └── inputs.py       # Regime calculation from indicators
│   ├── execution/      # Order execution layer
│   │   ├── manager.py      # Trade execution, risk management, P&L
│   │   ├── bridge.py       # Rithmic broker integration
│   │   ├── orders.py       # Order, BracketOrder, Position types
│   │   └── session.py      # Trading session configuration
│   └── api/            # FastAPI dashboard
│       └── server.py       # REST + WebSocket endpoints
├── scripts/            # Utility scripts
│   ├── run_demo.py         # Simulated market data
│   ├── run_replay.py       # Historical replay
│   ├── run_databento_backtest.py  # Real ES backtesting
│   ├── run_tier_backtest.py       # Tier progression backtest with Discord
│   ├── advanced_backtest.py       # 22 test scenarios
│   ├── stress_tests.py     # Drawdown, Monte Carlo
│   ├── download_historical_data.py # Download tick data from Databento
│   ├── watchdog.py         # System health monitor (separate process)
│   ├── watchdog.service    # Systemd service for watchdog
│   ├── deploy_headless.sh  # Production deployment
│   └── preflight_check.py  # Pre-deployment validation
├── static/             # Dashboard HTML/CSS/JS
├── data/
│   ├── state/          # Persistence files (trading_state.json)
│   └── tick_cache/     # Cached backtest data
├── main.py             # CLI + web dashboard entry
├── run_headless.py     # Production headless entry
├── requirements.txt
└── .env                # Credentials (not in repo)
```

## Supported Markets

| Symbol | Name | Tick Size | Tick Value |
|--------|------|-----------|------------|
| ES | E-mini S&P 500 | 0.25 | $12.50 |
| MES | Micro E-mini S&P 500 | 0.25 | $1.25 |
| NQ | E-mini Nasdaq-100 | 0.25 | $5.00 |
| MNQ | Micro E-mini Nasdaq-100 | 0.25 | $0.50 |
| CL | Crude Oil | 0.01 | $10.00 |
| GC | Gold | 0.10 | $10.00 |

## Signal Patterns

| Pattern | Description | Direction |
|---------|-------------|-----------|
| BUY_IMBALANCE | 300%+ buy ratio diagonal | LONG |
| SELL_IMBALANCE | 300%+ sell ratio diagonal | SHORT |
| STACKED_BUY_IMBALANCE | 3+ consecutive buy imbalances | LONG |
| STACKED_SELL_IMBALANCE | 3+ consecutive sell imbalances | SHORT |
| BUYING_EXHAUSTION | Declining buy volume at highs | SHORT (reversal) |
| SELLING_EXHAUSTION | Declining sell volume at lows | LONG (reversal) |
| BUYING_ABSORPTION | Large passive bids absorbing selling | LONG |
| SELLING_ABSORPTION | Large passive offers absorbing buying | SHORT |
| BULLISH_DELTA_DIVERGENCE | Price/delta mismatch (bullish) | LONG |
| BEARISH_DELTA_DIVERGENCE | Price/delta mismatch (bearish) | SHORT |

## Capital Tier System

Progressive account growth with automatic instrument/position sizing:

| Tier | Balance | Instrument | Contracts | Daily Loss Limit |
|------|---------|------------|-----------|------------------|
| 1 | $0-$3,500 | MES | 1-3 | $100 |
| 2 | $3,500-$5,000 | ES | 1 | $400 |
| 3 | $5,000-$7,500 | ES | 1-2 | $400 |
| 4 | $7,500-$10,000 | ES | 1-3 | $500 |
| 5 | $10,000+ | ES | 1-3 | $500 |

Position sizing modifiers:
- Stacked signals (2+ patterns): +1 contract
- Trending regime: +1 contract
- Win streak (3+): +1 contract
- Loss streak (2+): -1 contract

## Configuration

### Required Environment Variables (Live Mode)
```bash
RITHMIC_USER=your_username
RITHMIC_PASSWORD=your_password
RITHMIC_ACCOUNT_ID=your_account_id
```

### Optional Environment Variables
```bash
POLYGON_API_KEY=your_key           # Historical replay
DATABENTO_API_KEY=your_key         # ES futures tick data
DISCORD_WEBHOOK_URL=https://...    # Trade alerts (required for headless)
TRADING_SYMBOL=MES                 # Default symbol
TRADING_MODE=paper                 # paper or live
DAILY_PROFIT_TARGET=500
DAILY_LOSS_LIMIT=-300

# Margin limits - pauses trades if margins exceed these (high volatility periods)
MES_MARGIN_LIMIT=50                # Pause if MES margin > $50 (normal ~$40)
ES_MARGIN_LIMIT=500                # Pause if ES margin > $500 (normal ~$300)
```

### Default Trading Parameters
- Stop Loss: 16 ticks (4 points)
- Take Profit: 24 ticks (6 points)
- Max Position Size: 2 contracts
- Max Concurrent Trades: 1
- Session: 9:30 AM - 3:45 PM ET
- No-trade: 12:00 PM - 1:00 PM ET (lunch)

## Running the System

### Development Mode (with Dashboard)
```bash
cd /opt/tradebot
source venv/bin/activate
python main.py --symbol MES --mode paper --port 8000
```
Dashboard at http://localhost:8000

### Production Headless Mode
```bash
sudo ./scripts/deploy_headless.sh
sudo systemctl start tradebot
sudo journalctl -u tradebot -f  # View logs
```
All status via Discord webhooks.

### Backtesting
```bash
source venv/bin/activate
PYTHONPATH=. python scripts/run_databento_backtest.py --date 2025-11-20
PYTHONPATH=. python scripts/advanced_backtest.py --test 17
```

## Backtesting Results (198 Days Total)

Comprehensive testing across 3 periods with 120 million ticks:

| Period | Days | Trades | Ending Balance | P&L | P&L/Day | Win Days |
|--------|------|--------|----------------|-----|---------|----------|
| Jan-Feb 2025 | 30 | 208 | $74,241 | +$71,741 | $2,391 | 77% |
| Mar-May 2025 | 59 | 423 | $175,548 | +$173,048 | $2,933 | 93% |
| Jul-Nov 2025 | 109 | 627 | $184,269 | +$181,769 | $1,668 | 81% |
| **TOTAL** | **198** | **1,258** | - | **+$426,558** | **$2,154** | **84%** |

Key findings:
- **All periods profitable** - no period produced a loss
- **Tier 5 reached quickly** - within 5-10 trading days
- **Recovery consistent** - all tier drops recovered within 1-2 days
- **Mar-May strongest** - 93% winning days, highest P&L/day
- **Results deterministic** - identical results on repeated runs

Best performing patterns:
1. SELLING_EXHAUSTION: 209 trades, 67% win, $56,898
2. SELLING_ABSORPTION: 145 trades, 78% win, $50,112
3. BUYING_ABSORPTION: 147 trades, 73% win, $47,010

## Key Integration Points

### Rithmic (Primary)
- Live market data streaming
- Order execution with server-side OCO
- Position reconciliation
- Account balance tracking

### Databento
- Real ES futures tick data
- ~$1.20/day for full RTH session
- ~750,000 ticks/day

### Discord
- Trade open/close alerts
- Session start/stop
- Daily digest at 4:00 PM ET
- Error notifications
- Tier change notifications
- Watchdog health alerts

## Critical Implementation Details

### Execution Layer (Recently Audited)
Fixed issues in Nov 2025:
1. Fill deduplication inside asyncio lock
2. Stores tick_value at Position entry for tier-change safety
3. flatten_all() checks result.get("success")
4. fill_id includes timestamp
5. _processed_fills capped at 1000 entries

### State Persistence
- File: `data/state/trading_state.json`
- Saves: positions, P&L, trades, session config
- Auto-backup before writes
- Recovery on startup

### Position P&L Calculation
Uses tick_size and tick_value captured at entry time, not current symbol settings. Prevents miscalculation when tier changes instrument mid-trade.

### Watchdog Monitor (NEW)
Separate process that monitors trading system health:
- **Location:** `scripts/watchdog.py`
- **Service:** `scripts/watchdog.service` (systemd)
- **Checks:** Process running, heartbeat freshness, memory, disk, connection status
- **Alert tiers:**
  - Tier 1 (CRITICAL): System down, can't connect, position stuck
  - Tier 2 (WARNING): No ticks, high memory, multiple reconnects
  - Tier 3 (DAILY): Summary at market close
- **Heartbeat file:** `data/heartbeat.json` (written every 30 seconds by run_headless.py)
- **Cooldown:** 15 minutes between duplicate alerts

### Heartbeat System
run_headless.py writes heartbeat to `data/heartbeat.json` every 30 seconds containing:
- timestamp, last_tick_time, tick_count
- feed_connected, reconnect_count
- daily_pnl, trade_count, open_positions
- is_halted, halt_reason
- tier_name, balance, mode, symbol

Watchdog reads this file to determine system health.

## Common Commands

```bash
# Activate virtual environment
source /opt/tradebot/venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Run tests
PYTHONPATH=. pytest tests/

# Check preflight
PYTHONPATH=. python scripts/preflight_check.py

# View service logs
sudo journalctl -u tradebot -f

# Restart service
sudo systemctl restart tradebot

# Start/manage watchdog
sudo systemctl start watchdog
sudo systemctl status watchdog
sudo journalctl -u watchdog -f

# Download historical data
PYTHONPATH=. python scripts/download_historical_data.py

# Run tier progression backtest with Discord notifications
PYTHONPATH=. python scripts/run_tier_backtest.py --week 2025-10-22 --discord
PYTHONPATH=. python scripts/run_tier_backtest.py --dates 2025-10-22,2025-10-23 --discord
```

## Network Connectivity

Rithmic server (tested 2025-12-01):
- Host: rituz00100.rithmic.com (38.79.0.86)
- Hops: ~15
- Latency: 18.7ms avg
- Packet Loss: 0%

## Documentation Files

- `README.md` - Setup and features overview
- `BACKTESTING.md` - 22 test scenarios and results (Tests 1-22)
- `ORDER_FLOW_PROJECT.md` - Detailed project specification
- `ORDER-FLOW-TRADING-SYSTEM.md` - Architecture deep-dive

## Data Coverage

| Period | Contract | Days | Ticks |
|--------|----------|------|-------|
| Jan 13 - Feb 21, 2025 | ESH5/MESH5 | 30 | ~11M |
| Mar 10 - May 30, 2025 | ESM5/MESM5 | 60 | ~19M |
| Jul 1 - Nov 28, 2025 | ESU5/ESZ5 | 109 | ~90M |
| **Total** | - | **199** | **~120M** |

Cache location: `data/tick_cache/` (~7.5 GB)

## Current Status

**Production-ready, awaiting Rithmic credentials.**

Completed:
- All pattern detectors implemented and tested
- Regime detection and signal routing
- Execution layer with risk management
- Capital tier progression system
- Headless deployment with Discord notifications
- 198-day backtesting validation across 3 contract periods
- Critical execution layer audit fixes
- Watchdog health monitoring system
- Historical data downloader for Databento
- Tier progression backtester with Discord integration
- Heartbeat system for watchdog monitoring

Pending:
- Rithmic API credentials configuration
- Live trading activation
- Deploy watchdog service alongside tradebot service

## Watchdog Configuration

Environment variables for watchdog (in `.env`):
```bash
DISCORD_WEBHOOK_URL=https://...     # Required
HEARTBEAT_STALE_MINUTES=5           # Alert if heartbeat older than this
MEMORY_WARNING_PERCENT=85           # Alert on high memory
DISK_CRITICAL_MB=500                # Alert on low disk
WATCHDOG_CHECK_INTERVAL=60          # Check every N seconds
```

Deploy watchdog:
```bash
sudo cp scripts/watchdog.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable watchdog
sudo systemctl start watchdog
```
