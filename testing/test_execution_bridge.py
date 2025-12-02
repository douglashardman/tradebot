#!/usr/bin/env python3
"""
Test script to validate the execution bridge before live trading.

Tests:
1. Paper trade flow - signal generates order
2. Bracket order structure - entry + stop + target linked
3. Order state machine - PENDING → SUBMITTED → FILLED
4. Fill handling - position, balance, tier updates
5. Kill switch - flatten works
6. Reconnection - stale position detection

Usage:
    PYTHONPATH=. python scripts/test_execution_bridge.py
"""

import asyncio
import sys
import os
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.types import Signal, SignalPattern
from src.execution.session import TradingSession
from src.execution.manager import ExecutionManager
from src.execution.orders import BracketOrder, Position, Trade
from src.execution.bridge import ExecutionBridge
from src.data.adapters.rithmic import (
    RithmicAdapter, LiveOrder, LivePosition, OrderState
)
from src.core.capital import TierManager, TierState


class MockRithmicAdapter:
    """Mock Rithmic adapter for testing without broker connection."""

    def __init__(self):
        self._connected = True
        self.account_id = "TEST_ACCOUNT"
        self._orders = {}
        self._positions = {}
        self._on_fill_callbacks = []
        self._on_rejection_callbacks = []
        self._order_lock = asyncio.Lock()

        # Track submitted orders for verification
        self.submitted_orders = []
        self.cancelled_orders = []
        self.exit_requests = []

    def on_fill(self, callback):
        self._on_fill_callbacks.append(callback)

    def on_rejection(self, callback):
        self._on_rejection_callbacks.append(callback)

    async def submit_bracket_order(
        self,
        symbol: str,
        side: str,
        quantity: int,
        stop_ticks: int,
        target_ticks: int,
        exchange: str = "CME",
        bracket_id: Optional[str] = None,
    ) -> Optional[LiveOrder]:
        """Mock order submission."""
        import uuid
        order_id = str(uuid.uuid4())[:12]

        order = LiveOrder(
            order_id=order_id,
            symbol=symbol,
            exchange=exchange,
            side="BUY" if side == "LONG" else "SELL",
            quantity=quantity,
            order_type="MARKET",
            stop_ticks=stop_ticks,
            target_ticks=target_ticks,
            bracket_id=bracket_id or str(uuid.uuid4())[:8],
            is_entry=True,
            state=OrderState.SUBMITTED,
            submitted_at=datetime.now(timezone.utc),
        )

        self._orders[order_id] = order
        self.submitted_orders.append(order)

        return order

    async def simulate_fill(self, order: LiveOrder, fill_price: float):
        """Simulate a fill for testing."""
        order.state = OrderState.FILLED
        order.filled_quantity = order.quantity
        order.filled_price = fill_price
        order.filled_at = datetime.now(timezone.utc)

        fill_data = {
            "order_id": order.order_id,
            "fill_price": fill_price,
            "fill_qty": order.quantity,
            "order": order,
        }

        for callback in self._on_fill_callbacks:
            if asyncio.iscoroutinefunction(callback):
                await callback(fill_data)
            else:
                callback(fill_data)

    async def simulate_rejection(self, order: LiveOrder, reason: str):
        """Simulate a rejection for testing."""
        order.state = OrderState.REJECTED
        order.rejection_reason = reason

        rejection_data = {
            "order_id": order.order_id,
            "reason": reason,
            "order": order,
        }

        for callback in self._on_rejection_callbacks:
            if asyncio.iscoroutinefunction(callback):
                await callback(rejection_data)
            else:
                callback(rejection_data)

    async def exit_position(self, symbol: str, exchange: str = "CME") -> bool:
        """Mock position exit."""
        self.exit_requests.append({"symbol": symbol, "exchange": exchange})
        return True

    async def cancel_all_orders(self) -> bool:
        """Mock cancel all."""
        self.cancelled_orders.extend(list(self._orders.keys()))
        return True

    async def get_positions(self):
        """Return mock positions."""
        return list(self._positions.values())

    async def reconcile_positions(self):
        """Mock reconciliation."""
        return self._positions.copy()

    async def list_orders(self):
        """Return mock orders."""
        return list(self._orders.values())

    def get_tracked_orders(self):
        return self._orders.copy()

    def get_tracked_positions(self):
        return self._positions.copy()

    def add_mock_position(self, symbol: str, side: str, qty: int, price: float):
        """Add a mock broker position for testing reconciliation."""
        pos = {
            "symbol": symbol,
            "exchange": "CME",
            "quantity": qty if side == "LONG" else -qty,
            "avg_price": price,
        }
        self._positions[symbol] = pos


