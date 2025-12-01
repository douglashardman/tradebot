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

- **Historical Replay & Backtesting**
  - Polygon.io integration for ETF data (SPY, QQQ - free tier)
  - Databento integration for real ES futures tick data
  - Replay any trading day at configurable speed
  - Batch backtesting across multiple dates
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

## Headless Production Deployment

For locked-down servers with no exposed ports. All status via Discord webhooks.

### Quick Deploy

```bash
# On your server
git clone <repo> /opt/tradebot
cd /opt/tradebot
sudo ./scripts/deploy_headless.sh

# Edit credentials
sudo nano /opt/tradebot/.env

# Start
sudo systemctl start tradebot
```

### Required Environment Variables

```bash
# Rithmic (for live trading)
RITHMIC_USER=your_username
RITHMIC_PASSWORD=your_password
RITHMIC_ACCOUNT_ID=your_account_id  # Required for live mode

# Discord (required - all status goes here)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy

# Trading config
TRADING_SYMBOL=MES
TRADING_MODE=paper  # paper or live
DAILY_PROFIT_TARGET=500

# Margin protection (optional - uses these defaults)
MES_MARGIN_LIMIT=50    # Pause trading if MES margin > $50
ES_MARGIN_LIMIT=500    # Pause trading if ES margin > $500
```

### Margin Protection

The system checks margin requirements before each trade. On high-volatility periods (FOMC, CPI, elections), brokers temporarily increase margins. When margins exceed the configured limits:

1. New trades are paused (existing positions unaffected)
2. Discord alert sent when margins spike
3. Discord alert sent when margins normalize
4. Trading resumes automatically when margins return to normal

This handles temporary margin spikes (e.g., 9:30am-12pm during Fed announcements) without shutting down the entire day.

Normal margins: MES ~$40, ES ~$300. The system uses $50/$500 as thresholds to provide a buffer.

### Trading Modes

| Mode | Description |
|------|-------------|
| `paper` | Paper trading with simulated fills. Uses Rithmic/Databento for data only. |
| `live` | Live trading with real order execution via Rithmic. Requires RITHMIC_ACCOUNT_ID. |

**IMPORTANT:** Live mode will execute REAL trades with REAL money. Test thoroughly in paper mode first.

### What You'll Receive on Discord

| Event | Notification |
|-------|--------------|
| System reboot | Server restarted alert |
| System start | Session config and start time |
| High margin day | Not trading due to elevated margins |
| Trade opened | Entry, stop, target prices |
| Trade closed | P&L and exit reason |
| Tier change | Account promoted/demoted to new tier |
| Connection lost | Alert with timestamp |
| Connection restored | Confirmation |
| Loss limit hit | Session halted alert |
| Profit target hit | Success notification |
| 3:55 PM | Auto-flatten notification |
| 4:00 PM | Full daily digest with stats |
| Watchdog alerts | System health issues (stale heartbeat, etc.) |

### Service Commands

```bash
# Main trading service
sudo systemctl start tradebot
sudo systemctl stop tradebot
sudo systemctl restart tradebot
sudo journalctl -u tradebot -f          # View logs

# Watchdog health monitor
sudo systemctl start tradebot-watchdog
sudo systemctl status tradebot-watchdog
sudo journalctl -u tradebot-watchdog -f

# Deploy script (alternative)
sudo ./scripts/deploy_headless.sh --start    # Start
sudo ./scripts/deploy_headless.sh --stop     # Stop
sudo ./scripts/deploy_headless.sh --restart  # Restart
sudo ./scripts/deploy_headless.sh --logs     # View logs
sudo ./scripts/deploy_headless.sh --update   # Pull latest & restart
```

### Watchdog Monitor

A separate watchdog process monitors system health and sends alerts:

- **Heartbeat monitoring**: Alerts if main process stops updating
- **Memory/disk checks**: Warns on resource issues
- **Connection tracking**: Monitors feed reconnects
- **Daily summary**: EOD health report

The watchdog runs as `tradebot-watchdog.service` and reads heartbeat data from `data/heartbeat.json`.

### Health Check (No Port Needed)

Run manually via SSH:
```bash
sudo -u tradebot /opt/tradebot/venv/bin/python -c "
from src.core.persistence import get_persistence
state = get_persistence().load_state()
print(state)
"
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
│   │   ├── config.py      # Configuration management
│   │   ├── notifications.py # Discord webhook alerts
│   │   ├── persistence.py # State save/restore for crash recovery
│   │   ├── scheduler.py   # Auto-flatten & scheduled tasks
│   │   └── operations.py  # Headless operations manager
│   ├── data/
│   │   ├── adapters/      # Data feed adapters
│   │   │   ├── polygon.py # Polygon.io historical replay
│   │   │   ├── databento.py # Databento live feed
│   │   │   └── rithmic.py # Rithmic live feed (production)
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
│       └── server.py      # FastAPI server + WebSocket + /health
├── scripts/
│   ├── run_demo.py        # Demo with simulated data
│   ├── run_replay.py      # Historical replay
│   ├── deploy_headless.sh # Headless server deployment
│   ├── preflight_check.py # Pre-production verification
│   └── tradebot.service   # Systemd service file
├── static/                # Dashboard HTML/CSS/JS
├── tests/                 # Unit tests
├── main.py                # CLI entry point (with web dashboard)
├── run_headless.py        # Headless entry point (no web server)
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

### Polygon.io (ETF Replay - Free Tier)
- Free tier available with delayed data
- Use SPY/QQQ as ES proxy for backtesting
- Minute bar data converted to simulated ticks
- Sign up at https://polygon.io

### Databento (ES Futures Tick Data)
- Real ES futures tick-level data
- Pay-per-use model (~$1.20/day for full session)
- Historical and real-time available
- Sign up at https://databento.com

```python
# Example: Backtest ES with Databento
from src.data.adapters.databento import DatabentoAdapter

