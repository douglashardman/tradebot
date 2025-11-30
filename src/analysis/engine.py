"""Order Flow Engine - orchestrates all order flow analysis."""

from typing import Callable, Dict, List, Any

from src.core.types import Tick, FootprintBar, Signal
from src.core.config import get_config
from src.core.constants import get_symbol_profile
from src.data.aggregator import FootprintAggregator, CumulativeDelta, VolumeProfile
from src.analysis.detectors import (
    ImbalanceDetector,
    ExhaustionDetector,
    AbsorptionDetector,
    DeltaDivergenceDetector,
    UnfinishedBusinessDetector,
)


class OrderFlowEngine:
    """
    Main engine orchestrating all order flow analysis.

    Processes ticks, builds footprint bars, runs pattern detectors,
    and emits signals.
    """

    def __init__(self, config: Dict[str, Any] = None):
        """
        Initialize the order flow engine.

        Args:
            config: Configuration dict. If None, uses global config.
        """
        self.config = config or get_config().get_section("order_flow")
        self.symbol = config.get("symbol", "MES") if config else "MES"
        self.timeframe = config.get("timeframe", 300) if config else 300

        # Get symbol-specific settings
        symbol_profile = get_symbol_profile(self.symbol)

        # Aggregation components
        self.aggregator = FootprintAggregator(self.timeframe)
        self.cumulative_delta = CumulativeDelta()
        self.volume_profile = VolumeProfile()

        # Pattern detectors
        self.detectors = [
            ImbalanceDetector(
                threshold=self.config.get("imbalance_threshold", 3.0),
                min_volume=self.config.get(
                    "imbalance_min_volume",
                    symbol_profile.get("imbalance_min_volume", 10)
                )
            ),
            ExhaustionDetector(
                min_levels=self.config.get("exhaustion_min_levels", 3),
                min_decline_pct=self.config.get("exhaustion_min_decline", 0.30)
            ),
            AbsorptionDetector(
                min_volume=self.config.get(
                    "absorption_min_volume",
                    symbol_profile.get("absorption_min_volume", 100)
                )
            ),
            DeltaDivergenceDetector(
                lookback=self.config.get("divergence_lookback", 5)
            ),
            UnfinishedBusinessDetector(
                max_volume_threshold=self.config.get("unfinished_max_volume", 5)
            ),
        ]

        # Callbacks
        self.signal_callbacks: List[Callable[[Signal], None]] = []
        self.bar_callbacks: List[Callable[[FootprintBar], None]] = []

        # Wire up bar completion
        self.aggregator.on_bar_complete(self._on_bar_complete)

        # Statistics
        self.tick_count = 0
        self.bar_count = 0
        self.signal_count = 0

    def process_tick(self, tick: Tick) -> None:
        """
        Process incoming tick data.

        Args:
            tick: The tick to process
        """
        self.tick_count += 1
        self.aggregator.process_tick(tick)

    def _on_bar_complete(self, bar: FootprintBar) -> None:
        """Handle bar completion - run analysis."""
        self.bar_count += 1

        # Update cumulative tracking
        self.cumulative_delta.update(bar)
        self.volume_profile.add_bar(bar)

        # Notify bar callbacks first
        for callback in self.bar_callbacks:
            callback(bar)

        # Run pattern detection
        self._analyze_bar(bar)

    def _analyze_bar(self, bar: FootprintBar) -> None:
        """Run all pattern detectors on completed bar."""
        signals = []

        for detector in self.detectors:
            # Standard detect method
            if hasattr(detector, 'detect'):
                signals.extend(detector.detect(bar))

            # Stacked imbalance detection
            if hasattr(detector, 'detect_stacked_imbalances'):
                signals.extend(detector.detect_stacked_imbalances(bar))

            # Divergence detector uses add_bar
            if hasattr(detector, 'add_bar'):
                signals.extend(detector.add_bar(bar))

            # Unfinished business revisit check
            if hasattr(detector, 'check_revisit'):
                signals.extend(detector.check_revisit(bar))

        # Emit all signals
        for signal in signals:
            self._emit_signal(signal)

    def _emit_signal(self, signal: Signal) -> None:
        """Emit signal to all registered callbacks."""
        self.signal_count += 1
        for callback in self.signal_callbacks:
            callback(signal)

    def on_signal(self, callback: Callable[[Signal], None]) -> None:
        """Register a callback for signal events."""
        self.signal_callbacks.append(callback)

    def on_bar(self, callback: Callable[[FootprintBar], None]) -> None:
        """Register a callback for bar completion events."""
        self.bar_callbacks.append(callback)

    def get_state(self) -> Dict[str, Any]:
        """Get current analysis state."""
        current_bar = self.aggregator.current_bar

        state = {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "tick_count": self.tick_count,
            "bar_count": self.bar_count,
            "signal_count": self.signal_count,
            "cumulative_delta": self.cumulative_delta.value,
            "delta_slope": self.cumulative_delta.get_slope(),
            "current_bar": None,
            "poc": None,
            "value_area": None,
        }

        if current_bar:
            state["current_bar"] = {
                "start_time": current_bar.start_time.isoformat(),
                "delta": current_bar.delta,
                "volume": current_bar.total_volume,
                "buy_volume": current_bar.buy_volume,
                "sell_volume": current_bar.sell_volume,
                "levels": len(current_bar.levels),
                "high": current_bar.high_price,
                "low": current_bar.low_price,
                "close": current_bar.close_price,
            }

        if self.volume_profile.levels:
            state["poc"] = self.volume_profile.get_poc()
            va = self.volume_profile.get_value_area()
            if va:
                state["value_area"] = {"low": va[0], "high": va[1]}

        return state

    def get_recent_bars(self, count: int = 10) -> List[FootprintBar]:
        """Get recent completed bars."""
        return self.aggregator.get_recent_bars(count)

    def get_unfinished_levels(self) -> List[Dict]:
        """Get all active unfinished business levels."""
        unfinished_detector = next(
            (d for d in self.detectors if isinstance(d, UnfinishedBusinessDetector)),
            None
        )
        if not unfinished_detector:
            return []

        levels = unfinished_detector.get_active_levels(self.symbol)
        return [
            {"price": price, "time": time.isoformat(), "type": direction}
            for price, time, direction in levels
        ]

    def reset(self) -> None:
        """Reset the engine state."""
        self.aggregator.reset()
        self.cumulative_delta.reset()
        self.volume_profile.reset()

        for detector in self.detectors:
            if hasattr(detector, 'reset'):
                detector.reset()

        self.tick_count = 0
        self.bar_count = 0
        self.signal_count = 0