def test_passed(name: str):
    print(f"  ✅ {name}")

def test_failed(name: str, reason: str):
    print(f"  ❌ {name}: {reason}")
    return False


async def test_paper_trade_flow():
    """Test 1: Paper trade flow - signal generates order."""
    print("\n" + "="*60)
    print("TEST 1: Paper Trade Flow")
    print("="*60)

    # Setup
    session = TradingSession(
        mode="paper",
        symbol="MESH5",
        daily_profit_target=500,
        daily_loss_limit=-300,
        max_position_size=2,
        stop_loss_ticks=16,
        take_profit_ticks=24,
    )
    manager = ExecutionManager(session)

    # Create a signal
    signal = Signal(
        timestamp=datetime.now(),
        symbol="MESH5",
        pattern=SignalPattern.SELLING_EXHAUSTION,
        direction="LONG",
        strength=0.8,
        price=6100.00,
        approved=True,
    )

    # Process signal
    order = manager.on_signal(signal, absolute_size=1)

    # Verify
    all_passed = True

    if order is None:
        return test_failed("Order creation", "No order returned")
    test_passed("Order created")

    if order.side != "LONG":
        all_passed = test_failed("Order side", f"Expected LONG, got {order.side}")
    else:
        test_passed(f"Order side: {order.side}")

    if order.size != 1:
        all_passed = test_failed("Order size", f"Expected 1, got {order.size}")
    else:
        test_passed(f"Order size: {order.size}")

    if order.entry_price != 6100.00:
        all_passed = test_failed("Entry price", f"Expected 6100, got {order.entry_price}")
    else:
        test_passed(f"Entry price: {order.entry_price}")

    # In paper mode, should have created a position immediately
    if len(manager.open_positions) != 1:
        all_passed = test_failed("Position created", f"Expected 1 position, got {len(manager.open_positions)}")
    else:
        test_passed("Position created in paper mode")
        pos = manager.open_positions[0]
        test_passed(f"Position: {pos.side} {pos.size} @ {pos.entry_price}")

    return all_passed


async def test_bracket_order_structure():
    """Test 2: Bracket order structure - entry + stop + target linked."""
    print("\n" + "="*60)
    print("TEST 2: Bracket Order Structure")
    print("="*60)

    session = TradingSession(
        mode="paper",
        symbol="MESH5",
        stop_loss_ticks=16,
        take_profit_ticks=24,
    )
    manager = ExecutionManager(session)

    signal = Signal(
        timestamp=datetime.now(),
        symbol="MESH5",
        pattern=SignalPattern.BUYING_ABSORPTION,
        direction="LONG",
        strength=0.75,
        price=6100.00,
        approved=True,
    )

    order = manager.on_signal(signal, absolute_size=1)

    all_passed = True

    # Verify bracket structure
    tick_size = 0.25
    expected_stop = 6100.00 - (16 * tick_size)  # 6096.00
    expected_target = 6100.00 + (24 * tick_size)  # 6106.00

    if abs(order.stop_price - expected_stop) > 0.01:
        all_passed = test_failed("Stop price", f"Expected {expected_stop}, got {order.stop_price}")
    else:
        test_passed(f"Stop price: {order.stop_price} (16 ticks below entry)")

    if abs(order.target_price - expected_target) > 0.01:
        all_passed = test_failed("Target price", f"Expected {expected_target}, got {order.target_price}")
    else:
        test_passed(f"Target price: {order.target_price} (24 ticks above entry)")

    # Test SHORT order bracket
    signal_short = Signal(
        timestamp=datetime.now(),
        symbol="MESH5",
        pattern=SignalPattern.BUYING_EXHAUSTION,
        direction="SHORT",
        strength=0.75,
        price=6100.00,
        approved=True,
    )

    # Need a fresh manager for second order
    session2 = TradingSession(mode="paper", symbol="MESH5", stop_loss_ticks=16, take_profit_ticks=24)
    manager2 = ExecutionManager(session2)
    order_short = manager2.on_signal(signal_short, absolute_size=1)

    expected_stop_short = 6100.00 + (16 * tick_size)  # 6104.00
    expected_target_short = 6100.00 - (24 * tick_size)  # 6094.00

    if abs(order_short.stop_price - expected_stop_short) > 0.01:
        all_passed = test_failed("SHORT Stop price", f"Expected {expected_stop_short}, got {order_short.stop_price}")
    else:
        test_passed(f"SHORT Stop price: {order_short.stop_price} (16 ticks above entry)")

    if abs(order_short.target_price - expected_target_short) > 0.01:
        all_passed = test_failed("SHORT Target price", f"Expected {expected_target_short}, got {order_short.target_price}")
    else:
        test_passed(f"SHORT Target price: {order_short.target_price} (24 ticks below entry)")

    # Verify bracket_id linking
    if not order.bracket_id:
        all_passed = test_failed("Bracket ID", "No bracket_id set")
    else:
        test_passed(f"Bracket ID: {order.bracket_id}")

    return all_passed


