# CLAUDE.md - Order Flow Trading System

> This file provides Claude with comprehensive context about the tradebot project.
> Last updated: 2025-12-01 (tick data rollover at 5 PM ET)

## Quick Reference

```
Location: /opt/tradebot
Repo: git@github.com:douglashardman/tradebot.git
Python: 3.12 with venv at /opt/tradebot/venv
Primary Symbol: MES (Micro E-mini S&P 500)
Server: Vultr VPS, Ubuntu 24.04 LTS
Status: LIVE IN PRODUCTION (paper trading mode)
```

## Project Purpose

Real-time order flow analysis system for automated futures trading. Analyzes tick-by-tick market data to identify high-probability trading signals based on volume imbalances, exhaustion, absorption, and delta divergence patterns. Executes bracket orders with automated risk management.

## Architecture Overview

**Key Design Decision**: Databento for data feed (institutional-grade, faster), Rithmic only for order execution. This architecture supports future multi-tenant scaling with a single authoritative data source.

```
Tick Data (Databento - Primary)
    ↓
FootprintAggregator (5-min bars)
    ↓
OrderFlowEngine (Pattern Detection)
    ↓
StrategyRouter (Regime Filtering)
    ↓
ExecutionManager (Risk Management)
    ↓
ExecutionBridge (Rithmic Orders - when enabled)
    ↓
Position Tracking & P&L
    ↓
Discord Alerts (all notifications)
```

## Production Environment

### Server Details
- **Provider**: Vultr VPS
- **OS**: Ubuntu 24.04 LTS
- **Kernel**: 6.8.0-88-generic
- **Timezone**: America/New_York (EST/EDT)
- **User**: `tradebot` (all services run as this user)

### Security Hardening
- UFW firewall (SSH only, all other inbound blocked)
- fail2ban (24h ban after 3 failed SSH attempts)
- SSH key-only authentication (password auth disabled)
- Automatic security updates enabled

### Services (systemd)
| Service | Description | Status |
|---------|-------------|--------|
| `tradebot.service` | Main trading system | Enabled, auto-start |
| `tradebot-watchdog.service` | Health monitor | Enabled, auto-start |

Both services run as `tradebot` user and auto-restart on failure.

### Key Paths
```
/opt/tradebot/              # Code and venv
/opt/tradebot/.env          # Credentials (chmod 600)
/opt/tradebot/data/         # Runtime data
/opt/tradebot/data/ticks/   # Parquet tick files
/opt/tradebot/data/state/   # Persistence files
/home/tradebot/.ssh/        # SSH keys (deploy + export)
/var/log/tradebot/          # Log files
```

## Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.12 |
| Data Processing | Nautilus Trader, Pandas, NumPy |
| Live Data | **Databento** (primary tick feed) |
| Order Execution | Rithmic API (async_rithmic) - when enabled |
| Database | SQLite (live_trades.db) |
| Notifications | Discord webhooks |
| Logging | Structlog |

## Data Feed Architecture

### Databento (Primary - Always Used)
- Institutional-grade tick data
- Lower latency than Rithmic for data
- ~$1.20/day for full RTH session
- ~750,000 ticks/day for ES/MES
- Subscribed symbols: ES.FUT, MES.FUT (front-month auto-detected)

### Rithmic (Order Execution Only)
- Used ONLY for placing/managing orders
- NOT used for market data
- Credentials pending (~48h)
- Server: rituz00100.rithmic.com:443

## Configuration

### Environment Variables (.env)
```bash
# Databento (REQUIRED - primary data feed)
DATABENTO_API_KEY=db-xxxxx

# Discord (REQUIRED - all notifications)
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...

# Rithmic (for order execution - pending)
RITHMIC_USER=
RITHMIC_PASSWORD=
RITHMIC_ACCOUNT_ID=
USE_RITHMIC=false

# Trading Config
TRADING_SYMBOL=MES
TRADING_MODE=paper
STARTING_BALANCE=2500
DAILY_LOSS_LIMIT=-300

# Warmup (loads historical data on startup)
WARMUP_HOURS=3.0  # Hours of history to load (0 to disable)
```

### Historical Warmup
On startup, the system loads recent historical tick data from Databento and processes it to build bar history. This ensures the regime detector has enough data (21+ bars) to make accurate classifications immediately.

- Default: 3 hours of history (~36 bars at 5-min timeframe)
- Set `WARMUP_HOURS=0` to disable (start cold)
- Warmup happens before live feed connects
- Discord notification shows starting regime and confidence

### Trading Parameters
- Stop Loss: 16 ticks (4 points)
- Take Profit: 24 ticks (6 points)
- Max Position Size: Based on tier
- Session: 9:30 AM - 3:45 PM ET
- Futures trade: Sun 6 PM - Fri 5 PM ET (daily halt 5-6 PM ET)

## Capital Tier System

| Tier | Balance | Instrument | Contracts | Daily Loss Limit |
|------|---------|------------|-----------|------------------|
| 1 | $0-$3,500 | MES | 1-3 | $100 |
| 2 | $3,500-$5,000 | ES | 1 | $400 |
| 3 | $5,000-$7,500 | ES | 1-2 | $400 |
| 4 | $7,500-$10,000 | ES | 1-3 | $500 |
| 5 | $10,000+ | ES | 1-3 | $500 |

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

## Common Commands

