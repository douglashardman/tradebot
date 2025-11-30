"""Exhaustion detection - identifies declining volume at price extremes."""

from typing import Dict, List, Optional

from src.core.types import FootprintBar, Signal, SignalPattern


class ExhaustionDetector:
    """
    Detects exhaustion patterns at bar extremes.

    Exhaustion occurs when volume progressively declines at higher prices
    (buying exhaustion) or lower prices (selling exhaustion), indicating
    the move may be running out of steam.
    """

    def __init__(self, min_levels: int = 3, min_decline_pct: float = 0.30):
        """
        Initialize the detector.

        Args:
            min_levels: Minimum consecutive levels showing decline
            min_decline_pct: Minimum overall decline percentage (0.30 = 30%)
        """
        self.min_levels = min_levels
        self.min_decline_pct = min_decline_pct

    def detect(self, bar: FootprintBar) -> List[Signal]:
        """Detect exhaustion patterns at bar extremes."""
        signals = []
        levels = bar.get_sorted_levels(ascending=True)

        if len(levels) < self.min_levels:
            return signals

        # Check for buying exhaustion at bar top
        # Look for declining ask volume as price increases
        top_levels = levels[-(self.min_levels + 2):]
        if len(top_levels) >= self.min_levels:
            buy_exhaustion = self._check_exhaustion(
                [level.ask_volume for level in top_levels],
                ascending_price=True
            )

            if buy_exhaustion:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=bar.symbol,
                    pattern=SignalPattern.BUYING_EXHAUSTION,
                    direction="SHORT",  # Exhausted buyers = potential reversal
                    strength=buy_exhaustion["strength"],
                    price=bar.high_price,
                    details=buy_exhaustion
                ))

        # Check for selling exhaustion at bar bottom
        # Look for declining bid volume as price decreases
        bottom_levels = levels[:self.min_levels + 2]
        if len(bottom_levels) >= self.min_levels:
            sell_exhaustion = self._check_exhaustion(
                [level.bid_volume for level in reversed(bottom_levels)],
                ascending_price=False
            )

            if sell_exhaustion:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=bar.symbol,
                    pattern=SignalPattern.SELLING_EXHAUSTION,
                    direction="LONG",  # Exhausted sellers = potential reversal
                    strength=sell_exhaustion["strength"],
                    price=bar.low_price,
                    details=sell_exhaustion
                ))

        return signals

    def _check_exhaustion(
        self,
        volumes: List[int],
        ascending_price: bool
    ) -> Optional[Dict]:
        """
        Check if volumes show sequential decline.

        Args:
            volumes: List of volumes at consecutive price levels
            ascending_price: True if prices are ascending (for buying exhaustion)

        Returns:
            Dict with exhaustion details if detected, None otherwise
        """
        if len(volumes) < self.min_levels:
            return None

        # Count consecutive declines
        declines = 0
        for i in range(1, len(volumes)):
            if volumes[i] < volumes[i - 1]:
                declines += 1
            else:
                break  # Sequence broken

        if declines < self.min_levels - 1:
            return None

        # Calculate overall decline percentage
        if volumes[0] == 0:
            return None

        decline_pct = (volumes[0] - volumes[declines]) / volumes[0]

        if decline_pct < self.min_decline_pct:
            return None

        return {
            "consecutive_declines": declines,
            "decline_percentage": round(decline_pct, 3),
            "volumes": volumes[:declines + 1],
            "strength": min(decline_pct, 1.0),
            "pattern_type": "buying_exhaustion" if ascending_price else "selling_exhaustion"
        }