async def test_order_state_machine():
    """Test 3: Order state machine transitions."""
    print("\n" + "="*60)
    print("TEST 3: Order State Machine")
    print("="*60)

    mock_rithmic = MockRithmicAdapter()
    session = TradingSession(mode="live", symbol="MESH5")
    manager = ExecutionManager(session)

    bridge = ExecutionBridge(
        execution_manager=manager,
        rithmic_adapter=mock_rithmic,
    )

    # Create a bracket order
    bracket = BracketOrder(
        symbol="MESH5",
        side="LONG",
        size=1,
        entry_price=6100.00,
        stop_price=6096.00,
        target_price=6106.00,
    )

    all_passed = True

    # Submit order
    live_order = await bridge.submit_bracket_order(bracket)

    if live_order is None:
        return test_failed("Order submission", "No order returned")
    test_passed("Order submitted")

    # Check initial state
    if live_order.state != OrderState.SUBMITTED:
        all_passed = test_failed("Initial state", f"Expected SUBMITTED, got {live_order.state}")
    else:
        test_passed(f"State: PENDING → SUBMITTED")

    # Simulate fill
    await mock_rithmic.simulate_fill(live_order, 6100.25)

    if live_order.state != OrderState.FILLED:
        all_passed = test_failed("Filled state", f"Expected FILLED, got {live_order.state}")
    else:
        test_passed(f"State: SUBMITTED → FILLED")

    if live_order.filled_price != 6100.25:
        all_passed = test_failed("Fill price", f"Expected 6100.25, got {live_order.filled_price}")
    else:
        test_passed(f"Fill price: {live_order.filled_price}")

    return all_passed


async def test_fill_handling():
    """Test 4: Fill handling - position, balance, tier updates."""
    print("\n" + "="*60)
    print("TEST 4: Fill Handling")
    print("="*60)

    mock_rithmic = MockRithmicAdapter()
    session = TradingSession(
        mode="live",
        symbol="MESH5",
        paper_starting_balance=5000,
    )
    manager = ExecutionManager(session)

    # Track callbacks
    position_callbacks = []
    trade_callbacks = []

    manager.on_position(lambda p: position_callbacks.append(p))
    manager.on_trade(lambda t: trade_callbacks.append(t))

    bridge = ExecutionBridge(
        execution_manager=manager,
        rithmic_adapter=mock_rithmic,
    )

    all_passed = True

    # Submit and fill an entry
    bracket = BracketOrder(
        symbol="MESH5",
        side="LONG",
        size=1,
        entry_price=6100.00,
        stop_price=6096.00,
        target_price=6106.00,
    )

    live_order = await bridge.submit_bracket_order(bracket)
    await mock_rithmic.simulate_fill(live_order, 6100.25)

    # Verify position created
    if len(manager.open_positions) != 1:
        all_passed = test_failed("Position count", f"Expected 1, got {len(manager.open_positions)}")
    else:
        test_passed(f"Position created: {len(manager.open_positions)}")
        pos = manager.open_positions[0]
        test_passed(f"Position: {pos.side} {pos.size} @ {pos.entry_price}")

    # Verify position callback fired
    if len(position_callbacks) != 1:
        all_passed = test_failed("Position callback", f"Expected 1 callback, got {len(position_callbacks)}")
    else:
        test_passed("Position callback fired")

    # Verify bracket is active
    if not bracket.is_active or not bracket.is_filled:
        all_passed = test_failed("Bracket state", "Bracket not marked as active/filled")
    else:
        test_passed("Bracket marked active and filled")

    # Now simulate exit fill (target hit)
    # Create a mock exit order
    exit_order = LiveOrder(
        order_id="exit-123",
        symbol="MESH5",
        exchange="CME",
        side="SELL",
        quantity=1,
        order_type="LIMIT",
        bracket_id=bracket.bracket_id,
        is_entry=False,
    )
    mock_rithmic._orders["exit-123"] = exit_order

    # Simulate exit fill at target
    await mock_rithmic.simulate_fill(exit_order, 6106.00)

    # Verify position closed
    if len(manager.open_positions) != 0:
        all_passed = test_failed("Position closed", f"Expected 0 positions, got {len(manager.open_positions)}")
    else:
        test_passed("Position closed after exit fill")

    # Verify trade recorded
    if len(manager.completed_trades) != 1:
        all_passed = test_failed("Trade recorded", f"Expected 1 trade, got {len(manager.completed_trades)}")
    else:
        trade = manager.completed_trades[0]
        test_passed(f"Trade recorded: {trade.side} @ {trade.entry_price} → {trade.exit_price}")
        test_passed(f"P&L: ${trade.pnl:.2f} ({trade.pnl_ticks} ticks)")

    # Verify daily P&L updated
    if manager.daily_pnl == 0:
        all_passed = test_failed("Daily P&L", "Daily P&L not updated")
    else:
        test_passed(f"Daily P&L updated: ${manager.daily_pnl:.2f}")

    return all_passed


