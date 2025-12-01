# Production Server Rebuild Guide

> **Purpose**: Complete, copy-paste rebuild of the tradebot production server.
> **Last Updated**: 2025-12-01
> **Estimated Time**: 30-45 minutes

## Lessons Learned (Why This Doc Exists)

The original deployment had these issues:
1. **Split-brain user situation**: systemd service ran as `tradebot` user while manual runs were as `root`
2. **Conflicting service files**: `/etc/systemd/system/tradebot.service` didn't match what PROD.md specified
3. **Watchdog confusion**: System watchdog vs custom trading watchdog
4. **Permission chaos**: Files owned by different users, restrictive sandboxing blocked operations
5. **SSH key location mismatch**: Export script expected `/home/tradebot/.ssh/` but key was in `/root/.ssh/`

**The fix**: Everything runs as root. Simple. No user switching, no permission issues, no sandboxing problems.

---

## Server Requirements

| Requirement | Value |
|-------------|-------|
| Provider | Vultr (or similar VPS) |
| OS | Ubuntu 24.04 LTS |
| Location | US East (low latency to CME) |
| CPU | 2+ vCPU |
| RAM | 4GB minimum |
| Storage | 40GB+ SSD |

---

## Phase 1: Initial Server Setup

### 1.1 SSH into fresh server as root

```bash
ssh root@YOUR_SERVER_IP
```

### 1.2 System updates

```bash
apt update && apt upgrade -y
apt install -y git curl python3.12 python3.12-venv
```

### 1.3 Set timezone

```bash
timedatectl set-timezone America/New_York
```

### 1.4 Set hostname (optional but helpful)

```bash
hostnamectl set-hostname tradebot-prod
```

---

## Phase 2: Clone and Setup Repository

### 2.1 Clone the repo

```bash
git clone git@github.com:douglashardman/tradebot.git /opt/tradebot
cd /opt/tradebot
```

> **Note**: If SSH key not set up yet, use HTTPS:
> ```bash
> git clone https://github.com/douglashardman/tradebot.git /opt/tradebot
> ```

### 2.2 Create virtual environment

```bash
cd /opt/tradebot
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

### 2.3 Create data directories

```bash
mkdir -p /opt/tradebot/data/ticks
mkdir -p /opt/tradebot/data/state
mkdir -p /var/log/tradebot
```

---

## Phase 3: Configuration

### 3.1 Create .env file

```bash
cat > /opt/tradebot/.env << 'EOF'
# =========================================
# HEADLESS TRADING BOT CONFIGURATION
# =========================================

# -----------------------------------------
# RITHMIC CREDENTIALS (REQUIRED for live)
# -----------------------------------------
RITHMIC_USER=
RITHMIC_PASSWORD=
RITHMIC_SERVER=rituz00100.rithmic.com:443
RITHMIC_SYSTEM_NAME=Rithmic Test
RITHMIC_ACCOUNT_ID=

# -----------------------------------------
# DISCORD NOTIFICATIONS (REQUIRED)
# -----------------------------------------
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/1444740791728607313/IgnvF4hmqZ6jomJYINxZGPHCNK47odGyjDHpx8_VuVazgMkSoao3gHqLi4CQx3NfxjJ8

# -----------------------------------------
# TRADING CONFIGURATION
# -----------------------------------------
TRADING_SYMBOL=MES
TRADING_MODE=paper
USE_RITHMIC=false

# -----------------------------------------
# CAPITAL MANAGEMENT
# -----------------------------------------
STARTING_BALANCE=2500

# -----------------------------------------
# RISK PARAMETERS
# -----------------------------------------
DAILY_PROFIT_TARGET=100000
DAILY_LOSS_LIMIT=-300
MAX_POSITION_SIZE=1
STOP_LOSS_TICKS=16
TAKE_PROFIT_TICKS=24
FLATTEN_BEFORE_CLOSE_MINUTES=5
COMMISSION_PER_CONTRACT=4.50

