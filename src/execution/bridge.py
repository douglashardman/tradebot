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

        # Fill deduplication: track processed fill IDs to prevent double-processing
        # Limited to prevent unbounded memory growth (duplicates arrive close in time)
        self._processed_fills: set = set()
        self._max_processed_fills: int = 1000  # Clear when exceeds this

        # Partial fill tracking: bracket_id -> cumulative filled quantity
        self._entry_fill_totals: Dict[str, int] = {}

        # Task tracking: bracket_id -> asyncio.Task for pending submissions
        self._submission_tasks: Dict[str, asyncio.Task] = {}
        # Failed submissions: bracket_id -> error message
        self._failed_submissions: Dict[str, str] = {}

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
            stop_ticks = round((bracket.entry_price - bracket.stop_price) / tick_size)
            target_ticks = round((bracket.target_price - bracket.entry_price) / tick_size)
        else:
            stop_ticks = round((bracket.stop_price - bracket.entry_price) / tick_size)
            target_ticks = round((bracket.entry_price - bracket.target_price) / tick_size)

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
        Uses position lock to prevent race conditions with concurrent fills.
        """
        order_id = fill_data.get("order_id")
        fill_price = fill_data.get("fill_price", 0)
        fill_qty = fill_data.get("fill_qty", 0)
        fill_time = fill_data.get("fill_time") or datetime.now(timezone.utc).isoformat()
        live_order: Optional[LiveOrder] = fill_data.get("order")

        # Create unique fill ID for deduplication
        # Include timestamp to handle multiple fills at same price
        fill_id = fill_data.get("fill_id") or f"{order_id}_{fill_price}_{fill_qty}_{fill_time}"

        logger.info(f"Bridge processing fill: {order_id} - {fill_qty} @ {fill_price}")

        if not live_order:
            logger.warning(f"Fill for unknown order: {order_id}")
            return

        bracket_id = live_order.bracket_id

        # Acquire lock for all position mutations AND deduplication check
        # Both must be inside lock to prevent race conditions
        async with self.execution_manager._position_lock:
            # Check for duplicate fill (inside lock to prevent race)
            if fill_id in self._processed_fills:
                logger.warning(f"Duplicate fill ignored: {fill_id}")
                return
            self._processed_fills.add(fill_id)

            # Prevent unbounded memory growth - clear old fill IDs when limit exceeded
            # Duplicates would arrive close in time, so old IDs are safe to forget
            if len(self._processed_fills) > self._max_processed_fills:
                logger.debug(f"Clearing {len(self._processed_fills)} processed fill IDs (memory management)")
                self._processed_fills.clear()
                self._processed_fills.add(fill_id)  # Keep current one

            # Check if this is an entry fill (FILLED or PARTIALLY_FILLED)
            is_entry_fill = live_order.is_entry and live_order.state in (
                OrderState.FILLED, OrderState.PARTIALLY_FILLED
            )

            if is_entry_fill:
                # Track cumulative fills for this bracket
                prev_filled = self._entry_fill_totals.get(bracket_id, 0)
                self._entry_fill_totals[bracket_id] = prev_filled + fill_qty

                # Find the bracket order (could be pending or filled from prior partial)
                bracket = None
                if bracket_id in self._pending_brackets:
                    bracket, _ = self._pending_brackets[bracket_id]
                elif bracket_id in self._filled_brackets:
                    bracket, _ = self._filled_brackets[bracket_id]

                if bracket:
                    if prev_filled == 0:
                        # First fill - create new position with tick values captured at entry
                        # This ensures P&L is correct even if tier/symbol changes mid-trade
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
                            tick_size=self.execution_manager.tick_size,
                            tick_value=self.execution_manager.tick_value,
                        )

                        # Add to execution manager
                        self.execution_manager.open_positions.append(position)
                        bracket.is_active = True
                        bracket.entry_price = fill_price

                        # Track for exit processing
                        self._order_to_position[order_id] = position

                        # Move to filled brackets (even if partial)
                        if bracket_id in self._pending_brackets:
                            self._filled_brackets[bracket_id] = self._pending_brackets.pop(bracket_id)

                        logger.info(
                            f"Position opened: {bracket.side} {fill_qty} {bracket.symbol} @ {fill_price}"
                            + (" (partial)" if live_order.state == OrderState.PARTIALLY_FILLED else "")
                        )

                        # Notify callbacks
                        for callback in self.execution_manager.on_position_callbacks:
                            try:
                                callback(position)
                            except Exception as e:
                                logger.error(f"Error in position callback: {e}")
                    else:
                        # Additional fill - update existing position size
                        position = None
                        for pos in self.execution_manager.open_positions:
                            if pos.bracket_id == bracket_id:
                                position = pos
                                break

                        if position:
                            old_size = position.size
                            position.size += fill_qty
                            logger.info(
                                f"Position size updated: {bracket.side} {bracket.symbol} "
                                f"{old_size} -> {position.size} (additional fill: {fill_qty} @ {fill_price})"
                            )

                    # Mark as filled only on complete fill
                    if live_order.state == OrderState.FILLED:
                        bracket.is_filled = True

            # Check if this is an exit fill (stop or target)
            is_exit_fill = not live_order.is_entry and live_order.state in (
                OrderState.FILLED, OrderState.PARTIALLY_FILLED
            )

            if is_exit_fill:
                # Find the position for this bracket
                if bracket_id in self._filled_brackets:
                    bracket, entry_order = self._filled_brackets[bracket_id]

                    # Find the matching position (protected by lock)
                    position = None
                    for pos in self.execution_manager.open_positions:
                        if pos.bracket_id == bracket_id:
                            position = pos
                            break

                    if position:
                        # Determine exit reason by proximity to stop vs target
                        stop_distance = abs(fill_price - bracket.stop_price)
                        target_distance = abs(fill_price - bracket.target_price)

                        if stop_distance <= target_distance:
                            exit_reason = "STOP"
                        else:
                            exit_reason = "TARGET"

                        # Handle partial vs full exit
                        if live_order.state == OrderState.PARTIALLY_FILLED and fill_qty < position.size:
                            # Partial exit - reduce position size
                            old_size = position.size
                            position.size -= fill_qty
                            logger.info(
                                f"Partial exit: {bracket.side} {bracket.symbol} "
                                f"{old_size} -> {position.size} ({exit_reason} fill: {fill_qty} @ {fill_price})"
                            )
                        else:
                            # Full exit - close the position
                            self._close_position(position, fill_price, exit_reason)

                            # Clean up tracking
                            del self._filled_brackets[bracket_id]
                            self._entry_fill_totals.pop(bracket_id, None)

        # Call external fill callback (outside lock to avoid blocking)
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

        Delegates to ExecutionManager._close_position for unified logic.
        """
        return self.execution_manager._close_position(position, exit_price, exit_reason)

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

    async def flatten_all(
        self,
        verify: bool = True,
        verify_timeout: float = 5.0,
        verify_interval: float = 0.5
    ) -> Dict[str, Any]:
        """
        Flatten all positions (exit everything).

        Args:
            verify: If True, poll broker to verify positions are closed
            verify_timeout: Max seconds to wait for verification
            verify_interval: Seconds between verification checks

        Returns:
            Dict with 'success', 'verified', and 'broker_positions' keys.
        """
        logger.warning("Flattening all positions")

        symbol = self.execution_manager.symbol
        request_sent = await self.rithmic.exit_position(symbol)

        result = {
            "success": request_sent,
            "verified": False,
            "broker_positions": None,
            "local_positions_cleared": False,
        }

        if not request_sent:
            logger.error("Failed to send flatten request")
            return result

        logger.info("Flatten request sent successfully")

        if verify:
            # Poll broker to verify positions are actually closed
            elapsed = 0.0
            while elapsed < verify_timeout:
                await asyncio.sleep(verify_interval)
                elapsed += verify_interval

                broker_positions = await self.rithmic.reconcile_positions()
                result["broker_positions"] = len(broker_positions)

                if len(broker_positions) == 0:
                    result["verified"] = True
                    logger.info("Flatten verified: no broker positions remaining")
                    break

                logger.debug(
                    f"Flatten verification: {len(broker_positions)} positions "
                    f"remaining after {elapsed:.1f}s"
                )

            if not result["verified"]:
                logger.warning(
                    f"Flatten verification timed out after {verify_timeout}s - "
                    f"{result['broker_positions']} positions may still be open"
                )

        # Clear local position tracking regardless
        async with self.execution_manager._position_lock:
            self.execution_manager.open_positions.clear()
            self._filled_brackets.clear()
            self._pending_brackets.clear()
            result["local_positions_cleared"] = True

        return result

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
        Tasks are tracked in _submission_tasks for monitoring.

        Returns:
            List of submitted BracketOrders.
        """
        pending = self.execution_manager.pending_orders
        submitted = []

        for bracket in list(pending):
            # Schedule async submission with tracking
            task = asyncio.create_task(self._submit_and_track(bracket))
            self._submission_tasks[bracket.bracket_id] = task
            submitted.append(bracket)
            pending.remove(bracket)

        return submitted

    async def _submit_and_track(self, bracket: BracketOrder) -> bool:
        """
        Submit a bracket order and track the result.

        Returns:
            True if submission succeeded, False otherwise.
        """
        bracket_id = bracket.bracket_id
        try:
            live_order = await self.submit_bracket_order_with_retry(bracket)
            if live_order:
                # Success - clean up tracking
                self._submission_tasks.pop(bracket_id, None)
                return True
            else:
                # Failed after retries
                error_msg = "All retry attempts exhausted"
                self._failed_submissions[bracket_id] = error_msg
                logger.error(f"Failed to submit bracket {bracket_id}: {error_msg}")
                return False
        except Exception as e:
            # Unexpected error
            error_msg = str(e)
            self._failed_submissions[bracket_id] = error_msg
            logger.error(f"Exception submitting bracket {bracket_id}: {error_msg}")
            return False
        finally:
            # Always clean up task reference
            self._submission_tasks.pop(bracket_id, None)

    async def await_pending_submissions(self, timeout: float = 30.0) -> Dict[str, bool]:
        """
        Wait for all pending submission tasks to complete.

        Args:
            timeout: Maximum time to wait in seconds.

        Returns:
            Dict mapping bracket_id to success (True/False).
        """
        if not self._submission_tasks:
            return {}

        results = {}
        tasks = list(self._submission_tasks.items())

        try:
            # Wait for all tasks with timeout
            done, pending = await asyncio.wait(
                [task for _, task in tasks],
                timeout=timeout,
                return_when=asyncio.ALL_COMPLETED
            )

            # Process completed tasks
            for bracket_id, task in tasks:
                if task in done:
                    try:
                        results[bracket_id] = task.result()
                    except Exception as e:
                        results[bracket_id] = False
                        self._failed_submissions[bracket_id] = str(e)
                else:
                    # Timed out
                    results[bracket_id] = False
                    self._failed_submissions[bracket_id] = "Submission timed out"
                    task.cancel()

        except Exception as e:
            logger.error(f"Error awaiting submissions: {e}")

        return results

    def get_failed_submissions(self) -> Dict[str, str]:
        """Get all failed submission bracket_ids and their error messages."""
        return dict(self._failed_submissions)

    def clear_failed_submissions(self) -> None:
        """Clear the failed submissions tracking."""
        self._failed_submissions.clear()

    async def submit_bracket_order_with_retry(
        self,
        bracket: BracketOrder,
        max_retries: int = 3,
        base_delay: float = 0.5,
        timeout_seconds: float = 10.0,
    ) -> Optional[LiveOrder]:
        """
        Submit a bracket order with exponential backoff retry and timeout.

        Args:
            bracket: The BracketOrder to submit
            max_retries: Maximum number of retry attempts
            base_delay: Base delay in seconds (doubles each retry)
            timeout_seconds: Timeout per submission attempt

        Returns:
            LiveOrder if successful, None if all retries failed.
        """
        last_error = None

        for attempt in range(max_retries + 1):
            try:
                # Add timeout to prevent hanging on broker issues
                live_order = await asyncio.wait_for(
                    self.submit_bracket_order(bracket),
                    timeout=timeout_seconds
                )
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
            except asyncio.TimeoutError:
                last_error = f"Timeout after {timeout_seconds}s"
                logger.warning(
                    f"Bracket submission timed out (attempt {attempt + 1}/{max_retries + 1})"
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
            "pending_submissions": len(self._submission_tasks),
            "failed_submissions": len(self._failed_submissions),
            "broker_connected": self.rithmic._connected,
            "execution_halted": self.execution_manager.is_halted,
        }