async def test_kill_switch():
    """Test 5: Kill switch - flatten works."""
    print("\n" + "="*60)
    print("TEST 5: Kill Switch (Flatten)")
    print("="*60)

    mock_rithmic = MockRithmicAdapter()
    session = TradingSession(mode="live", symbol="MESH5")
    manager = ExecutionManager(session)

    bridge = ExecutionBridge(
        execution_manager=manager,
        rithmic_adapter=mock_rithmic,
    )

    all_passed = True

    # Create a position first
    bracket = BracketOrder(
        symbol="MESH5",
        side="LONG",
        size=2,
        entry_price=6100.00,
        stop_price=6096.00,
        target_price=6106.00,
    )

    live_order = await bridge.submit_bracket_order(bracket)
    await mock_rithmic.simulate_fill(live_order, 6100.00)

    # Verify position exists
    if len(manager.open_positions) != 1:
        return test_failed("Setup", "Failed to create position for test")
    test_passed("Position created for flatten test")

    # Call flatten
    success = await bridge.flatten_all()

    if not success:
        all_passed = test_failed("Flatten call", "flatten_all() returned False")
    else:
        test_passed("flatten_all() returned True")

    # Verify exit request was sent
    if len(mock_rithmic.exit_requests) != 1:
        all_passed = test_failed("Exit request", f"Expected 1 exit request, got {len(mock_rithmic.exit_requests)}")
    else:
        test_passed(f"Exit request sent: {mock_rithmic.exit_requests[0]}")

    return all_passed


async def test_rejection_handling():
    """Test rejection handling and cleanup."""
    print("\n" + "="*60)
    print("TEST 6: Rejection Handling")
    print("="*60)

    mock_rithmic = MockRithmicAdapter()
    session = TradingSession(mode="live", symbol="MESH5")
    manager = ExecutionManager(session)

    rejection_received = []

    def on_rejection(data):
        rejection_received.append(data)

    bridge = ExecutionBridge(
        execution_manager=manager,
        rithmic_adapter=mock_rithmic,
        on_rejection_callback=on_rejection,
    )

    all_passed = True

    # Submit order
    bracket = BracketOrder(
        symbol="MESH5",
        side="LONG",
        size=1,
        entry_price=6100.00,
        stop_price=6096.00,
        target_price=6106.00,
    )

    live_order = await bridge.submit_bracket_order(bracket)

    # Simulate rejection
    await mock_rithmic.simulate_rejection(live_order, "Insufficient buying power")

    # Verify rejection callback fired
    if len(rejection_received) != 1:
        all_passed = test_failed("Rejection callback", f"Expected 1 callback, got {len(rejection_received)}")
    else:
        test_passed("Rejection callback fired")
        test_passed(f"Rejection reason: {rejection_received[0].get('reason')}")

    # Verify order state
    if live_order.state != OrderState.REJECTED:
        all_passed = test_failed("Order state", f"Expected REJECTED, got {live_order.state}")
    else:
        test_passed("Order marked as REJECTED")

    # Verify no position was created
    if len(manager.open_positions) != 0:
        all_passed = test_failed("No position", f"Expected 0 positions, got {len(manager.open_positions)}")
    else:
        test_passed("No position created on rejection")

    return all_passed