# -----------------------------------------
# DATABENTO (Primary tick data feed)
# -----------------------------------------
DATABENTO_API_KEY=db-p6WfdTmwQxiqeM3ScgwVJamhgBEB6

# -----------------------------------------
# POLYGON (Optional - for historical replay)
# -----------------------------------------
# POLYGON_API_KEY=
EOF

chmod 600 /opt/tradebot/.env
```

---

## Phase 4: SSH Key for Tick Data Export

### 4.1 Create SSH key for automated exports

```bash
mkdir -p /root/.ssh
ssh-keygen -t ed25519 -C "tradebot-prod-sync" -f /root/.ssh/tradebot_sync -N ""
```

### 4.2 Display public key (add to home server)

```bash
cat /root/.ssh/tradebot_sync.pub
```

**Copy this output and add it to the home server's authorized_keys:**

On home server (99.69.168.225):
```bash
echo "ssh-ed25519 AAAAC3... tradebot-prod-sync" >> /home/faded-vibes/.ssh/authorized_keys
```

### 4.3 Test SSH connection

```bash
ssh -i /root/.ssh/tradebot_sync -o StrictHostKeyChecking=no faded-vibes@99.69.168.225 "echo 'Connection successful'"
```

---

## Phase 5: Systemd Services

### 5.1 Create main tradebot service

```bash
cat > /etc/systemd/system/tradebot.service << 'EOF'
[Unit]
Description=Tradebot Headless Trading System
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tradebot
Environment=PYTHONPATH=/opt/tradebot
EnvironmentFile=/opt/tradebot/.env
ExecStart=/opt/tradebot/venv/bin/python run_headless.py --symbol MES --paper
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

### 5.2 Create watchdog service

```bash
cat > /etc/systemd/system/tradebot-watchdog.service << 'EOF'
[Unit]
Description=Tradebot Watchdog Monitor
After=network-online.target tradebot.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/tradebot
Environment=PYTHONPATH=/opt/tradebot
EnvironmentFile=/opt/tradebot/.env
ExecStart=/opt/tradebot/venv/bin/python scripts/watchdog.py
Restart=always
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

### 5.3 Enable and start services

```bash
systemctl daemon-reload
systemctl enable tradebot
systemctl enable tradebot-watchdog
systemctl start tradebot
systemctl start tradebot-watchdog
```

### 5.4 Verify services are running

```bash
systemctl status tradebot
systemctl status tradebot-watchdog
```

---

## Phase 6: Cron Jobs

### 6.1 Fix export script SSH key path

The export script needs to use root's SSH key:

```bash
sed -i 's|/home/tradebot/.ssh/tradebot_sync|/root/.ssh/tradebot_sync|g' /opt/tradebot/scripts/export_tick_data.sh
chmod +x /opt/tradebot/scripts/export_tick_data.sh
```

### 6.2 Setup cron for tick export

```bash
crontab -e
```

Add this line:
```
# Tick data export at 11:01 PM ET (Mon-Fri)
1 23 * * 1-5 /opt/tradebot/scripts/export_tick_data.sh >> /var/log/tick_export.log 2>&1
```

Or do it non-interactively:
```bash
(crontab -l 2>/dev/null; echo "1 23 * * 1-5 /opt/tradebot/scripts/export_tick_data.sh >> /var/log/tick_export.log 2>&1") | crontab -
```

---

## Phase 7: Verification

### 7.1 Check heartbeat (wait ~30 seconds after start)

```bash
cat /opt/tradebot/data/heartbeat.json | python3 -m json.tool
```

Expected output shows:
- `feed_connected: true`
- `tick_count` increasing
- `mode: "paper"`
- `symbol: "MES"`

### 7.2 Check logs

```bash
journalctl -u tradebot -f
```

Look for:
- "HEADLESS TRADING SYSTEM STARTUP"
- "Databento feed connected"
- Tick counts increasing

### 7.3 Check Discord

You should receive a startup notification in Discord.

### 7.4 Test manual export

```bash
/opt/tradebot/scripts/export_tick_data.sh
```

---

## Quick Reference Commands

```bash
# View trading logs
journalctl -u tradebot -f

