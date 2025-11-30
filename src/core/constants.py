"""Constants and symbol configurations."""

# Tick sizes for futures contracts and ETFs
TICK_SIZES = {
    "ES": 0.25,
    "MES": 0.25,
    "NQ": 0.25,
    "MNQ": 0.25,
    "CL": 0.01,
    "GC": 0.10,
    "SI": 0.005,
    "RTY": 0.10,
    "M2K": 0.10,
    "YM": 1.0,
    "MYM": 1.0,
    # ETFs (for backtesting with Polygon free tier)
    "SPY": 0.01,
    "QQQ": 0.01,
    "IWM": 0.01,
}

# Dollar value per tick
TICK_VALUES = {
    "ES": 12.50,
    "MES": 1.25,
    "NQ": 5.00,
    "MNQ": 0.50,
    "CL": 10.00,
    "GC": 10.00,
    "SI": 25.00,
    "RTY": 5.00,
    "M2K": 0.50,
    "YM": 5.00,
    "MYM": 0.50,
    # ETFs - value per tick is $0.01 per share (for 100 share lots, use size=100)
    "SPY": 0.01,
    "QQQ": 0.01,
    "IWM": 0.01,
}

# Symbol-specific tuning parameters
SYMBOL_PROFILES = {
    "ES": {
        "imbalance_min_volume": 20,
        "absorption_min_volume": 150,
        "typical_bar_volume": 5000,
        "stop_ticks": 16,   # 4 points
        "target_ticks": 24,  # 6 points
    },
    "MES": {
        "imbalance_min_volume": 5,
        "absorption_min_volume": 30,
        "typical_bar_volume": 500,
        "stop_ticks": 16,
        "target_ticks": 24,
    },
    "NQ": {
        "imbalance_min_volume": 15,
        "absorption_min_volume": 100,
        "typical_bar_volume": 3000,
        "stop_ticks": 20,   # 5 points
        "target_ticks": 32,  # 8 points
    },
    "MNQ": {
        "imbalance_min_volume": 5,
        "absorption_min_volume": 25,
        "typical_bar_volume": 300,
        "stop_ticks": 20,
        "target_ticks": 32,
    },
    "CL": {
        "imbalance_min_volume": 30,
        "absorption_min_volume": 200,
        "typical_bar_volume": 8000,
        "stop_ticks": 20,   # 0.20
        "target_ticks": 30,  # 0.30
    },
    "GC": {
        "imbalance_min_volume": 15,
        "absorption_min_volume": 100,
        "typical_bar_volume": 2000,
        "stop_ticks": 20,   # 2.00
        "target_ticks": 30,  # 3.00
    },
    # ETFs for backtesting
    "SPY": {
        "imbalance_min_volume": 1000,
        "absorption_min_volume": 5000,
        "typical_bar_volume": 100000,
        "stop_ticks": 50,   # $0.50
        "target_ticks": 100,  # $1.00
    },
    "QQQ": {
        "imbalance_min_volume": 500,
        "absorption_min_volume": 3000,
        "typical_bar_volume": 50000,
        "stop_ticks": 50,   # $0.50
        "target_ticks": 100,  # $1.00
    },
}


def normalize_price(price: float, symbol: str) -> float:
    """Round price to valid tick increment."""
    # Try 3-char symbol first (MES, MNQ), then 2-char (ES, NQ, CL, GC)
    tick_size = TICK_SIZES.get(symbol[:3], TICK_SIZES.get(symbol[:2], 0.25))
    return round(price / tick_size) * tick_size


def get_symbol_profile(symbol: str) -> dict:
    """Get tuning parameters for a symbol."""
    # Try 3-char symbol first, then 2-char, then default to MES
    return SYMBOL_PROFILES.get(
        symbol[:3],
        SYMBOL_PROFILES.get(symbol[:2], SYMBOL_PROFILES["MES"])
    )