async def test_reconciliation():
    """Test 7: Startup reconciliation detects stale positions."""
    print("\n" + "="*60)
    print("TEST 7: Position Reconciliation")
    print("="*60)

    mock_rithmic = MockRithmicAdapter()
    session = TradingSession(mode="live", symbol="MESH5")
    manager = ExecutionManager(session)

    bridge = ExecutionBridge(
        execution_manager=manager,
        rithmic_adapter=mock_rithmic,
    )

    all_passed = True

    # Test 1: Clean startup (no broker positions, no local positions)
    result = await bridge.reconcile_on_startup()

    if not result.get("reconciled"):
        all_passed = test_failed("Clean startup", "Reconciliation failed on clean state")
    else:
        test_passed("Clean startup: reconciled OK")

    # Test 2: Stale position (broker has position, we don't)
    mock_rithmic.add_mock_position("MESH5", "LONG", 2, 6100.00)

    result = await bridge.reconcile_on_startup()

    if result.get("reconciled"):
        all_passed = test_failed("Stale position", "Should have detected mismatch")
    else:
        test_passed("Stale position detected: reconciliation blocked")

    if not manager.is_halted:
        all_passed = test_failed("Halt on mismatch", "Manager should be halted")
    else:
        test_passed(f"Manager halted: {manager.halt_reason}")

    return all_passed


async def test_tier_integration():
    """Test 8: Tier manager integration with fills."""
    print("\n" + "="*60)
    print("TEST 8: Tier Manager Integration")
    print("="*60)

    # Create tier manager
    tier_state = TierState(
        balance=5000.0,
        tier_index=2,
        tier_name="ES Entry",
        instrument="ES",
        max_contracts=1,
        daily_loss_limit=-150,
    )
    tier_manager = TierManager(tier_state)

    all_passed = True

    initial_balance = tier_manager.state.balance
    test_passed(f"Initial balance: ${initial_balance:,.2f}")

    # Simulate winning trade
    tier_manager.record_trade(150.0)  # Win $150

    if tier_manager.state.balance != initial_balance + 150:
        all_passed = test_failed("Balance update", f"Expected {initial_balance + 150}, got {tier_manager.state.balance}")
    else:
        test_passed(f"Balance after win: ${tier_manager.state.balance:,.2f}")

    if tier_manager.state.win_streak != 1:
        all_passed = test_failed("Win streak", f"Expected 1, got {tier_manager.state.win_streak}")
    else:
        test_passed(f"Win streak: {tier_manager.state.win_streak}")

    # Simulate losing trade
    tier_manager.record_trade(-75.0)

    if tier_manager.state.loss_streak != 1:
        all_passed = test_failed("Loss streak", f"Expected 1, got {tier_manager.state.loss_streak}")
    else:
        test_passed(f"Loss streak: {tier_manager.state.loss_streak}")

    if tier_manager.state.win_streak != 0:
        all_passed = test_failed("Win streak reset", f"Expected 0, got {tier_manager.state.win_streak}")
    else:
        test_passed("Win streak reset on loss")

    return all_passed


async def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("EXECUTION BRIDGE VALIDATION TESTS")
    print("="*60)

    tests = [
        ("Paper Trade Flow", test_paper_trade_flow),
        ("Bracket Order Structure", test_bracket_order_structure),
        ("Order State Machine", test_order_state_machine),
        ("Fill Handling", test_fill_handling),
        ("Kill Switch", test_kill_switch),
        ("Rejection Handling", test_rejection_handling),
        ("Position Reconciliation", test_reconciliation),
        ("Tier Integration", test_tier_integration),
    ]

    results = []
    for name, test_func in tests:
        try:
            passed = await test_func()
            results.append((name, passed))
        except Exception as e:
            print(f"\n  💥 EXCEPTION: {e}")
            import traceback
            traceback.print_exc()
            results.append((name, False))

    # Summary
    print("\n" + "="*60)
    print("SUMMARY")
    print("="*60)

    passed_count = sum(1 for _, passed in results if passed)
    total_count = len(results)

    for name, passed in results:
        status = "✅ PASSED" if passed else "❌ FAILED"
        print(f"  {status}: {name}")

    print("\n" + "-"*40)
    print(f"  Total: {passed_count}/{total_count} tests passed")

    if passed_count == total_count:
        print("\n  🎉 ALL TESTS PASSED - Ready for paper testing!")
    else:
        print("\n  ⚠️  Some tests failed - Review before proceeding!")
        return 1

    return 0


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
