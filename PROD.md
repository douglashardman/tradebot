# Production Server Setup Guide

This document contains everything needed to rebuild the production trading server from scratch.

## Server Details

- **Provider**: Vultr (or similar VPS)
- **OS**: Ubuntu 24.04 LTS
- **Location**: US East (low latency to CME)
- **Specs**: 2 vCPU, 4GB RAM minimum

## Initial Server Setup

### 1. System Updates

```bash
apt update && apt upgrade -y
apt install -y python3.12 python3.12-venv git curl
```

### 2. Create Tradebot User (Optional)

```bash
useradd -m -s /bin/bash tradebot
usermod -aG sudo tradebot
```

### 3. Clone Repository

```bash
git clone https://github.com/your-username/tradebot.git /opt/tradebot
cd /opt/tradebot
```

### 4. Create Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Environment Configuration

### .env File

Create `/opt/tradebot/.env` with the following:

```bash
# =============================================================================
# TRADEBOT PRODUCTION CONFIGURATION
# =============================================================================

# -----------------------------------------------------------------------------
# Data Feed - Databento (Primary tick data source)
# -----------------------------------------------------------------------------
DATABENTO_API_KEY=db-xxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# -----------------------------------------------------------------------------
# Execution - Rithmic (Order execution only)
# -----------------------------------------------------------------------------
USE_RITHMIC=false  # Set to true when ready for live execution
RITHMIC_USER=your_username
RITHMIC_PASSWORD=your_password
RITHMIC_ACCOUNT_ID=your_account_id

# -----------------------------------------------------------------------------
# Notifications - Discord
# -----------------------------------------------------------------------------
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/xxx/yyy

# -----------------------------------------------------------------------------
# Trading Configuration
# -----------------------------------------------------------------------------
TRADING_SYMBOL=MES
TRADING_MODE=paper
STARTING_BALANCE=2500
DAILY_PROFIT_TARGET=100000  # Effectively disabled
DAILY_LOSS_LIMIT=-100

# Stop/Target Configuration
STOP_LOSS_TICKS=16
TAKE_PROFIT_TICKS=24

# -----------------------------------------------------------------------------
# Margin Protection
# -----------------------------------------------------------------------------
MES_MARGIN_LIMIT=50
ES_MARGIN_LIMIT=500
```

## Data Architecture

### Tick Data Flow

```
Databento Live API
       │
       ▼
┌──────────────────┐
│ Front-Month      │  Filters to MESZ5/ESZ5 only
│ Contract Filter  │  (skips MESH6, spreads, etc.)
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ Tick Logger      │  Accumulates in memory during day
│ (In-Memory)      │
└────────┬─────────┘
         │ Flush at EOD/shutdown
         ▼
┌──────────────────┐
│ Parquet Files    │  /opt/tradebot/data/ticks/YYYY-MM-DD.parquet
└────────┬─────────┘
         │ SCP at 11:01 PM ET
         ▼
┌──────────────────┐
│ Home Server      │  faded-vibes@99.69.168.225
│ Backup Storage   │  /home/faded-vibes/tradebot/data/tick_cache/
└──────────────────┘
```

### Front-Month Contract Logic

The system automatically determines the front-month contract based on:
- **March (H)**: Front month Jan-Feb
- **June (M)**: Front month Mar-May
- **September (U)**: Front month Jun-Aug
- **December (Z)**: Front month Sep-Nov

Current (December 2025): **MESZ5** and **ESZ5**

## SSH Key Setup for Tick Data Export

### On Production Server

The SSH key for automated tick data export is stored at:
- **Private key**: `/root/.ssh/tradebot_sync`
- **Public key**: `/root/.ssh/tradebot_sync.pub`

To regenerate (if rebuilding server):

```bash
ssh-keygen -t ed25519 -C "tradebot-prod-sync" -f /root/.ssh/tradebot_sync -N ""
cat /root/.ssh/tradebot_sync.pub
# Copy this output to home server's authorized_keys
```

### On Home Server (99.69.168.225)

Add the public key to `/home/faded-vibes/.ssh/authorized_keys`:

```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJc2H3oDyv3fr1fg6nDGktvrXZ25X+WwkTbaq2wxY3c7 tradebot-prod-sync
```

### Test Connection

```bash
ssh -i /root/.ssh/tradebot_sync faded-vibes@99.69.168.225 "echo 'Connection successful'"
```

## Tick Data Export Cron Job

### Export Script

Location: `/opt/tradebot/scripts/export_tick_data.sh`

The script:
1. Flushes any remaining ticks from memory to Parquet
2. SCPs today's Parquet file to home server
3. Logs the operation

### Cron Configuration

```bash
crontab -e
```

Add:
```
# Tick data export at 11:01 PM ET (Mon-Fri)
1 23 * * 1-5 /opt/tradebot/scripts/export_tick_data.sh >> /var/log/tick_export.log 2>&1
```