# View watchdog logs
journalctl -u tradebot-watchdog -f

# Check heartbeat
cat /opt/tradebot/data/heartbeat.json | python3 -m json.tool

# Restart trading system
systemctl restart tradebot

# Stop trading system
systemctl stop tradebot

# Check running processes
ps aux | grep python

# Manual start for debugging
cd /opt/tradebot && source venv/bin/activate
PYTHONPATH=. python run_headless.py --symbol MES --paper

# Check tick data files
ls -la /opt/tradebot/data/ticks/

# Manual tick export
/opt/tradebot/scripts/export_tick_data.sh

# View export logs
tail -f /var/log/tick_export.log
```

---

## Troubleshooting

### "Not a trading day" on weekdays
Check timezone:
```bash
timedatectl
```
Should show `America/New_York`.

### Session already exists error
```bash
cd /opt/tradebot && source venv/bin/activate
python -c "
import sqlite3
from datetime import date
conn = sqlite3.connect('data/live_trades.db')
cursor = conn.cursor()
cursor.execute(\"DELETE FROM sessions WHERE date = ?\", (str(date.today()),))
print(f'Deleted {cursor.rowcount} session(s)')
conn.commit()
conn.close()
"
```

### No ticks coming in
1. Check Databento API key is valid
2. Check market is open (9:30 AM - 4:00 PM ET, Mon-Fri)
3. Check logs: `journalctl -u tradebot -n 100`

### Export failing
1. Test SSH: `ssh -i /root/.ssh/tradebot_sync faded-vibes@99.69.168.225 "echo ok"`
2. Check key path in export script
3. Ensure home server has public key in authorized_keys

---

## Network Requirements

### Outbound connections (all that's needed)
- `api.databento.com:443` - Tick data
- `discord.com:443` - Webhooks
- `99.69.168.225:22` - Home server for exports
- `github.com:443` - Code updates

### No inbound ports required
System is fully headless. All monitoring via Discord.

---

## Files to Backup

Before wiping/rebuilding, save:
1. `/opt/tradebot/.env` - Credentials
2. `/root/.ssh/tradebot_sync` - SSH private key
3. `/root/.ssh/tradebot_sync.pub` - SSH public key
4. `/opt/tradebot/data/live_trades.db` - Trade history (if any)
5. `/opt/tradebot/data/ticks/*.parquet` - Tick data (if not exported)

---

## Architecture Summary

```
                    ┌─────────────────────────────────────┐
                    │         Production Server           │
                    │         (runs as root)              │
                    └─────────────────────────────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         │                           │                           │
         ▼                           ▼                           ▼
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│ tradebot.service│       │ tradebot-watchdog│      │   cron job      │
│                 │       │    .service      │       │ (11:01 PM ET)   │
│ run_headless.py │       │  watchdog.py     │       │ export_tick_data│
└────────┬────────┘       └────────┬─────────┘       └────────┬────────┘
         │                         │                          │
         │ writes                  │ reads                    │ scps
         ▼                         ▼                          ▼
┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐
│ heartbeat.json  │◄──────│ Health checks   │       │  Home Server    │
│ ticks/*.parquet │       │ Discord alerts  │       │ 99.69.168.225   │
│ live_trades.db  │       └─────────────────┘       └─────────────────┘
└─────────────────┘
         │
         │ alerts
         ▼
┌─────────────────┐
│    Discord      │
│   Webhooks      │
└─────────────────┘
```

---

## Version History

| Date | Change |
|------|--------|
| 2025-12-01 | Initial rebuild doc created after split-brain incident |
