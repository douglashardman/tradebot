"""Databento data feed adapter for live and historical data."""

import os
from datetime import datetime, timezone
from typing import Callable, List, Optional
import threading

from src.core.types import Tick


class DatabentoAdapter:
    """
    Adapter for Databento data feed.

    Supports both live streaming and historical replay.
    Uses the same interface for both, making backtesting seamless.

    Databento symbols for ES futures:
    - Live: "ES.FUT" (continuous front month)
    - Historical: "ESH4" (specific contract, e.g., March 2024)

    Dataset for CME futures: "GLBX.MDP3"
    """

    def __init__(self, api_key: str = None):
        """
        Initialize the Databento adapter.

        Args:
            api_key: Databento API key. If None, reads from DATABENTO_API_KEY env var.
        """
        self.api_key = api_key or os.getenv("DATABENTO_API_KEY")
        if not self.api_key:
            raise ValueError(
                "Databento API key required. Set DATABENTO_API_KEY env var or pass api_key."
            )

        self.callbacks: List[Callable[[Tick], None]] = []
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # Symbol mapping: our symbol -> Databento symbol
        self.symbol_map = {
            "ES": "ES.FUT",
            "MES": "MES.FUT",
            "NQ": "NQ.FUT",
            "MNQ": "MNQ.FUT",
            "CL": "CL.FUT",
            "GC": "GC.FUT",
        }

    def register_callback(self, callback: Callable[[Tick], None]) -> None:
        """Register a callback to receive ticks."""
        self.callbacks.append(callback)

    def _emit_tick(self, tick: Tick) -> None:
        """Emit tick to all registered callbacks."""
        for callback in self.callbacks:
            callback(tick)

    def _convert_trade(self, record, symbol: str) -> Tick:
        """
        Convert Databento trade record to our Tick format.

        Databento trade fields:
        - ts_event: Exchange timestamp (nanoseconds)
        - price: Trade price (as integer, divide by 1e9)
        - size: Trade size
        - side: 'A' (ask/sell aggressor), 'B' (bid/buy aggressor), 'N' (none)
        """
        # Convert nanosecond timestamp to datetime
        ts_ns = record.ts_event
        ts_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)

        # Convert price (Databento uses fixed-point integers)
        price = float(record.price) / 1e9

        # Map side: Databento 'A' = sell aggressor (our "BID"), 'B' = buy aggressor (our "ASK")
        # This is because:
        # - 'A' (Ask) means someone hit the ask with a market buy -> buy aggressor -> "ASK" in our system
        # - 'B' (Bid) means someone hit the bid with a market sell -> sell aggressor -> "BID" in our system
        # Wait, let me reconsider:
        # Databento docs say: "Ask for a sell aggressor, Bid for a buy aggressor"
        # So 'A' = sell aggressor = hitting bids = "BID" in our terminology
        # And 'B' = buy aggressor = lifting offers = "ASK" in our terminology

        if record.side == 'A':
            side = "BID"  # Sell aggressor hits bids
        elif record.side == 'B':
            side = "ASK"  # Buy aggressor lifts asks
        else:
            # No side specified - default based on tick direction or skip
            side = "ASK"  # Default to buy

        return Tick(
            timestamp=ts_dt,
            price=price,
            volume=int(record.size),
            side=side,
            symbol=symbol
        )

    def start_live(self, symbol: str, dataset: str = "GLBX.MDP3") -> None:
        """
        Start live data streaming.

        Args:
            symbol: Our symbol (e.g., "ES", "MES")
            dataset: Databento dataset ID (default CME Globex)
        """
        try:
            import databento as db
        except ImportError:
            raise ImportError("databento package required. Install with: pip install databento")

        db_symbol = self.symbol_map.get(symbol, f"{symbol}.FUT")

        def _stream():
            client = db.Live(key=self.api_key)
            client.subscribe(
                dataset=dataset,
                schema="trades",
                stype_in="parent",
                symbols=db_symbol,
            )

            self._running = True
            for record in client:
                if not self._running:
                    break

                try:
                    tick = self._convert_trade(record, symbol)
                    self._emit_tick(tick)
                except Exception as e:
                    print(f"Error processing trade: {e}")

        self._thread = threading.Thread(target=_stream, daemon=True)
        self._thread.start()

    def stop_live(self) -> None:
        """Stop live data streaming."""
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)
            self._thread = None

    def get_historical(
        self,
        symbol: str,
        start: str,
        end: str,
        dataset: str = "GLBX.MDP3"
    ) -> List[Tick]:
        """
        Get historical tick data.

        Args:
            symbol: Contract symbol (e.g., "ESH4" for March 2024 ES)
            start: Start date (YYYY-MM-DD)
            end: End date (YYYY-MM-DD)
            dataset: Databento dataset ID

        Returns:
            List of Tick objects
        """
        try:
            import databento as db
        except ImportError:
            raise ImportError("databento package required. Install with: pip install databento")

        client = db.Historical(key=self.api_key)

        data = client.timeseries.get_range(
            dataset=dataset,
            symbols=symbol,
            schema="trades",
            start=start,
            end=end,
        )

        # Determine our symbol from the contract symbol
        our_symbol = symbol[:2] if symbol[:3] not in ["MES", "MNQ"] else symbol[:3]

        ticks = []
        for record in data:
            try:
                tick = self._convert_trade(record, our_symbol)
                ticks.append(tick)
            except Exception as e:
                print(f"Error converting trade: {e}")

        return ticks

    def replay_historical(
        self,
        symbol: str,
        start: str,
        end: str,
        speed: float = 1.0,
        dataset: str = "GLBX.MDP3"
    ) -> None:
        """
        Replay historical data through callbacks at specified speed.

        Args:
            symbol: Contract symbol
            start: Start date
            end: End date
            speed: Replay speed multiplier (1.0 = real-time, 10.0 = 10x faster)
            dataset: Databento dataset ID
        """
        import time

        ticks = self.get_historical(symbol, start, end, dataset)

        if not ticks:
            print("No historical data found")
            return

        print(f"Replaying {len(ticks)} ticks from {start} to {end}")

        self._running = True
        last_ts = ticks[0].timestamp

        for tick in ticks:
            if not self._running:
                break

            # Calculate delay based on timestamp difference
            if speed > 0:
                delta = (tick.timestamp - last_ts).total_seconds()
                if delta > 0:
                    time.sleep(delta / speed)

            self._emit_tick(tick)
            last_ts = tick.timestamp

        self._running = False
        print("Replay complete")


