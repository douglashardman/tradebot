"""State persistence for crash recovery and session continuity."""

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default state file location
DEFAULT_STATE_DIR = Path(__file__).parent.parent.parent / "data" / "state"
STATE_FILE = "trading_state.json"
BACKUP_FILE = "trading_state.backup.json"


class StatePersistence:
    """
    Persists trading state to disk for crash recovery.

    Saves:
    - Open positions
    - Daily P&L
    - Completed trades
    - Session configuration
    - Halt state

    Automatically saves state on each trade or position change.
    On startup, can restore from saved state.
    """

    def __init__(self, state_dir: Optional[Path] = None):
        """
        Initialize state persistence.

        Args:
            state_dir: Directory for state files. Defaults to data/state/
        """
        self.state_dir = Path(state_dir) if state_dir else DEFAULT_STATE_DIR
        self.state_file = self.state_dir / STATE_FILE
        self.backup_file = self.state_dir / BACKUP_FILE

        # Ensure directory exists
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def save_state(self, state: Dict[str, Any]) -> bool:
        """
        Save trading state to disk.

        Args:
            state: State dictionary to save.

        Returns:
            True if saved successfully.
        """
        try:
            # Backup existing state file first
            if self.state_file.exists():
                self.state_file.rename(self.backup_file)

            # Add metadata
            state["_saved_at"] = datetime.now().isoformat()
            state["_version"] = 1

            # Write new state
            with open(self.state_file, "w") as f:
                json.dump(state, f, indent=2, default=str)

            logger.debug(f"State saved to {self.state_file}")
            return True

        except Exception as e:
            logger.error(f"Failed to save state: {e}")
            # Try to restore backup if save failed
            if self.backup_file.exists():
                self.backup_file.rename(self.state_file)
            return False

    def load_state(self) -> Optional[Dict[str, Any]]:
        """
        Load trading state from disk.

        Returns:
            State dictionary or None if not found/invalid.
        """
        try:
            if not self.state_file.exists():
                logger.info("No saved state found")
                return None

            with open(self.state_file) as f:
                state = json.load(f)

            saved_at = state.get("_saved_at")
            logger.info(f"Loaded state from {saved_at}")

            return state

        except json.JSONDecodeError as e:
            logger.error(f"Corrupted state file: {e}")
            # Try backup
            return self._load_backup()
        except Exception as e:
            logger.error(f"Failed to load state: {e}")
            return self._load_backup()

    def _load_backup(self) -> Optional[Dict[str, Any]]:
        """Load from backup file."""
        try:
            if not self.backup_file.exists():
                return None

            with open(self.backup_file) as f:
                state = json.load(f)

            logger.warning("Loaded state from backup file")
            return state

        except Exception as e:
            logger.error(f"Failed to load backup: {e}")
            return None

    def clear_state(self) -> None:
        """Clear saved state (call after clean session end)."""
        try:
            if self.state_file.exists():
                self.state_file.unlink()
            if self.backup_file.exists():
                self.backup_file.unlink()
            logger.info("State files cleared")
        except Exception as e:
            logger.error(f"Failed to clear state: {e}")

    def has_saved_state(self) -> bool:
        """Check if there's saved state to restore."""
        return self.state_file.exists() or self.backup_file.exists()

    def get_state_age(self) -> Optional[float]:
        """
        Get age of saved state in seconds.

        Returns:
            Age in seconds or None if no state.
        """
        state = self.load_state()
        if not state or "_saved_at" not in state:
            return None

        try:
            saved_at = datetime.fromisoformat(state["_saved_at"])
            return (datetime.now() - saved_at).total_seconds()
        except Exception:
            return None


def serialize_positions(positions: List[Any]) -> List[Dict]:
    """Serialize position objects to dicts."""
    result = []
    for pos in positions:
        if hasattr(pos, "to_dict"):
            result.append(pos.to_dict())
        else:
            result.append({
                "symbol": getattr(pos, "symbol", ""),
                "side": getattr(pos, "side", ""),
                "size": getattr(pos, "size", 0),
                "entry_price": getattr(pos, "entry_price", 0),
                "entry_time": str(getattr(pos, "entry_time", "")),
                "stop_price": getattr(pos, "stop_price", 0),
                "target_price": getattr(pos, "target_price", 0),
            })
    return result


def serialize_trades(trades: List[Any]) -> List[Dict]:
    """Serialize trade objects to dicts."""
    result = []
    for trade in trades:
        if hasattr(trade, "to_dict"):
            result.append(trade.to_dict())
        else:
            result.append({
                "symbol": getattr(trade, "symbol", ""),
                "side": getattr(trade, "side", ""),
                "size": getattr(trade, "size", 0),
                "entry_price": getattr(trade, "entry_price", 0),
                "exit_price": getattr(trade, "exit_price", 0),
                "pnl": getattr(trade, "pnl", 0),
                "exit_reason": getattr(trade, "exit_reason", ""),
            })
    return result


# Global persistence instance
_persistence: Optional[StatePersistence] = None


def get_persistence() -> StatePersistence:
    """Get global persistence instance."""
    global _persistence
    if _persistence is None:
        _persistence = StatePersistence()
    return _persistence
