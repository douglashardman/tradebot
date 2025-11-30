"""Imbalance detection - identifies aggressive buying/selling pressure."""

from typing import List, Tuple

from src.core.types import FootprintBar, Signal, SignalPattern
from src.core.constants import TICK_SIZES


class ImbalanceDetector:
    """
    Detects volume imbalances using diagonal comparison.

    An imbalance occurs when one side of the market is significantly
    more aggressive than the other at adjacent price levels.
    """

    def __init__(self, threshold: float = 3.0, min_volume: int = 10):
        """
        Initialize the detector.

        Args:
            threshold: Ratio required to flag imbalance (3.0 = 300%)
            min_volume: Minimum volume on dominant side to consider
        """
        self.threshold = threshold
        self.min_volume = min_volume

    def detect(self, bar: FootprintBar) -> List[Signal]:
        """
        Detect imbalances in a footprint bar.

        Uses diagonal comparison:
        - Buy imbalance: AskVolume[price] / BidVolume[price-1]
        - Sell imbalance: BidVolume[price] / AskVolume[price+1]
        """
        signals = []
        levels = bar.get_sorted_levels(ascending=True)

        if len(levels) < 2:
            return signals

        for i in range(1, len(levels)):
            current = levels[i]
            below = levels[i - 1]

            # Buy imbalance: aggressive buying lifting offers
            if below.bid_volume > 0 and current.ask_volume >= self.min_volume:
                ratio = current.ask_volume / below.bid_volume
                if ratio >= self.threshold:
                    signals.append(Signal(
                        timestamp=bar.end_time,
                        symbol=bar.symbol,
                        pattern=SignalPattern.BUY_IMBALANCE,
                        direction="LONG",
                        strength=min(ratio / 10, 1.0),
                        price=current.price,
                        details={
                            "ratio": round(ratio, 2),
                            "ask_volume": current.ask_volume,
                            "bid_volume_below": below.bid_volume,
                            "price_below": below.price,
                        }
                    ))

            # Sell imbalance: aggressive selling hitting bids
            if i < len(levels) - 1:
                above = levels[i + 1]
                if above.ask_volume > 0 and current.bid_volume >= self.min_volume:
                    ratio = current.bid_volume / above.ask_volume
                    if ratio >= self.threshold:
                        signals.append(Signal(
                            timestamp=bar.end_time,
                            symbol=bar.symbol,
                            pattern=SignalPattern.SELL_IMBALANCE,
                            direction="SHORT",
                            strength=min(ratio / 10, 1.0),
                            price=current.price,
                            details={
                                "ratio": round(ratio, 2),
                                "bid_volume": current.bid_volume,
                                "ask_volume_above": above.ask_volume,
                                "price_above": above.price,
                            }
                        ))

        return signals

    def detect_stacked_imbalances(self, bar: FootprintBar, min_stack: int = 3) -> List[Signal]:
        """
        Detect consecutive imbalances stacked vertically.

        Stacked imbalances are more significant - they show sustained
        aggressive activity across multiple price levels.

        Args:
            bar: The footprint bar to analyze
            min_stack: Minimum consecutive imbalances required

        Returns:
            List of stacked imbalance signals
        """
        imbalances = self.detect(bar)
        signals = []

        # Separate by direction
        buy_imbalances = sorted(
            [s for s in imbalances if s.direction == "LONG"],
            key=lambda x: x.price
        )
        sell_imbalances = sorted(
            [s for s in imbalances if s.direction == "SHORT"],
            key=lambda x: x.price
        )

        # Check for stacked buy imbalances
        buy_stacks = self._find_stacks(buy_imbalances, bar.symbol)
        for stack in buy_stacks:
            if len(stack) >= min_stack:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=bar.symbol,
                    pattern=SignalPattern.STACKED_BUY_IMBALANCE,
                    direction="LONG",
                    strength=min(len(stack) / 5, 1.0),
                    price=stack[-1].price,  # Top of stack
                    details={
                        "stack_size": len(stack),
                        "prices": [s.price for s in stack],
                        "bottom_price": stack[0].price,
                        "top_price": stack[-1].price,
                    }
                ))

        # Check for stacked sell imbalances
        sell_stacks = self._find_stacks(sell_imbalances, bar.symbol)
        for stack in sell_stacks:
            if len(stack) >= min_stack:
                signals.append(Signal(
                    timestamp=bar.end_time,
                    symbol=bar.symbol,
                    pattern=SignalPattern.STACKED_SELL_IMBALANCE,
                    direction="SHORT",
                    strength=min(len(stack) / 5, 1.0),
                    price=stack[0].price,  # Bottom of stack
                    details={
                        "stack_size": len(stack),
                        "prices": [s.price for s in stack],
                        "bottom_price": stack[0].price,
                        "top_price": stack[-1].price,
                    }
                ))

        return signals

    def _find_stacks(self, imbalances: List[Signal], symbol: str) -> List[List[Signal]]:
        """Find groups of consecutive price levels with imbalances."""
        if not imbalances:
            return []

        tick_size = TICK_SIZES.get(symbol[:3], TICK_SIZES.get(symbol[:2], 0.25))
        stacks = []
        current_stack = [imbalances[0]]

        for i in range(1, len(imbalances)):
            prev_price = imbalances[i - 1].price
            curr_price = imbalances[i].price
            expected_diff = tick_size

            # Check if prices are consecutive
            if abs(curr_price - prev_price - expected_diff) < 0.001:
                current_stack.append(imbalances[i])
            else:
                if len(current_stack) > 1:
                    stacks.append(current_stack)
                current_stack = [imbalances[i]]

        # Don't forget the last stack
        if len(current_stack) > 1:
            stacks.append(current_stack)

        return stacks
