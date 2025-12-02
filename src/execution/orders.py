"""Order types and position management."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Literal
from enum import Enum
import uuid

from src.core.constants import TICK_SIZES


class OrderStatus(Enum):
    """Order lifecycle status."""
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


class OrderType(Enum):
    """Order types."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"


@dataclass
class Order:
    """Represents a single order."""
    symbol: str
    side: Literal["BUY", "SELL"]
    size: int
    order_type: OrderType = OrderType.MARKET
    limit_price: Optional[float] = None
    stop_price: Optional[float] = None

    # Identifiers
    order_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    client_order_id: Optional[str] = None

    # Status tracking
    status: OrderStatus = OrderStatus.PENDING
    filled_size: int = 0
    filled_price: Optional[float] = None

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None

    # Metadata
    signal_id: Optional[str] = None
    notes: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "order_type": self.order_type.value,
            "limit_price": self.limit_price,
            "stop_price": self.stop_price,
            "status": self.status.value,
            "filled_size": self.filled_size,
            "filled_price": self.filled_price,
            "created_at": self.created_at.isoformat(),
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
        }


@dataclass
class BracketOrder:
    """
    A bracket order with entry, stop loss, and take profit.

    This is the primary order type for our system.
    """
    symbol: str
    side: Literal["LONG", "SHORT"]
    size: int
    entry_price: float
    stop_price: float
    target_price: float

    # Identifiers
    bracket_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    signal_id: Optional[str] = None

    # Component orders (populated on submission)
    entry_order: Optional[Order] = None
    stop_order: Optional[Order] = None
    target_order: Optional[Order] = None

    # Status
    is_active: bool = False
    is_filled: bool = False
    is_closed: bool = False

    # Timestamps
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        return {
            "bracket_id": self.bracket_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "is_active": self.is_active,
            "is_filled": self.is_filled,
            "is_closed": self.is_closed,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class Position:
    """Represents an open position."""
    symbol: str
    side: Literal["LONG", "SHORT"]
    size: int
    entry_price: float
    entry_time: datetime

    # P&L tracking
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0

    # Stops
    stop_price: Optional[float] = None
    target_price: Optional[float] = None

    # Identifiers
    position_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    bracket_id: Optional[str] = None

    # Tick values captured at position open (for correct P&L calculation if symbol changes)
    tick_size: Optional[float] = None
    tick_value: Optional[float] = None

    def update_pnl(self, current_price: float, tick_value: float) -> float:
        """
        Update unrealized P&L based on current price.

        Args:
            current_price: Current market price
            tick_value: Dollar value per tick (used as fallback if position doesn't have its own)

        Returns:
            Unrealized P&L in dollars
        """
        self.current_price = current_price
        price_diff = current_price - self.entry_price

        if self.side == "SHORT":
            price_diff = -price_diff

        # Use position's captured tick values if available (for tier change safety)
        # Otherwise fall back to symbol lookup or provided value
        if self.tick_size is not None:
            ts = self.tick_size
        else:
            symbol_base = self.symbol[:3] if self.symbol[:3] in TICK_SIZES else self.symbol[:2]
            ts = TICK_SIZES.get(symbol_base, 0.25)

        tv = self.tick_value if self.tick_value is not None else tick_value

        # Convert to ticks, then to dollars
        ticks = price_diff / ts
        self.unrealized_pnl = ticks * tv * self.size

        return self.unrealized_pnl

    def to_dict(self) -> dict:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "current_price": self.current_price,
            "unrealized_pnl": self.unrealized_pnl,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "bracket_id": self.bracket_id,
            # Tick values captured at entry - critical for correct P&L on tier changes
            "tick_size": self.tick_size,
            "tick_value": self.tick_value,
        }


@dataclass
class Trade:
    """A completed trade (entry + exit)."""
    symbol: str
    side: Literal["LONG", "SHORT"]
    size: int

    # Entry
    entry_price: float
    entry_time: datetime

    # Exit
    exit_price: float
    exit_time: datetime
    exit_reason: Literal["TARGET", "STOP", "MANUAL", "HALTED", "TIMEOUT", "AUTO_FLATTEN"]

    # P&L
    pnl: float
    pnl_ticks: int

    # Bracket levels (planned stop/target)
    stop_price: Optional[float] = None
    target_price: Optional[float] = None

    # Context
    trade_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    bracket_id: Optional[str] = None  # Links to originating bracket order
    signal_pattern: Optional[str] = None
    regime: Optional[str] = None
    regime_confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "size": self.size,
            "entry_price": self.entry_price,
            "entry_time": self.entry_time.isoformat(),
            "exit_price": self.exit_price,
            "exit_time": self.exit_time.isoformat(),
            "exit_reason": self.exit_reason,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "pnl": self.pnl,
            "pnl_ticks": self.pnl_ticks,
            "bracket_id": self.bracket_id,
            "signal_pattern": self.signal_pattern,
            "regime": self.regime,
        }
