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

**The fix**: Everything runs as the `tradebot` user consistently. Code lives in `/opt/tradebot`, SSH keys in `/home/tradebot/.ssh/`, cron jobs under the `tradebot` user.

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

## Architecture Overview

- **Data Feed**: Databento (institutional-grade, faster than Rithmic)
- **Order Execution**: Rithmic (when enabled)
- **All services run as**: `tradebot` user
- **Code location**: `/opt/tradebot`
- **SSH keys**: `/home/tradebot/.ssh/`

```
                    ┌─────────────────────────────────────┐
                    │         Production Server           │
                    │      (runs as tradebot user)        │
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

## Phase 1: Initial Server Setup & Hardening

### 1.1 SSH into fresh server as root

```bash
ssh root@YOUR_SERVER_IP
```

### 1.2 System updates and hardening

```bash
# Update system
apt update && apt upgrade -y
apt install -y git curl python3.12 python3.12-venv unattended-upgrades fail2ban

# Enable automatic security updates
dpkg-reconfigure -plow unattended-upgrades --frontend=noninteractive

# Set timezone
timedatectl set-timezone America/New_York

# Configure UFW firewall
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh
ufw --force enable

# Configure fail2ban for SSH
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime = 1h
findtime = 10m
maxretry = 5
banaction = ufw

[sshd]
enabled = true
port = ssh
filter = sshd
logpath = /var/log/auth.log
maxretry = 3
bantime = 24h
EOF

