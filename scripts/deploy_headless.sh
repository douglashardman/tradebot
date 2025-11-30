#!/bin/bash
#
# deploy_headless.sh - Deploy headless trading bot on locked-down server
#
# For servers with:
#   - No exposed ports (SSH only)
#   - All status via Discord
#   - Auto-start on boot
#
# Usage:
#   ./scripts/deploy_headless.sh              # Full deploy
#   ./scripts/deploy_headless.sh --update     # Update code only
#   ./scripts/deploy_headless.sh --restart    # Restart service
#   ./scripts/deploy_headless.sh --logs       # View logs
#   ./scripts/deploy_headless.sh --status     # Check status
#

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
echo_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
echo_error() { echo -e "${RED}[ERROR]${NC} $1"; }
echo_step() { echo -e "${BLUE}[STEP]${NC} $1"; }

# Configuration
INSTALL_DIR="/opt/tradebot"
SERVICE_NAME="tradebot"
LOG_DIR="/var/log/tradebot"
USER="tradebot"
GROUP="tradebot"

# Get script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

# Parse arguments
ACTION="deploy"

while [[ $# -gt 0 ]]; do
    case $1 in
        --update)
            ACTION="update"
            shift
            ;;
        --restart)
            ACTION="restart"
            shift
            ;;
        --logs)
            ACTION="logs"
            shift
            ;;
        --status)
            ACTION="status"
            shift
            ;;
        --stop)
            ACTION="stop"
            shift
            ;;
        --start)
            ACTION="start"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [--update|--restart|--logs|--status|--stop|--start]"
            exit 0
            ;;
        *)
            echo_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Helper functions
check_root() {
    if [[ $EUID -ne 0 ]]; then
        echo_error "This script must be run as root (sudo)"
        exit 1
    fi
}

# Quick actions that don't need root
case $ACTION in
    logs)
        journalctl -u $SERVICE_NAME -f
        exit 0
        ;;
    status)
        systemctl status $SERVICE_NAME
        exit 0
        ;;
esac

# Actions that need root
check_root

case $ACTION in
    restart)
        echo_info "Restarting $SERVICE_NAME..."
        systemctl restart $SERVICE_NAME
        sleep 2
        systemctl status $SERVICE_NAME --no-pager
        exit 0
        ;;
    stop)
        echo_info "Stopping $SERVICE_NAME..."
        systemctl stop $SERVICE_NAME
        exit 0
        ;;
    start)
        echo_info "Starting $SERVICE_NAME..."
        systemctl start $SERVICE_NAME
        sleep 2
        systemctl status $SERVICE_NAME --no-pager
        exit 0
        ;;
    update)
        echo_info "Updating code from repository..."
        cd $INSTALL_DIR

        # Pull latest code (as tradebot user)
        sudo -u $USER git pull

        # Update dependencies
        sudo -u $USER $INSTALL_DIR/venv/bin/pip install -r requirements.txt --quiet

        # Restart service
        systemctl restart $SERVICE_NAME
        sleep 2

        echo_info "Update complete!"
        systemctl status $SERVICE_NAME --no-pager
        exit 0
        ;;
esac

# Full deployment
echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  Headless Trading Bot Deployment${NC}"
echo -e "${GREEN}  (No exposed ports, Discord-only status)${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""

# Step 1: Create user
echo_step "1/7: Creating service user..."
if ! id "$USER" &>/dev/null; then
    useradd -r -s /bin/false -m -d /opt/tradebot $USER
    echo_info "Created user: $USER"
else
    echo_info "User $USER already exists"
fi

# Step 2: Create directories
echo_step "2/7: Creating directories..."
mkdir -p $INSTALL_DIR
mkdir -p $LOG_DIR
mkdir -p $INSTALL_DIR/data/state
mkdir -p $INSTALL_DIR/data/tick_cache

# Step 3: Copy files
echo_step "3/7: Copying files to $INSTALL_DIR..."
rsync -av --exclude='.git' --exclude='venv' --exclude='__pycache__' \
    --exclude='*.pyc' --exclude='data/*.db' --exclude='data/tick_cache/*' \
    --exclude='.env' \
    "$REPO_DIR/" "$INSTALL_DIR/"