adapter = DatabentoAdapter()
contract = DatabentoAdapter.get_front_month_contract("ES")  # ESZ5

ticks = adapter.get_session_ticks(
    contract=contract,
    date="2025-11-20",
    start_time="09:30",
    end_time="16:00"
)
# Returns ~750,000 ticks for full RTH session
```

### Databento Cost Estimates
| Duration | Approx Ticks | Cost |
|----------|--------------|------|
| 1 hour | ~115,000 | ~$0.18 |
| Full day (6.5h) | ~750,000 | ~$1.20 |
| 1 week (5 days) | ~3.75M | ~$6.00 |
| 1 month (20 days) | ~15M | ~$24.00 |

## Backtesting Results (198 Days: Jan-Nov 2025)

Tier progression backtest starting from $2,500 with the capital management system across 3 contract periods.

### Overall Performance
| Metric | Value |
|--------|-------|
| Total Trading Days | 198 |
| Total P&L | +$426,558 |
| Avg Daily P&L | $2,154 |
| Total Trades | 1,258 |
| Winning Days | 84% |
| Tier Drops | 3 (all recovered within 1-2 days) |

### Period Breakdown
| Period | Days | Trades | P&L | P&L/Day | Win Days |
|--------|------|--------|-----|---------|----------|
| Jan-Feb 2025 | 30 | 208 | +$71,741 | $2,391 | 77% |
| Mar-May 2025 | 59 | 423 | +$173,048 | $2,933 | 93% |
| Jul-Nov 2025 | 109 | 627 | +$181,769 | $1,668 | 81% |

### Key Findings
- All periods profitable - no period produced a loss
- Tier 5 reached within 5-10 trading days in all periods
- Mar-May strongest: 93% winning days, highest P&L/day
- Results deterministic - identical on repeated runs

### Performance by Pattern
| Pattern | Trades | Win% | Net P&L |
|---------|--------|------|---------|
| SELLING_EXHAUSTION | 209 | 67% | $56,898 |
| SELLING_ABSORPTION | 145 | 78% | $50,112 |
| BUYING_ABSORPTION | 147 | 73% | $47,010 |
| BUYING_EXHAUSTION | 104 | 74% | $34,560 |

See `BACKTESTING.md` for comprehensive 22-test analysis including stress tests and parameter optimization.

### Running Backtests

```bash
# Run backtest for a single day
PYTHONPATH=. python scripts/run_databento_backtest.py --date 2025-11-20

# Run batch backtests
PYTHONPATH=. python scripts/run_batch_backtest.py

# View backtest database summary
PYTHONPATH=. python -c "from src.data.backtest_db import print_summary; print_summary()"
```

## Stress Testing

The system includes comprehensive stress tests to validate robustness:

```bash
# Run all stress tests
PYTHONPATH=. python scripts/stress_tests.py

# Individual tests
PYTHONPATH=. python scripts/stress_tests.py --slippage      # 1-2 tick slippage
PYTHONPATH=. python scripts/stress_tests.py --time-of-day   # Hourly breakdown
PYTHONPATH=. python scripts/stress_tests.py --day-of-week   # Monday-Friday analysis
PYTHONPATH=. python scripts/stress_tests.py --monte-carlo   # 1000 randomizations
```

## Execution System Audit (November 2025)

The execution layer underwent a comprehensive code audit with 7 critical/high issues identified and fixed:

### Issues Fixed

| ID | Severity | Issue | Fix |
|----|----------|-------|-----|
| C1 | Critical | Fill deduplication race condition | Moved check inside asyncio lock |
| C2 | Critical | Wrong attribute name in tier change | Fixed `self.execution_manager` → `self.manager` |
| C3 | Critical | flatten_all() return check always truthy | Check `result.get("success")` instead |
| C4 | Critical | Tier change P&L miscalculation | Store tick values on Position at entry |
| H1 | High | fill_id collision for same-price fills | Added timestamp to fill_id generation |
| H2 | High | Unbounded _processed_fills memory growth | Added max size limit with auto-clear |
| H3 | High | Order submission bypass task tracking | Use tracked submission method |

### Key Changes

**`src/execution/bridge.py`**
- Fill deduplication now atomic inside lock (lines 154-166)
- fill_id includes timestamp for uniqueness
- _processed_fills bounded to 1000 entries
- Position creation captures tick_size/tick_value

**`src/execution/manager.py`**
- Position stores tick values at entry time
- _close_position uses position's tick values for P&L
- Protects against tier change mid-trade miscalculation

**`src/execution/orders.py`**
- Position dataclass includes tick_size/tick_value fields
- update_pnl uses captured values when available

**`run_headless.py`**
- Fixed attribute reference for tier changes
- Fixed flatten return value handling
- Order submission uses tracked method

## License

MIT
