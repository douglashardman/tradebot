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
        - price: Trade price (already in normal decimal format like 6765.25)
        - size: Trade size
        - side: 'A' (ask/sell aggressor), 'B' (bid/buy aggressor), 'N' (none)
        """
        # Convert nanosecond timestamp to datetime
        ts_ns = record.ts_event
        ts_dt = datetime.fromtimestamp(ts_ns / 1e9, tz=timezone.utc)

        # Price from raw records is in fixed-point (divide by 1e9)
        # Note: .to_df() auto-converts, but raw iteration does not
        raw_price = float(record.price)
        price = raw_price / 1e9 if raw_price > 1e6 else raw_price

        # Map side:
        # 'B' = Buy aggressor = lifting the ask = buyer initiated = "ASK" in our system
        # 'A' = Sell aggressor = hitting the bid = seller initiated = "BID" in our system
        if record.side == 'A':
            side = "BID"  # Sell aggressor hits bids
        elif record.side == 'B':
            side = "ASK"  # Buy aggressor lifts asks
        else:
            side = "ASK"  # Default

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


    def get_session_ticks(
        self,
        contract: str,
        date: str,
        start_time: str = "09:30",
        end_time: str = "16:00",
        dataset: str = "GLBX.MDP3"
    ) -> List[Tick]:
        """
        Get tick data for a trading session.

        Args:
            contract: Databento contract symbol (e.g., "ESZ5" for Dec 2025)
            date: Date string YYYY-MM-DD
            start_time: Session start time HH:MM (default 09:30 ET)
            end_time: Session end time HH:MM (default 16:00 ET)
            dataset: Databento dataset ID

        Returns:
            List of Tick objects for the session
        """
        try:
            import databento as db
        except ImportError:
            raise ImportError("databento package required: pip install databento")

        client = db.Historical(key=self.api_key)

        # Construct datetime range (times are in ET, convert to UTC by adding 5 hours)
        # Note: This is a simplification - proper timezone handling would be better
        start_dt = f"{date}T{start_time}:00-05:00"
        end_dt = f"{date}T{end_time}:00-05:00"

        print(f"Fetching {contract} from {start_dt} to {end_dt}...")

        data = client.timeseries.get_range(
            dataset=dataset,
            symbols=[contract],
            schema="trades",
            start=start_dt,
            end=end_dt,
        )

        # Determine our symbol from the contract symbol
        our_symbol = contract[:2] if contract[:3] not in ["MES", "MNQ"] else contract[:3]

        ticks = []
        for record in data:
            try:
                tick = self._convert_trade(record, our_symbol)
                ticks.append(tick)
            except Exception as e:
                print(f"Error converting trade: {e}")

        print(f"Got {len(ticks):,} ticks")
        return ticks

    @staticmethod
    def get_front_month_contract(symbol: str, date: str = None) -> str:
        """
        Get the front month contract symbol for a given base symbol and date.

        ES contract months: H (Mar), M (Jun), U (Sep), Z (Dec)
        Contract rolls ~2 weeks before expiration (3rd Friday of contract month)

        Args:
            symbol: Base symbol (ES, MES, NQ, etc.)
            date: Date to get contract for (YYYY-MM-DD), defaults to today

        Returns:
            Contract symbol (e.g., "ESZ5" for Dec 2025)
        """
        from datetime import datetime as dt

        if date:
            d = dt.strptime(date, "%Y-%m-%d")
        else:
            d = dt.now()

        # Contract months and their roll dates (approximate)
        # H=March, M=June, U=September, Z=December
        # Roll happens ~2 weeks before 3rd Friday
        month = d.month
        year = d.year % 10  # Get LAST DIGIT only (2025 -> 5, 2026 -> 6)

        if month <= 2 or (month == 3 and d.day < 7):
            contract_month = "H"  # March
        elif month <= 5 or (month == 6 and d.day < 7):
            contract_month = "M"  # June
        elif month <= 8 or (month == 9 and d.day < 7):
            contract_month = "U"  # September
        elif month <= 11 or (month == 12 and d.day < 7):
            contract_month = "Z"  # December
        else:
            # Roll to next year's March
            contract_month = "H"
            year = (year + 1) % 10

        return f"{symbol}{contract_month}{year}"


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