```bash
# View trading logs
journalctl -u tradebot -f

# View watchdog logs
journalctl -u tradebot-watchdog -f

# Check heartbeat (system health)
cat /opt/tradebot/data/heartbeat.json | python3 -m json.tool

# Restart trading system
sudo systemctl restart tradebot

# Stop trading system
sudo systemctl stop tradebot

# Check service status
systemctl status tradebot tradebot-watchdog

# Pull latest code
cd /opt/tradebot && sudo -u tradebot git pull

# Manual start for debugging
sudo -u tradebot bash -c 'cd /opt/tradebot && source venv/bin/activate && PYTHONPATH=. python run_headless.py --symbol MES --paper'

# Check tick data files
ls -la /opt/tradebot/data/ticks/

# Install new dependencies
sudo -u tradebot /opt/tradebot/venv/bin/pip install <package>
```

## Heartbeat System

`run_headless.py` writes to `data/heartbeat.json` every 30 seconds:
```json
{
  "timestamp": "2025-12-01T14:12:56",
  "last_tick_time": "2025-12-01T14:12:55",
  "tick_count": 948,
  "bar_count": 0,
  "signal_count": 0,
  "feed_connected": true,
  "reconnect_count": 1,
  "daily_pnl": 0.0,
  "trade_count": 0,
  "open_positions": 0,
  "is_halted": false,
  "tier_name": "Tier 1: MES Building",
  "balance": 2500.0,
  "mode": "paper",
  "symbol": "MES"
}
```

## Session Restart Handling

The system handles restarts gracefully:
- `get_or_create_session()` resumes existing daily session on restart
- No manual intervention needed for reboots/crashes
- Trades and P&L tracked continuously within the day

## Tick Data Export & Daily Recap

Automated export to home server during daily futures halt:
- **Schedule**: 5:01 PM ET, Mon-Fri (cron under tradebot user)
- **Destination**: faded-vibes@99.69.168.225:/home/faded-vibes/tradebot/data/tick_cache/
- **SSH Key**: /home/tradebot/.ssh/tradebot_sync

### Trading Day Rollover
Tick data files roll at **5 PM ET** (during daily halt) instead of midnight:
- Aligns with futures trading day (6 PM - 5 PM ET)
- A trading day file contains: previous day 5 PM ET → current day 5 PM ET
- Export runs at 5:01 PM ET, just after rollover

### Files Exported
| File | Format | Contents |
|------|--------|----------|
| `YYYY-MM-DD.parquet` | Parquet | Raw tick data (symbol, price, size, side, timestamp) |
| `YYYY-MM-DD_recap.json` | JSON | Daily session summary (see below) |

### Daily Recap Contents
The recap JSON includes:
- **Session info**: mode, symbol, tier, status
- **Heartbeat stats**: tick count, bar count, signal count, P&L
- **All signals**: detected signals with strength and accept/reject status
- **All bars**: completed 5-min bars with OHLC, volume, delta
- **Trades**: entry/exit prices, P&L, duration
- **Errors/warnings**: any issues from the day

### Manual Recap Generation
```bash
# Generate recap for today
sudo -u tradebot /opt/tradebot/venv/bin/python /opt/tradebot/scripts/daily_recap.py

# Generate recap for specific date (no export)
sudo -u tradebot /opt/tradebot/venv/bin/python /opt/tradebot/scripts/daily_recap.py --date 2025-12-01 --no-export
```

## Watchdog Monitor

Separate process monitoring system health:
- Checks every 60 seconds
- Alerts via Discord on:
  - Trading process not running (CRITICAL)
  - Heartbeat stale > 5 min (WARNING)
  - High memory > 85% (WARNING)
  - Low disk < 500 MB (CRITICAL)
- 15-minute cooldown between duplicate alerts

## Backtesting Results (198 Days)

| Period | Days | Trades | P&L | P&L/Day | Win Days |
|--------|------|--------|-----|---------|----------|
| Jan-Feb 2025 | 30 | 208 | +$71,741 | $2,391 | 77% |
| Mar-May 2025 | 59 | 423 | +$173,048 | $2,933 | 93% |
| Jul-Nov 2025 | 109 | 627 | +$181,769 | $1,668 | 81% |
| **TOTAL** | **198** | **1,258** | **+$426,558** | **$2,154** | **84%** |

## Current Status

**PRODUCTION LIVE** - Paper trading mode with Databento feed.

### Operational
- Databento tick feed connected and streaming
- Pattern detection running
- Discord notifications working
- Watchdog monitoring active
- Auto-restart on reboot working
- Session restart handling fixed

### Pending
- Rithmic credentials (~48h) for live order execution
- Switch from paper to live mode when ready

## Documentation Files

- `CLAUDE.md` - This file (Claude context)
- `PROD-REBUILD.md` - Complete server rebuild guide
- `BACKTESTING.md` - 22 test scenarios and results
- `README.md` - Project overview

## Troubleshooting

### No ticks coming in
1. Check Databento API key: `grep DATABENTO /opt/tradebot/.env`
2. Check market hours (futures: Sun 6 PM - Fri 5 PM ET)
3. Check logs: `journalctl -u tradebot -n 100`

### Service won't start
1. Check logs: `journalctl -u tradebot -n 50`
2. Session errors are now auto-handled (get_or_create_session)
3. Try manual start to see full error output

### SSH issues
- All SSH keys in `/home/tradebot/.ssh/`
- Deploy key: `id_ed25519` (for git pull)
- Export key: `tradebot_sync` (for tick data export)
- Password auth is disabled server-wide
