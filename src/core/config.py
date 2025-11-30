"""Configuration loading and management."""

import os
from pathlib import Path
from typing import Any, Dict, Optional
import yaml
from dotenv import load_dotenv

# Load environment variables
load_dotenv()


DEFAULT_CONFIG = {
    # Data feed
    "data_feed": {
        "provider": "rithmic",  # or "cqg"
    },

    # Trading
    "trading": {
        "default_symbol": "MES",
        "default_timeframe": 300,  # 5 minutes
    },

    # Order Flow Engine
    "order_flow": {
        "imbalance_threshold": 3.0,
        "imbalance_min_volume": 10,
        "stacked_imbalance_min": 3,
        "exhaustion_min_levels": 3,
        "exhaustion_min_decline": 0.30,
        "divergence_lookback": 5,
        "absorption_min_volume": 100,
        "unfinished_max_volume": 5,
    },

    # Regime Detection
    "regime": {
        "min_regime_score": 4.0,
        "min_regime_confidence": 0.6,
        "adx_trend_threshold": 25,
        "adx_weak_threshold": 20,
        "atr_high_percentile": 70,
        "news_buffer_minutes": 15,
        "no_trade_before_open_minutes": 5,
        "no_trade_before_close_minutes": 15,
    },

    # Execution
    "execution": {
        "default_stop_ticks": 16,
        "default_target_ticks": 24,
        "max_slippage_ticks": 2,
    },

    # Risk (defaults, can be overridden per session)
    "risk": {
        "daily_profit_target": 500.0,
        "daily_loss_limit": -300.0,
        "max_position_size": 2,
        "max_concurrent_trades": 1,
    },

    # Dashboard
    "dashboard": {
        "host": "0.0.0.0",
        "port": 8000,
    },

    # Database
    "database": {
        "url": os.getenv("DATABASE_URL", "postgresql://localhost/orderflow"),
    },

    # Logging
    "logging": {
        "level": "INFO",
        "file": "logs/trading.log",
    },
}


class Config:
    """Configuration manager."""

    def __init__(self, config_path: Optional[str] = None):
        self._config = DEFAULT_CONFIG.copy()

        if config_path:
            self._load_file(config_path)

        self._apply_env_overrides()

    def _load_file(self, path: str) -> None:
        """Load configuration from YAML file."""
        config_file = Path(path)
        if config_file.exists():
            with open(config_file) as f:
                file_config = yaml.safe_load(f)
                if file_config:
                    self._deep_merge(self._config, file_config)

    def _deep_merge(self, base: dict, override: dict) -> None:
        """Deep merge override into base."""
        for key, value in override.items():
            if key in base and isinstance(base[key], dict) and isinstance(value, dict):
                self._deep_merge(base[key], value)
            else:
                base[key] = value

    def _apply_env_overrides(self) -> None:
        """Apply environment variable overrides."""
        # Database URL
        if os.getenv("DATABASE_URL"):
            self._config["database"]["url"] = os.getenv("DATABASE_URL")

        # Rithmic credentials
        if os.getenv("RITHMIC_USER"):
            self._config.setdefault("data_feed", {})
            self._config["data_feed"]["rithmic_user"] = os.getenv("RITHMIC_USER")
        if os.getenv("RITHMIC_PASSWORD"):
            self._config["data_feed"]["rithmic_password"] = os.getenv("RITHMIC_PASSWORD")
        if os.getenv("RITHMIC_SERVER"):
            self._config["data_feed"]["rithmic_server"] = os.getenv("RITHMIC_SERVER")

    def get(self, key: str, default: Any = None) -> Any:
        """Get a config value using dot notation (e.g., 'order_flow.imbalance_threshold')."""
        keys = key.split(".")
        value = self._config

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def get_section(self, section: str) -> Dict[str, Any]:
        """Get an entire config section."""
        return self._config.get(section, {})

    @property
    def all(self) -> Dict[str, Any]:
        """Get entire configuration."""
        return self._config


# Global config instance
_config: Optional[Config] = None


def get_config() -> Config:
    """Get the global config instance."""
    global _config
    if _config is None:
        _config = Config()
    return _config


def load_config(path: str) -> Config:
    """Load config from file and set as global."""
    global _config
    _config = Config(path)
    return _config