### Manual Export

```bash
/opt/tradebot/scripts/export_tick_data.sh
```

### Check Export Logs

```bash
tail -f /var/log/tick_export.log
```

## Systemd Services

### Main Trading Service

File: `/etc/systemd/system/tradebot.service`

```ini
[Unit]
Description=Tradebot Headless Trading System
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tradebot
Environment=PYTHONPATH=/opt/tradebot
ExecStart=/opt/tradebot/venv/bin/python run_headless.py --symbol MES --paper
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

### Watchdog Service

File: `/etc/systemd/system/tradebot-watchdog.service`

```ini
[Unit]
Description=Tradebot Watchdog Monitor
After=tradebot.service

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tradebot
Environment=PYTHONPATH=/opt/tradebot
ExecStart=/opt/tradebot/venv/bin/python scripts/watchdog.py
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

### Service Commands

```bash
# Enable services on boot
sudo systemctl enable tradebot
sudo systemctl enable tradebot-watchdog

# Start/stop/restart
sudo systemctl start tradebot
sudo systemctl stop tradebot
sudo systemctl restart tradebot

# View logs
sudo journalctl -u tradebot -f
sudo journalctl -u tradebot-watchdog -f

# Check status
sudo systemctl status tradebot
```

## Directory Structure

```
/opt/tradebot/
├── .env                    # Configuration (not in git)
├── run_headless.py         # Main headless entry point
├── src/
│   ├── data/
│   │   ├── adapters/
│   │   │   └── databento.py    # Live data feed
│   │   └── tick_logger.py      # Parquet storage
│   ├── core/
│   │   └── ...
│   └── ...
├── scripts/
│   ├── export_tick_data.sh     # Nightly export script
│   ├── watchdog.py             # Health monitor
│   └── deploy_headless.sh      # Deployment helper
├── data/
│   ├── ticks/                  # Parquet tick files
│   │   └── YYYY-MM-DD.parquet
│   ├── heartbeat.json          # Watchdog health file
│   ├── live_trades.db          # Trade database
│   └── tier_state.json         # Capital tier state
└── venv/                       # Python virtual environment
```

## Monitoring

### Heartbeat File

The system writes `/opt/tradebot/data/heartbeat.json` every tick:

```json
{
  "timestamp": "2025-12-01T17:20:26.932780",
  "last_tick_time": "2025-12-01T17:20:26.511362",
  "tick_count": 755,
  "bar_count": 1,
  "signal_count": 2,
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

### Quick Health Check

```bash
cat /opt/tradebot/data/heartbeat.json | python3 -m json.tool
```

### Check Running Process

```bash
ps aux | grep run_headless
```

## Troubleshooting

### Session Already Exists Error

If you see `sqlite3.IntegrityError: UNIQUE constraint failed: sessions.date`:

```bash
cd /opt/tradebot && source venv/bin/activate
python -c "
import sqlite3
conn = sqlite3.connect('data/live_trades.db')
cursor = conn.cursor()
cursor.execute(\"DELETE FROM sessions WHERE date = '$(date +%Y-%m-%d)'\")
print(f'Deleted {cursor.rowcount} session(s)')
conn.commit()
conn.close()
"
```

### Check Databento Connection

```bash
cd /opt/tradebot && source venv/bin/activate
python -c "
import databento as db
client = db.Historical()
print('Databento connection OK')
"
```

### View Recent Logs

```bash
# Last 100 lines of trading logs
journalctl -u tradebot -n 100

# Follow logs in real-time
journalctl -u tradebot -f
```

### Manual Start for Debugging

```bash
cd /opt/tradebot
source venv/bin/activate
PYTHONPATH=. python run_headless.py --symbol MES --paper
```

## Backup Procedures

### What to Backup

1. **Configuration**: `/opt/tradebot/.env`
2. **Trade Database**: `/opt/tradebot/data/live_trades.db`
3. **Tier State**: `/opt/tradebot/data/tier_state.json`
4. **SSH Keys**: `/root/.ssh/tradebot_sync*`

### Restore Procedure

1. Clone repo
2. Create venv and install requirements
3. Restore `.env` file
4. Restore SSH keys (or regenerate and update home server)
5. Enable and start systemd services

## Network Requirements

### Outbound Connections

- **Databento API**: `api.databento.com:443`
- **Discord Webhooks**: `discord.com:443`
- **Home Server SSH**: `99.69.168.225:22`
- **GitHub**: `github.com:443`

### No Inbound Ports Required

The system is fully headless with no exposed ports. All monitoring via Discord.

## Version History

| Date | Change |
|------|--------|
| 2025-12-01 | Initial production deployment |
| 2025-12-01 | Added front-month contract filtering |
| 2025-12-01 | Added tick data logging (Parquet) |
| 2025-12-01 | Added SCP export to home server |
