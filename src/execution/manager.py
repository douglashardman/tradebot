"""Execution manager - handles trade execution and risk management."""

from datetime import datetime
from typing import Callable, Dict, List, Optional, Any
import logging

from src.core.types import Signal
from src.core.constants import TICK_SIZES, TICK_VALUES
from src.execution.session import TradingSession
from src.execution.orders import (
    Order, OrderType, OrderStatus,
    BracketOrder, Position, Trade
)

logger = logging.getLogger(__name__)


class ExecutionManager:
    """
    Manages trade execution and risk.

    Responsible for:
    - Validating signals before execution
    - Enforcing session risk limits
    - Managing open positions
    - Tracking P&L
    - Generating bracket orders
    """

    def __init__(self, session: TradingSession):
        self.session = session
        self.symbol = session.symbol

        # Get tick info for symbol
        symbol_base = self.symbol[:3] if self.symbol[:3] in TICK_SIZES else self.symbol[:2]
        self.tick_size = TICK_SIZES.get(symbol_base, 0.25)
        self.tick_value = TICK_VALUES.get(symbol_base, 1.25)

        # Session state
        self.daily_pnl: float = 0.0
        self.open_positions: List[Position] = []
        self.pending_orders: List[BracketOrder] = []
        self.completed_trades: List[Trade] = []

        # Halt state
        self.is_halted: bool = False
        self.halt_reason: Optional[str] = None

        # Callbacks
        self.on_trade_callbacks: List[Callable[[Trade], None]] = []
        self.on_position_callbacks: List[Callable[[Position], None]] = []

        # Paper trading state
        if session.mode == "paper":
            self.paper_balance = session.paper_starting_balance

    def on_signal(self, signal: Signal, regime_multiplier: float = 1.0) -> Optional[BracketOrder]:
        """
        Process an approved signal into a trade.

        Args:
            signal: The approved signal to execute
            regime_multiplier: Position size multiplier from regime

        Returns:
            BracketOrder if trade was submitted, None otherwise
        """
        # Check halt conditions
        if self.is_halted:
            logger.info(f"Signal rejected: Session halted - {self.halt_reason}")
            return None

        if not signal.approved:
            logger.debug(f"Signal not approved: {signal.rejection_reason}")
            return None

        # Check daily limits
        if self.daily_pnl >= self.session.daily_profit_target:
            self._halt("Daily profit target reached")
            return None

        if self.daily_pnl <= self.session.daily_loss_limit:
            self._halt("Daily loss limit reached")
            return None

        # Check position limits
        if len(self.open_positions) >= self.session.max_concurrent_trades:
            logger.debug("Max concurrent trades reached")
            return None

        # Check trading hours
        if not self.session.is_within_trading_hours():
            logger.debug("Outside trading hours")
            return None

        # Calculate position size
        base_size = self.session.max_position_size
        size = max(1, int(base_size * regime_multiplier))

        # Generate bracket order
        order = self._create_bracket_order(signal, size)

        if self.session.mode == "paper":
            # Simulate immediate fill for paper trading
            self._simulate_fill(order, signal)
        else:
            # Queue for live execution
            self.pending_orders.append(order)

        return order

    def _create_bracket_order(self, signal: Signal, size: int) -> BracketOrder:
        """Create a bracket order from a signal."""
        entry_price = signal.price

        if signal.direction == "LONG":
            stop_price = entry_price - (self.session.stop_loss_ticks * self.tick_size)
            target_price = entry_price + (self.session.take_profit_ticks * self.tick_size)
        else:
            stop_price = entry_price + (self.session.stop_loss_ticks * self.tick_size)
            target_price = entry_price - (self.session.take_profit_ticks * self.tick_size)

        return BracketOrder(
            symbol=self.symbol,
            side=signal.direction,
            size=size,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            signal_id=str(id(signal)),
        )

    def _simulate_fill(self, order: BracketOrder, signal: Signal) -> None:
        """Simulate order fill for paper trading."""
        now = datetime.now()

        # Create position
        position = Position(
            symbol=order.symbol,
            side=order.side,
            size=order.size,
            entry_price=order.entry_price,
            entry_time=now,
            stop_price=order.stop_price,
            target_price=order.target_price,
            bracket_id=order.bracket_id,
        )

        self.open_positions.append(position)
        order.is_active = True
        order.is_filled = True

        logger.info(
            f"Paper fill: {order.side} {order.size} {order.symbol} @ {order.entry_price} "
            f"(stop: {order.stop_price}, target: {order.target_price})"
        )

        # Notify callbacks
        for callback in self.on_position_callbacks:
            callback(position)

    def update_prices(self, current_price: float) -> None:
        """
        Update positions with current price and check stops/targets.

        Call this on each tick or bar close.

        If session.conservative_fills is True, targets require price to go
        1 tick BEYOND the target to fill (simulates being last in queue).
        """
        if not self.open_positions:
            return

        # Check if we need conservative fill logic (price must go through target)
        conservative = getattr(self.session, 'conservative_fills', False)

        for position in list(self.open_positions):  # Copy list to allow modification
            position.update_pnl(current_price, self.tick_value)

            # Check stop loss - exit at stop price (not current price which may have gapped)
            if position.side == "LONG" and current_price <= position.stop_price:
                self._close_position(position, position.stop_price, "STOP")
            elif position.side == "SHORT" and current_price >= position.stop_price:
                self._close_position(position, position.stop_price, "STOP")

            # Check take profit
            # If conservative_fills: require price to go 1 tick PAST target (simulate queue position)
            # Standard: fill when price touches target
            elif position.side == "LONG":
                if conservative:
                    # Must go BEYOND target (strict inequality = 1 tick through)
                    if current_price > position.target_price:
                        self._close_position(position, position.target_price, "TARGET")
                else:
                    if current_price >= position.target_price:
                        self._close_position(position, position.target_price, "TARGET")
            elif position.side == "SHORT":
                if conservative:
                    # Must go BEYOND target (strict inequality = 1 tick through)
                    if current_price < position.target_price:
                        self._close_position(position, position.target_price, "TARGET")
                else:
                    if current_price <= position.target_price:
                        self._close_position(position, position.target_price, "TARGET")

    def _close_position(
        self,
        position: Position,
        exit_price: float,
        reason: str
    ) -> Trade:
        """Close a position and create a trade record."""
        now = datetime.now()

        # Calculate P&L
        price_diff = exit_price - position.entry_price
        if position.side == "SHORT":
            price_diff = -price_diff

        pnl_ticks = int(price_diff / self.tick_size)
        pnl = pnl_ticks * self.tick_value * position.size


        # Create trade record
        trade = Trade(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            entry_time=position.entry_time,
            exit_price=exit_price,
            exit_time=now,
            exit_reason=reason,
            pnl=pnl,
            pnl_ticks=pnl_ticks,
        )

        # Update session state
        self.daily_pnl += pnl
        self.completed_trades.append(trade)
        self.open_positions.remove(position)

        if self.session.mode == "paper":
            self.paper_balance += pnl

        logger.info(
            f"Position closed: {reason} - {trade.side} {trade.size} {trade.symbol} "
            f"@ {exit_price} | P&L: ${pnl:.2f} ({pnl_ticks} ticks)"
        )

        # Check limits after close
        if self.daily_pnl >= self.session.daily_profit_target:
            self._halt("Daily profit target reached")
        elif self.daily_pnl <= self.session.daily_loss_limit:
            self._halt("Daily loss limit reached")

        # Notify callbacks
        for callback in self.on_trade_callbacks:
            callback(trade)

        return trade

    def close_all_positions(self, current_price: float, reason: str = "MANUAL") -> List[Trade]:
        """Close all open positions."""
        trades = []
        for position in list(self.open_positions):
            trade = self._close_position(position, current_price, reason)
            trades.append(trade)
        return trades

    def _halt(self, reason: str) -> None:
        """Halt trading for the session."""
        self.is_halted = True
        self.halt_reason = reason
        logger.warning(f"Trading halted: {reason}")

    def resume(self) -> None:
        """Resume trading (if within limits)."""
        if self.daily_pnl >= self.session.daily_profit_target:
            logger.warning("Cannot resume: profit target reached")
            return
        if self.daily_pnl <= self.session.daily_loss_limit:
            logger.warning("Cannot resume: loss limit reached")
            return

        self.is_halted = False
        self.halt_reason = None
        logger.info("Trading resumed")

    def update_symbol(self, symbol: str) -> None:
        """Update symbol and recalculate tick values."""
        self.symbol = symbol
        self.session.symbol = symbol

        # Recalculate tick info for new symbol
        symbol_base = symbol[:3] if symbol[:3] in TICK_SIZES else symbol[:2]
        self.tick_size = TICK_SIZES.get(symbol_base, 0.25)
        self.tick_value = TICK_VALUES.get(symbol_base, 1.25)

        logger.info(f"Symbol updated to {symbol}: tick_size={self.tick_size}, tick_value=${self.tick_value}")

    def on_trade(self, callback: Callable[[Trade], None]) -> None:
        """Register callback for trade completion."""
        self.on_trade_callbacks.append(callback)

    def on_position(self, callback: Callable[[Position], None]) -> None:
        """Register callback for new positions."""
        self.on_position_callbacks.append(callback)

    def get_state(self) -> Dict[str, Any]:
        """Get current execution state."""
        return {
            "mode": self.session.mode,
            "symbol": self.symbol,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_profit_target": self.session.daily_profit_target,
            "daily_loss_limit": self.session.daily_loss_limit,
            "is_halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "open_positions": len(self.open_positions),
            "completed_trades": len(self.completed_trades),
            "win_count": sum(1 for t in self.completed_trades if t.pnl > 0),
            "loss_count": sum(1 for t in self.completed_trades if t.pnl <= 0),
            "paper_balance": self.paper_balance if self.session.mode == "paper" else None,
            "positions": [p.to_dict() for p in self.open_positions],
        }

    def get_statistics(self) -> Dict[str, Any]:
        """Get trading statistics for the session."""
        if not self.completed_trades:
            return {
                "total_trades": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "average_win": 0.0,
                "average_loss": 0.0,
                "profit_factor": 0.0,
                "largest_win": 0.0,
                "largest_loss": 0.0,
            }

        wins = [t for t in self.completed_trades if t.pnl > 0]
        losses = [t for t in self.completed_trades if t.pnl <= 0]

        total_wins = sum(t.pnl for t in wins)
        total_losses = abs(sum(t.pnl for t in losses))

        return {
            "total_trades": len(self.completed_trades),
            "win_rate": len(wins) / len(self.completed_trades),
            "total_pnl": self.daily_pnl,
            "average_win": total_wins / len(wins) if wins else 0.0,
            "average_loss": total_losses / len(losses) if losses else 0.0,
            "profit_factor": total_wins / total_losses if total_losses > 0 else float('inf'),
            "largest_win": max(t.pnl for t in wins) if wins else 0.0,
            "largest_loss": min(t.pnl for t in losses) if losses else 0.0,
        }
