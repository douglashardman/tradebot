#!/bin/bash
#
# Export tick data to remote server via SCP
#
# This script:
# 1. Flushes today's tick data to Parquet
# 2. SCPs the file to a remote server
# 3. Logs the operation
#
# Configure via environment variables or edit below:
#   TICK_EXPORT_HOST    - Remote server hostname/IP
#   TICK_EXPORT_USER    - SSH username
#   TICK_EXPORT_PATH    - Destination path on remote
#   TICK_EXPORT_KEY     - SSH key path (optional)
#
# Cron example (run at 11:01 PM ET):
#   1 23 * * 1-5 /opt/tradebot/scripts/export_tick_data.sh >> /var/log/tick_export.log 2>&1
#

set -e

# Configuration - override via environment or edit here
TICK_EXPORT_HOST="${TICK_EXPORT_HOST:-99.69.168.225}"
TICK_EXPORT_USER="${TICK_EXPORT_USER:-faded-vibes}"
TICK_EXPORT_PATH="${TICK_EXPORT_PATH:-/home/faded-vibes/tradebot/data/tick_cache}"
TICK_EXPORT_KEY="${TICK_EXPORT_KEY:-/root/.ssh/tradebot_sync}"

# Local paths
TRADEBOT_DIR="/opt/tradebot"
TICK_DATA_DIR="${TRADEBOT_DIR}/data/ticks"
VENV_PYTHON="${TRADEBOT_DIR}/venv/bin/python"
LOG_FILE="/var/log/tick_export.log"

# Get today's date
TODAY=$(date +%Y-%m-%d)

echo "=========================================="
echo "Tick Data Export - $(date)"
echo "=========================================="

# First, flush any remaining ticks to Parquet
echo "Flushing tick data to Parquet..."
cd "${TRADEBOT_DIR}"
${VENV_PYTHON} -c "
from src.data.tick_logger import get_tick_logger
logger = get_tick_logger()
paths = logger.flush_all()
if paths:
    print(f'Flushed: {paths}')
else:
    print('No data to flush')
" || echo "Warning: Flush command failed (system may not be running)"

# Check if today's file exists
PARQUET_FILE="${TICK_DATA_DIR}/${TODAY}.parquet"

if [[ ! -f "${PARQUET_FILE}" ]]; then
    echo "No tick data file for ${TODAY}"
    echo "Looking for any recent files..."
    ls -la "${TICK_DATA_DIR}/"*.parquet 2>/dev/null || echo "No parquet files found"
    exit 0
fi

# Get file size
FILE_SIZE=$(du -h "${PARQUET_FILE}" | cut -f1)
echo "Found ${PARQUET_FILE} (${FILE_SIZE})"

# Build SCP command
SCP_OPTS=""
if [[ -n "${TICK_EXPORT_KEY}" ]]; then
    SCP_OPTS="-i ${TICK_EXPORT_KEY}"
fi

DEST="${TICK_EXPORT_USER}@${TICK_EXPORT_HOST}:${TICK_EXPORT_PATH}/"

echo "Uploading to ${DEST}..."

# Perform SCP
if scp ${SCP_OPTS} "${PARQUET_FILE}" "${DEST}"; then
    echo "SUCCESS: Uploaded ${TODAY}.parquet to ${TICK_EXPORT_HOST}"

    # Optional: Verify remote file
    if ssh ${SCP_OPTS} "${TICK_EXPORT_USER}@${TICK_EXPORT_HOST}" "ls -la ${TICK_EXPORT_PATH}/${TODAY}.parquet" 2>/dev/null; then
        echo "Verified file exists on remote server"
    fi
else
    echo "ERROR: SCP failed"
    exit 1
fi

echo "Export complete"
echo ""
