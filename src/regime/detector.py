"""Regime detection - classifies current market state."""

from datetime import datetime
from typing import Dict, List, Tuple

from src.core.types import Regime, RegimeInputs


DEFAULT_REGIME_CONFIG = {
    "min_regime_score": 4.0,
    "adx_trend_threshold": 25,
    "adx_weak_threshold": 20,
    "atr_high_percentile": 70,
    "atr_extreme_percentile": 85,
    "min_bars_in_regime": 2,
    "min_regime_confidence": 0.6,
    "news_buffer_minutes": 15,
    "no_trade_before_open_minutes": 5,
    "no_trade_before_close_minutes": 15,
}


class RegimeDetector:
    """
    Classifies current market regime based on technical and order flow inputs.

    Regimes:
    - TRENDING_UP: Strong bullish trend with momentum
    - TRENDING_DOWN: Strong bearish trend with momentum
    - RANGING: Low volatility, mean-reverting behavior
    - VOLATILE: High volatility, choppy conditions
    - NO_TRADE: Avoid trading (news, session edges, low volume)
    """

    def __init__(self, config: dict = None):
        self.config = {**DEFAULT_REGIME_CONFIG, **(config or {})}
        self.current_regime = Regime.NO_TRADE
        self.regime_confidence = 0.0
        self.regime_history: List[Tuple[datetime, Regime, float]] = []

        # Regime persistence counter
        self._regime_count = 0

    def classify(self, inputs: RegimeInputs) -> Tuple[Regime, float]:
        """
        Classify current market regime.

        Args:
            inputs: All calculated regime inputs

        Returns:
            Tuple of (Regime, confidence 0.0-1.0)
        """
        # Hard overrides - these always take precedence
        if self._should_not_trade(inputs):
            return self._update(Regime.NO_TRADE, 1.0)

        # Score each regime
        scores = {
            Regime.TRENDING_UP: self._score_trending_up(inputs),
            Regime.TRENDING_DOWN: self._score_trending_down(inputs),
            Regime.RANGING: self._score_ranging(inputs),
            Regime.VOLATILE: self._score_volatile(inputs),
        }

        # Find winner
        sorted_scores = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        winner, winner_score = sorted_scores[0]
        runner_up_score = sorted_scores[1][1]

        # No clear winner
        if winner_score == 0:
            return self._update(Regime.NO_TRADE, 0.5)

        # Calculate confidence based on margin
        margin = (winner_score - runner_up_score) / winner_score
        confidence = min(0.5 + (margin * 0.5), 1.0)

        # Score too low - default to volatile
        if winner_score < self.config["min_regime_score"]:
            return self._update(Regime.VOLATILE, 0.5)

        return self._update(winner, confidence)

    def _should_not_trade(self, inputs: RegimeInputs) -> bool:
        """Check for conditions that should prevent trading."""
        # Too close to session close
        if inputs.minutes_to_close < self.config["no_trade_before_close_minutes"]:
            return True

        # In news window
        if inputs.is_news_window:
            return True

        # Too close to session open
        if inputs.minutes_since_open < self.config["no_trade_before_open_minutes"]:
            return True

        # Volume too low (less than 30% of average)
        if inputs.volume_vs_average < 0.3:
            return True

        return False

    def _score_trending_up(self, inputs: RegimeInputs) -> float:
        """Score likelihood of uptrend regime."""
        score = 0.0

        # ADX indicates trend
        if inputs.adx_14 > self.config["adx_trend_threshold"]:
            score += 2.0
        elif inputs.adx_14 > self.config["adx_weak_threshold"]:
            score += 1.0

        # EMA crossover bullish
        if inputs.ema_trend > 0:
            score += 1.5

        # Price above VWAP
        if inputs.price_vs_vwap > 0:
            score += 1.0

        # Market structure bullish
        if inputs.higher_highs and inputs.higher_lows:
            score += 2.0
        elif inputs.higher_lows:
            score += 1.0

        # Delta confirms bullish
        if inputs.cumulative_delta > 0 and inputs.delta_slope > 0:
            score += 1.5
        elif inputs.cumulative_delta > 0:
            score += 0.5

        # ADX trending (slope positive = strengthening trend)
        if inputs.adx_slope > 0:
            score += 0.5

        return score

    def _score_trending_down(self, inputs: RegimeInputs) -> float:
        """Score likelihood of downtrend regime."""
        score = 0.0

        # ADX indicates trend
        if inputs.adx_14 > self.config["adx_trend_threshold"]:
            score += 2.0
        elif inputs.adx_14 > self.config["adx_weak_threshold"]:
            score += 1.0

        # EMA crossover bearish
        if inputs.ema_trend < 0:
            score += 1.5

        # Price below VWAP
        if inputs.price_vs_vwap < 0:
            score += 1.0

        # Market structure bearish
        if inputs.lower_highs and inputs.lower_lows:
            score += 2.0
        elif inputs.lower_highs:
            score += 1.0

        # Delta confirms bearish
        if inputs.cumulative_delta < 0 and inputs.delta_slope < 0:
            score += 1.5
        elif inputs.cumulative_delta < 0:
            score += 0.5

        # ADX trending
        if inputs.adx_slope > 0:
            score += 0.5

        return score

    def _score_ranging(self, inputs: RegimeInputs) -> float:
        """Score likelihood of ranging/consolidating regime."""
        score = 0.0

        # Low ADX = no trend
        if inputs.adx_14 < self.config["adx_weak_threshold"]:
            score += 2.0
        elif inputs.adx_14 < self.config["adx_trend_threshold"]:
            score += 1.0

        # Price near VWAP
        if abs(inputs.price_vs_vwap) < 0.5:
            score += 1.0

        # No clear market structure
        if not (inputs.higher_highs or inputs.lower_lows):
            score += 1.5

        # Multiple bars in range
        if inputs.range_bound_bars >= 3:
            score += 2.0
        elif inputs.range_bound_bars >= 2:
            score += 1.0

        # Neutral delta
        if abs(inputs.cumulative_delta) < 500:
            score += 1.0

        # Low volatility
        if inputs.atr_percentile < 50:
            score += 1.0

        return score

    def _score_volatile(self, inputs: RegimeInputs) -> float:
        """Score likelihood of volatile/choppy regime."""
        score = 0.0

        # High ATR percentile
        if inputs.atr_percentile > self.config["atr_extreme_percentile"]:
            score += 2.5
        elif inputs.atr_percentile > self.config["atr_high_percentile"]:
            score += 1.5

        # Wide bar ranges
        if inputs.bar_range_avg > inputs.atr_14 * 1.5:
            score += 1.5

        # High volume
        if inputs.volume_vs_average > 2.0:
            score += 1.0

        # ADX declining from trend (trend losing steam)
        if self.config["adx_weak_threshold"] <= inputs.adx_14 <= self.config["adx_trend_threshold"]:
            if inputs.adx_slope < 0:
                score += 1.0

        # Rapid delta changes
        if abs(inputs.delta_slope) > 100:
            score += 1.0

        return score

    def _update(self, regime: Regime, confidence: float) -> Tuple[Regime, float]:
        """Update current regime and history."""
        now = datetime.now()

        # Check if regime is changing
        if regime != self.current_regime:
            self._regime_count = 1
        else:
            self._regime_count += 1

        # Only record significant changes
        if (not self.regime_history or
            self.regime_history[-1][1] != regime or
            abs(self.regime_history[-1][2] - confidence) > 0.2):

            self.regime_history.append((now, regime, confidence))

            # Keep history bounded
            if len(self.regime_history) > 100:
                self.regime_history = self.regime_history[-100:]

        self.current_regime = regime
        self.regime_confidence = confidence

        return regime, confidence

    def get_regime_duration(self) -> int:
        """Get how many bars the current regime has persisted."""
        return self._regime_count

    def get_recent_history(self, count: int = 10) -> List[Tuple[datetime, Regime, float]]:
        """Get recent regime history."""
        return self.regime_history[-count:]

    def reset(self) -> None:
        """Reset the detector."""
        self.current_regime = Regime.NO_TRADE
        self.regime_confidence = 0.0
        self.regime_history.clear()
        self._regime_count = 0
