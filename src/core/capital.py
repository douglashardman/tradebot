"""
Capital and tier management for progressive account growth.

Tiers:
- Tier 1: $2,500 - $3,500 | MES | 1-3 contracts | $100 loss limit
- Tier 2: $3,500 - $5,000 | ES  | 1 contract   | $400 loss limit
- Tier 3: $5,000 - $7,500 | ES  | 1-2 contracts | $400 loss limit
- Tier 4: $7,500 - $10,000| ES  | 1-3 contracts | $500 loss limit
- Tier 5: $10,000+        | ES  | 1-3 contracts | $500 loss limit

Scaling logic (combined/additive):
- Base: 1 contract
- Stacked signals (2+ patterns): +1 contract
- Trending regime: +1 contract
- Win streak (3+): +1 contract
- Loss streak (2+): -1 contract
"""

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Tier definitions
TIERS = [
    {
        "name": "Tier 1: MES Building",
        "min_balance": 0,
        "max_balance": 3500,
        "instrument": "MES",
        "base_contracts": 1,
        "max_contracts": 3,
        "daily_loss_limit": -100,
        "scaling_enabled": True,
    },
    {
        "name": "Tier 2: ES Entry",
        "min_balance": 3500,
        "max_balance": 5000,
        "instrument": "ES",
        "base_contracts": 1,
        "max_contracts": 1,
        "daily_loss_limit": -400,
        "scaling_enabled": False,
    },
    {
        "name": "Tier 3: ES Growth",
        "min_balance": 5000,
        "max_balance": 7500,
        "instrument": "ES",
        "base_contracts": 1,
        "max_contracts": 2,
        "daily_loss_limit": -400,
        "scaling_enabled": True,
    },
    {
        "name": "Tier 4: ES Scaling",
        "min_balance": 7500,
        "max_balance": 10000,
        "instrument": "ES",
        "base_contracts": 1,
        "max_contracts": 3,
        "daily_loss_limit": -500,
        "scaling_enabled": True,
    },
    {
        "name": "Tier 5: ES Full",
        "min_balance": 10000,
        "max_balance": float('inf'),
        "instrument": "ES",
        "base_contracts": 1,
        "max_contracts": 3,
        "daily_loss_limit": -500,
        "scaling_enabled": True,
    },
]


@dataclass
class TierState:
    """Current tier and balance state."""
    balance: float = 2500.0
    tier_index: int = 0
    tier_name: str = "Tier 1: MES Building"
    instrument: str = "MES"
    max_contracts: int = 3
    daily_loss_limit: float = -100.0
    scaling_enabled: bool = True

    # Session tracking
    session_start_balance: float = 2500.0
    session_pnl: float = 0.0

    # Streak tracking for combined sizing
    win_streak: int = 0
    loss_streak: int = 0

    # History
    tier_changes: List[Dict[str, Any]] = field(default_factory=list)


