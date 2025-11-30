"""Trading session configuration and management."""

from dataclasses import dataclass, field
from datetime import time, datetime
from typing import List, Tuple, Literal, Optional


@dataclass
class TradingSession:
    """Configuration for a trading session."""

    # Mode
    mode: Literal["paper", "live"] = "paper"
    paper_starting_balance: float = 10000.0

    # Instrument
    symbol: str = "MES"  # Start with micros for safety

    # Risk Limits (hard stops)
    daily_profit_target: float = 500.0
    daily_loss_limit: float = -300.0
    max_position_size: int = 2
    max_concurrent_trades: int = 1

    # Per-trade risk (Scalping mode - quick wins)
    stop_loss_ticks: int = 5       # 1.25 points ($6.25 on MES)
    take_profit_ticks: int = 4     # 1.00 points ($5.00 on MES)
    breakeven_ticks: int = 2       # Move stop to breakeven after 2 ticks profit

    # Fill simulation (for backtesting realism)
    # If True, require price to go 1 tick BEYOND target to fill (simulates being last in queue)
    conservative_fills: bool = False

    # Slippage simulation for paper trading (in ticks)
    # Simulates realistic fills by worsening entry price
    paper_slippage_ticks: int = 1

    # Time Controls
    trading_start: time = field(default_factory=lambda: time(9, 30))
    trading_end: time = field(default_factory=lambda: time(15, 45))
    no_trade_windows: List[Tuple[time, time]] = field(default_factory=list)

    # For backtesting: bypass trading hours check
    bypass_trading_hours: bool = False

    # Strategy Controls
    enabled_patterns: Optional[List[str]] = None  # None = all enabled
    min_signal_strength: float = 0.6
    min_regime_confidence: float = 0.7

    # Session tracking
    session_id: Optional[str] = None
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None

    def __post_init__(self):
        # Default no-trade windows if not specified
        if not self.no_trade_windows:
            self.no_trade_windows = [
                (time(12, 0), time(13, 0)),  # Lunch doldrums
            ]

        # Generate session ID if not provided
        if not self.session_id:
            self.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    def is_within_trading_hours(self, current_time: time = None) -> bool:
        """Check if current time is within trading hours."""
        # Bypass for backtesting
        if self.bypass_trading_hours:
            return True

        if current_time is None:
            current_time = datetime.now().time()

        # Before start or after end
        if current_time < self.trading_start or current_time > self.trading_end:
            return False

        # Check no-trade windows
        for start, end in self.no_trade_windows:
            if start <= current_time <= end:
                return False

        return True

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "mode": self.mode,
            "paper_starting_balance": self.paper_starting_balance,
            "symbol": self.symbol,
            "daily_profit_target": self.daily_profit_target,
            "daily_loss_limit": self.daily_loss_limit,
            "max_position_size": self.max_position_size,
            "max_concurrent_trades": self.max_concurrent_trades,
            "stop_loss_ticks": self.stop_loss_ticks,
            "take_profit_ticks": self.take_profit_ticks,
            "breakeven_ticks": self.breakeven_ticks,
            "paper_slippage_ticks": self.paper_slippage_ticks,
            "trading_start": self.trading_start.isoformat(),
            "trading_end": self.trading_end.isoformat(),
            "min_signal_strength": self.min_signal_strength,
            "min_regime_confidence": self.min_regime_confidence,
            "session_id": self.session_id,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "ended_at": self.ended_at.isoformat() if self.ended_at else None,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TradingSession":
        """Create from dictionary."""
        # Convert time strings back to time objects
        if isinstance(data.get("trading_start"), str):
            data["trading_start"] = time.fromisoformat(data["trading_start"])
        if isinstance(data.get("trading_end"), str):
            data["trading_end"] = time.fromisoformat(data["trading_end"])
        if isinstance(data.get("started_at"), str):
            data["started_at"] = datetime.fromisoformat(data["started_at"])
        if isinstance(data.get("ended_at"), str):
            data["ended_at"] = datetime.fromisoformat(data["ended_at"])

        return cls(**data)