class DatabentoHistoricalLoader:
    """
    Utility class for loading and caching historical data.

    Useful for backtesting without re-downloading data.
    """

    def __init__(self, api_key: str = None, cache_dir: str = "data/cache"):
        self.adapter = DatabentoAdapter(api_key)
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)

    def load(
        self,
        symbol: str,
        start: str,
        end: str,
        use_cache: bool = True
    ) -> List[Tick]:
        """
        Load historical data, using cache if available.

        Args:
            symbol: Contract symbol
            start: Start date
            end: End date
            use_cache: Whether to use cached data

        Returns:
            List of Tick objects
        """
        import json

        cache_file = os.path.join(
            self.cache_dir,
            f"{symbol}_{start}_{end}.json"
        )

        # Try cache first
        if use_cache and os.path.exists(cache_file):
            print(f"Loading from cache: {cache_file}")
            with open(cache_file) as f:
                data = json.load(f)

            ticks = []
            for d in data:
                ticks.append(Tick(
                    timestamp=datetime.fromisoformat(d["timestamp"]),
                    price=d["price"],
                    volume=d["volume"],
                    side=d["side"],
                    symbol=d["symbol"]
                ))
            return ticks

        # Download fresh data
        print(f"Downloading data for {symbol} from {start} to {end}")
        ticks = self.adapter.get_historical(symbol, start, end)

        # Cache it
        if use_cache and ticks:
            data = [
                {
                    "timestamp": t.timestamp.isoformat(),
                    "price": t.price,
                    "volume": t.volume,
                    "side": t.side,
                    "symbol": t.symbol
                }
                for t in ticks
            ]
            with open(cache_file, "w") as f:
                json.dump(data, f)
            print(f"Cached to: {cache_file}")

        return ticks
