"""Strategy router - filters signals based on current regime."""

from typing import Dict, List, Optional, Any

from src.core.types import Signal, SignalPattern, Regime, FootprintBar
from src.regime.detector import RegimeDetector
from src.regime.inputs import RegimeInputsCalculator


# Define which patterns are enabled/disabled for each regime
STRATEGY_REGIME_MAP: Dict[Regime, Dict[str, Any]] = {
    Regime.TRENDING_UP: {
        "enabled_patterns": [
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.BUYING_ABSORPTION,
            SignalPattern.SELLING_EXHAUSTION,
            SignalPattern.BULLISH_DELTA_DIVERGENCE,
            SignalPattern.BUY_IMBALANCE,
        ],
        "disabled_patterns": [
            SignalPattern.SELL_IMBALANCE,
            SignalPattern.STACKED_SELL_IMBALANCE,
            SignalPattern.BEARISH_DELTA_DIVERGENCE,
        ],
        "bias": "LONG",
        "position_size_multiplier": 1.0,
        "description": "Trend following - favor long entries with momentum",
    },

    Regime.TRENDING_DOWN: {
        "enabled_patterns": [
            SignalPattern.STACKED_SELL_IMBALANCE,
            SignalPattern.SELLING_ABSORPTION,
            SignalPattern.BUYING_EXHAUSTION,
            SignalPattern.BEARISH_DELTA_DIVERGENCE,
            SignalPattern.SELL_IMBALANCE,
        ],
        "disabled_patterns": [
            SignalPattern.BUY_IMBALANCE,
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.BULLISH_DELTA_DIVERGENCE,
        ],
        "bias": "SHORT",
        "position_size_multiplier": 1.0,
        "description": "Trend following - favor short entries with momentum",
    },

    Regime.RANGING: {
        "enabled_patterns": [
            SignalPattern.BUYING_EXHAUSTION,
            SignalPattern.SELLING_EXHAUSTION,
            SignalPattern.BUYING_ABSORPTION,
            SignalPattern.SELLING_ABSORPTION,
            SignalPattern.UNFINISHED_HIGH,
            SignalPattern.UNFINISHED_LOW,
        ],
        "disabled_patterns": [
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.STACKED_SELL_IMBALANCE,
        ],
        "bias": None,  # Trade both directions
        "position_size_multiplier": 0.75,
        "description": "Mean reversion - trade extremes and reversals",
    },

    Regime.VOLATILE: {
        "enabled_patterns": [
            SignalPattern.STACKED_BUY_IMBALANCE,
            SignalPattern.STACKED_SELL_IMBALANCE,
        ],
        "disabled_patterns": [
            SignalPattern.BUY_IMBALANCE,
            SignalPattern.SELL_IMBALANCE,
            SignalPattern.BUYING_EXHAUSTION,
            SignalPattern.SELLING_EXHAUSTION,
        ],
        "bias": None,
        "position_size_multiplier": 0.5,
        "description": "High volatility - only trade strongest signals",
    },

    Regime.NO_TRADE: {
        "enabled_patterns": [],
        "disabled_patterns": [],  # All disabled
        "bias": None,
        "position_size_multiplier": 0,
        "description": "No trading - sit out",
    },
}


