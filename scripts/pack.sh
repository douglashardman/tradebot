#!/bin/bash
#
# pack.sh - Package the tradebot for deployment to a new machine
#
# Usage: ./scripts/pack.sh [output_name]
#
# Creates a tarball with:
#   - Source code
#   - Data (tick cache, database)
#   - Config templates
#   - Deploy scripts
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
OUTPUT_NAME="${1:-tradebot-deploy}"
OUTPUT_FILE="${OUTPUT_NAME}-$(date +%Y%m%d-%H%M%S).tar.gz"

cd "$PROJECT_DIR"

echo "=========================================="
echo "  Tradebot Pack Script"
echo "=========================================="
echo ""
echo "Project directory: $PROJECT_DIR"
echo "Output file: $OUTPUT_FILE"
echo ""

# Check what we're packaging
echo "Checking data sizes..."
TICK_CACHE_SIZE=$(du -sh data/tick_cache 2>/dev/null | cut -f1 || echo "0")
DB_SIZE=$(du -sh data/backtests.db 2>/dev/null | cut -f1 || echo "0")
echo "  - Tick cache: $TICK_CACHE_SIZE"
echo "  - Database: $DB_SIZE"
echo ""

# Ask about including data
read -p "Include tick cache data? (y/n) [y]: " INCLUDE_CACHE
INCLUDE_CACHE=${INCLUDE_CACHE:-y}

read -p "Include backtest database? (y/n) [y]: " INCLUDE_DB
INCLUDE_DB=${INCLUDE_DB:-y}

# Build the file list
FILES=(
    # Core source
    "src/"
    "scripts/"
    "main.py"
    "requirements.txt"
    "pyproject.toml"

    # Dashboard
    "dashboard/"
    "static/"

    # Documentation
    "README.md"
    "ORDER-FLOW-TRADING-SYSTEM.md"

    # Config templates
    ".env.example"
    ".gitignore"
)

# Conditionally add data
if [[ "$INCLUDE_CACHE" =~ ^[Yy]$ ]]; then
    FILES+=("data/tick_cache/")
fi

if [[ "$INCLUDE_DB" =~ ^[Yy]$ ]]; then
    FILES+=("data/backtests.db")
fi

# Create temporary directory structure for clean packaging
TEMP_DIR=$(mktemp -d)
PACK_DIR="$TEMP_DIR/tradebot"
mkdir -p "$PACK_DIR"

echo "Copying files..."
for item in "${FILES[@]}"; do
    if [ -e "$item" ]; then
        # Create parent directory if needed
        parent=$(dirname "$item")
        if [ "$parent" != "." ]; then
            mkdir -p "$PACK_DIR/$parent"
        fi
        cp -r "$item" "$PACK_DIR/$item"
        echo "  + $item"
    else
        echo "  - $item (not found, skipping)"
    fi
done

# Ensure data directory exists
mkdir -p "$PACK_DIR/data"
mkdir -p "$PACK_DIR/logs"

# Create the tarball
echo ""
echo "Creating archive..."
cd "$TEMP_DIR"
tar -czvf "$PROJECT_DIR/$OUTPUT_FILE" tradebot/

# Cleanup
rm -rf "$TEMP_DIR"

# Final output
FINAL_SIZE=$(du -h "$PROJECT_DIR/$OUTPUT_FILE" | cut -f1)
echo ""
echo "=========================================="
echo "  Pack Complete!"
echo "=========================================="
echo ""
echo "Output: $OUTPUT_FILE"
echo "Size: $FINAL_SIZE"
echo ""
echo "To deploy on the new machine:"
echo ""
echo "  1. Copy this file to the new machine:"
echo "     scp $OUTPUT_FILE user@newmachine:~/"
echo ""
echo "  2. On the new machine, extract and run deploy:"
echo "     tar -xzf $OUTPUT_FILE"
echo "     cd tradebot"
echo "     ./scripts/deploy.sh"
echo ""
