"""
Configuration loader — reads config.yaml + .env and exposes typed settings.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv


@dataclass
class UniverseConfig:
    refresh_interval_seconds: int = 300
    min_24h_quote_volume_usdt: float = 50_000
    max_spread_pct: float = 2.0
    min_trade_count_24h: int = 500
    excluded_quote_assets: List[str] = field(default_factory=lambda: ["TUSD", "BUSD", "FDUSD"])
    excluded_base_patterns: List[str] = field(default_factory=lambda: ["UP", "DOWN", "BULL", "BEAR"])


@dataclass
class RadarWeights:
    return_acceleration: float = 0.25
    volume_burst: float = 0.30
    spread_compression: float = 0.15
    quote_activity: float = 0.15
    early_buildup: float = 0.15


@dataclass
class RadarConfig:
    tick_interval_seconds: int = 1
    promotion_top_n: int = 10
    cooldown_seconds: int = 300
    weights: RadarWeights = field(default_factory=RadarWeights)
    promotion_score_threshold: float = 0.65
    buildup_price_range_max_pct: float = 1.0
    buildup_volume_increase_min: float = 1.5


@dataclass
class MonitorConfig:
    max_promoted_symbols: int = 15
    book_depth_levels: int = 20
    book_update_speed_ms: int = 100
    rolling_window_seconds: int = 60
    flow_window_seconds: int = 5


@dataclass
class LongEngineConfig:
    max_spread_pct: float = 1.5
    min_top_book_notional_usdt: float = 100
    min_warmup_seconds: int = 30
    max_extension_pct: float = 5.0
    score_threshold: float = 0.70
    persistence_ticks: int = 3
    max_watch_duration_seconds: int = 60
    confirmation_count_required: int = 2
    max_slippage_pct: float = 0.5
    max_price_beyond_trigger_pct: float = 0.3


@dataclass
class ExecutionConfig:
    order_type: str = "LIMIT"
    fill_timeout_seconds: int = 5
    max_book_consumption_pct: float = 10.0


@dataclass
class ExitConfig:
    hard_stop_pct: float = 2.0
    time_stop_seconds: int = 300
    trailing_stop_activation_pct: float = 1.0
    trailing_stop_distance_pct: float = 0.5
    flow_invalidation_exit: bool = True


@dataclass
class RiskConfig:
    max_risk_per_trade_pct: float = 1.0
    max_concurrent_positions: int = 3
    max_total_exposure_pct: float = 10.0
    max_daily_drawdown_pct: float = 3.0
    max_consecutive_losses: int = 5
    stale_data_timeout_seconds: int = 30
    max_order_rejects: int = 5
    symbol_blacklist: List[str] = field(default_factory=list)


@dataclass
class LoggingConfig:
    log_dir: str = "logs"
    level: str = "INFO"
    rotation_mb: int = 50
    retention_days: int = 30
    structured_json: bool = True


@dataclass
class AppConfig:
    """Top-level application configuration."""

    trade_mode: str = "minimum_quantity"  # "minimum_quantity" or "normal"
    dry_run: bool = True

    # Binance credentials (loaded from .env)
    api_key: str = ""
    api_secret: str = ""
    testnet: bool = False

    # Sub-configs
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    radar: RadarConfig = field(default_factory=RadarConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    long_engine: LongEngineConfig = field(default_factory=LongEngineConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    exit: ExitConfig = field(default_factory=ExitConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @property
    def is_minimum_quantity_mode(self) -> bool:
        return self.trade_mode == "minimum_quantity"


def _build_dataclass(cls, data: dict):
    """Recursively build a dataclass from a dict, ignoring unknown keys."""
    if data is None:
        return cls()
    field_names = {f.name for f in cls.__dataclass_fields__.values()}
    filtered = {}
    for k, v in data.items():
        if k not in field_names:
            continue
        f = cls.__dataclass_fields__[k]
        # If the field is itself a dataclass, recurse
        if hasattr(f.type, "__dataclass_fields__"):
            filtered[k] = _build_dataclass(f.type, v)
        else:
            filtered[k] = v
    return cls(**filtered)


def load_config(config_path: str = None, env_path: str = None) -> AppConfig:
    """Load configuration from YAML file and .env credentials."""
    project_root = Path(__file__).resolve().parent.parent

    # Load .env
    env_file = Path(env_path) if env_path else project_root / ".env"
    load_dotenv(env_file)

    # Load YAML
    yaml_file = Path(config_path) if config_path else project_root / "config.yaml"
    raw = {}
    if yaml_file.exists():
        with open(yaml_file, "r") as f:
            raw = yaml.safe_load(f) or {}

    # Build top-level config
    cfg = AppConfig(
        trade_mode=raw.get("trade_mode", "minimum_quantity"),
        dry_run=raw.get("dry_run", True),
        api_key=os.getenv("BINANCE_API_KEY", ""),
        api_secret=os.getenv("BINANCE_API_SECRET", ""),
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
        universe=_build_dataclass(UniverseConfig, raw.get("universe")),
        radar=_build_dataclass(RadarConfig, raw.get("radar")),
        monitor=_build_dataclass(MonitorConfig, raw.get("monitor")),
        long_engine=_build_dataclass(LongEngineConfig, raw.get("long_engine")),
        execution=_build_dataclass(ExecutionConfig, raw.get("execution")),
        exit=_build_dataclass(ExitConfig, raw.get("exit")),
        risk=_build_dataclass(RiskConfig, raw.get("risk")),
        logging=_build_dataclass(LoggingConfig, raw.get("logging")),
    )

    return cfg
