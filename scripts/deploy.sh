#!/bin/bash
#
# deploy.sh - Deploy tradebot on a new machine
#
# Usage: ./scripts/deploy.sh
#
# This script:
#   1. Installs system dependencies (Python, etc.)
#   2. Creates Python virtual environment
#   3. Installs Python packages
#   4. Sets up environment variables
#   5. Installs systemd service for auto-start
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
SERVICE_NAME="tradebot"
PYTHON_VERSION="python3"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  Tradebot Deployment Script${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "Project directory: $PROJECT_DIR"
echo ""

# Check if running as root (we'll need sudo for some parts)
if [ "$EUID" -eq 0 ]; then
    echo -e "${RED}Please don't run this script as root.${NC}"
    echo "It will ask for sudo when needed."
    exit 1
fi

# Function to check command exists
command_exists() {
    command -v "$1" >/dev/null 2>&1
}

# ===========================================
# Step 1: Install system dependencies
# ===========================================
echo -e "${YELLOW}Step 1: Checking system dependencies...${NC}"

PACKAGES_NEEDED=""

if ! command_exists python3; then
    PACKAGES_NEEDED="$PACKAGES_NEEDED python3"
fi

if ! command_exists pip3; then
    PACKAGES_NEEDED="$PACKAGES_NEEDED python3-pip"
fi

# Check for python3-venv
if ! $PYTHON_VERSION -m venv --help >/dev/null 2>&1; then
    PACKAGES_NEEDED="$PACKAGES_NEEDED python3-venv"
fi

if [ -n "$PACKAGES_NEEDED" ]; then
    echo "Installing system packages:$PACKAGES_NEEDED"
    sudo apt-get update
    sudo apt-get install -y $PACKAGES_NEEDED
else
    echo "  All system dependencies already installed."
fi

# ===========================================
# Step 2: Create virtual environment
# ===========================================
echo ""
echo -e "${YELLOW}Step 2: Setting up Python virtual environment...${NC}"

cd "$PROJECT_DIR"

if [ ! -d "venv" ]; then
    echo "  Creating virtual environment..."
    $PYTHON_VERSION -m venv venv
else
    echo "  Virtual environment already exists."
fi

# Activate venv
source venv/bin/activate

# Upgrade pip
echo "  Upgrading pip..."
pip install --upgrade pip --quiet

# ===========================================
# Step 3: Install Python dependencies
# ===========================================
echo ""
echo -e "${YELLOW}Step 3: Installing Python dependencies...${NC}"

pip install -r requirements.txt

echo "  Dependencies installed."

# ===========================================
# Step 4: Set up environment variables
# ===========================================
echo ""
echo -e "${YELLOW}Step 4: Setting up environment variables...${NC}"

if [ ! -f ".env" ]; then
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo ""
        echo -e "${YELLOW}  Created .env from template.${NC}"
        echo "  Please edit .env and add your API keys:"
        echo ""
        echo "    nano $PROJECT_DIR/.env"
        echo ""
        echo "  Required keys:"
        echo "    - DATABENTO_API_KEY (for live data)"
        echo "    - POLYGON_API_KEY (optional, for historical replay)"
        echo ""
        read -p "  Would you like to edit .env now? (y/n) [y]: " EDIT_ENV
        EDIT_ENV=${EDIT_ENV:-y}
        if [[ "$EDIT_ENV" =~ ^[Yy]$ ]]; then
            ${EDITOR:-nano} .env
        fi
    else
        echo -e "${RED}  Warning: No .env.example found.${NC}"
        echo "  You'll need to create .env manually."
    fi
else
    echo "  .env already exists."
fi

# ===========================================
# Step 5: Create data directories
# ===========================================
echo ""
echo -e "${YELLOW}Step 5: Creating data directories...${NC}"

mkdir -p data/tick_cache
mkdir -p logs

echo "  Directories created."

# ===========================================
# Step 6: Install systemd service
# ===========================================
echo ""
echo -e "${YELLOW}Step 6: Setting up systemd service...${NC}"

read -p "Install systemd service for auto-start? (y/n) [y]: " INSTALL_SERVICE
INSTALL_SERVICE=${INSTALL_SERVICE:-y}

if [[ "$INSTALL_SERVICE" =~ ^[Yy]$ ]]; then
    # Get current user
    CURRENT_USER=$(whoami)

    # Create service file
    SERVICE_FILE="/tmp/${SERVICE_NAME}.service"
    cat > "$SERVICE_FILE" << EOF
[Unit]
Description=Tradebot Order Flow Trading System
After=network.target

[Service]
Type=simple
User=$CURRENT_USER
WorkingDirectory=$PROJECT_DIR
Environment="PATH=$PROJECT_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$PROJECT_DIR/venv/bin/python main.py --mode paper
Restart=always
RestartSec=10

# Logging
StandardOutput=append:$PROJECT_DIR/logs/tradebot.log
StandardError=append:$PROJECT_DIR/logs/tradebot-error.log

# Security
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$PROJECT_DIR/data $PROJECT_DIR/logs

[Install]
WantedBy=multi-user.target
EOF

    # Install service
    sudo cp "$SERVICE_FILE" /etc/systemd/system/${SERVICE_NAME}.service
    sudo systemctl daemon-reload
    sudo systemctl enable ${SERVICE_NAME}

    echo ""
    echo "  Service installed and enabled."
    echo ""
    echo "  Service commands:"
    echo "    sudo systemctl start $SERVICE_NAME    # Start the bot"
    echo "    sudo systemctl stop $SERVICE_NAME     # Stop the bot"
    echo "    sudo systemctl restart $SERVICE_NAME  # Restart the bot"
    echo "    sudo systemctl status $SERVICE_NAME   # Check status"
    echo "    journalctl -u $SERVICE_NAME -f        # View logs"
    echo ""

    read -p "  Start the service now? (y/n) [n]: " START_NOW
    START_NOW=${START_NOW:-n}
    if [[ "$START_NOW" =~ ^[Yy]$ ]]; then
        sudo systemctl start ${SERVICE_NAME}
        sleep 2
        sudo systemctl status ${SERVICE_NAME} --no-pager
    fi
else
    echo "  Skipping systemd service installation."
    echo ""
    echo "  To run manually:"
    echo "    cd $PROJECT_DIR"
    echo "    source venv/bin/activate"
    echo "    python main.py --mode paper"
fi

# ===========================================
# Complete!
# ===========================================
echo ""
echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  Deployment Complete!${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "Project location: $PROJECT_DIR"
echo ""
echo "Quick commands:"
echo "  cd $PROJECT_DIR && source venv/bin/activate"
echo ""
echo "  # Run manually"
echo "  python main.py --mode paper"
echo ""
echo "  # Or use systemd"
echo "  sudo systemctl start $SERVICE_NAME"
echo ""
echo "  # View dashboard"
echo "  http://$(hostname -I | awk '{print $1}'):8000"
echo ""
