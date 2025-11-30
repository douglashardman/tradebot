"""Execution bridge connecting ExecutionManager to broker (Rithmic)."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Any

from src.execution.orders import BracketOrder, Position, Trade
from src.execution.manager import ExecutionManager
from src.data.adapters.rithmic import RithmicAdapter, LiveOrder, OrderState
from src.core.constants import TICK_SIZES

logger = logging.getLogger(__name__)


class ExecutionBridge:
    """
    Bridge between the trading system and broker.

    Responsibilities:
    - Submit bracket orders from ExecutionManager to Rithmic
    - Process fill callbacks and update positions
    - Handle order rejections with notifications
    - Reconcile positions on startup/reconnect
    - Provide crash recovery logic
    """

    def __init__(
        self,
        execution_manager: ExecutionManager,
        rithmic_adapter: RithmicAdapter,
        on_fill_callback: Optional[Callable] = None,
        on_rejection_callback: Optional[Callable] = None,
    ):
        """
        Initialize execution bridge.

        Args:
            execution_manager: The ExecutionManager instance
            rithmic_adapter: The RithmicAdapter instance
            on_fill_callback: Optional callback for fill events
            on_rejection_callback: Optional callback for rejection events
        """
        self.execution_manager = execution_manager
        self.rithmic = rithmic_adapter
        self._on_fill_callback = on_fill_callback
        self._on_rejection_callback = on_rejection_callback

        # Order tracking: bracket_id -> (BracketOrder, LiveOrder)
        self._pending_brackets: Dict[str, tuple] = {}
        self._filled_brackets: Dict[str, tuple] = {}

        # Position tracking: order_id -> Position
        self._order_to_position: Dict[str, Position] = {}

        # Register callbacks with Rithmic adapter
        self.rithmic.on_fill(self._handle_fill)
        self.rithmic.on_rejection(self._handle_rejection)

        logger.info("Execution bridge initialized")

    async def submit_bracket_order(
        self,
        bracket: BracketOrder,
    ) -> Optional[LiveOrder]:
        """
        Submit a bracket order to the broker.

        Args:
            bracket: The BracketOrder from ExecutionManager

        Returns:
            LiveOrder if submitted successfully, None otherwise.
        """
        symbol = bracket.symbol
        side = bracket.side  # LONG or SHORT
        quantity = bracket.size

        # Calculate stop and target in ticks
        symbol_base = symbol[:3] if symbol[:3] in TICK_SIZES else symbol[:2]
        tick_size = TICK_SIZES.get(symbol_base, 0.25)

        if side == "LONG":
            stop_ticks = int((bracket.entry_price - bracket.stop_price) / tick_size)
            target_ticks = int((bracket.target_price - bracket.entry_price) / tick_size)
        else:
            stop_ticks = int((bracket.stop_price - bracket.entry_price) / tick_size)
            target_ticks = int((bracket.entry_price - bracket.target_price) / tick_size)

        logger.info(
            f"Submitting bracket: {side} {quantity} {symbol} @ market "
            f"(stop: {stop_ticks} ticks, target: {target_ticks} ticks)"
        )

        # Submit to Rithmic
        live_order = await self.rithmic.submit_bracket_order(
            symbol=symbol,
            side=side,
            quantity=quantity,
            stop_ticks=stop_ticks,
            target_ticks=target_ticks,
            bracket_id=bracket.bracket_id,
        )

        if live_order:
            # Track the order
            self._pending_brackets[bracket.bracket_id] = (bracket, live_order)
            logger.info(f"Bracket {bracket.bracket_id} submitted: order_id={live_order.order_id}")
            return live_order
        else:
            logger.error(f"Failed to submit bracket {bracket.bracket_id}")
            return None

    async def _handle_fill(self, fill_data: dict) -> None:
        """
        Handle fill notification from Rithmic.

        This is called when an entry fill, stop fill, or target fill occurs.
        """
        order_id = fill_data.get("order_id")
        fill_price = fill_data.get("fill_price", 0)
        fill_qty = fill_data.get("fill_qty", 0)
        live_order: Optional[LiveOrder] = fill_data.get("order")

        logger.info(f"Bridge processing fill: {order_id} - {fill_qty} @ {fill_price}")

        if not live_order:
            logger.warning(f"Fill for unknown order: {order_id}")
            return

        bracket_id = live_order.bracket_id

        # Check if this is an entry fill
        if live_order.is_entry and live_order.state == OrderState.FILLED:
            # Find the bracket order
            if bracket_id in self._pending_brackets:
                bracket, _ = self._pending_brackets[bracket_id]

                # Create position in ExecutionManager
                now = datetime.now(timezone.utc)
                position = Position(
                    symbol=bracket.symbol,
                    side=bracket.side,
                    size=fill_qty,
                    entry_price=fill_price,
                    entry_time=now,
                    stop_price=bracket.stop_price,
                    target_price=bracket.target_price,
                    bracket_id=bracket_id,
                )

                # Add to execution manager
                self.execution_manager.open_positions.append(position)
                bracket.is_active = True
                bracket.is_filled = True
                bracket.entry_price = fill_price  # Update with actual fill price

                # Track for exit processing
                self._order_to_position[order_id] = position
                self._filled_brackets[bracket_id] = self._pending_brackets.pop(bracket_id)

                logger.info(
                    f"Position opened: {bracket.side} {fill_qty} {bracket.symbol} @ {fill_price}"
                )

                # Notify callbacks
                for callback in self.execution_manager.on_position_callbacks:
                    try:
                        callback(position)
                    except Exception as e:
                        logger.error(f"Error in position callback: {e}")

        # Check if this is an exit fill (stop or target)
        elif not live_order.is_entry and live_order.state == OrderState.FILLED:
            # Find the position for this bracket
            if bracket_id in self._filled_brackets:
                bracket, entry_order = self._filled_brackets[bracket_id]

                # Find the matching position
                position = None
                for pos in self.execution_manager.open_positions:
                    if pos.bracket_id == bracket_id:
                        position = pos
                        break

                if position:
                    # Determine exit reason based on fill price vs stop/target
                    if bracket.side == "LONG":
                        if fill_price <= bracket.stop_price:
                            exit_reason = "STOP"
                        else:
                            exit_reason = "TARGET"
                    else:
                        if fill_price >= bracket.stop_price:
                            exit_reason = "STOP"
                        else:
                            exit_reason = "TARGET"

                    # Close the position
                    self._close_position(position, fill_price, exit_reason)

                    # Clean up tracking
                    del self._filled_brackets[bracket_id]

        # Call external fill callback
        if self._on_fill_callback:
            try:
                if asyncio.iscoroutinefunction(self._on_fill_callback):
                    await self._on_fill_callback(fill_data)
                else:
                    self._on_fill_callback(fill_data)
            except Exception as e:
                logger.error(f"Error in external fill callback: {e}")

    def _close_position(
        self,
        position: Position,
        exit_price: float,
        exit_reason: str,
    ) -> Trade:
        """
        Close a position and record the trade.

        This mirrors ExecutionManager._close_position but for live fills.
        """
        now = datetime.now(timezone.utc)
        em = self.execution_manager

        # Calculate P&L
        price_diff = exit_price - position.entry_price
        if position.side == "SHORT":
            price_diff = -price_diff

        pnl_ticks = int(price_diff / em.tick_size)
        pnl = pnl_ticks * em.tick_value * position.size

        # Create trade record
        trade = Trade(
            symbol=position.symbol,
            side=position.side,
            size=position.size,
            entry_price=position.entry_price,
            entry_time=position.entry_time,
            exit_price=exit_price,
            exit_time=now,
            exit_reason=exit_reason,
            stop_price=position.stop_price,
            target_price=position.target_price,
            pnl=pnl,
            pnl_ticks=pnl_ticks,
        )

        # Update execution manager state
        em.daily_pnl += pnl
        em.completed_trades.append(trade)
        if position in em.open_positions:
            em.open_positions.remove(position)

        logger.info(
            f"Position closed: {exit_reason} - {trade.side} {trade.size} {trade.symbol} "
            f"@ {exit_price} | P&L: ${pnl:.2f} ({pnl_ticks} ticks)"
        )

        # Check limits
        if em.daily_pnl >= em.session.daily_profit_target:
            em._halt("Daily profit target reached")
        elif em.daily_pnl <= em.session.daily_loss_limit:
            em._halt("Daily loss limit reached")

        # Notify trade callbacks
        for callback in em.on_trade_callbacks:
            try:
                callback(trade)
            except Exception as e:
                logger.error(f"Error in trade callback: {e}")

        return trade

    async def _handle_rejection(self, rejection_data: dict) -> None:
        """Handle order rejection from Rithmic."""
        order_id = rejection_data.get("order_id")
        reason = rejection_data.get("reason", "Unknown")
        live_order: Optional[LiveOrder] = rejection_data.get("order")

        logger.error(f"Order rejected: {order_id} - {reason}")

        if live_order and live_order.bracket_id:
            bracket_id = live_order.bracket_id

            # Remove from pending
            if bracket_id in self._pending_brackets:
                bracket, _ = self._pending_brackets.pop(bracket_id)
                logger.warning(f"Bracket {bracket_id} rejected: {reason}")

        # Call external rejection callback
        if self._on_rejection_callback:
            try:
                if asyncio.iscoroutinefunction(self._on_rejection_callback):
                    await self._on_rejection_callback(rejection_data)
                else:
                    self._on_rejection_callback(rejection_data)
            except Exception as e:
                logger.error(f"Error in external rejection callback: {e}")

    async def flatten_all(self) -> bool:
        """
        Flatten all positions (exit everything).

        Returns:
            True if flatten request sent, False otherwise.
        """
        logger.warning("Flattening all positions")

        symbol = self.execution_manager.symbol
        success = await self.rithmic.exit_position(symbol)

        if success:
            logger.info("Flatten request sent successfully")
        else:
            logger.error("Failed to send flatten request")

        return success

    async def cancel_all_orders(self) -> bool:
        """Cancel all pending orders."""
        return await self.rithmic.cancel_all_orders()

    async def reconcile_on_startup(self) -> Dict[str, Any]:
        """
        Reconcile positions and orders on startup.

        Call this after connecting to ensure we have accurate state.

        Returns:
            Dictionary with reconciliation results.
        """
        logger.info("Starting position reconciliation...")

        # Get broker positions
        broker_positions = await self.rithmic.reconcile_positions()

        # Get open orders
        broker_orders = await self.rithmic.list_orders()

        results = {
            "broker_positions": len(broker_positions),
            "broker_orders": len(broker_orders),
            "local_positions": len(self.execution_manager.open_positions),
            "reconciled": True,
        }

        # Check for position mismatch
        if len(broker_positions) > 0:
            logger.warning(
                f"Found {len(broker_positions)} broker position(s) on startup!"
            )
            for symbol, pos in broker_positions.items():
                # Handle both LivePosition objects and dict from mock
                if hasattr(pos, 'side'):
                    logger.warning(
                        f"  {pos.side} {pos.quantity} {symbol} @ {pos.avg_price}"
                    )
                else:
                    # Dict format from broker
                    qty = pos.get('quantity', 0)
                    side = "LONG" if qty > 0 else "SHORT"
                    logger.warning(
                        f"  {side} {abs(qty)} {symbol} @ {pos.get('avg_price', 0)}"
                    )

            # If we have broker positions but no local positions,
            # we likely crashed with open positions - DON'T auto-trade
            if len(self.execution_manager.open_positions) == 0:
                logger.error(
                    "POSITION MISMATCH: Broker has positions but local state is empty!"
                )
                logger.error(
                    "This may indicate a crash. Please reconcile manually."
                )
                self.execution_manager._halt(
                    "Position mismatch on startup - manual reconciliation required"
                )
                results["reconciled"] = False
                results["action_required"] = "Manual position reconciliation"

        logger.info(f"Reconciliation complete: {results}")
        return results

    def process_pending_orders(self) -> List[BracketOrder]:
        """
        Get pending orders from ExecutionManager and submit them.

        Call this periodically to submit any new bracket orders.

        Returns:
            List of submitted BracketOrders.
        """
        pending = self.execution_manager.pending_orders
        submitted = []

        for bracket in list(pending):
            # Schedule async submission
            asyncio.create_task(self._submit_and_track(bracket))
            submitted.append(bracket)
            pending.remove(bracket)

        return submitted

    async def _submit_and_track(self, bracket: BracketOrder) -> None:
        """Submit a bracket order and handle the result."""
        live_order = await self.submit_bracket_order_with_retry(bracket)
        if not live_order:
            logger.error(f"Failed to submit bracket after all retries: {bracket.bracket_id}")

    async def submit_bracket_order_with_retry(
        self,
        bracket: BracketOrder,
        max_retries: int = 3,
        base_delay: float = 0.5,
    ) -> Optional[LiveOrder]:
        """
        Submit a bracket order with exponential backoff retry.

        Args:
            bracket: The BracketOrder to submit
            max_retries: Maximum number of retry attempts
            base_delay: Base delay in seconds (doubles each retry)

        Returns:
            LiveOrder if successful, None if all retries failed.
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                live_order = await self.submit_bracket_order(bracket)
                if live_order:
                    if attempt > 0:
                        logger.info(
                            f"Bracket {bracket.bracket_id} submitted on retry {attempt}"
                        )
                    return live_order
                else:
                    last_error = "submit_bracket_order returned None"
                    logger.warning(
                        f"Bracket submission returned None (attempt {attempt + 1}/{max_retries + 1})"
                    )
            except Exception as e:
                last_error = str(e)
                logger.warning(
                    f"Bracket submission exception (attempt {attempt + 1}/{max_retries + 1}): {e}"
                )

            # Don't sleep after the last attempt
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)  # Exponential backoff
                logger.info(f"Retrying in {delay:.1f}s...")
                await asyncio.sleep(delay)

        logger.error(
            f"All {max_retries + 1} attempts failed for bracket {bracket.bracket_id}: {last_error}"
        )
        return None

    def get_state(self) -> Dict[str, Any]:
        """Get current bridge state for monitoring."""
        return {
            "pending_brackets": len(self._pending_brackets),
            "filled_brackets": len(self._filled_brackets),
            "tracked_positions": len(self._order_to_position),
            "broker_connected": self.rithmic._connected,
            "execution_halted": self.execution_manager.is_halted,
        }