# Step 4: Create virtual environment and install dependencies
echo_step "4/7: Setting up Python environment..."
if [[ ! -d "$INSTALL_DIR/venv" ]]; then
    python3 -m venv $INSTALL_DIR/venv
fi

$INSTALL_DIR/venv/bin/pip install --upgrade pip --quiet
$INSTALL_DIR/venv/bin/pip install -r $INSTALL_DIR/requirements.txt --quiet
echo_info "Dependencies installed"

# Step 5: Create .env file
echo_step "5/7: Setting up configuration..."
if [[ ! -f "$INSTALL_DIR/.env" ]]; then
    cat > $INSTALL_DIR/.env << 'ENVEOF'
# =========================================
# HEADLESS TRADING BOT CONFIGURATION
# =========================================
# Edit this file with your credentials
# All fields marked REQUIRED must be set

# -----------------------------------------
# RITHMIC CREDENTIALS (REQUIRED for live)
# -----------------------------------------
RITHMIC_USER=your_rithmic_username
RITHMIC_PASSWORD=your_rithmic_password
RITHMIC_SERVER=rituz00100.rithmic.com:443
RITHMIC_SYSTEM_NAME=Rithmic Test

# -----------------------------------------
# DISCORD NOTIFICATIONS (REQUIRED)
# -----------------------------------------
# Create a webhook in your Discord server:
# Server Settings > Integrations > Webhooks > New Webhook
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK_ID/YOUR_WEBHOOK_TOKEN

# -----------------------------------------
# TRADING CONFIGURATION
# -----------------------------------------
TRADING_SYMBOL=MES
TRADING_MODE=paper
USE_RITHMIC=true

# -----------------------------------------
# RISK PARAMETERS
# -----------------------------------------
DAILY_PROFIT_TARGET=500
DAILY_LOSS_LIMIT=-300
MAX_POSITION_SIZE=1
STOP_LOSS_TICKS=16
TAKE_PROFIT_TICKS=24
FLATTEN_BEFORE_CLOSE_MINUTES=5

# -----------------------------------------
# DATABENTO (Optional - for paper trading)
# -----------------------------------------
# DATABENTO_API_KEY=your_databento_key
ENVEOF

    chmod 600 $INSTALL_DIR/.env
    echo_warn "IMPORTANT: You must edit $INSTALL_DIR/.env with your credentials!"
else
    echo_info ".env already exists, preserving it"
fi

# Step 6: Set permissions
echo_step "6/7: Setting permissions..."
chown -R $USER:$GROUP $INSTALL_DIR
chown -R $USER:$GROUP $LOG_DIR
chmod 600 $INSTALL_DIR/.env

# Step 7: Install and enable systemd service
echo_step "7/7: Installing systemd service..."

# Create service file
cat > /etc/systemd/system/$SERVICE_NAME.service << SERVICEEOF
[Unit]
Description=Headless Trading Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
Group=$GROUP
WorkingDirectory=$INSTALL_DIR
Environment="PATH=$INSTALL_DIR/venv/bin:/usr/bin"
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/run_headless.py
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

# Security hardening (no exposed ports needed)
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$INSTALL_DIR/data $LOG_DIR
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICEEOF

systemctl daemon-reload
systemctl enable $SERVICE_NAME

echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "NEXT STEPS:"
echo ""
echo "  1. Edit credentials:"
echo "     sudo nano $INSTALL_DIR/.env"
echo ""
echo "  2. Test the configuration (dry run):"
echo "     sudo -u $USER $INSTALL_DIR/venv/bin/python $INSTALL_DIR/run_headless.py --dry-run"
echo ""
echo "  3. Start the service:"
echo "     sudo systemctl start $SERVICE_NAME"
echo ""
echo "  4. Check status:"
echo "     sudo systemctl status $SERVICE_NAME"
echo ""
echo "  5. View logs:"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo ""
echo "SERVICE COMMANDS:"
echo "  $0 --start     Start the bot"
echo "  $0 --stop      Stop the bot"
echo "  $0 --restart   Restart the bot"
echo "  $0 --status    Check status"
echo "  $0 --logs      View live logs"
echo "  $0 --update    Pull latest code and restart"
echo ""
echo -e "${YELLOW}The bot will automatically start on boot.${NC}"
echo -e "${YELLOW}All status updates will be sent to Discord.${NC}"
echo ""