class StrategyRouter:
    """
    Routes signals through regime filter and applies position sizing.

    Evaluates each signal against the current market regime to determine
    if it should be traded and with what position size.
    """

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.regime_detector = RegimeDetector(config.get("regime", {}))
        self.inputs_calculator = RegimeInputsCalculator(config)

        self.current_regime = Regime.NO_TRADE
        self.regime_confidence = 0.0

        # Statistics
        self.signals_evaluated = 0
        self.signals_approved = 0
        self.signals_rejected = 0

    def on_bar(self, bar: FootprintBar) -> None:
        """
        Update regime on each bar completion.

        Args:
            bar: Completed footprint bar
        """
        self.inputs_calculator.add_bar(bar)
        inputs = self.inputs_calculator.calculate()
        self.current_regime, self.regime_confidence = self.regime_detector.classify(inputs)

    def evaluate_signal(self, signal: Signal) -> Signal:
        """
        Evaluate a signal against current regime.

        Annotates the signal with:
        - regime: Current regime name
        - approved: Whether the signal should be traded
        - rejection_reason: Why it was rejected (if applicable)

        Args:
            signal: The signal to evaluate

        Returns:
            The annotated signal
        """
        self.signals_evaluated += 1
        signal.regime = self.current_regime.value

        regime_config = STRATEGY_REGIME_MAP.get(self.current_regime)
        if not regime_config:
            signal.approved = False
            signal.rejection_reason = "Unknown regime"
            self.signals_rejected += 1
            return signal

        # Check if pattern is explicitly disabled
        if signal.pattern in regime_config.get("disabled_patterns", []):
            signal.approved = False
            signal.rejection_reason = f"Pattern disabled in {self.current_regime.value}"
            self.signals_rejected += 1
            return signal

        # Check if pattern is in enabled list (if list exists)
        enabled = regime_config.get("enabled_patterns")
        if enabled and signal.pattern not in enabled:
            signal.approved = False
            signal.rejection_reason = f"Pattern not enabled for {self.current_regime.value}"
            self.signals_rejected += 1
            return signal

        # Check bias alignment
        bias = regime_config.get("bias")
        if bias and signal.direction != bias:
            signal.approved = False
            signal.rejection_reason = f"Direction {signal.direction} conflicts with {bias} bias"
            self.signals_rejected += 1
            return signal

        # Check minimum signal strength
        min_strength = self.config.get("min_signal_strength", 0.5)
        if signal.strength < min_strength:
            signal.approved = False
            signal.rejection_reason = f"Strength {signal.strength:.2f} below minimum {min_strength}"
            self.signals_rejected += 1
            return signal

        # Check regime confidence
        min_confidence = self.config.get("min_regime_confidence", 0.6)
        if self.regime_confidence < min_confidence:
            signal.approved = False
            signal.rejection_reason = f"Regime confidence {self.regime_confidence:.2f} below minimum"
            self.signals_rejected += 1
            return signal

        # Signal approved!
        signal.approved = True
        self.signals_approved += 1
        return signal

    def get_position_size_multiplier(self) -> float:
        """Get position size multiplier for current regime."""
        regime_config = STRATEGY_REGIME_MAP.get(self.current_regime, {})
        return regime_config.get("position_size_multiplier", 0)

    def get_current_bias(self) -> Optional[str]:
        """Get current directional bias (LONG, SHORT, or None)."""
        regime_config = STRATEGY_REGIME_MAP.get(self.current_regime, {})
        return regime_config.get("bias")

    def get_state(self) -> Dict[str, Any]:
        """Get current router state."""
        regime_config = STRATEGY_REGIME_MAP.get(self.current_regime, {})

        return {
            "current_regime": self.current_regime.value,
            "regime_confidence": round(self.regime_confidence, 3),
            "regime_duration": self.regime_detector.get_regime_duration(),
            "bias": regime_config.get("bias"),
            "position_multiplier": regime_config.get("position_size_multiplier", 0),
            "description": regime_config.get("description", ""),
            "signals_evaluated": self.signals_evaluated,
            "signals_approved": self.signals_approved,
            "signals_rejected": self.signals_rejected,
            "approval_rate": (
                self.signals_approved / self.signals_evaluated
                if self.signals_evaluated > 0 else 0
            ),
        }

    def reset(self) -> None:
        """Reset the router state."""
        self.regime_detector.reset()
        self.inputs_calculator.reset()
        self.current_regime = Regime.NO_TRADE
        self.regime_confidence = 0.0
        self.signals_evaluated = 0
        self.signals_approved = 0
        self.signals_rejected = 0
