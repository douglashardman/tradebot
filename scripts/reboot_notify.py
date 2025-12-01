#!/usr/bin/env python3
"""
Send Discord notification on system reboot.
Run once at startup via systemd.
"""

import os
import sys
import json
import socket
import subprocess
from datetime import datetime
from urllib.request import Request, urlopen
from urllib.error import URLError

# Load .env file
env_path = "/opt/tradebot/.env"
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ[key] = value

WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL")

def get_system_info():
    """Gather system information."""
    info = {
        "hostname": socket.gethostname(),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "uptime": "unknown",
        "ip": "unknown",
    }

    # Get uptime
    try:
        with open("/proc/uptime") as f:
            uptime_seconds = float(f.read().split()[0])
            info["uptime"] = f"{int(uptime_seconds)} seconds"
    except:
        pass

    # Get IP
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            info["ip"] = result.stdout.strip().split()[0]
    except:
        pass

    return info

def send_notification():
    """Send reboot notification to Discord."""
    if not WEBHOOK_URL:
        print("ERROR: DISCORD_WEBHOOK_URL not set")
        sys.exit(1)

    info = get_system_info()

    embed = {
        "title": "🔄 System Rebooted",
        "description": (
            f"**Hostname:** {info['hostname']}\n"
            f"**Time:** {info['time']}\n"
            f"**IP:** {info['ip']}\n"
            f"**Uptime:** {info['uptime']}\n\n"
            "Services starting up..."
        ),
        "color": 3447003,  # Blue
        "footer": {"text": "Tradebot System Monitor"},
    }

    payload = json.dumps({"embeds": [embed]}).encode("utf-8")

    try:
        req = Request(
            WEBHOOK_URL,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Tradebot/1.0",
            },
            method="POST",
        )
        with urlopen(req, timeout=10) as resp:
            if resp.status in (200, 204):
                print("Reboot notification sent successfully")
            else:
                print(f"Discord returned status {resp.status}")
    except URLError as e:
        print(f"Failed to send notification: {e}")
        sys.exit(1)

if __name__ == "__main__":
    send_notification()
