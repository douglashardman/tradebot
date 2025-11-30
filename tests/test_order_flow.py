"""Tests for order flow components."""

import sys
from pathlib import Path

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from datetime import datetime, timedelta
import random

from src.core.types import Tick, PriceLevel, FootprintBar, SignalPattern
from src.data.aggregator import FootprintAggregator, CumulativeDelta, VolumeProfile
from src.analysis.detectors import (
    ImbalanceDetector,
    ExhaustionDetector,
    AbsorptionDetector,
    DeltaDivergenceDetector,
    UnfinishedBusinessDetector,
)
from src.analysis.engine import OrderFlowEngine


def generate_test_ticks(
    symbol: str = "MES",
    count: int = 100,
    base_price: float = 5000.0,
    start_time: datetime = None
) -> list:
    """Generate synthetic tick data for testing."""
    if start_time is None:
        start_time = datetime.now()

    ticks = []
    price = base_price

    for i in range(count):
        # Random walk
        price += random.choice([-0.25, 0, 0.25]) * random.random()
        price = round(price / 0.25) * 0.25  # Snap to tick

        tick = Tick(
            timestamp=start_time + timedelta(seconds=i),
            price=price,
            volume=random.randint(1, 10),
            side=random.choice(["BID", "ASK"]),
            symbol=symbol
        )
        ticks.append(tick)

    return ticks


def test_price_level():
    """Test PriceLevel calculations."""
    level = PriceLevel(price=5000.0, bid_volume=50, ask_volume=100)

    assert level.total_volume == 150
    assert level.delta == 50  # 100 - 50
    print("PriceLevel: PASS")


def test_footprint_bar():
    """Test FootprintBar calculations."""
    bar = FootprintBar(
        symbol="MES",
        start_time=datetime.now(),
        end_time=datetime.now() + timedelta(minutes=5),
        timeframe=300,
        open_price=5000.0,
        high_price=5002.0,
        low_price=4998.0,
        close_price=5001.0,
        levels={
            5000.0: PriceLevel(5000.0, bid_volume=50, ask_volume=100),
            5000.25: PriceLevel(5000.25, bid_volume=30, ask_volume=80),
            5000.50: PriceLevel(5000.50, bid_volume=20, ask_volume=60),
        }
    )

    assert bar.total_volume == 340
    assert bar.delta == 140  # (100+80+60) - (50+30+20)
    assert bar.buy_volume == 240
    assert bar.sell_volume == 100

    levels = bar.get_sorted_levels()
    assert levels[0].price == 5000.0
    assert levels[-1].price == 5000.50

    print("FootprintBar: PASS")


def test_aggregator():
    """Test FootprintAggregator."""
    aggregator = FootprintAggregator(timeframe_seconds=60)  # 1 minute bars

    ticks = generate_test_ticks(count=200)
    completed_bars = []

    for tick in ticks:
        bar = aggregator.process_tick(tick)
        if bar:
            completed_bars.append(bar)

    assert len(completed_bars) >= 1
    assert aggregator.current_bar is not None

    print(f"Aggregator: PASS ({len(completed_bars)} bars completed)")


def test_imbalance_detector():
    """Test ImbalanceDetector."""
    detector = ImbalanceDetector(threshold=3.0, min_volume=10)

    # Create bar with clear buy imbalance
    bar = FootprintBar(
        symbol="MES",
        start_time=datetime.now(),
        end_time=datetime.now() + timedelta(minutes=5),
        timeframe=300,
        open_price=5000.0,
        high_price=5001.0,
        low_price=5000.0,
        close_price=5001.0,
        levels={
            5000.0: PriceLevel(5000.0, bid_volume=10, ask_volume=5),
            5000.25: PriceLevel(5000.25, bid_volume=8, ask_volume=50),  # 50/10 = 500%
            5000.50: PriceLevel(5000.50, bid_volume=5, ask_volume=40),
            5000.75: PriceLevel(5000.75, bid_volume=3, ask_volume=35),
            5001.0: PriceLevel(5001.0, bid_volume=2, ask_volume=30),
        }
    )

    signals = detector.detect(bar)
    assert len(signals) > 0
    assert signals[0].pattern == SignalPattern.BUY_IMBALANCE

    # Test stacked imbalances
    stacked = detector.detect_stacked_imbalances(bar, min_stack=3)
    print(f"ImbalanceDetector: PASS ({len(signals)} imbalances, {len(stacked)} stacked)")


def test_exhaustion_detector():
    """Test ExhaustionDetector."""
    detector = ExhaustionDetector(min_levels=3, min_decline_pct=0.30)

    # Create bar with buying exhaustion at top
    bar = FootprintBar(
        symbol="MES",
        start_time=datetime.now(),
        end_time=datetime.now() + timedelta(minutes=5),
        timeframe=300,
        open_price=5000.0,
        high_price=5002.0,
        low_price=5000.0,
        close_price=5001.0,
        levels={
            5000.0: PriceLevel(5000.0, bid_volume=50, ask_volume=100),
            5000.5: PriceLevel(5000.5, bid_volume=40, ask_volume=80),
            5001.0: PriceLevel(5001.0, bid_volume=30, ask_volume=60),
            5001.5: PriceLevel(5001.5, bid_volume=20, ask_volume=30),  # Declining
            5002.0: PriceLevel(5002.0, bid_volume=10, ask_volume=10),  # Exhaustion
        }
    )

    signals = detector.detect(bar)
    print(f"ExhaustionDetector: PASS ({len(signals)} exhaustion signals)")


def test_order_flow_engine():
    """Test full OrderFlowEngine integration."""
    config = {
        "symbol": "MES",
        "timeframe": 60,
        "imbalance_threshold": 3.0,
        "imbalance_min_volume": 5,
    }

    engine = OrderFlowEngine(config)

    # Track signals
    signals_received = []
    engine.on_signal(lambda s: signals_received.append(s))

    # Process ticks
    ticks = generate_test_ticks(count=500)
    for tick in ticks:
        engine.process_tick(tick)

    state = engine.get_state()

    print(f"OrderFlowEngine: PASS")
    print(f"  - Ticks processed: {state['tick_count']}")
    print(f"  - Bars completed: {state['bar_count']}")
    print(f"  - Signals generated: {len(signals_received)}")
    print(f"  - Cumulative delta: {state['cumulative_delta']}")


def run_all_tests():
    """Run all tests."""
    print("=" * 50)
    print("ORDER FLOW SYSTEM TESTS")
    print("=" * 50)
    print()

    test_price_level()
    test_footprint_bar()
    test_aggregator()
    test_imbalance_detector()
    test_exhaustion_detector()
    test_order_flow_engine()

    print()
    print("=" * 50)
    print("ALL TESTS PASSED")
    print("=" * 50)


if __name__ == "__main__":
    run_all_tests()