systemctl enable fail2ban
systemctl restart fail2ban
```

### 1.3 Create tradebot user (if not exists)

```bash
useradd -m -s /bin/bash tradebot
```

### 1.4 Set up SSH key access for tradebot user

```bash
mkdir -p /home/tradebot/.ssh
# Add your public key:
echo "ssh-ed25519 AAAA... your-email@example.com" > /home/tradebot/.ssh/authorized_keys
chmod 700 /home/tradebot/.ssh
chmod 600 /home/tradebot/.ssh/authorized_keys
chown -R tradebot:tradebot /home/tradebot/.ssh
```

---

## Phase 2: Clone and Setup Repository

### 2.1 Generate deploy key for GitHub

```bash
sudo -u tradebot ssh-keygen -t ed25519 -C "tradebot-deploy-key" -f /home/tradebot/.ssh/id_ed25519 -N ""
cat /home/tradebot/.ssh/id_ed25519.pub
```

Add the public key to GitHub: Repo → Settings → Deploy keys

### 2.2 Clone the repo

```bash
sudo -u tradebot ssh-keyscan github.com >> /home/tradebot/.ssh/known_hosts
sudo -u tradebot git clone git@github.com:douglashardman/tradebot.git /home/tradebot/tradebot-tmp
sudo mv /home/tradebot/tradebot-tmp/* /home/tradebot/tradebot-tmp/.* /opt/tradebot/ 2>/dev/null
sudo rmdir /home/tradebot/tradebot-tmp
sudo chown -R tradebot:tradebot /opt/tradebot
```

### 2.3 Create virtual environment

```bash
sudo -u tradebot python3.12 -m venv /opt/tradebot/venv
sudo -u tradebot /opt/tradebot/venv/bin/pip install --upgrade pip
sudo -u tradebot /opt/tradebot/venv/bin/pip install -r /opt/tradebot/requirements.txt
sudo -u tradebot /opt/tradebot/venv/bin/pip install psutil  # For watchdog
```

### 2.4 Create data directories

```bash
mkdir -p /opt/tradebot/data/ticks
mkdir -p /opt/tradebot/data/state
mkdir -p /var/log/tradebot
chown -R tradebot:tradebot /opt/tradebot/data /var/log/tradebot
```

---

## Phase 3: Configuration

### 3.1 Create .env file

```bash
sudo -u tradebot tee /opt/tradebot/.env << 'EOF'
# =========================================
# HEADLESS TRADING BOT CONFIGURATION
# =========================================

# -----------------------------------------
# RITHMIC CREDENTIALS (for order execution)
# -----------------------------------------
RITHMIC_USER=
RITHMIC_PASSWORD=
RITHMIC_SERVER=rituz00100.rithmic.com:443
RITHMIC_SYSTEM_NAME=Rithmic Test
RITHMIC_ACCOUNT_ID=
USE_RITHMIC=false

# -----------------------------------------
# DISCORD NOTIFICATIONS
# -----------------------------------------
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_HERE

# -----------------------------------------
# TRADING CONFIGURATION
# -----------------------------------------
TRADING_SYMBOL=MES
TRADING_MODE=paper

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
DATABENTO_API_KEY=YOUR_DATABENTO_KEY_HERE
EOF

chmod 600 /opt/tradebot/.env
```

---

## Phase 4: SSH Key for Tick Data Export

### 4.1 Create SSH key for automated exports

```bash
sudo -u tradebot ssh-keygen -t ed25519 -C "tradebot-prod-sync" -f /home/tradebot/.ssh/tradebot_sync -N ""
cat /home/tradebot/.ssh/tradebot_sync.pub
```

### 4.2 Add public key to home server

On home server (99.69.168.225):
```bash
echo "ssh-ed25519 AAAAC3... tradebot-prod-sync" >> /home/faded-vibes/.ssh/authorized_keys
```

### 4.3 Test SSH connection

```bash
sudo -u tradebot ssh -i /home/tradebot/.ssh/tradebot_sync -o StrictHostKeyChecking=accept-new faded-vibes@99.69.168.225 "echo 'Connection successful'"
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
User=tradebot
Group=tradebot
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
User=tradebot
Group=tradebot
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
systemctl enable tradebot tradebot-watchdog
systemctl start tradebot tradebot-watchdog
```

### 5.4 Verify services are running

```bash
systemctl status tradebot tradebot-watchdog
```

---

## Phase 6: Cron Jobs

### 6.1 Setup cron for tick export (as tradebot user)

```bash
echo "# Tick data export at 11:01 PM ET (Mon-Fri)
1 23 * * 1-5 /opt/tradebot/scripts/export_tick_data.sh >> /var/log/tradebot/tick_export.log 2>&1" | sudo -u tradebot crontab -
```

### 6.2 Verify crontab

```bash
sudo -u tradebot crontab -l
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
sudo -u tradebot /opt/tradebot/scripts/export_tick_data.sh
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
sudo systemctl restart tradebot

# Stop trading system
sudo systemctl stop tradebot

# Check running processes
ps aux | grep python

# Manual start for debugging (as tradebot user)
sudo -u tradebot bash -c 'cd /opt/tradebot && source venv/bin/activate && PYTHONPATH=. python run_headless.py --symbol MES --paper'

# Check tick data files
ls -la /opt/tradebot/data/ticks/

# Manual tick export
sudo -u tradebot /opt/tradebot/scripts/export_tick_data.sh

# View export logs
tail -f /var/log/tradebot/tick_export.log

# Pull latest code
cd /opt/tradebot && sudo -u tradebot git pull
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
sudo -u tradebot /opt/tradebot/venv/bin/python -c "
import sqlite3
from datetime import date
conn = sqlite3.connect('/opt/tradebot/data/live_trades.db')
cursor = conn.cursor()
cursor.execute(\"DELETE FROM sessions WHERE date = ?\", (str(date.today()),))
print(f'Deleted {cursor.rowcount} session(s)')
conn.commit()
conn.close()
"
```

### No ticks coming in
1. Check Databento API key is valid
2. Check market is open (futures trade Sun 6 PM - Fri 5 PM ET, with daily halt 5-6 PM ET)
3. Check logs: `journalctl -u tradebot -n 100`

### Export failing
1. Test SSH: `sudo -u tradebot ssh -i /home/tradebot/.ssh/tradebot_sync faded-vibes@99.69.168.225 "echo ok"`
2. Check key path in export script (should be `/home/tradebot/.ssh/tradebot_sync`)
3. Ensure home server has public key in authorized_keys

---

## Network Requirements

### Outbound connections (all that's needed)
- `api.databento.com:443` - Tick data
- `discord.com:443` - Webhooks
- `99.69.168.225:22` - Home server for exports
- `github.com:22` - Code updates (SSH)

### No inbound ports required
System is fully headless. Only SSH (port 22) is open for admin access.

---

## Files to Backup

Before wiping/rebuilding, save:
1. `/opt/tradebot/.env` - Credentials
2. `/home/tradebot/.ssh/tradebot_sync` - SSH private key for exports
3. `/home/tradebot/.ssh/tradebot_sync.pub` - SSH public key
4. `/home/tradebot/.ssh/id_ed25519` - Deploy key private
5. `/opt/tradebot/data/live_trades.db` - Trade history (if any)
6. `/opt/tradebot/data/ticks/*.parquet` - Tick data (if not exported)

---

## Version History

| Date | Change |
|------|--------|
| 2025-12-01 | Updated to use `tradebot` user consistently (no more root) |
| 2025-12-01 | Added server hardening (UFW, fail2ban, SSH hardening) |
| 2025-12-01 | Fixed SSH key paths to `/home/tradebot/.ssh/` |
| 2025-12-01 | Initial rebuild doc created after split-brain incident |