class TierManager:
    """
    Manages capital tiers and position sizing.

    Features:
    - Automatic tier progression based on balance
    - Instrument switching (MES ↔ ES)
    - Combined position sizing (stacked + regime + streak)
    - Balance persistence
    - Discord notifications on tier changes
    """

    def __init__(
        self,
        starting_balance: float = 2500.0,
        state_file: Optional[Path] = None,
        on_tier_change: Optional[Callable] = None,
    ):
        """
        Initialize tier manager.

        Args:
            starting_balance: Initial account balance.
            state_file: Path to persist state (optional).
            on_tier_change: Callback when tier changes (for Discord alerts).
        """
        self.state_file = state_file or Path("data/tier_state.json")
        self.on_tier_change = on_tier_change

        # Try to load existing state
        loaded_state = self._load_state()
        if loaded_state:
            self.state = loaded_state
            logger.info(f"Loaded tier state: {self.state.tier_name} @ ${self.state.balance:,.2f}")
        else:
            self.state = TierState(balance=starting_balance)
            self._update_tier()
            logger.info(f"Initialized new tier state: {self.state.tier_name} @ ${self.state.balance:,.2f}")

    def _load_state(self) -> Optional[TierState]:
        """Load state from file."""
        try:
            if self.state_file.exists():
                with open(self.state_file) as f:
                    data = json.load(f)
                return TierState(
                    balance=data.get("balance", 2500.0),
                    tier_index=data.get("tier_index", 0),
                    tier_name=data.get("tier_name", ""),
                    instrument=data.get("instrument", "MES"),
                    max_contracts=data.get("max_contracts", 3),
                    daily_loss_limit=data.get("daily_loss_limit", -100.0),
                    scaling_enabled=data.get("scaling_enabled", True),
                    session_start_balance=data.get("session_start_balance", 2500.0),
                    session_pnl=data.get("session_pnl", 0.0),
                    win_streak=data.get("win_streak", 0),
                    loss_streak=data.get("loss_streak", 0),
                    tier_changes=data.get("tier_changes", []),
                )
        except Exception as e:
            logger.error(f"Failed to load tier state: {e}")
        return None

    def save_state(self) -> None:
        """Save state to file."""
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.state_file, "w") as f:
                json.dump({
                    "balance": self.state.balance,
                    "tier_index": self.state.tier_index,
                    "tier_name": self.state.tier_name,
                    "instrument": self.state.instrument,
                    "max_contracts": self.state.max_contracts,
                    "daily_loss_limit": self.state.daily_loss_limit,
                    "scaling_enabled": self.state.scaling_enabled,
                    "session_start_balance": self.state.session_start_balance,
                    "session_pnl": self.state.session_pnl,
                    "win_streak": self.state.win_streak,
                    "loss_streak": self.state.loss_streak,
                    "tier_changes": self.state.tier_changes,
                    "_saved_at": datetime.now().isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save tier state: {e}")

    def _update_tier(self) -> bool:
        """
        Update tier based on current balance.

        Returns:
            True if tier changed, False otherwise.
        """
        old_tier_index = self.state.tier_index
        old_instrument = self.state.instrument

        # Find appropriate tier
        for i, tier in enumerate(TIERS):
            if tier["min_balance"] <= self.state.balance < tier["max_balance"]:
                self.state.tier_index = i
                self.state.tier_name = tier["name"]
                self.state.instrument = tier["instrument"]
                self.state.max_contracts = tier["max_contracts"]
                self.state.daily_loss_limit = tier["daily_loss_limit"]
                self.state.scaling_enabled = tier["scaling_enabled"]
                break

        # Check if tier changed
        if self.state.tier_index != old_tier_index:
            change = {
                "timestamp": datetime.now().isoformat(),
                "from_tier": old_tier_index,
                "to_tier": self.state.tier_index,
                "from_instrument": old_instrument,
                "to_instrument": self.state.instrument,
                "balance": self.state.balance,
            }
            self.state.tier_changes.append(change)

            direction = "UP" if self.state.tier_index > old_tier_index else "DOWN"
            logger.info(
                f"TIER CHANGE {direction}: {TIERS[old_tier_index]['name']} → {self.state.tier_name} "
                f"(Balance: ${self.state.balance:,.2f})"
            )

            # Trigger callback for Discord notification
            if self.on_tier_change:
                self.on_tier_change(change)

            return True

        return False

    def start_session(self) -> Dict[str, Any]:
        """
        Start a new trading session.

        Returns:
            Session configuration dict.
        """
        self.state.session_start_balance = self.state.balance
        self.state.session_pnl = 0.0

        # Update tier in case balance changed between sessions
        self._update_tier()
        self.save_state()

        return {
            "balance": self.state.balance,
            "instrument": self.state.instrument,
            "max_contracts": self.state.max_contracts,
            "daily_loss_limit": self.state.daily_loss_limit,
            "scaling_enabled": self.state.scaling_enabled,
            "tier_name": self.state.tier_name,
        }

    def end_session(self, session_pnl: float = None) -> Dict[str, Any]:
        """
        End trading session.

        Note: Balance is already updated by record_trade() calls during the session.
        This method just finalizes the session state and checks for tier changes.

        Args:
            session_pnl: Optional P&L for the session (for logging only, NOT added to balance).
                         If not provided, uses the tracked session_pnl.

        Returns:
            Summary dict.
        """
        # Use tracked session_pnl if not provided
        if session_pnl is None:
            session_pnl = self.state.session_pnl

        # Balance is already updated by record_trade() - don't add again!
        # Just record the final session_pnl for reference
        old_session_start = self.state.session_start_balance

        # Streaks are already updated per-trade in record_trade()
        # No need to update them again here

        # Check for tier change (in case any edge cases)
        tier_changed = self._update_tier()
        self.save_state()

        return {
            "old_balance": old_session_start,
            "new_balance": self.state.balance,
            "session_pnl": session_pnl,
            "tier_changed": tier_changed,
            "new_tier": self.state.tier_name if tier_changed else None,
            "new_instrument": self.state.instrument if tier_changed else None,
            "win_streak": self.state.win_streak,
            "loss_streak": self.state.loss_streak,
        }

    def record_trade(self, pnl: float) -> None:
        """
        Record a completed trade and update balance.

        Args:
            pnl: Trade P&L.
        """
        self.state.balance += pnl
        self.state.session_pnl += pnl

        # Update streaks based on trade
        if pnl > 0:
            self.state.win_streak += 1
            self.state.loss_streak = 0
        elif pnl < 0:
            self.state.loss_streak += 1
            self.state.win_streak = 0

        # Check for tier change mid-session
        self._update_tier()
        self.save_state()

    def get_position_size(
        self,
        regime: str,
        stacked_count: int = 1,
        use_streaks: bool = True,
    ) -> int:
        """
        Calculate position size using combined/additive logic.

        Args:
            regime: Current market regime (TRENDING_UP, TRENDING_DOWN, RANGING, etc.)
            stacked_count: Number of signals firing in same direction.
            use_streaks: Whether to apply streak adjustments.

        Returns:
            Position size (number of contracts).
        """
        if not self.state.scaling_enabled:
            # Scaling disabled for this tier (e.g., Tier 2 is always 1 contract)
            return 1

        # Base size
        size = 1

        # Stacked signals bonus: +1 when 2+ patterns fire together
        if stacked_count >= 2:
            size += 1

        # Regime bonus: +1 in trending markets
        if regime in ["TRENDING_UP", "TRENDING_DOWN"]:
            size += 1

        # Streak adjustment
        if use_streaks:
            if self.state.win_streak >= 3:
                size += 1
            elif self.state.loss_streak >= 2:
                size -= 1

        # Clamp to tier limits
        size = max(1, min(size, self.state.max_contracts))

        return size

    def should_halt(self, session_pnl: float) -> bool:
        """
        Check if session should halt due to loss limit.

        Args:
            session_pnl: Current session P&L.

        Returns:
            True if should halt.
        """
        return session_pnl <= self.state.daily_loss_limit

    def get_status(self) -> Dict[str, Any]:
        """Get current tier status."""
        return {
            "balance": self.state.balance,
            "tier_index": self.state.tier_index,
            "tier_name": self.state.tier_name,
            "instrument": self.state.instrument,
            "max_contracts": self.state.max_contracts,
            "daily_loss_limit": self.state.daily_loss_limit,
            "scaling_enabled": self.state.scaling_enabled,
            "session_pnl": self.state.session_pnl,
            "win_streak": self.state.win_streak,
            "loss_streak": self.state.loss_streak,
        }

    def set_balance(self, balance: float) -> None:
        """
        Set balance directly (e.g., from Rithmic account query).

        Args:
            balance: New account balance.
        """
        old_balance = self.state.balance
        self.state.balance = balance

        if abs(old_balance - balance) > 0.01:
            logger.info(f"Balance updated: ${old_balance:,.2f} → ${balance:,.2f}")
            self._update_tier()
            self.save_state()


# Global tier manager instance
_tier_manager: Optional[TierManager] = None


def get_tier_manager() -> Optional[TierManager]:
    """Get global tier manager instance."""
    return _tier_manager


def initialize_tier_manager(
    starting_balance: float = 2500.0,
    on_tier_change: Optional[Callable] = None,
) -> TierManager:
    """
    Initialize the global tier manager.

    Args:
        starting_balance: Initial account balance.
        on_tier_change: Callback for tier changes.

    Returns:
        Configured TierManager instance.
    """
    global _tier_manager
    _tier_manager = TierManager(
        starting_balance=starting_balance,
        on_tier_change=on_tier_change,
    )
    return _tier_manager
