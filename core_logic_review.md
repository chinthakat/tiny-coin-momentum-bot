# TinyCoins — Core Logic Code Review (V2)

Generated: 2026-03-07

## Table of Contents

1. [Configuration](#configuration) — `core/config.py`
2. [SQLite Database Layer](#sqlite-database-layer) — `core/database.py`
3. [Radar Features](#radar-features) — `radar/features.py`
4. [Radar Scorer](#radar-scorer) — `radar/scorer.py`
5. [Radar Service](#radar-service) — `radar/service.py`
6. [History Preloader](#history-preloader) — `radar/history_preloader.py`
7. [Market Regime Filter](#market-regime-filter) — `radar/regime_filter.py`
8. [Monitor State](#monitor-state) — `monitor/state.py`
9. [Deep Microstructure Features](#deep-microstructure-features) — `monitor/features.py`
10. [Symbol Lifecycle](#symbol-lifecycle) — `signals/lifecycle.py`
11. [Long Engine](#long-engine) — `signals/long_engine.py`
12. [Execution Engine](#execution-engine) — `execution/engine.py`
13. [Exit Manager](#exit-manager) — `execution/exit_manager.py`
14. [Risk Engine](#risk-engine) — `risk/engine.py`
15. [Missed Opportunity Analyzer](#missed-opportunity-analyzer) — `analysis/missed_opportunity.py`
16. [CSV Data Logger](#csv-data-logger) — `logging_/csv_logger.py`
17. [Main Orchestrator](#main-orchestrator) — `main.py`

---

## Configuration

**File:** `core/config.py`

```python
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
    fast_ignition_threshold: float = 1.5      # V2: fast-ignition promotion threshold
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
    max_depth_coeff_of_variation: float = 0.5  # V2: liquidity stability filter


@dataclass
class ExecutionConfig:
    order_type: str = "LIMIT"
    fill_timeout_seconds: int = 5
    max_book_consumption_pct: float = 10.0
    risk_per_trade_usd: float = 5.0       # FIX #12: risk budget per trade
    max_notional_per_trade: float = 50.0   # FIX #12: max notional cap
    max_slippage_pct: float = 0.3          # FIX #13: max entry buffer


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
    max_total_notional: float = 200.0      # FIX #15: portfolio notional cap
    max_symbol_notional: float = 50.0      # FIX #15: per-symbol notional cap
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
    dashboard_port: int = 8080

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
        dashboard_port=raw.get("dashboard_port", 8080),
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
```

---

## SQLite Database Layer

**File:** `core/database.py`

```python
"""
SQLite database layer — lightweight persistent storage for all trading data.

Stores ticks, radar scores, deep features, promotions, signals, and trades.
Each session is tagged with a unique run_id. Old runs are archived on restart.
"""

import json
import sqlite3
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger(__name__)

# Default: keep last 3 runs, delete older
DEFAULT_KEEP_RUNS = 3


class TinyCoinsDB:
    """
    Thread-safe SQLite database for TinyCoins trading data.

    Uses WAL mode for concurrent reads, batch inserts for performance,
    and run_id tagging for session management.
    """

    def __init__(self, db_path: str = "data/tinycoins.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = Lock()
        self._run_id: str = ""

        # Batch buffers
        self._tick_buffer: deque = deque()
        self._radar_buffer: deque = deque()
        self._deep_buffer: deque = deque()

    @property
    def run_id(self) -> str:
        return self._run_id

    # ─── Initialization ───

    def connect(self) -> None:
        """Open the database connection and create tables."""
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=10,
            check_same_thread=False,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA cache_size=-8000")  # 8MB cache
        self._conn.row_factory = sqlite3.Row
        self._create_tables()
        logger.info("database_connected", path=str(self._db_path))

    def close(self) -> None:
        """Flush pending data and close the connection."""
        self.flush()
        if self._conn:
            # Mark current run as completed
            if self._run_id:
                self._conn.execute(
                    "UPDATE runs SET ended_at = ?, status = 'completed' WHERE run_id = ?",
                    (datetime.now(timezone.utc).isoformat(), self._run_id),
                )
                self._conn.commit()
            self._conn.close()
            self._conn = None
            logger.info("database_closed")

    def _create_tables(self) -> None:
        """Create all tables if they don't exist."""
        with self._lock:
            c = self._conn
            c.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id      TEXT PRIMARY KEY,
                    started_at  TEXT NOT NULL,
                    ended_at    TEXT,
                    trade_mode  TEXT,
                    dry_run     INTEGER,
                    status      TEXT DEFAULT 'active'
                );

                CREATE TABLE IF NOT EXISTS ticks (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    symbol      TEXT NOT NULL,
                    price       REAL,
                    quote_volume REAL,
                    spread_pct  REAL
                );

                CREATE TABLE IF NOT EXISTS radar_scores (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    symbol      TEXT NOT NULL,
                    composite_score     REAL,
                    fast_ignition_score REAL,
                    label       TEXT,
                    return_10s  REAL,
                    return_30s  REAL,
                    return_60s  REAL,
                    volume_burst REAL,
                    spread_compression REAL,
                    buildup_score REAL,
                    price_range REAL
                );

                CREATE TABLE IF NOT EXISTS deep_features (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    symbol      TEXT NOT NULL,
                    book_imbalance      REAL,
                    flow_imbalance      REAL,
                    ask_depletion       REAL,
                    rehydration_rate    REAL,
                    spread_stability    REAL,
                    volume_acceleration REAL,
                    micro_return        REAL,
                    book_depth_quality  REAL,
                    bid_notional_top5   REAL,
                    ask_notional_top5   REAL
                );

                CREATE TABLE IF NOT EXISTS promotions (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    symbol      TEXT NOT NULL,
                    action      TEXT NOT NULL,
                    score       REAL,
                    label       TEXT
                );

                CREATE TABLE IF NOT EXISTS signals (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    ts          REAL NOT NULL,
                    symbol      TEXT NOT NULL,
                    event       TEXT NOT NULL,
                    details     TEXT
                );

                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id      TEXT NOT NULL,
                    symbol      TEXT NOT NULL,
                    side        TEXT,
                    entry_price REAL,
                    exit_price  REAL,
                    quantity    REAL,
                    entry_ts    REAL,
                    exit_ts     REAL,
                    pnl         REAL,
                    exit_reason TEXT
                );

                -- Indexes for common queries
                CREATE INDEX IF NOT EXISTS idx_ticks_run_symbol
                    ON ticks(run_id, symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_radar_run_symbol
                    ON radar_scores(run_id, symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_deep_run_symbol
                    ON deep_features(run_id, symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_promotions_run
                    ON promotions(run_id, ts);
                CREATE INDEX IF NOT EXISTS idx_signals_run
                    ON signals(run_id, symbol, ts);
                CREATE INDEX IF NOT EXISTS idx_trades_run
                    ON trades(run_id, symbol);
            """)
            c.commit()

    # ─── Run Management ───

    def start_new_run(self, trade_mode: str, dry_run: bool) -> str:
        """
        Archive any active runs and start a new one.

        Returns the new run_id.
        """
        with self._lock:
            now = datetime.now(timezone.utc).isoformat()

            # Mark any active runs as completed
            self._conn.execute(
                "UPDATE runs SET ended_at = ?, status = 'completed' "
                "WHERE status = 'active'",
                (now,),
            )

            # Create new run
            self._run_id = str(uuid.uuid4())[:8]  # short UUID
            self._conn.execute(
                "INSERT INTO runs (run_id, started_at, trade_mode, dry_run, status) "
                "VALUES (?, ?, ?, ?, 'active')",
                (self._run_id, now, trade_mode, int(dry_run)),
            )
            self._conn.commit()

        logger.info("new_run_started", run_id=self._run_id)
        return self._run_id

    def archive_old_runs(self, keep_last: int = DEFAULT_KEEP_RUNS) -> int:
        """
        Delete data for runs older than the most recent N.

        Returns number of runs deleted.
        """
        with self._lock:
            # Get all runs ordered by start time
            rows = self._conn.execute(
                "SELECT run_id FROM runs ORDER BY started_at DESC"
            ).fetchall()

            if len(rows) <= keep_last:
                return 0

            to_delete = [r["run_id"] for r in rows[keep_last:]]

            for rid in to_delete:
                for table in ["ticks", "radar_scores", "deep_features",
                              "promotions", "signals", "trades"]:
                    self._conn.execute(
                        f"DELETE FROM {table} WHERE run_id = ?", (rid,)
                    )
                self._conn.execute(
                    "DELETE FROM runs WHERE run_id = ?", (rid,)
                )

            self._conn.commit()

        logger.info("old_runs_archived", deleted=len(to_delete), kept=keep_last)
        return len(to_delete)

    # ─── Batch Buffering ───

    def buffer_tick(
        self, symbol: str, price: float, quote_volume: float, spread_pct: float
    ) -> None:
        """Add a tick to the write buffer."""
        self._tick_buffer.append((
            self._run_id, time.time(), symbol, price, quote_volume, spread_pct
        ))

    def buffer_radar_score(
        self, symbol: str, composite: float, fast_ign: float, label: str,
        r10: float, r30: float, r60: float, vol_burst: float,
        spread_comp: float, buildup: float, price_range: float,
    ) -> None:
        """Add a radar score to the write buffer."""
        self._radar_buffer.append((
            self._run_id, time.time(), symbol, composite, fast_ign, label,
            r10, r30, r60, vol_burst, spread_comp, buildup, price_range
        ))

    def buffer_deep_features(
        self, symbol: str, book_imb: float, flow_imb: float,
        ask_dep: float, rehydration: float, spread_stab: float,
        vol_accel: float, micro_ret: float, depth_qual: float,
        bid5: float, ask5: float,
    ) -> None:
        """Add deep features to the write buffer."""
        self._deep_buffer.append((
            self._run_id, time.time(), symbol, book_imb, flow_imb,
            ask_dep, rehydration, spread_stab, vol_accel, micro_ret,
            depth_qual, bid5, ask5
        ))

    def flush(self) -> int:
        """
        Write all buffered data to the database.

        Returns total rows flushed.
        """
        if not self._conn:
            return 0

        total = 0
        with self._lock:
            # Flush ticks
            if self._tick_buffer:
                ticks = list(self._tick_buffer)
                self._tick_buffer.clear()
                self._conn.executemany(
                    "INSERT INTO ticks (run_id, ts, symbol, price, quote_volume, spread_pct) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    ticks,
                )
                total += len(ticks)

            # Flush radar scores
            if self._radar_buffer:
                scores = list(self._radar_buffer)
                self._radar_buffer.clear()
                self._conn.executemany(
                    "INSERT INTO radar_scores "
                    "(run_id, ts, symbol, composite_score, fast_ignition_score, label, "
                    "return_10s, return_30s, return_60s, volume_burst, "
                    "spread_compression, buildup_score, price_range) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    scores,
                )
                total += len(scores)

            # Flush deep features
            if self._deep_buffer:
                deep = list(self._deep_buffer)
                self._deep_buffer.clear()
                self._conn.executemany(
                    "INSERT INTO deep_features "
                    "(run_id, ts, symbol, book_imbalance, flow_imbalance, "
                    "ask_depletion, rehydration_rate, spread_stability, "
                    "volume_acceleration, micro_return, book_depth_quality, "
                    "bid_notional_top5, ask_notional_top5) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    deep,
                )
                total += len(deep)

            if total > 0:
                self._conn.commit()

        return total

    # ─── Direct Inserts (for event-driven data) ───

    def log_promotion(
        self, symbol: str, action: str, score: float = 0, label: str = ""
    ) -> None:
        """Log a promotion or demotion event."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO promotions (run_id, ts, symbol, action, score, label) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._run_id, time.time(), symbol, action, score, label),
            )
            self._conn.commit()

    def log_signal(
        self, symbol: str, event: str, details: Optional[Dict] = None
    ) -> None:
        """Log a signal lifecycle event."""
        with self._lock:
            detail_json = json.dumps(details) if details else None
            self._conn.execute(
                "INSERT INTO signals (run_id, ts, symbol, event, details) "
                "VALUES (?, ?, ?, ?, ?)",
                (self._run_id, time.time(), symbol, event, detail_json),
            )
            self._conn.commit()

    def log_trade_entry(
        self, symbol: str, side: str, price: float, quantity: float
    ) -> None:
        """Log trade entry."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades (run_id, symbol, side, entry_price, quantity, entry_ts) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (self._run_id, symbol, side, price, quantity, time.time()),
            )
            self._conn.commit()

    def log_trade_exit(
        self, symbol: str, exit_price: float, pnl: float, reason: str
    ) -> None:
        """Update last trade for symbol with exit data."""
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET exit_price = ?, exit_ts = ?, pnl = ?, exit_reason = ? "
                "WHERE run_id = ? AND symbol = ? AND exit_ts IS NULL "
                "ORDER BY entry_ts DESC LIMIT 1",
                (exit_price, time.time(), pnl, reason, self._run_id, symbol),
            )
            self._conn.commit()

    # ─── Query Methods ───

    def get_symbol_ticks(
        self, symbol: str, run_id: Optional[str] = None, limit: int = 1000
    ) -> List[Dict]:
        """Get price ticks for a symbol."""
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT ts, price, quote_volume, spread_pct FROM ticks "
            "WHERE run_id = ? AND symbol = ? ORDER BY ts DESC LIMIT ?",
            (rid, symbol, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_symbol_scores(
        self, symbol: str, run_id: Optional[str] = None, limit: int = 500
    ) -> List[Dict]:
        """Get radar scores for a symbol."""
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT * FROM radar_scores "
            "WHERE run_id = ? AND symbol = ? ORDER BY ts DESC LIMIT ?",
            (rid, symbol, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_run_trades(self, run_id: Optional[str] = None) -> List[Dict]:
        """Get all trades for a run."""
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT * FROM trades WHERE run_id = ? ORDER BY entry_ts",
            (rid,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_run_promotions(self, run_id: Optional[str] = None) -> List[Dict]:
        """Get all promotions for a run."""
        rid = run_id or self._run_id
        rows = self._conn.execute(
            "SELECT * FROM promotions WHERE run_id = ? ORDER BY ts",
            (rid,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_all_runs(self) -> List[Dict]:
        """Get all runs ordered by start time."""
        rows = self._conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_run_stats(self, run_id: Optional[str] = None) -> Dict:
        """Get summary statistics for a run."""
        rid = run_id or self._run_id
        tick_count = self._conn.execute(
            "SELECT COUNT(*) FROM ticks WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        score_count = self._conn.execute(
            "SELECT COUNT(*) FROM radar_scores WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        promo_count = self._conn.execute(
            "SELECT COUNT(*) FROM promotions WHERE run_id = ? AND action = 'promote'",
            (rid,),
        ).fetchone()[0]
        trade_count = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE run_id = ?", (rid,)
        ).fetchone()[0]
        signal_count = self._conn.execute(
            "SELECT COUNT(*) FROM signals WHERE run_id = ?", (rid,)
        ).fetchone()[0]

        return {
            "run_id": rid,
            "ticks": tick_count,
            "radar_scores": score_count,
            "promotions": promo_count,
            "trades": trade_count,
            "signals": signal_count,
        }
```

---

## Radar Features

**File:** `radar/features.py`

```python
"""
Radar-layer features — cheap, broad metrics computed for the entire universe.

These features are designed to be computed per-symbol from mini-ticker data
on a 1-second cadence without requiring order book depth.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

import numpy as np
import structlog

from core.config import RadarConfig

logger = structlog.get_logger(__name__)

# Rolling window size for baseline calculations
BASELINE_WINDOW = 60  # 60 seconds of history


@dataclass
class RadarFeatureValues:
    """Computed radar feature values for one symbol."""

    symbol: str
    return_10s: float = 0.0
    return_30s: float = 0.0
    return_60s: float = 0.0
    return_acceleration: float = 0.0    # change in velocity
    fast_ignition_score: float = 0.0    # V2: abrupt pump detection
    volume_burst_ratio: float = 0.0     # recent vs baseline
    spread_compression: float = 0.0     # negative = compressed
    quote_activity_ratio: float = 0.0   # update freq vs baseline
    early_buildup_score: float = 0.0    # flat price + rising vol
    price_range_60s_pct: float = 0.0    # true (high-low)/mid range
    computed_at: float = 0.0


@dataclass
class SymbolRadarState:
    """Rolling state for a single symbol in the radar layer."""

    symbol: str
    # Price history: (timestamp, price)
    prices: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Volume delta history: (timestamp, delta_volume)
    volume_deltas: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Update count history: (timestamp,)
    updates: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Spread history: (timestamp, spread_pct)
    spreads: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Last known values
    last_price: float = 0.0
    last_volume: float = 0.0
    prev_cumulative_volume: float = 0.0  # FIX #1: previous cumulative volume
    last_update_time: float = 0.0
    warmup_start: float = 0.0

    @property
    def is_warmed_up(self) -> bool:
        """Has at least 30 seconds of data."""
        if not self.prices:
            return False
        return (time.time() - self.warmup_start) >= 30

    def record_update(
        self, price: float, quote_volume: float, spread_pct: float = 0.0
    ) -> None:
        """
        Record a new data point.

        FIX #1: quote_volume from mini-ticker is 24h rolling cumulative.
        We compute the delta between consecutive updates and store that.
        Handles backwards jumps (day rollover / reset) gracefully.
        """
        now = time.time()
        if self.warmup_start == 0:
            self.warmup_start = now

        self.prices.append((now, price))
        self.updates.append(now)
        if spread_pct > 0:
            self.spreads.append((now, spread_pct))

        # FIX #1: Convert cumulative volume to delta
        if self.prev_cumulative_volume > 0 and quote_volume >= self.prev_cumulative_volume:
            delta = quote_volume - self.prev_cumulative_volume
            self.volume_deltas.append((now, delta))
        elif self.prev_cumulative_volume > 0 and quote_volume < self.prev_cumulative_volume:
            # Backwards jump: day rollover or reset — treat as small positive
            self.volume_deltas.append((now, quote_volume * 0.001))
        # else: first update, no delta yet

        self.prev_cumulative_volume = quote_volume
        self.last_price = price
        self.last_volume = quote_volume
        self.last_update_time = now


class RadarFeatureEngine:
    """
    Computes radar-layer features for all monitored symbols.

    Features are cheap enough to compute for hundreds of symbols per second.
    """

    def __init__(self, config: RadarConfig):
        self._config = config
        self._states: Dict[str, SymbolRadarState] = {}

    def get_or_create_state(self, symbol: str) -> SymbolRadarState:
        if symbol not in self._states:
            self._states[symbol] = SymbolRadarState(symbol=symbol)
        return self._states[symbol]

    def update(
        self, symbol: str, price: float, quote_volume: float, spread_pct: float = 0.0
    ) -> None:
        """Feed new data into the radar state."""
        state = self.get_or_create_state(symbol)
        state.record_update(price, quote_volume, spread_pct)

    def compute_features(self, symbol: str) -> Optional[RadarFeatureValues]:
        """Compute all radar features for a symbol."""
        state = self._states.get(symbol)
        if state is None or not state.is_warmed_up:
            return None

        now = time.time()
        features = RadarFeatureValues(symbol=symbol, computed_at=now)

        # ─── 1. Short-horizon returns (FIX #2) ───
        features.return_10s = self._compute_return(state, now, 10)
        features.return_30s = self._compute_return(state, now, 30)
        features.return_60s = self._compute_return(state, now, 60)

        # Return acceleration = change in velocity (30s return - 60s return normalized)
        features.return_acceleration = features.return_30s - (features.return_60s * 0.5)

        # ─── 2. Volume burst ratio (FIX #1) ───
        features.volume_burst_ratio = self._compute_volume_burst(state, now)

        # ─── 3. Spread compression ───
        features.spread_compression = self._compute_spread_compression(state, now)

        # ─── 4. Quote activity ratio ───
        features.quote_activity_ratio = self._compute_activity_ratio(state, now)

        # ─── 5. True price range (FIX #3) ───
        features.price_range_60s_pct = self._compute_true_range(state, now, 60)

        # ─── 6. Early buildup score (uses true range) ───
        features.early_buildup_score = self._compute_buildup_score(state, features)

        # ─── 7. Fast-ignition score (V2) ───
        features.fast_ignition_score = self._compute_fast_ignition(features)

        return features

    def _compute_return(
        self, state: SymbolRadarState, now: float, lookback_seconds: int
    ) -> float:
        """
        Compute return over the given lookback period.

        FIX #2: Uses the LAST sample at or before the cutoff (not the first after).
        This gives consistent lookback horizons.
        """
        if len(state.prices) < 2:
            return 0.0

        cutoff = now - lookback_seconds
        past_price = None

        # Iterate and find the last sample with ts <= cutoff
        for ts, p in state.prices:
            if ts <= cutoff:
                past_price = p
            else:
                # Past the cutoff — if we haven't found one, use the first available
                if past_price is None:
                    past_price = p
                break

        # Fallback: if all samples are after cutoff, use the oldest
        if past_price is None:
            past_price = state.prices[0][1]

        if past_price <= 0:
            return 0.0

        current = state.last_price
        return ((current - past_price) / past_price) * 100.0

    def _compute_volume_burst(self, state: SymbolRadarState, now: float) -> float:
        """
        Compare recent 10s volume rate against 60s baseline.

        FIX #1: Uses per-update delta volumes instead of cumulative volume differences.
        """
        if len(state.volume_deltas) < 2:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_vol = sum(v for ts, v in state.volume_deltas if ts >= recent_cutoff)
        baseline_vol = sum(v for ts, v in state.volume_deltas if ts >= baseline_cutoff)

        if baseline_vol <= 0:
            return 1.0 if recent_vol > 0 else 0.0

        # Normalize: ratio of recent rate vs baseline rate
        recent_rate = recent_vol / 10.0
        baseline_rate = baseline_vol / 60.0

        if baseline_rate <= 0:
            return 1.0 if recent_rate > 0 else 0.0

        return min(recent_rate / baseline_rate, 10.0)  # cap at 10x

    def _compute_spread_compression(
        self, state: SymbolRadarState, now: float
    ) -> float:
        """
        Negative = spread is tightening (good for buildup).
        Positive = spread is widening (unstable).
        """
        if len(state.spreads) < 5:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_spreads = [s for ts, s in state.spreads if ts >= recent_cutoff]
        baseline_spreads = [s for ts, s in state.spreads if ts >= baseline_cutoff]

        if not recent_spreads or not baseline_spreads:
            return 0.0

        recent_avg = np.mean(recent_spreads)
        baseline_avg = np.mean(baseline_spreads)

        if baseline_avg <= 0:
            return 0.0

        # Positive = widening, negative = compressing
        return ((recent_avg - baseline_avg) / baseline_avg) * 100.0

    def _compute_activity_ratio(self, state: SymbolRadarState, now: float) -> float:
        """How frequently the symbol is updating vs baseline."""
        if len(state.updates) < 5:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_count = sum(1 for ts in state.updates if ts >= recent_cutoff)
        baseline_count = sum(1 for ts in state.updates if ts >= baseline_cutoff)

        recent_rate = recent_count / 10.0
        baseline_rate = baseline_count / 60.0

        if baseline_rate <= 0:
            return 1.0 if recent_rate > 0 else 0.0

        return min(recent_rate / baseline_rate, 10.0)  # cap at 10x

    def _compute_true_range(
        self, state: SymbolRadarState, now: float, lookback_seconds: int
    ) -> float:
        """
        FIX #3: Compute true (high - low) / mid range over window.

        Unlike abs(return), this captures actual volatility including round-trips.
        """
        cutoff = now - lookback_seconds
        prices_in_window = [p for ts, p in state.prices if ts >= cutoff]

        if len(prices_in_window) < 2:
            return 0.0

        high = max(prices_in_window)
        low = min(prices_in_window)
        mid = (high + low) / 2.0

        if mid <= 0:
            return 0.0

        return ((high - low) / mid) * 100.0

    def _compute_buildup_score(
        self, state: SymbolRadarState, features: RadarFeatureValues
    ) -> float:
        """
        Detect early buildup: flat price + rising volume + tightening spread.

        FIX #3: Uses true price range instead of net return for flatness detection.

        Returns 0..1 score.
        """
        score = 0.0

        # FIX #3: Price should be in a tight TRUE range (not just flat net return)
        if features.price_range_60s_pct <= self._config.buildup_price_range_max_pct:
            score += 0.3  # Price is genuinely flat — good

        # Volume should be increasing
        if features.volume_burst_ratio >= self._config.buildup_volume_increase_min:
            score += 0.4  # Volume rising against flat price

        # Spread should be compressing (negative spread_compression)
        if features.spread_compression < -5:
            score += 0.15  # Spread tightening

        # Activity should be above normal
        if features.quote_activity_ratio > 1.5:
            score += 0.15  # More updates than normal

        return min(score, 1.0)

    def _compute_fast_ignition(self, features: RadarFeatureValues) -> float:
        """
        V2: Fast-ignition score for catching abrupt pump starts.

        Weighted heavily toward short-term velocity and volume burst.
        Spread penalty prevents triggering on illiquid spikes.
        """
        score = 0.0

        # Strong short-term return (up to 2x weight)
        score += max(features.return_10s, 0) * 2.0

        # Acceleration (up to 1.8x)
        score += max(features.return_acceleration, 0) * 1.8

        # Volume burst (up to 1.5x)
        vol_contrib = min(features.volume_burst_ratio, 10.0)
        score += vol_contrib * 0.15

        # Spread penalty: wider spread = more penalty
        if features.spread_compression > 10:
            score -= 1.0
        elif features.spread_compression > 5:
            score -= 0.5

        return max(score, 0.0)

    def cleanup_stale(self, active_symbols: set) -> None:
        """Remove state for symbols no longer in the universe."""
        stale = [s for s in self._states if s not in active_symbols]
        for s in stale:
            del self._states[s]
        if stale:
            logger.debug("radar_state_cleaned", removed=len(stale))
```

---

## Radar Scorer

**File:** `radar/scorer.py`

```python
"""
Radar scoring and promotion engine.

Converts radar features into composite scores, labels symbols,
and promotes top candidates to high-resolution monitoring.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set, Tuple

import numpy as np
import structlog

from core.config import RadarConfig
from core.events import RadarLabel
from radar.features import RadarFeatureEngine, RadarFeatureValues

logger = structlog.get_logger(__name__)


@dataclass
class RadarResult:
    """Scoring result for a single symbol."""

    symbol: str
    composite_score: float = 0.0
    fast_ignition_score: float = 0.0  # V2: abrupt pump score
    label: RadarLabel = RadarLabel.NORMAL
    features: Optional[RadarFeatureValues] = None
    scored_at: float = 0.0

    @property
    def effective_score(self) -> float:
        """Highest of composite and fast-ignition scores."""
        return max(self.composite_score, self.fast_ignition_score)


@dataclass
class CooldownEntry:
    """Tracks cooldown state for a symbol."""

    symbol: str
    started_at: float
    duration: float
    reason: str

    @property
    def is_active(self) -> bool:
        return (time.time() - self.started_at) < self.duration


@dataclass
class FeatureStats:
    """Rolling statistics for z-score normalization of a single feature."""

    values: Deque = field(default_factory=lambda: deque(maxlen=120))

    def push(self, v: float) -> None:
        self.values.append(v)

    @property
    def mean(self) -> float:
        if len(self.values) < 5:
            return 0.0
        return float(np.mean(self.values))

    @property
    def std(self) -> float:
        if len(self.values) < 5:
            return 1.0  # avoid div zero
        s = float(np.std(self.values))
        return max(s, 1e-6)

    def z_score(self, v: float) -> float:
        """Compute z-score, clipped to [0, 1]."""
        if len(self.values) < 5:
            return 0.0
        z = (v - self.mean) / self.std
        return min(max(z / 3.0, 0), 1.0)  # normalize ~3σ → [0, 1]


class RadarScorer:
    """
    Computes composite radar scores and manages symbol promotion.

    FIX #4: Uses per-symbol z-score normalization instead of fixed caps.
    FIX #5: Reserves promotion slots by label class for better coverage.
    """

    def __init__(self, config: RadarConfig, feature_engine: RadarFeatureEngine):
        self._config = config
        self._features = feature_engine
        self._cooldowns: Dict[str, CooldownEntry] = {}
        self._promoted: Set[str] = set()
        self._last_results: Dict[str, RadarResult] = {}

        # FIX #4: Per-symbol rolling stats for z-score normalization
        self._feature_stats: Dict[str, Dict[str, FeatureStats]] = {}

    @property
    def promoted_symbols(self) -> Set[str]:
        return self._promoted.copy()

    @property
    def last_results(self) -> Dict[str, RadarResult]:
        return self._last_results

    def _get_stats(self, symbol: str) -> Dict[str, FeatureStats]:
        """Get or create per-symbol feature statistics."""
        if symbol not in self._feature_stats:
            self._feature_stats[symbol] = {
                "ret_accel": FeatureStats(),
                "vol_burst": FeatureStats(),
                "spread_comp": FeatureStats(),
                "activity": FeatureStats(),
                "buildup": FeatureStats(),
            }
        return self._feature_stats[symbol]

    def _compute_composite_score(self, features: RadarFeatureValues) -> float:
        """
        FIX #4: Weighted combination using z-score normalization.

        Each feature is scored relative to its own recent history,
        making scores comparable across symbols with different regimes.
        """
        w = self._config.weights
        stats = self._get_stats(features.symbol)

        # Push raw values into history
        stats["ret_accel"].push(features.return_acceleration)
        stats["vol_burst"].push(features.volume_burst_ratio)
        stats["spread_comp"].push(-features.spread_compression)  # negate: compression is good
        stats["activity"].push(features.quote_activity_ratio)
        stats["buildup"].push(features.early_buildup_score)

        # Z-score normalize each feature
        ret_score = stats["ret_accel"].z_score(features.return_acceleration)
        vol_score = stats["vol_burst"].z_score(features.volume_burst_ratio)
        spread_score = stats["spread_comp"].z_score(-features.spread_compression)
        activity_score = stats["activity"].z_score(features.quote_activity_ratio)

        # Buildup is already 0..1 and composite, keep it raw
        buildup_score = features.early_buildup_score

        composite = (
            w.return_acceleration * ret_score
            + w.volume_burst * vol_score
            + w.spread_compression * spread_score
            + w.quote_activity * activity_score
            + w.early_buildup * buildup_score
        )

        return min(max(composite, 0.0), 1.0)

    def _classify_label(
        self, features: RadarFeatureValues, score: float
    ) -> RadarLabel:
        """Assign a radar label based on feature pattern."""
        # Already extended: large recent return
        if features.return_60s > 5.0:
            return RadarLabel.ALREADY_EXTENDED

        # Illiquid trap: high score but bad spread
        if score > 0.5 and features.spread_compression > 20:
            return RadarLabel.ILLIQUID_TRAP

        # Ignition risk: high acceleration + volume
        if features.return_acceleration > 2.0 and features.volume_burst_ratio > 2.0:
            return RadarLabel.IGNITION_RISK

        # Building pressure: buildup characteristics
        if features.early_buildup_score > 0.5:
            return RadarLabel.BUILDING_PRESSURE

        # Cooling off: negative recent return
        if features.return_30s < -1.0:
            return RadarLabel.COOLING_OFF

        return RadarLabel.NORMAL

    def score_all(
        self, eligible_symbols: Set[str]
    ) -> List[RadarResult]:
        """Score all eligible symbols and return sorted results."""
        results: List[RadarResult] = []

        for symbol in eligible_symbols:
            features = self._features.compute_features(symbol)
            if features is None:
                continue

            score = self._compute_composite_score(features)
            fast_score = features.fast_ignition_score  # V2: already computed
            label = self._classify_label(features, score)

            # V2: If fast-ignition is strong, override label
            if fast_score > self._config.fast_ignition_threshold and label == RadarLabel.NORMAL:
                label = RadarLabel.IGNITION_RISK

            result = RadarResult(
                symbol=symbol,
                composite_score=score,
                fast_ignition_score=fast_score,
                label=label,
                features=features,
                scored_at=time.time(),
            )
            results.append(result)

        # Sort by effective score (max of composite, fast) descending
        results.sort(key=lambda r: r.effective_score, reverse=True)

        # Cache results
        self._last_results = {r.symbol: r for r in results}

        return results

    def select_promotions(
        self,
        results: List[RadarResult],
        long_eligible: Set[str],
    ) -> Tuple[Set[str], Set[str]]:
        """
        FIX #5: Select symbols with quota-based promotion by label class.

        Reserves slots for different label types to improve coverage
        and reduce homogeneity.
        """
        # Clean up expired cooldowns
        self._cooldowns = {
            s: c for s, c in self._cooldowns.items() if c.is_active
        }

        # Filter candidates
        candidates: List[RadarResult] = []
        for r in results:
            # V2: Promote if EITHER score exceeds threshold
            passes_composite = r.composite_score >= self._config.promotion_score_threshold
            passes_fast = r.fast_ignition_score >= self._config.fast_ignition_threshold
            if not (passes_composite or passes_fast):
                continue
            if r.symbol not in long_eligible:
                continue
            if r.symbol in self._cooldowns:
                continue
            if r.label == RadarLabel.ALREADY_EXTENDED:
                continue
            if r.label == RadarLabel.ILLIQUID_TRAP:
                continue
            candidates.append(r)

        # FIX #5: Quota-based selection by label
        max_n = self._config.promotion_top_n
        new_promoted: Set[str] = set()

        # Define quotas per label class
        quotas = {
            RadarLabel.IGNITION_RISK: min(3, max_n // 3),
            RadarLabel.BUILDING_PRESSURE: min(5, max_n // 2),
        }
        # Remaining slots for any label
        reserved_total = sum(quotas.values())
        general_slots = max(max_n - reserved_total, 2)

        # Fill reserved slots first
        for label, quota in quotas.items():
            label_candidates = [c for c in candidates if c.label == label]
            for r in label_candidates[:quota]:
                if len(new_promoted) < max_n:
                    new_promoted.add(r.symbol)

        # Fill remaining with top overall (regardless of label)
        for r in candidates:
            if r.symbol not in new_promoted and len(new_promoted) < max_n:
                new_promoted.add(r.symbol)

        # Determine additions and removals
        to_promote = new_promoted - self._promoted
        to_demote = self._promoted - new_promoted

        # Update state
        self._promoted = new_promoted

        if to_promote:
            logger.info(
                "radar_promoted",
                symbols=list(to_promote),
                scores={
                    s: round(self._last_results[s].composite_score, 3)
                    for s in to_promote
                    if s in self._last_results
                },
            )
        if to_demote:
            logger.info("radar_demoted", symbols=list(to_demote))

        return to_promote, to_demote

    def add_cooldown(self, symbol: str, reason: str = "invalidated") -> None:
        """Put a symbol into cooldown after invalidation."""
        self._cooldowns[symbol] = CooldownEntry(
            symbol=symbol,
            started_at=time.time(),
            duration=self._config.cooldown_seconds,
            reason=reason,
        )
        self._promoted.discard(symbol)
        logger.info(
            "radar_cooldown_added",
            symbol=symbol,
            reason=reason,
            duration_s=self._config.cooldown_seconds,
        )

    def cleanup_stats(self, active_symbols: set) -> None:
        """Remove statistics for symbols no longer in the universe."""
        stale = [s for s in self._feature_stats if s not in active_symbols]
        for s in stale:
            del self._feature_stats[s]
```

---

## Radar Service

**File:** `radar/service.py`

```python
"""
Radar Service — exchange-wide radar scanner.

Subscribes to the mini-ticker stream and feeds data into
the radar feature engine and scorer on a 1-second cadence.
"""

import asyncio
import time
from typing import Dict, List, Optional, Set

import structlog

from core.config import AppConfig
from core.events import MarketStatUpdate, EventSource
from radar.features import RadarFeatureEngine
from radar.scorer import RadarScorer, RadarResult
from universe.manager import UniverseManager

logger = structlog.get_logger(__name__)


class RadarService:
    """
    Exchange-wide radar scanner.

    Ingests lightweight mini-ticker stream, computes radar features,
    scores all symbols, and promotes top candidates for deeper analysis.
    """

    def __init__(
        self,
        config: AppConfig,
        universe: UniverseManager,
        feature_engine: RadarFeatureEngine,
        scorer: RadarScorer,
    ):
        self._config = config
        self._universe = universe
        self._features = feature_engine
        self._scorer = scorer
        self._running = False
        self._tick_count: int = 0
        self._regime_filter = None  # V2: injected externally
        self._missed_opp = None    # V2: injected externally
        self._db = None            # V2: injected externally
        self._positive_returns: int = 0  # V2: market momentum tracker
        self._total_updates: int = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def handle_mini_ticker(self, msg) -> None:
        """
        Process incoming mini-ticker messages.

        msg can be a single dict or a list of dicts from the combined stream.
        """
        if isinstance(msg, dict):
            items = [msg]
        elif isinstance(msg, list):
            items = msg
        else:
            return

        radar_eligible = self._universe.universe.radar_eligible
        now = time.time()

        for item in items:
            # Skip error messages
            if isinstance(item, dict) and item.get("e") == "error":
                continue

            symbol = item.get("s", "")
            if not symbol or symbol not in radar_eligible:
                continue

            try:
                close_price = float(item.get("c", 0))
                quote_volume = float(item.get("q", 0))
                open_price = float(item.get("o", 0))

                if close_price <= 0:
                    continue

                # Rough spread estimate from open/close
                spread_pct = 0.0
                if open_price > 0:
                    spread_pct = abs(close_price - open_price) / open_price * 100

                self._features.update(symbol, close_price, quote_volume, spread_pct)

                # DB: Buffer tick for promoted symbols only
                if self._db and symbol in (self._scorer.promoted_symbols or set()):
                    self._db.buffer_tick(symbol, close_price, quote_volume, spread_pct)

                # V2: Feed BTC data to regime filter
                if symbol == "BTCUSDT" and self._regime_filter:
                    self._regime_filter.update_btc(close_price, quote_volume)

                # V2: Feed price to missed-opportunity tracker
                if self._missed_opp:
                    self._missed_opp.update_price(symbol, close_price)

                # V2: Track positive returns for momentum
                state = self._features.get_or_create_state(symbol)
                if len(state.prices) >= 2:
                    prev = state.prices[-2][1] if len(state.prices) >= 2 else close_price
                    if prev > 0 and close_price > prev:
                        self._positive_returns += 1
                self._total_updates += 1

            except (ValueError, TypeError):
                continue

    async def run_scoring_loop(self, on_promotion_change=None) -> None:
        """
        Periodic scoring loop — runs every tick_interval_seconds.

        Calls the scorer, determines promotions/demotions, and
        invokes the callback with changes.
        """
        self._running = True
        interval = self._config.radar.tick_interval_seconds

        while self._running:
            try:
                await asyncio.sleep(interval)
                self._tick_count += 1

                eligible = self._universe.universe.radar_eligible
                if not eligible:
                    continue

                # Score all eligible symbols
                results = self._scorer.score_all(eligible)

                # Select promotions
                long_eligible = self._universe.universe.long_eligible
                to_promote, to_demote = self._scorer.select_promotions(
                    results, long_eligible
                )

                # DB: Buffer radar scores for scored symbols
                if self._db:
                    for r in results[:20]:  # top 20 only
                        f = r.features
                        if f:
                            self._db.buffer_radar_score(
                                r.symbol, r.composite_score, r.fast_ignition_score,
                                r.label.value,
                                f.return_10s, f.return_30s, f.return_60s,
                                f.volume_burst_ratio, f.spread_compression,
                                f.early_buildup_score, f.price_range_60s_pct,
                            )

                # Notify callback if there are changes
                if (to_promote or to_demote) and on_promotion_change:
                    await on_promotion_change(to_promote, to_demote)

                # Periodic cleanup
                if self._tick_count % 60 == 0:
                    self._features.cleanup_stale(eligible)

                # V2: Update market momentum for regime filter
                if self._regime_filter and self._total_updates > 0:
                    self._regime_filter.update_market_momentum(
                        self._total_updates, self._positive_returns
                    )
                    self._positive_returns = 0
                    self._total_updates = 0

                # Log top scores periodically
                if self._tick_count % 10 == 0 and results:
                    top_5 = results[:5]
                    logger.debug(
                        "radar_top_scores",
                        top=[
                            {
                                "sym": r.symbol,
                                "score": round(r.composite_score, 3),
                                "label": r.label.value,
                            }
                            for r in top_5
                        ],
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("radar_scoring_error", error=str(e))

        self._running = False

    def stop(self) -> None:
        self._running = False
```

---

## History Preloader

**File:** `radar/history_preloader.py`

```python
"""
Session history preloader — seeds radar baselines from historical klines.

At startup, fetches 6 hours of 1-minute klines for radar-eligible symbols
to give the z-score normalizer and volume burst detector proper baselines.
"""

import asyncio
import time
from typing import Set

import structlog

from exchange.client import ExchangeClient
from radar.features import RadarFeatureEngine

logger = structlog.get_logger(__name__)

# Binance rate limit: ~1200 req/min, but be conservative
BATCH_SIZE = 10
BATCH_DELAY_S = 0.5
KLINES_LIMIT = 360  # 6 hours of 1m candles


async def preload_radar_history(
    exchange: ExchangeClient,
    radar_features: RadarFeatureEngine,
    symbols: Set[str],
    limit: int = KLINES_LIMIT,
) -> int:
    """
    Fetch historical klines and seed radar state for all symbols.

    Returns count of symbols successfully preloaded.
    """
    total = len(symbols)
    loaded = 0
    failed = 0
    symbol_list = list(symbols)

    logger.info(
        "history_preload_starting",
        symbol_count=total,
        klines_per_symbol=limit,
    )

    for i in range(0, total, BATCH_SIZE):
        batch = symbol_list[i : i + BATCH_SIZE]
        tasks = [_preload_symbol(exchange, radar_features, s, limit) for s in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, result in zip(batch, results):
            if isinstance(result, Exception):
                failed += 1
            elif result:
                loaded += 1
            else:
                failed += 1

        # Rate limit
        if i + BATCH_SIZE < total:
            await asyncio.sleep(BATCH_DELAY_S)

        # Progress log every 50 symbols
        done = min(i + BATCH_SIZE, total)
        if done % 50 == 0 or done == total:
            logger.info(
                "history_preload_progress",
                done=done,
                total=total,
                loaded=loaded,
                failed=failed,
            )

    logger.info(
        "history_preload_complete",
        loaded=loaded,
        failed=failed,
        total=total,
    )
    return loaded


async def _preload_symbol(
    exchange: ExchangeClient,
    radar_features: RadarFeatureEngine,
    symbol: str,
    limit: int,
) -> bool:
    """Fetch klines for one symbol and seed its radar state."""
    try:
        klines = await exchange.get_klines(symbol, interval="1m", limit=limit)
        if not klines:
            return False

        state = radar_features.get_or_create_state(symbol)

        prev_vol = 0.0
        for k in klines:
            # Kline format: [open_time, open, high, low, close, volume,
            #                 close_time, quote_volume, trades, ...]
            ts = k[0] / 1000.0  # convert ms to seconds
            close = float(k[4])
            quote_vol = float(k[7])

            if close <= 0:
                continue

            # Seed price history
            state.prices.append((ts, close))

            # Seed volume delta history
            if prev_vol > 0 and quote_vol >= prev_vol:
                delta = quote_vol - prev_vol
                state.volume_deltas.append((ts, delta))
            prev_vol = quote_vol

            # Seed update frequency
            state.updates.append(ts)

        # Set state timestamps
        if state.prices:
            state.last_price = state.prices[-1][1]
            state.last_update_time = state.prices[-1][0]
            state.warmup_start = state.prices[0][0]
            state.prev_cumulative_volume = prev_vol

        return True

    except Exception as e:
        logger.debug("history_preload_symbol_error", symbol=symbol, error=str(e))
        return False
```

---

## Market Regime Filter

**File:** `radar/regime_filter.py`

```python
"""
Market regime filter — tracks BTC and market-wide conditions.

Disables trading when the market is dead or excessively volatile.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


class MarketRegime(Enum):
    ACTIVE = "active"
    CHOPPY = "choppy"
    DEAD = "dead"


@dataclass
class RegimeState:
    """Rolling BTC and market-wide stats."""

    # BTC price history: (timestamp, price)
    btc_prices: Deque = field(default_factory=lambda: deque(maxlen=1800))
    # BTC volume deltas: (timestamp, delta)
    btc_volume_deltas: Deque = field(default_factory=lambda: deque(maxlen=1800))
    btc_prev_volume: float = 0.0

    # Market-wide momentum: (timestamp, pct_positive)
    market_momentum: Deque = field(default_factory=lambda: deque(maxlen=60))

    last_regime: MarketRegime = MarketRegime.ACTIVE
    last_computed: float = 0.0


class RegimeFilter:
    """
    Tracks BTC volatility, volume, and market-wide momentum
    to determine if the overall market environment supports trading.
    """

    def __init__(
        self,
        vol_lookback_s: int = 1800,     # 30m for volatility
        dead_vol_threshold: float = 0.02,   # BTC 30m std < 0.02% → dead
        dead_momentum_threshold: float = 0.3,  # < 30% of symbols positive → dead
    ):
        self._state = RegimeState()
        self._vol_lookback = vol_lookback_s
        self._dead_vol = dead_vol_threshold
        self._dead_momentum = dead_momentum_threshold

    @property
    def regime(self) -> MarketRegime:
        return self._state.last_regime

    @property
    def is_trading_allowed(self) -> bool:
        return self._state.last_regime != MarketRegime.DEAD

    def update_btc(self, price: float, quote_volume: float) -> None:
        """Feed BTC price and volume from mini-ticker."""
        now = time.time()
        self._state.btc_prices.append((now, price))

        # Volume delta
        if self._state.btc_prev_volume > 0 and quote_volume >= self._state.btc_prev_volume:
            delta = quote_volume - self._state.btc_prev_volume
            self._state.btc_volume_deltas.append((now, delta))
        self._state.btc_prev_volume = quote_volume

    def update_market_momentum(self, total_symbols: int, positive_count: int) -> None:
        """Record what fraction of symbols have positive short-term return."""
        now = time.time()
        ratio = positive_count / max(total_symbols, 1)
        self._state.market_momentum.append((now, ratio))

    def compute_regime(self) -> MarketRegime:
        """Compute the current market regime."""
        now = time.time()

        # BTC volatility (std of 1m returns over last 30m)
        btc_vol = self._compute_btc_volatility(now)

        # BTC volume trend
        btc_vol_active = self._compute_btc_volume_activity(now)

        # Market momentum
        momentum = self._compute_momentum(now)

        # Classify
        if btc_vol < self._dead_vol and momentum < self._dead_momentum:
            regime = MarketRegime.DEAD
        elif btc_vol > self._dead_vol * 3 or momentum > 0.6:
            regime = MarketRegime.ACTIVE
        else:
            regime = MarketRegime.CHOPPY

        if regime != self._state.last_regime:
            logger.info(
                "regime_change",
                old=self._state.last_regime.value,
                new=regime.value,
                btc_vol=round(btc_vol, 4),
                momentum=round(momentum, 3),
            )

        self._state.last_regime = regime
        self._state.last_computed = now
        return regime

    def _compute_btc_volatility(self, now: float) -> float:
        """Standard deviation of 1-minute BTC returns over 30m."""
        cutoff = now - self._vol_lookback
        prices = [(ts, p) for ts, p in self._state.btc_prices if ts >= cutoff]

        if len(prices) < 10:
            return 0.1  # assume active until we have data

        # Compute minute-interval returns
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1][1] > 0:
                ret = (prices[i][1] - prices[i - 1][1]) / prices[i - 1][1] * 100
                returns.append(ret)

        if not returns:
            return 0.1

        return float(np.std(returns))

    def _compute_btc_volume_activity(self, now: float) -> float:
        """Recent vs baseline BTC volume rate."""
        cutoff_recent = now - 60
        cutoff_base = now - 600

        recent = sum(v for ts, v in self._state.btc_volume_deltas if ts >= cutoff_recent)
        baseline = sum(v for ts, v in self._state.btc_volume_deltas if ts >= cutoff_base)

        rate_recent = recent / 60.0
        rate_base = baseline / 600.0

        if rate_base <= 0:
            return 1.0 if rate_recent > 0 else 0.0
        return rate_recent / rate_base

    def _compute_momentum(self, now: float) -> float:
        """Average fraction of symbols with positive return."""
        cutoff = now - 30
        vals = [r for ts, r in self._state.market_momentum if ts >= cutoff]
        if not vals:
            return 0.5  # assume neutral
        return float(np.mean(vals))

    def get_summary(self) -> dict:
        """Return current regime summary for dashboard/CSV."""
        return {
            "regime": self._state.last_regime.value,
            "trading_allowed": self.is_trading_allowed,
        }
```

---

## Monitor State

**File:** `monitor/state.py`

```python
"""
Per-symbol rolling state for high-resolution monitoring.

Maintains ring buffers for price, spread, volume, flow, order book
snapshots, and ask notional history for promoted symbols.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

import numpy as np

from core.config import MonitorConfig
from core.events import AggTrade, BestBidAsk, OrderBookLevel, OrderBookUpdate


@dataclass
class SymbolState:
    """Live rolling state for a single promoted symbol."""

    symbol: str

    # Ring buffers: (timestamp, value)
    prices: Deque = field(default_factory=lambda: deque(maxlen=600))
    spreads: Deque = field(default_factory=lambda: deque(maxlen=600))
    volumes: Deque = field(default_factory=lambda: deque(maxlen=600))

    # Flow tracking: (timestamp, notional, is_buy_aggressor)
    trades: Deque = field(default_factory=lambda: deque(maxlen=2000))

    # Latest order book
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)

    # Latest BBO
    best_bid: float = 0.0
    best_ask: float = 0.0
    best_bid_qty: float = 0.0
    best_ask_qty: float = 0.0

    # FIX #6 & #7: Rolling ask notional history for baseline (V2: extended to 300s)
    ask_notional_history: Deque = field(default_factory=lambda: deque(maxlen=600))

    # V2: Bid depth history for liquidity stability filter
    bid_depth_history: Deque = field(default_factory=lambda: deque(maxlen=60))

    # Tracking
    warmup_start: float = 0.0
    last_update: float = 0.0

    @property
    def mid_price(self) -> float:
        if self.best_bid > 0 and self.best_ask > 0:
            return (self.best_bid + self.best_ask) / 2.0
        return self.prices[-1][1] if self.prices else 0.0

    @property
    def spread_pct(self) -> float:
        mid = self.mid_price
        if mid <= 0 or self.best_ask <= 0 or self.best_bid <= 0:
            return 0.0
        return ((self.best_ask - self.best_bid) / mid) * 100.0

    @property
    def is_warmed_up(self) -> bool:
        if self.warmup_start == 0:
            return False
        return (time.time() - self.warmup_start) >= 30

    def is_stale(self, timeout_s: float = 10.0) -> bool:
        """FIX #16: Check if this symbol's data is stale."""
        if self.last_update == 0:
            return True
        return (time.time() - self.last_update) > timeout_s

    def update_bbo(self, bbo: BestBidAsk) -> None:
        now = time.time()
        if self.warmup_start == 0:
            self.warmup_start = now

        self.best_bid = bbo.bid_price
        self.best_ask = bbo.ask_price
        self.best_bid_qty = bbo.bid_qty
        self.best_ask_qty = bbo.ask_qty
        self.prices.append((now, bbo.mid_price))
        self.spreads.append((now, bbo.spread_pct))
        self.last_update = now

    def update_book(self, update: OrderBookUpdate) -> None:
        now = time.time()
        self.bids = update.bids
        self.asks = update.asks
        self.last_update = now

        # FIX #6: Record ask notional snapshot for historical baseline
        ask_top5 = sum(a.notional for a in self.asks[:5])
        self.ask_notional_history.append((now, ask_top5))

        # V2: Record bid depth for liquidity stability filter
        bid_top5 = sum(b.notional for b in self.bids[:5])
        self.bid_depth_history.append((now, bid_top5))

    def update_trade(self, trade: AggTrade) -> None:
        now = time.time()
        if self.warmup_start == 0:
            self.warmup_start = now

        self.trades.append((now, trade.notional, trade.is_buy_aggressor))
        self.volumes.append((now, trade.notional))
        self.last_update = now

    # ─── Convenience queries ───

    def recent_prices(self, seconds: float) -> List[float]:
        cutoff = time.time() - seconds
        return [p for ts, p in self.prices if ts >= cutoff]

    def recent_spreads(self, seconds: float) -> List[float]:
        cutoff = time.time() - seconds
        return [s for ts, s in self.spreads if ts >= cutoff]

    def buy_sell_flow(self, seconds: float) -> Tuple[float, float]:
        """Returns (buy_notional, sell_notional) over recent window."""
        cutoff = time.time() - seconds
        buy = sum(n for ts, n, is_buy in self.trades if ts >= cutoff and is_buy)
        sell = sum(n for ts, n, is_buy in self.trades if ts >= cutoff and not is_buy)
        return buy, sell

    def total_volume(self, seconds: float) -> float:
        cutoff = time.time() - seconds
        return sum(v for ts, v in self.volumes if ts >= cutoff)

    def book_bid_notional(self, levels: int = 5) -> float:
        return sum(b.notional for b in self.bids[:levels])

    def book_ask_notional(self, levels: int = 5) -> float:
        return sum(a.notional for a in self.asks[:levels])

    def ask_notional_baseline(self, seconds: float = 60) -> float:
        """FIX #6: Historical median of top-5 ask notional."""
        cutoff = time.time() - seconds
        vals = [v for ts, v in self.ask_notional_history if ts >= cutoff]
        if len(vals) < 3:
            return 0.0
        return float(np.median(vals))

    def ask_notional_at(self, seconds_ago: float) -> float:
        """FIX #7: Get ask notional from N seconds ago for rehydration."""
        target = time.time() - seconds_ago
        closest_val = 0.0
        closest_dist = float("inf")
        for ts, v in self.ask_notional_history:
            dist = abs(ts - target)
            if dist < closest_dist:
                closest_dist = dist
                closest_val = v
        return closest_val


class StateEngine:
    """Manages per-symbol state for all promoted symbols."""

    def __init__(self, config: MonitorConfig):
        self._config = config
        self._states: Dict[str, SymbolState] = {}

    def get_or_create(self, symbol: str) -> SymbolState:
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]

    def get(self, symbol: str) -> Optional[SymbolState]:
        return self._states.get(symbol)

    def remove(self, symbol: str) -> None:
        self._states.pop(symbol, None)

    @property
    def all_symbols(self) -> List[str]:
        return list(self._states.keys())
```

---

## Deep Microstructure Features

**File:** `monitor/features.py`

```python
"""
Deep microstructure features — computed for promoted symbols only.

These require order book depth, aggregate trades, and BBO data.
"""

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import structlog

from core.config import MonitorConfig
from monitor.state import StateEngine, SymbolState

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class DeepFeatures:
    """Computed deep microstructure features for one symbol."""

    symbol: str
    # Order Book Imbalance: positive = buy pressure
    book_imbalance: float = 0.0
    # Flow Imbalance: -1 to +1, positive = buy aggressor dominant
    flow_imbalance: float = 0.0
    # Ask Depletion: how thin are asks vs historical baseline (positive = thin asks)
    ask_depletion: float = 0.0
    # Rehydration Rate: how fast asks refill after depletion (lower = slower = bullish)
    rehydration_rate: float = 0.0
    # Spread Stability: coefficient of variation of spread (lower = more stable)
    spread_stability: float = 0.0
    # Volume Acceleration: recent vs baseline
    volume_acceleration: float = 0.0
    # Micro Return: very short-term price move %
    micro_return: float = 0.0
    # Book Depth Quality: top-5 notional available
    book_depth_quality: float = 0.0
    # Timestamp
    computed_at: float = 0.0


class DeepFeatureEngine:
    """Computes deep microstructure features from per-symbol state."""

    def __init__(self, config: MonitorConfig, state_engine: StateEngine):
        self._config = config
        self._state = state_engine

    def compute(self, symbol: str) -> Optional[DeepFeatures]:
        """Compute all deep features for a promoted symbol."""
        state = self._state.get(symbol)
        if state is None or not state.is_warmed_up:
            return None

        features = DeepFeatures(symbol=symbol, computed_at=time.time())
        flow_window = self._config.flow_window_seconds

        # ─── 1. Order Book Imbalance ───
        bid_notional = state.book_bid_notional(levels=10)
        ask_notional = state.book_ask_notional(levels=10)
        total = bid_notional + ask_notional
        if total > 0:
            features.book_imbalance = (bid_notional - ask_notional) / total

        # ─── 2. Flow Imbalance ───
        buy_flow, sell_flow = state.buy_sell_flow(flow_window)
        flow_total = buy_flow + sell_flow
        if flow_total > 0:
            features.flow_imbalance = (buy_flow - sell_flow) / flow_total

        # ─── 3. Ask Depletion (FIX #6) ───
        # Compare current top-5 ask notional against historical median baseline
        current_ask = state.book_ask_notional(levels=5)
        baseline_ask = state.ask_notional_baseline(seconds=60)
        if baseline_ask > 0:
            features.ask_depletion = 1.0 - (current_ask / baseline_ask)
        else:
            features.ask_depletion = 0.0

        # ─── 4. Rehydration Rate (FIX #7) ───
        # Compare ask notional now vs N seconds ago
        # If asks were thin and are refilling quickly, rehydration is high (bearish)
        # If asks stay thin, rehydration is low (bullish — true vacuum)
        ask_5s_ago = state.ask_notional_at(5.0)
        if ask_5s_ago > 0 and current_ask > 0:
            # rehydration_rate > 1 means asks are growing (refilling)
            # rehydration_rate < 1 means asks are still depleted
            features.rehydration_rate = current_ask / ask_5s_ago
        elif ask_5s_ago <= 0 and current_ask > 0:
            features.rehydration_rate = 2.0  # asks appeared from nothing — fast refill
        else:
            features.rehydration_rate = 0.5  # no data — assume neutral

        # ─── 5. Spread Stability (FIX #8) ───
        # Use coefficient of variation instead of raw std
        recent_spreads = state.recent_spreads(10)
        if len(recent_spreads) >= 3:
            spread_mean = float(np.mean(recent_spreads))
            spread_std = float(np.std(recent_spreads))
            if spread_mean > 0:
                features.spread_stability = spread_std / spread_mean  # CoV
            else:
                features.spread_stability = 0.0

        # ─── 6. Volume Acceleration ───
        vol_recent = state.total_volume(10)
        vol_baseline = state.total_volume(60)
        baseline_rate = vol_baseline / 60.0
        recent_rate = vol_recent / 10.0
        if baseline_rate > 0:
            features.volume_acceleration = recent_rate / baseline_rate
        elif recent_rate > 0:
            features.volume_acceleration = 5.0  # large burst from zero baseline

        # ─── 7. Micro Return ───
        recent_prices = state.recent_prices(5)
        if len(recent_prices) >= 2 and recent_prices[0] > 0:
            features.micro_return = (
                (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
            ) * 100.0

        # ─── 8. Book Depth Quality ───
        features.book_depth_quality = bid_notional + current_ask

        return features
```

---

## Symbol Lifecycle

**File:** `signals/lifecycle.py`

```python
"""
Symbol lifecycle state machine — manages transitions between states.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog

from core.events import SymbolState

logger = structlog.get_logger(__name__)


@dataclass
class LifecycleEntry:
    """Tracks lifecycle state for a single symbol."""

    symbol: str
    state: SymbolState = SymbolState.NORMAL
    entered_at: float = field(default_factory=time.time)
    history: List[tuple] = field(default_factory=list)  # (state, timestamp)
    metadata: Dict = field(default_factory=dict)

    def transition(self, new_state: SymbolState, reason: str = "") -> None:
        """Transition to a new state, recording history."""
        old = self.state
        self.history.append((old.value, self.entered_at))
        self.state = new_state
        self.entered_at = time.time()
        logger.info(
            "lifecycle_transition",
            symbol=self.symbol,
            from_state=old.value,
            to_state=new_state.value,
            reason=reason,
        )

    @property
    def time_in_state(self) -> float:
        return time.time() - self.entered_at


# Valid transitions
VALID_TRANSITIONS = {
    SymbolState.NORMAL: {SymbolState.SUSPICIOUS, SymbolState.HIGH_RES_MONITOR},
    SymbolState.SUSPICIOUS: {SymbolState.DANGER_WATCH, SymbolState.NORMAL, SymbolState.HIGH_RES_MONITOR},
    SymbolState.DANGER_WATCH: {SymbolState.HIGH_RES_MONITOR, SymbolState.NORMAL, SymbolState.COOLDOWN},
    SymbolState.HIGH_RES_MONITOR: {SymbolState.LONG_WATCH, SymbolState.SHORT_WATCH, SymbolState.COOLDOWN, SymbolState.NORMAL},
    SymbolState.LONG_WATCH: {SymbolState.ORDER_PENDING, SymbolState.COOLDOWN, SymbolState.HIGH_RES_MONITOR, SymbolState.NORMAL},
    SymbolState.SHORT_WATCH: {SymbolState.ORDER_PENDING, SymbolState.COOLDOWN, SymbolState.HIGH_RES_MONITOR, SymbolState.NORMAL},
    SymbolState.ORDER_PENDING: {SymbolState.LIVE_POSITION, SymbolState.COOLDOWN, SymbolState.NORMAL},
    SymbolState.LIVE_POSITION: {SymbolState.COOLDOWN, SymbolState.NORMAL},
    SymbolState.COOLDOWN: {SymbolState.NORMAL},
}


class LifecycleManager:
    """Manages lifecycle state transitions for all symbols."""

    def __init__(self):
        self._entries: Dict[str, LifecycleEntry] = {}

    def get_or_create(self, symbol: str) -> LifecycleEntry:
        if symbol not in self._entries:
            self._entries[symbol] = LifecycleEntry(symbol=symbol)
        return self._entries[symbol]

    def get(self, symbol: str) -> Optional[LifecycleEntry]:
        return self._entries.get(symbol)

    def transition(
        self, symbol: str, new_state: SymbolState, reason: str = ""
    ) -> bool:
        """
        Attempt a state transition. Returns True if successful.
        Validates against allowed transitions.
        """
        entry = self.get_or_create(symbol)
        allowed = VALID_TRANSITIONS.get(entry.state, set())

        if new_state not in allowed:
            logger.warning(
                "invalid_transition",
                symbol=symbol,
                current=entry.state.value,
                requested=new_state.value,
            )
            return False

        entry.transition(new_state, reason)
        return True

    def get_symbols_in_state(self, state: SymbolState) -> List[str]:
        return [s for s, e in self._entries.items() if e.state == state]

    def cleanup(self, active_symbols: set) -> None:
        """Remove entries for symbols no longer tracked."""
        stale = [s for s in self._entries if s not in active_symbols and self._entries[s].state == SymbolState.NORMAL]
        for s in stale:
            del self._entries[s]
```

---

## Long Engine

**File:** `signals/long_engine.py`

```python
"""
Ignition Long Engine — detects and manages long trade opportunities.

Implements the full pipeline from §11 of the strategy:
hard filters → composite score → persistence → watch → confirmation → entry.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import structlog

from core.config import AppConfig, LongEngineConfig
from core.events import SymbolState
from monitor.features import DeepFeatureEngine, DeepFeatures
from monitor.state import StateEngine
from signals.lifecycle import LifecycleManager

logger = structlog.get_logger(__name__)


@dataclass
class LongWatchState:
    """Tracks a symbol in long-watch."""

    symbol: str
    started_at: float = field(default_factory=time.time)
    price_at_start: float = 0.0
    high_since: float = 0.0
    low_since: float = float("inf")
    confirmation_types: Set[str] = field(default_factory=set)
    confirmation_ticks: int = 0  # number of separate ticks with confirmations
    last_confirm_tick: int = 0   # tick id when last confirmation was counted
    last_score: float = 0.0
    reason: str = ""

    @property
    def duration(self) -> float:
        return time.time() - self.started_at


class LongEngine:
    """
    Ignition Long Engine — evaluates promoted symbols for long entry.

    Pipeline:
    1. Hard filters (spread, depth, warmup, extension)
    2. Composite long score from deep features
    3. Persistence filter (N consecutive ticks above threshold)
    4. Watch state management
    5. Confirmation logic (local high break, buy flow, ask depletion)
    6. Entry signal emission
    """

    def __init__(
        self,
        config: AppConfig,
        state_engine: StateEngine,
        feature_engine: DeepFeatureEngine,
        lifecycle: LifecycleManager,
    ):
        self._config = config
        self._lcfg: LongEngineConfig = config.long_engine
        self._state = state_engine
        self._features = feature_engine
        self._lifecycle = lifecycle
        self._watches: Dict[str, LongWatchState] = {}
        self._persistence: Dict[str, int] = {}  # symbol → consecutive above threshold
        self._eval_tick: int = 0  # global tick counter

    # ─── Hard Filters ───

    def _passes_hard_filters(self, symbol: str, features: DeepFeatures) -> bool:
        """Check hard prerequisites before scoring."""
        state = self._state.get(symbol)
        if state is None or not state.is_warmed_up:
            return False

        # Spread check
        if state.spread_pct > self._lcfg.max_spread_pct:
            return False

        # Depth check
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._lcfg.min_top_book_notional_usdt:
            return False

        # Extension check: don't enter if already too extended
        recent = state.recent_prices(60)
        if len(recent) >= 2 and recent[0] > 0:
            extension = ((recent[-1] - recent[0]) / recent[0]) * 100
            if extension > self._lcfg.max_extension_pct:
                return False

        # V2: Liquidity stability filter — reject unstable books
        if len(state.bid_depth_history) >= 5:
            import numpy as np
            depths = [v for ts, v in state.bid_depth_history if ts >= time.time() - 10]
            if len(depths) >= 3:
                mean_depth = float(np.mean(depths))
                if mean_depth > 0:
                    cv = float(np.std(depths)) / mean_depth
                    if cv > self._lcfg.max_depth_coeff_of_variation:
                        return False

        return True

    # ─── Composite Score ───

    def _compute_long_score(self, features: DeepFeatures) -> float:
        """Weighted combination of deep features → 0..1 long score."""
        score = 0.0

        # Book imbalance: positive = buy pressure (+0.20)
        score += max(features.book_imbalance, 0) * 0.20

        # Flow imbalance: positive = buy aggressor (+0.25)
        score += max(features.flow_imbalance, 0) * 0.25

        # Ask depletion: positive = thin asks (+0.15)
        score += max(features.ask_depletion, 0) * 0.15

        # Volume acceleration: higher = more activity (+0.20)
        vol_norm = min(features.volume_acceleration / 5.0, 1.0)
        score += vol_norm * 0.20

        # Spread stability: lower variance = better (+0.10)
        spread_score = max(0, 1.0 - features.spread_stability * 10)
        score += spread_score * 0.10

        # Micro return: small positive = good (+0.10)
        if 0 < features.micro_return < 3.0:
            ret_score = features.micro_return / 3.0
            score += ret_score * 0.10

        return min(max(score, 0), 1.0)

    # ─── Watch Invalidation (Fix #11) ───

    def _check_watch_invalidation(
        self, symbol: str, features: DeepFeatures, watch: LongWatchState
    ) -> Optional[str]:
        """
        Check if watch should be invalidated based on microstructure breakdown.

        Returns reason string if invalidated, None otherwise.
        """
        state = self._state.get(symbol)
        if state is None:
            return "state_lost"

        # Flow flip: sell aggressor dominant
        if features.flow_imbalance < -0.3:
            return "flow_flip_negative"

        # Spread blowout: spread more than 2x the configured max
        if state.spread_pct > self._lcfg.max_spread_pct * 2.0:
            return "spread_blowout"

        # Depth collapse: top-5 book notional dropped below threshold
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._lcfg.min_top_book_notional_usdt * 0.5:
            return "depth_collapse"

        # Sharp negative micro return during watch
        if features.micro_return < -1.5:
            return "sharp_negative_micro_return"

        # Ask refill: asks came back strongly (vacuum was fake)
        if features.ask_depletion < -0.3:
            return "ask_refill"

        return None

    # ─── Evaluation Loop ───

    def evaluate(self, promoted_symbols: Set[str]) -> List[str]:
        """
        Evaluate all promoted symbols. Returns list of symbols ready for entry.
        """
        self._eval_tick += 1
        entry_signals: List[str] = []

        for symbol in promoted_symbols:
            features = self._features.compute(symbol)
            if features is None:
                continue

            # Hard filters
            if not self._passes_hard_filters(symbol, features):
                self._persistence[symbol] = 0
                continue

            # Compute score
            score = self._compute_long_score(features)

            # Persistence filter
            if score >= self._lcfg.score_threshold:
                self._persistence[symbol] = self._persistence.get(symbol, 0) + 1
            else:
                self._persistence[symbol] = 0
                # If in watch, check invalidation
                if symbol in self._watches:
                    self._invalidate_watch(symbol, "score_below_threshold")
                continue

            # Need N consecutive ticks above threshold
            if self._persistence[symbol] < self._lcfg.persistence_ticks:
                continue

            # ─── Watch management ───
            if symbol not in self._watches:
                self._enter_watch(symbol, score, features)
            else:
                watch = self._watches[symbol]
                watch.last_score = score

                # FIX #11: Check microstructure invalidation conditions
                invalidation = self._check_watch_invalidation(symbol, features, watch)
                if invalidation:
                    self._invalidate_watch(symbol, invalidation)
                    continue

                # Check watch timeout
                if watch.duration > self._lcfg.max_watch_duration_seconds:
                    self._invalidate_watch(symbol, "watch_timeout")
                    continue

                # FIX #10: Store prior high BEFORE updating
                state = self._state.get(symbol)
                if state and state.mid_price > 0:
                    prior_high = watch.high_since
                    watch.high_since = max(watch.high_since, state.mid_price)
                    watch.low_since = min(watch.low_since, state.mid_price)

                    # FIX #9: Confirmation with per-tick tracking
                    if self._check_confirmation(
                        symbol, features, watch, prior_high
                    ):
                        entry_signals.append(symbol)

        return entry_signals

    def _enter_watch(self, symbol: str, score: float, features: DeepFeatures) -> None:
        """Enter long watch state."""
        state = self._state.get(symbol)
        mid = state.mid_price if state else 0.0

        self._watches[symbol] = LongWatchState(
            symbol=symbol,
            price_at_start=mid,
            high_since=mid,
            low_since=mid,
            last_score=score,
            reason="high_long_score",
        )
        self._lifecycle.transition(symbol, SymbolState.LONG_WATCH, "long_score_persisted")

        logger.info(
            "long_watch_entered",
            symbol=symbol,
            score=round(score, 3),
            price=mid,
        )

    def _check_confirmation(
        self,
        symbol: str,
        features: DeepFeatures,
        watch: LongWatchState,
        prior_high: float,
    ) -> bool:
        """
        Check if confirmation conditions are met for entry.

        FIX #9: Confirmations from one tick only count as one confirmation tick.
        FIX #10: Breakout checks against prior_high (before update).
        """
        state = self._state.get(symbol)
        if state is None:
            return False

        # Collect which confirmation types fired THIS tick
        this_tick_confirmations: Set[str] = set()

        # Condition 1: Price breaks recent local high (uses prior_high)
        if prior_high > 0 and state.mid_price > prior_high * 1.001:
            this_tick_confirmations.add("price_breakout")

        # Condition 2: Sustained buy flow
        buy, sell = state.buy_sell_flow(5)
        if buy > 0 and sell > 0 and (buy / (buy + sell)) > 0.65:
            this_tick_confirmations.add("sustained_buy_flow")

        # Condition 3: Ask depletion persists with stable spread
        if features.ask_depletion > 0.3 and features.spread_stability < 0.5:
            this_tick_confirmations.add("ask_depletion_persistent")

        # Only count as a new confirmation tick if we have new types
        if this_tick_confirmations and self._eval_tick != watch.last_confirm_tick:
            watch.confirmation_types.update(this_tick_confirmations)
            watch.confirmation_ticks += 1
            watch.last_confirm_tick = self._eval_tick

        # Need enough confirmation ticks (not just enough types in one tick)
        if watch.confirmation_ticks >= self._lcfg.confirmation_count_required:
            logger.info(
                "long_confirmed",
                symbol=symbol,
                confirmation_ticks=watch.confirmation_ticks,
                confirmation_types=list(watch.confirmation_types),
                score=round(watch.last_score, 3),
            )
            return True

        return False

    def _invalidate_watch(self, symbol: str, reason: str) -> None:
        """Cancel the watch and put symbol into cooldown."""
        self._watches.pop(symbol, None)
        self._persistence[symbol] = 0
        self._lifecycle.transition(symbol, SymbolState.COOLDOWN, reason)
        logger.info("long_watch_invalidated", symbol=symbol, reason=reason)

    def consume_entry(self, symbol: str) -> Optional[LongWatchState]:
        """Remove and return the watch state for a confirmed entry."""
        return self._watches.pop(symbol, None)
```

---

## Execution Engine

**File:** `execution/engine.py`

```python
"""
Execution engine — decides sizing, validates entry, and places orders.

Supports minimum-quantity mode for low-risk testing
and risk-based sizing for normal operation.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig
from core.events import SymbolState
from exchange.metadata import MetadataCache
from exchange.order_manager import OrderManager, OrderRecord
from monitor.features import DeepFeatureEngine
from monitor.state import StateEngine
from signals.lifecycle import LifecycleManager

logger = structlog.get_logger(__name__)


@dataclass
class Position:
    """Live position record."""

    symbol: str
    side: str                     # BUY
    entry_price: float = 0.0
    quantity: float = 0.0
    entry_time: float = field(default_factory=time.time)
    highest_price: float = 0.0
    lowest_price: float = float("inf")
    stop_price: float = 0.0
    trailing_active: bool = False
    trailing_stop_price: float = 0.0
    trade_mode: str = ""
    order_record: Optional[OrderRecord] = None

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def hold_time(self) -> float:
        return time.time() - self.entry_time

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return ((current_price - self.entry_price) / self.entry_price) * 100.0


class ExecutionEngine:
    """
    Handles trade sizing, entry validation, and order placement.

    In minimum_quantity mode, position size = exchange minQty.
    In normal mode, size = min(risk-budget, liquidity-cap, max-notional).
    """

    def __init__(
        self,
        config: AppConfig,
        metadata: MetadataCache,
        order_manager: OrderManager,
        state_engine: StateEngine,
        feature_engine: DeepFeatureEngine,
        lifecycle: LifecycleManager,
        risk_engine=None,  # FIX #14: optional risk engine reference
    ):
        self._config = config
        self._metadata = metadata
        self._orders = order_manager
        self._state = state_engine
        self._features = feature_engine
        self._lifecycle = lifecycle
        self._risk_engine = risk_engine  # FIX #14
        self._positions: Dict[str, Position] = {}
        self._trade_history: List[Dict] = []  # completed trades

    @property
    def positions(self) -> Dict[str, Position]:
        return self._positions

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def trade_history(self) -> List[Dict]:
        return self._trade_history

    def total_open_notional(self) -> float:
        """Total notional across all open positions."""
        total = 0.0
        for pos in self._positions.values():
            state = self._state.get(pos.symbol)
            price = state.mid_price if state else pos.entry_price
            total += price * pos.quantity
        return total

    def _calculate_size(
        self, symbol: str, current_price: float, stop_distance_pct: float
    ) -> Optional[float]:
        """
        Calculate position size based on trade mode.

        FIX #12: Normal mode now implements proper risk-based sizing.
        """
        if self._config.is_minimum_quantity_mode:
            # ─── Minimum Quantity Mode ───
            min_qty = self._metadata.get_min_trade_quantity(symbol, current_price)
            if min_qty is None:
                logger.error("min_qty_unavailable", symbol=symbol)
                return None
            logger.info(
                "sizing_minimum_quantity",
                symbol=symbol,
                min_qty=min_qty,
                est_notional=round(min_qty * current_price, 4),
            )
            return min_qty
        else:
            # ─── FIX #12: Risk-Based Sizing ───
            min_qty = self._metadata.get_min_trade_quantity(symbol, current_price)
            if min_qty is None:
                return None

            # Risk budget per trade (from config)
            risk_budget = self._config.execution.risk_per_trade_usd

            # Size from risk: risk_budget / (stop_distance * price)
            if stop_distance_pct > 0 and current_price > 0:
                risk_qty = risk_budget / (stop_distance_pct / 100.0 * current_price)
            else:
                risk_qty = min_qty

            # Liquidity cap: don't take more than 5% of visible top-5 book
            state = self._state.get(symbol)
            if state:
                ask_notional = state.book_ask_notional(5)
                if ask_notional > 0:
                    liquidity_cap_qty = (ask_notional * 0.05) / current_price
                    risk_qty = min(risk_qty, liquidity_cap_qty)

            # Max notional cap
            max_notional = self._config.execution.max_notional_per_trade
            if max_notional > 0 and current_price > 0:
                max_notional_qty = max_notional / current_price
                risk_qty = min(risk_qty, max_notional_qty)

            # Floor at minimum quantity
            final_qty = max(risk_qty, min_qty)

            # Round to step size
            info = self._metadata.get(symbol)
            if info and info.step_size > 0:
                final_qty = self._metadata.round_step_size(final_qty, info.step_size)

            logger.info(
                "sizing_risk_based",
                symbol=symbol,
                risk_qty=round(risk_qty, 8),
                final_qty=round(final_qty, 8),
                est_notional=round(final_qty * current_price, 4),
            )
            return final_qty

    async def execute_entry(self, symbol: str, reason: str = "") -> Optional[Position]:
        """
        Attempt to enter a long position on the given symbol.

        Performs final pre-entry validation before placing the order.
        """
        # ─── FIX #14: Check risk engine permission ───
        if self._risk_engine is not None:
            if not self._risk_engine.can_open_position(
                self.position_count,
                self.total_open_notional(),
            ):
                logger.info("entry_rejected_risk", symbol=symbol)
                return None

        # ─── Pre-entry validation ───
        state = self._state.get(symbol)
        if state is None:
            logger.warning("entry_no_state", symbol=symbol)
            return None

        # FIX #16: Check per-symbol staleness
        if state.is_stale(timeout_s=10.0):
            logger.warning("entry_rejected_stale_data", symbol=symbol,
                           age_s=round(time.time() - state.last_update, 1))
            return None

        features = self._features.compute(symbol)
        if features is None:
            logger.warning("entry_no_features", symbol=symbol)
            return None

        current_price = state.mid_price
        if current_price <= 0:
            logger.warning("entry_invalid_price", symbol=symbol)
            return None

        # Spread still acceptable?
        if state.spread_pct > self._config.long_engine.max_spread_pct:
            logger.info("entry_rejected_spread", symbol=symbol, spread=state.spread_pct)
            return None

        # Still have depth?
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._config.long_engine.min_top_book_notional_usdt:
            logger.info("entry_rejected_depth", symbol=symbol, depth=top_notional)
            return None

        # ─── Size calculation ───
        stop_distance = self._config.exit.hard_stop_pct
        qty = self._calculate_size(symbol, current_price, stop_distance)
        if qty is None or qty <= 0:
            return None

        # ─── FIX #13: Adaptive entry price buffer ───
        info = self._metadata.get(symbol)
        entry_price = state.best_ask

        # Buffer = max(spread * 0.25, 0.05%), capped at max_slippage
        spread_buffer_pct = max(state.spread_pct * 0.25, 0.05)
        max_slippage = getattr(self._config.execution, 'max_slippage_pct', 0.3)
        buffer_pct = min(spread_buffer_pct, max_slippage) / 100.0

        entry_price = entry_price * (1 + buffer_pct)

        if info and info.tick_size > 0:
            entry_price = self._metadata.round_tick_size(entry_price, info.tick_size)

        # ─── Lifecycle transition ───
        self._lifecycle.transition(symbol, SymbolState.ORDER_PENDING, f"entry_{reason}")

        # ─── Place order ───
        order_type = self._config.execution.order_type
        order = await self._orders.place_order(
            symbol=symbol,
            side="BUY",
            quantity=qty,
            price=entry_price if order_type == "LIMIT" else None,
            order_type=order_type,
        )

        if order is None:
            self._lifecycle.transition(symbol, SymbolState.COOLDOWN, "order_failed")
            return None

        # ─── Create position ───
        fill_price = order.avg_fill_price if order.avg_fill_price > 0 else entry_price
        stop_price = fill_price * (1 - stop_distance / 100.0)

        position = Position(
            symbol=symbol,
            side="BUY",
            entry_price=fill_price,
            quantity=order.filled_qty if order.filled_qty > 0 else qty,
            highest_price=fill_price,
            stop_price=stop_price,
            trade_mode=self._config.trade_mode,
            order_record=order,
        )

        self._positions[symbol] = position
        self._lifecycle.transition(symbol, SymbolState.LIVE_POSITION, "filled")

        logger.info(
            "position_opened",
            symbol=symbol,
            side="BUY",
            qty=position.quantity,
            entry_price=position.entry_price,
            stop_price=round(stop_price, 8),
            notional=round(position.notional, 4),
            trade_mode=position.trade_mode,
            buffer_pct=round(buffer_pct * 100, 3),
            is_dry_run=order.is_dry_run,
        )

        return position

    async def close_position(
        self, symbol: str, reason: str = "manual"
    ) -> bool:
        """Close a live position."""
        position = self._positions.get(symbol)
        if position is None:
            return False

        state = self._state.get(symbol)
        current_price = state.mid_price if state else position.entry_price

        # Place sell order
        order = await self._orders.place_order(
            symbol=symbol,
            side="SELL",
            quantity=position.quantity,
            price=None,  # Market sell for exits
            order_type="MARKET",
        )

        exit_price = order.avg_fill_price if order and order.avg_fill_price > 0 else current_price
        pnl_pct = position.unrealized_pnl_pct(exit_price)
        pnl_usd = (exit_price - position.entry_price) * position.quantity

        # Record trade in history
        self._trade_history.append({
            "symbol": symbol,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "quantity": position.quantity,
            "pnl_pct": round(pnl_pct, 3),
            "pnl_usd": round(pnl_usd, 6),
            "hold_time_s": round(position.hold_time, 1),
            "reason": reason,
            "trade_mode": position.trade_mode,
            "closed_at": time.time(),
        })

        # Notify risk engine
        if self._risk_engine is not None:
            self._risk_engine.record_trade_result(pnl_usd)

        del self._positions[symbol]
        self._lifecycle.transition(symbol, SymbolState.COOLDOWN, f"exit_{reason}")

        logger.info(
            "position_closed",
            symbol=symbol,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl_pct=round(pnl_pct, 3),
            hold_time_s=round(position.hold_time, 1),
            reason=reason,
            trade_mode=position.trade_mode,
        )

        return True
```

---

## Exit Manager

**File:** `execution/exit_manager.py`

```python
"""
Exit manager — handles all exit logic for live positions.

Implements hard stops, flow invalidation, time stops, and trailing stops.
"""

import asyncio
import time
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig, ExitConfig
from monitor.features import DeepFeatureEngine
from monitor.state import StateEngine

logger = structlog.get_logger(__name__)


class ExitManager:
    """
    Monitors all live positions and triggers exits when conditions are met.

    Exit triggers:
    1. Hard stop: price drops below stop_price
    2. Flow invalidation: microstructure thesis breaks
    3. Time stop: position held too long
    4. Trailing stop: price pulls back from high after activation
    """

    def __init__(
        self,
        config: AppConfig,
        state_engine: StateEngine,
        feature_engine: DeepFeatureEngine,
    ):
        self._config = config
        self._ecfg: ExitConfig = config.exit
        self._state = state_engine
        self._features = feature_engine

    async def check_exits(self, execution_engine) -> List[str]:
        """
        Check all live positions for exit conditions.
        Returns list of symbols that were exited.
        """
        exited: List[str] = []
        positions = dict(execution_engine.positions)  # copy to avoid mutation during iteration

        for symbol, position in positions.items():
            state = self._state.get(symbol)
            if state is None:
                continue

            current_price = state.mid_price
            if current_price <= 0:
                continue

            # Update position tracking
            position.highest_price = max(position.highest_price, current_price)
            position.lowest_price = min(position.lowest_price, current_price)

            exit_reason = self._check_exit_conditions(
                symbol, position, current_price, state
            )

            if exit_reason:
                success = await execution_engine.close_position(symbol, exit_reason)
                if success:
                    exited.append(symbol)

        return exited

    def _check_exit_conditions(
        self, symbol: str, position, current_price: float, state
    ) -> Optional[str]:
        """Check all exit conditions. Returns reason string if should exit, else None."""

        # ─── 1. Hard Stop ───
        if current_price <= position.stop_price:
            return "hard_stop"

        # ─── 2. Flow Invalidation ───
        if self._ecfg.flow_invalidation_exit:
            features = self._features.compute(symbol)
            if features:
                # Flow turned negative: sell aggressor dominant
                if features.flow_imbalance < -0.4:
                    return "flow_invalidation_negative"

                # Ask refill: asks came back strongly
                if features.ask_depletion < -0.3:
                    return "flow_invalidation_ask_refill"

                # Support vanishing: bid side depleted
                if features.book_imbalance < -0.5:
                    return "flow_invalidation_support_gone"

        # ─── 3. Time Stop ───
        if position.hold_time > self._ecfg.time_stop_seconds:
            return "time_stop"

        # ─── 4. Trailing Stop ───
        pnl_pct = position.unrealized_pnl_pct(current_price)

        # Activate trailing after sufficient gain
        if pnl_pct >= self._ecfg.trailing_stop_activation_pct:
            position.trailing_active = True
            # Set trailing stop price
            trail_price = position.highest_price * (
                1 - self._ecfg.trailing_stop_distance_pct / 100.0
            )
            if trail_price > position.trailing_stop_price:
                position.trailing_stop_price = trail_price

        if position.trailing_active and current_price <= position.trailing_stop_price:
            return "trailing_stop"

        return None

    async def run_exit_loop(self, execution_engine, interval: float = 1.0) -> None:
        """Continuously check exits on a fixed interval."""
        while True:
            try:
                exited = await self.check_exits(execution_engine)
                if exited:
                    logger.info("exit_loop_closed", symbols=exited)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("exit_loop_error", error=str(e))
            await asyncio.sleep(interval)
```

---

## Risk Engine

**File:** `risk/engine.py`

```python
"""
Risk engine — enforces per-trade and portfolio-level risk controls.

Implements kill switches for drawdown, consecutive losses, stale data,
and order reject spikes. Includes portfolio-level exposure limits.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig, RiskConfig
from exchange.order_manager import OrderManager

logger = structlog.get_logger(__name__)


@dataclass
class DailyStats:
    """Tracks daily performance metrics."""

    date: str = ""
    starting_capital: float = 0.0
    realized_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    consecutive_losses: int = 0
    max_consecutive_losses: int = 0

    @property
    def drawdown_pct(self) -> float:
        if self.starting_capital <= 0:
            return 0.0
        return (abs(min(self.realized_pnl, 0)) / self.starting_capital) * 100.0

    @property
    def win_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return self.win_count / self.trade_count


class KillSwitch:
    """Kill switch that halts all trading when triggered."""

    def __init__(self):
        self.is_triggered: bool = False
        self.triggered_at: float = 0.0
        self.reason: str = ""
        self.manual_override: bool = False

    def trigger(self, reason: str) -> None:
        if not self.is_triggered:
            self.is_triggered = True
            self.triggered_at = time.time()
            self.reason = reason
            logger.critical("KILL_SWITCH_TRIGGERED", reason=reason)

    def reset(self) -> None:
        self.is_triggered = False
        self.reason = ""
        logger.info("kill_switch_reset")


class RiskEngine:
    """
    Enforces risk limits and manages kill switches.

    FIX #15: Includes portfolio-level exposure limits.
    FIX #16: Supports per-symbol staleness checks.
    """

    def __init__(
        self,
        config: AppConfig,
        order_manager: OrderManager,
    ):
        self._config = config
        self._rcfg: RiskConfig = config.risk
        self._orders = order_manager
        self._kill_switch = KillSwitch()
        self._daily_stats = DailyStats()
        self._last_data_time: float = time.time()

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def daily_stats(self) -> DailyStats:
        return self._daily_stats

    @property
    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed."""
        return not self._kill_switch.is_triggered

    def update_data_timestamp(self) -> None:
        """Called whenever fresh market data arrives."""
        self._last_data_time = time.time()

    def record_trade_result(self, pnl: float) -> None:
        """Record the result of a closed trade."""
        self._daily_stats.trade_count += 1
        self._daily_stats.realized_pnl += pnl

        if pnl >= 0:
            self._daily_stats.win_count += 1
            self._daily_stats.consecutive_losses = 0
        else:
            self._daily_stats.loss_count += 1
            self._daily_stats.consecutive_losses += 1
            self._daily_stats.max_consecutive_losses = max(
                self._daily_stats.max_consecutive_losses,
                self._daily_stats.consecutive_losses,
            )

    def can_open_position(
        self,
        position_count: int,
        total_open_notional: float = 0,
        symbol_notional: float = 0,
    ) -> bool:
        """
        Check if a new position is allowed given current limits.

        FIX #15: Now checks total notional, per-symbol notional,
        and concurrent position count.
        """
        if not self.is_trading_allowed:
            return False

        # Concurrent position limit
        if position_count >= self._rcfg.max_concurrent_positions:
            logger.info("risk_max_positions_reached", count=position_count)
            return False

        # FIX #15: Total open notional limit
        max_total = getattr(self._rcfg, 'max_total_notional', 0)
        if max_total > 0 and total_open_notional >= max_total:
            logger.info(
                "risk_max_notional_reached",
                total=round(total_open_notional, 2),
                limit=max_total,
            )
            return False

        # FIX #15: Per-symbol notional cap
        max_symbol = getattr(self._rcfg, 'max_symbol_notional', 0)
        if max_symbol > 0 and symbol_notional >= max_symbol:
            logger.info(
                "risk_symbol_notional_exceeded",
                notional=round(symbol_notional, 2),
                limit=max_symbol,
            )
            return False

        return True

    def check_risk_conditions(
        self,
        position_count: int,
        ws_last_message_time: float,
    ) -> None:
        """
        Periodic risk check — triggers kill switch if limits are breached.
        Should be called every second.
        """
        # ─── 1. Daily drawdown ───
        if self._daily_stats.drawdown_pct > self._rcfg.max_daily_drawdown_pct:
            self._kill_switch.trigger(
                f"daily_drawdown_{self._daily_stats.drawdown_pct:.1f}pct"
            )

        # ─── 2. Consecutive losses ───
        if self._daily_stats.consecutive_losses >= self._rcfg.max_consecutive_losses:
            self._kill_switch.trigger(
                f"consecutive_losses_{self._daily_stats.consecutive_losses}"
            )

        # ─── 3. Stale data (global feed) ───
        data_age = time.time() - ws_last_message_time
        if data_age > self._rcfg.stale_data_timeout_seconds:
            self._kill_switch.trigger(f"stale_data_{data_age:.0f}s")

        # ─── 4. Order reject spike ───
        if self._orders.consecutive_rejects >= self._rcfg.max_order_rejects:
            self._kill_switch.trigger(
                f"order_rejects_{self._orders.consecutive_rejects}"
            )

    def check_position_staleness(
        self,
        state_engine,
        positions: Dict,
        stale_timeout: float = 15.0,
    ) -> List[str]:
        """
        FIX #16: Check per-symbol staleness for live positions.

        Returns list of symbols with stale data that should be force-exited.
        """
        stale_symbols = []
        for symbol in positions:
            state = state_engine.get(symbol)
            if state is not None and state.is_stale(stale_timeout):
                stale_symbols.append(symbol)
                logger.warning(
                    "position_stale_data",
                    symbol=symbol,
                    age_s=round(time.time() - state.last_update, 1),
                )
        return stale_symbols

    def get_status_summary(self) -> Dict:
        """Return a summary of current risk status."""
        return {
            "trading_allowed": self.is_trading_allowed,
            "kill_switch": {
                "triggered": self._kill_switch.is_triggered,
                "reason": self._kill_switch.reason,
            },
            "daily": {
                "pnl": round(self._daily_stats.realized_pnl, 4),
                "drawdown_pct": round(self._daily_stats.drawdown_pct, 2),
                "trades": self._daily_stats.trade_count,
                "win_rate": round(self._daily_stats.win_rate * 100, 1),
                "consecutive_losses": self._daily_stats.consecutive_losses,
            },
        }
```

---

## Missed Opportunity Analyzer

**File:** `analysis/missed_opportunity.py`

```python
"""
Missed-opportunity analysis — periodic report of top movers vs system behavior.

Every 15 minutes, logs the top 20 movers and whether the system
promoted, monitored, watched, or traded them.
"""

import csv
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Set, Tuple

import structlog

from radar.features import RadarFeatureEngine
from radar.scorer import RadarScorer
from signals.lifecycle import LifecycleManager

logger = structlog.get_logger(__name__)


@dataclass
class SymbolReturnTracker:
    """Tracks price history for return calculation."""

    prices: Deque = field(default_factory=lambda: deque(maxlen=1800))

    def add(self, price: float, ts: float) -> None:
        self.prices.append((ts, price))

    def return_over(self, seconds: float) -> float:
        """Compute return over the last N seconds."""
        now = time.time()
        cutoff = now - seconds
        past_price = None
        for ts, p in self.prices:
            if ts <= cutoff:
                past_price = p
        if past_price is None or past_price <= 0:
            return 0.0
        current = self.prices[-1][1] if self.prices else 0.0
        if current <= 0:
            return 0.0
        return ((current - past_price) / past_price) * 100.0


CSV_COLUMNS = [
    "timestamp_utc",
    "symbol",
    "return_15m_pct",
    "was_scored",
    "radar_score",
    "fast_ignition_score",
    "was_promoted",
    "had_deep_data",
    "lifecycle_state",
    "entry_would_pass_filters",
    "notes",
]


class MissedOpportunityAnalyzer:
    """
    Tracks all symbol returns and generates reports of top movers
    vs what the system actually did with them.
    """

    def __init__(
        self,
        radar_features: RadarFeatureEngine,
        radar_scorer: RadarScorer,
        lifecycle: LifecycleManager,
        state_engine=None,
        output_dir: str = "data",
        report_interval_seconds: int = 900,  # 15 minutes
    ):
        self._radar_features = radar_features
        self._radar_scorer = radar_scorer
        self._lifecycle = lifecycle
        self._state_engine = state_engine
        self._trackers: Dict[str, SymbolReturnTracker] = {}
        self._interval = report_interval_seconds
        self._last_report: float = 0.0

        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._filepath = self._output_dir / f"missed_opportunities_{date_str}.csv"

        if not self._filepath.exists():
            with open(self._filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_COLUMNS)

        logger.info("missed_opportunity_analyzer_initialized", path=str(self._filepath))

    def update_price(self, symbol: str, price: float) -> None:
        """Feed a price update from mini-ticker."""
        if symbol not in self._trackers:
            self._trackers[symbol] = SymbolReturnTracker()
        self._trackers[symbol].add(price, time.time())

    def should_report(self) -> bool:
        return (time.time() - self._last_report) >= self._interval

    def generate_report(self) -> int:
        """
        Generate a missed-opportunity report.

        Returns the number of rows written.
        """
        now = time.time()
        now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        timestamp_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")

        # Calculate 15m return for all tracked symbols
        returns: List[Tuple[str, float]] = []
        for symbol, tracker in self._trackers.items():
            if len(tracker.prices) < 10:
                continue
            ret = tracker.return_over(900)  # 15 minutes
            returns.append((symbol, ret))

        if not returns:
            return 0

        # Sort by return descending — top movers
        returns.sort(key=lambda x: x[1], reverse=True)
        top_20 = returns[:20]

        promoted = self._radar_scorer.promoted_symbols
        results = self._radar_scorer.last_results

        rows = []
        for symbol, ret_15m in top_20:
            # Was scored?
            result = results.get(symbol)
            was_scored = result is not None
            radar_score = round(result.composite_score, 4) if result else 0.0
            fast_score = round(result.fast_ignition_score, 4) if result else 0.0

            # Was promoted?
            was_promoted = symbol in promoted

            # Had deep data?
            had_deep = False
            if self._state_engine:
                state = self._state_engine.get(symbol)
                had_deep = state is not None and state.is_warmed_up

            # Lifecycle state
            lc_entry = self._lifecycle.get(symbol)
            lc_state = lc_entry.state.value if lc_entry else ""

            # Notes
            notes = []
            if ret_15m > 3.0 and not was_promoted:
                notes.append("MISSED_PUMP")
            if was_promoted and not had_deep:
                notes.append("PROMOTED_NO_DEEP")
            if had_deep and lc_state == "normal":
                notes.append("DEEP_NO_WATCH")

            rows.append([
                timestamp_str,
                symbol,
                round(ret_15m, 4),
                1 if was_scored else 0,
                radar_score,
                fast_score,
                1 if was_promoted else 0,
                1 if had_deep else 0,
                lc_state,
                "",  # entry_would_pass_filters — complex, leave for now
                "; ".join(notes),
            ])

        with open(self._filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        self._last_report = now

        # Log summary
        missed = sum(1 for r in rows if "MISSED_PUMP" in r[-1])
        if missed > 0:
            logger.warning(
                "missed_opportunities_detected",
                missed_pumps=missed,
                top_mover=top_20[0][0] if top_20 else "",
                top_return=round(top_20[0][1], 2) if top_20 else 0,
            )
        else:
            logger.info(
                "missed_opportunity_report",
                movers=len(top_20),
                top_return=round(top_20[0][1], 2) if top_20 else 0,
            )

        return len(rows)
```

---

## CSV Data Logger

**File:** `logging_/csv_logger.py`

```python
"""
CSV data logger — writes all shortlisted coin data at 1-minute intervals.

Captures radar features, composite scores, labels, deep microstructure features,
lifecycle state, and live market data for offline analysis.
"""

import csv
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import structlog

from core.config import AppConfig
from monitor.features import DeepFeatureEngine
from monitor.state import StateEngine
from radar.features import RadarFeatureEngine
from radar.scorer import RadarScorer
from signals.lifecycle import LifecycleManager

logger = structlog.get_logger(__name__)

# All columns in the CSV
CSV_COLUMNS = [
    # ─── Meta ───
    "timestamp_utc",
    "timestamp_epoch",
    "symbol",
    # ─── Raw market data ───
    "last_price",
    "best_bid",
    "best_ask",
    "mid_price",
    "spread_pct",
    "bid_qty",
    "ask_qty",
    # ─── Radar features ───
    "return_10s",
    "return_30s",
    "return_60s",
    "return_acceleration",
    "volume_burst_ratio",
    "spread_compression",
    "quote_activity_ratio",
    "early_buildup_score",
    "price_range_60s_pct",
    # ─── Radar scores ───
    "radar_composite_score",
    "fast_ignition_score",
    "radar_label",
    "is_promoted",
    # ─── Deep features (only for promoted symbols) ───
    "book_imbalance",
    "flow_imbalance",
    "ask_depletion",
    "rehydration_rate",
    "spread_stability",
    "volume_acceleration",
    "micro_return",
    "book_depth_quality",
    # ─── Order book derived ───
    "bid_notional_top5",
    "ask_notional_top5",
    "bid_notional_top10",
    "ask_notional_top10",
    "ask_notional_baseline_60s",
    # ─── Flow ───
    "buy_flow_5s",
    "sell_flow_5s",
    "flow_ratio_5s",
    "total_volume_10s",
    "total_volume_60s",
    # ─── Lifecycle ───
    "lifecycle_state",
    # ─── V2: Debug fields ───
    "deep_stream_attached",
    "deep_data_age_ms",
    "market_regime",
    # ─── Position info (if in position) ───
    "has_position",
    "position_entry_price",
    "position_qty",
    "position_pnl_pct",
    "position_hold_time_s",
]


class CsvDataLogger:
    """
    Writes comprehensive snapshot data for all shortlisted coins
    to a CSV file at a configurable interval.
    """

    def __init__(
        self,
        config: AppConfig,
        radar_features: RadarFeatureEngine,
        radar_scorer: RadarScorer,
        state_engine: StateEngine,
        deep_features: DeepFeatureEngine,
        lifecycle: LifecycleManager,
        output_dir: str = "data",
        interval_seconds: int = 60,
    ):
        self._config = config
        self._radar_features = radar_features
        self._radar_scorer = radar_scorer
        self._state_engine = state_engine
        self._deep_features = deep_features
        self._lifecycle = lifecycle
        self._interval = interval_seconds
        self._execution = None  # set externally
        self._regime_filter = None  # set externally
        self._ws_manager = None  # set externally
        self._last_write: float = 0.0

        # Create output directory
        self._output_dir = Path(output_dir)
        self._output_dir.mkdir(parents=True, exist_ok=True)

        # Generate filename with date
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
        self._filepath = self._output_dir / f"shortlist_data_{date_str}.csv"

        # Write header if file doesn't exist
        if not self._filepath.exists():
            self._write_header()

        logger.info(
            "csv_logger_initialized",
            path=str(self._filepath),
            interval_s=self._interval,
            columns=len(CSV_COLUMNS),
        )

    def _write_header(self) -> None:
        """Write CSV header row."""
        with open(self._filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(CSV_COLUMNS)

    def set_execution(self, execution_engine) -> None:
        """Inject execution engine reference for position data."""
        self._execution = execution_engine

    def set_regime_filter(self, regime_filter) -> None:
        """Inject regime filter for market regime field."""
        self._regime_filter = regime_filter

    def set_ws_manager(self, ws_manager) -> None:
        """Inject WS manager for deep stream status."""
        self._ws_manager = ws_manager

    def should_write(self) -> bool:
        """Check if enough time has passed since last write."""
        return (time.time() - self._last_write) >= self._interval

    def write_snapshot(self) -> int:
        """
        Write one snapshot of all shortlisted coins to CSV.

        Returns the number of rows written.
        """
        now = time.time()
        now_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        timestamp_str = now_utc.strftime("%Y-%m-%d %H:%M:%S")

        # Get all scored results (shortlisted coins)
        results = self._radar_scorer.last_results
        if not results:
            return 0

        promoted = self._radar_scorer.promoted_symbols
        positions = self._execution.positions if self._execution else {}

        rows = []
        for symbol, result in results.items():
            row = self._build_row(
                symbol, result, promoted, positions,
                timestamp_str, now,
            )
            rows.append(row)

        # Append to CSV
        with open(self._filepath, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerows(rows)

        self._last_write = now
        logger.info(
            "csv_snapshot_written",
            rows=len(rows),
            file=str(self._filepath),
        )
        return len(rows)

    def _build_row(
        self, symbol, result, promoted, positions,
        timestamp_str, now,
    ) -> list:
        """Build a single CSV row for a symbol."""
        features = result.features
        is_promoted = symbol in promoted

        # ─── Radar features ───
        r10 = features.return_10s if features else 0
        r30 = features.return_30s if features else 0
        r60 = features.return_60s if features else 0
        ret_accel = features.return_acceleration if features else 0
        vol_burst = features.volume_burst_ratio if features else 0
        spread_comp = features.spread_compression if features else 0
        activity = features.quote_activity_ratio if features else 0
        buildup = features.early_buildup_score if features else 0
        price_range = features.price_range_60s_pct if features else 0

        # ─── Raw market data from radar state ───
        radar_state = self._radar_features.get_or_create_state(symbol)
        last_price = radar_state.last_price

        # ─── Deep features + monitor state (promoted only) ───
        best_bid = best_ask = mid = spread_pct = 0.0
        bid_qty = ask_qty = 0.0
        book_imb = flow_imb = ask_dep = rehydration = 0.0
        spread_stab = vol_accel = micro_ret = book_depth = 0.0
        bid5 = ask5 = bid10 = ask10 = ask_base = 0.0
        buy_flow = sell_flow = flow_ratio = 0.0
        vol10 = vol60 = 0.0

        state = self._state_engine.get(symbol)
        if state and is_promoted:
            best_bid = state.best_bid
            best_ask = state.best_ask
            mid = state.mid_price
            spread_pct = state.spread_pct
            bid_qty = state.best_bid_qty
            ask_qty = state.best_ask_qty

            bid5 = state.book_bid_notional(5)
            ask5 = state.book_ask_notional(5)
            bid10 = state.book_bid_notional(10)
            ask10 = state.book_ask_notional(10)
            ask_base = state.ask_notional_baseline(60)

            bf, sf = state.buy_sell_flow(5)
            buy_flow = bf
            sell_flow = sf
            flow_ratio = bf / (bf + sf) if (bf + sf) > 0 else 0

            vol10 = state.total_volume(10)
            vol60 = state.total_volume(60)

            # Deep features
            deep = self._deep_features.compute(symbol)
            if deep:
                book_imb = deep.book_imbalance
                flow_imb = deep.flow_imbalance
                ask_dep = deep.ask_depletion
                rehydration = deep.rehydration_rate
                spread_stab = deep.spread_stability
                vol_accel = deep.volume_acceleration
                micro_ret = deep.micro_return
                book_depth = deep.book_depth_quality

        # ─── Lifecycle ───
        lc_state = ""
        if self._lifecycle:
            lc_entry = self._lifecycle.get(symbol)
            lc_state = lc_entry.state.value if lc_entry else ""

        # ─── V2: Debug fields ───
        deep_attached = 0
        deep_age_ms = 0.0
        if self._ws_manager and is_promoted:
            deep_attached = 1 if symbol in self._ws_manager._active_symbols else 0
        if state and is_promoted and state.last_update > 0:
            deep_age_ms = round((now - state.last_update) * 1000, 0)

        regime = ""
        if self._regime_filter:
            regime = self._regime_filter.regime.value

        # ─── Position ───
        has_pos = symbol in positions
        pos_entry = pos_qty = pos_pnl = pos_hold = 0.0
        if has_pos:
            pos = positions[symbol]
            pos_entry = pos.entry_price
            pos_qty = pos.quantity
            current = mid if mid > 0 else last_price
            pos_pnl = pos.unrealized_pnl_pct(current)
            pos_hold = pos.hold_time

        return [
            timestamp_str,
            round(now, 3),
            symbol,
            # Raw market
            round(last_price, 8),
            round(best_bid, 8),
            round(best_ask, 8),
            round(mid, 8),
            round(spread_pct, 4),
            round(bid_qty, 4),
            round(ask_qty, 4),
            # Radar features
            round(r10, 4),
            round(r30, 4),
            round(r60, 4),
            round(ret_accel, 4),
            round(vol_burst, 4),
            round(spread_comp, 4),
            round(activity, 4),
            round(buildup, 4),
            round(price_range, 4),
            # Radar scores
            round(result.composite_score, 4),
            round(result.fast_ignition_score, 4),
            result.label.value,
            1 if is_promoted else 0,
            # Deep features
            round(book_imb, 4),
            round(flow_imb, 4),
            round(ask_dep, 4),
            round(rehydration, 4),
            round(spread_stab, 4),
            round(vol_accel, 4),
            round(micro_ret, 4),
            round(book_depth, 2),
            # Order book
            round(bid5, 2),
            round(ask5, 2),
            round(bid10, 2),
            round(ask10, 2),
            round(ask_base, 2),
            # Flow
            round(buy_flow, 4),
            round(sell_flow, 4),
            round(flow_ratio, 4),
            round(vol10, 4),
            round(vol60, 4),
            # Lifecycle
            lc_state,
            # V2: Debug
            deep_attached,
            deep_age_ms,
            regime,
            # Position
            1 if has_pos else 0,
            round(pos_entry, 8),
            round(pos_qty, 8),
            round(pos_pnl, 4),
            round(pos_hold, 1),
        ]
```

---

## Main Orchestrator

**File:** `main.py`

```python
"""
TinyCoins Momentum Event Trading System — Main Orchestrator

Entry point that wires all components together and runs the async event loop.
"""

import asyncio
import signal
import sys
from pathlib import Path

import structlog

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.config import load_config
from core.clock import Clock
from exchange.client import ExchangeClient
from exchange.metadata import MetadataCache
from exchange.websocket_manager import WebSocketManager
from exchange.order_manager import OrderManager
from universe.manager import UniverseManager
from radar.features import RadarFeatureEngine
from radar.scorer import RadarScorer
from radar.service import RadarService
from monitor.state import StateEngine
from monitor.features import DeepFeatureEngine
from monitor.stream_manager import PromotedStreamManager
from signals.lifecycle import LifecycleManager
from signals.long_engine import LongEngine
from execution.engine import ExecutionEngine
from execution.exit_manager import ExitManager
from risk.engine import RiskEngine
from dashboard.server import DashboardServer
from logging_.logger import setup_logging
from logging_.csv_logger import CsvDataLogger
from radar.history_preloader import preload_radar_history
from radar.regime_filter import RegimeFilter
from analysis.missed_opportunity import MissedOpportunityAnalyzer
from core.database import TinyCoinsDB

logger = structlog.get_logger("main")


class TinyCoinsSystem:
    """
    Main orchestrator — wires all components and runs the event loop.
    """

    def __init__(self):
        self._config = None
        self._running = False
        self._tasks = []
        self._flush_counter = 0

    async def start(self) -> None:
        """Initialize and start all system components."""
        # ─── 1. Load configuration ───
        self._config = load_config()
        setup_logging(self._config.logging)

        logger.info(
            "system_starting",
            trade_mode=self._config.trade_mode,
            dry_run=self._config.dry_run,
            testnet=self._config.testnet,
        )

        if not self._config.api_key or self._config.api_key == "your_api_key_here":
            logger.error("NO_API_KEY — Please set BINANCE_API_KEY in .env file")
            print("\n⚠️  No Binance API key configured!")
            print("   Copy .env.example to .env and set your credentials.")
            print("   See: https://www.binance.com/en/my/settings/api-management\n")
            return

        # ─── 2. Initialize database ───
        db = TinyCoinsDB(db_path="data/tinycoins.db")
        db.connect()
        run_id = db.start_new_run(self._config.trade_mode, self._config.dry_run)
        archived = db.archive_old_runs(keep_last=3)
        if archived:
            logger.info("old_runs_cleaned", count=archived)

        # ─── 3. Initialize exchange client ───
        exchange = ExchangeClient(self._config)
        await exchange.connect()

        # ─── 3. Initialize clock ───
        clock = Clock()
        await clock.sync(exchange.client)

        # ─── 4. Initialize metadata cache ───
        metadata = MetadataCache()
        await metadata.refresh(exchange)

        # ─── 5. Initialize all components ───
        universe = UniverseManager(self._config, metadata)
        await universe.refresh(exchange)

        radar_features = RadarFeatureEngine(self._config.radar)
        radar_scorer = RadarScorer(self._config.radar, radar_features)
        radar_service = RadarService(
            self._config, universe, radar_features, radar_scorer
        )
        radar_service._db = db  # V2: DB reference for tick/score storage

        state_engine = StateEngine(self._config.monitor)
        deep_features = DeepFeatureEngine(self._config.monitor, state_engine)
        lifecycle = LifecycleManager()

        ws_manager = WebSocketManager(self._config, exchange.socket_manager)
        stream_manager = PromotedStreamManager(
            self._config, ws_manager, state_engine
        )

        order_manager = OrderManager(self._config, exchange, metadata)
        risk_engine = RiskEngine(self._config, order_manager)
        execution = ExecutionEngine(
            self._config, metadata, order_manager,
            state_engine, deep_features, lifecycle,
            risk_engine=risk_engine,
        )
        execution._db = db  # V2: DB reference for trade logging
        exit_manager = ExitManager(self._config, state_engine, deep_features)

        long_engine = LongEngine(
            self._config, state_engine, deep_features, lifecycle
        )

        # CSV data logger (1-minute snapshots)
        csv_logger = CsvDataLogger(
            self._config, radar_features, radar_scorer,
            state_engine, deep_features, lifecycle,
            output_dir="data", interval_seconds=60,
        )
        csv_logger.set_execution(execution)
        csv_logger.set_ws_manager(ws_manager)

        # V2: Regime filter
        regime_filter = RegimeFilter()
        csv_logger.set_regime_filter(regime_filter)

        # V2: Missed-opportunity analyzer
        missed_opp = MissedOpportunityAnalyzer(
            radar_features, radar_scorer, lifecycle,
            state_engine=state_engine,
            output_dir="data",
            report_interval_seconds=900,
        )

        # V2: Inject into radar service for data feeding
        radar_service._regime_filter = regime_filter
        radar_service._missed_opp = missed_opp

        # V2: History preload (seed radar baselines)
        print("  \u23f3 Loading historical data for radar baselines...")
        uni_pre = universe.universe
        preloaded = await preload_radar_history(
            exchange, radar_features, uni_pre.radar_eligible, limit=360
        )
        print(f"  \u2705 Preloaded {preloaded} symbol histories\n")

        # ─── 6. Promotion/demotion callback ───
        async def on_promotion_change(to_promote, to_demote):
            for symbol in to_demote:
                await stream_manager.unsubscribe(symbol)
                db.log_promotion(symbol, "demote")
            for symbol in to_promote:
                await stream_manager.subscribe(symbol)
                result = radar_scorer.last_results.get(symbol)
                score = result.effective_score if result else 0
                label = result.label.value if result else ""
                db.log_promotion(symbol, "promote", score, label)

        # ─── 7. Print startup summary ───
        uni = universe.universe
        print("\n" + "=" * 60)
        print("  🪙  TinyCoins Momentum Trading System")
        print("=" * 60)
        print(f"  Mode:       {self._config.trade_mode}")
        print(f"  Dry Run:    {self._config.dry_run}")
        print(f"  Testnet:    {self._config.testnet}")
        print(f"  Universe:   {len(uni.monitored)} monitored")
        print(f"              {len(uni.radar_eligible)} radar-eligible")
        print(f"              {len(uni.long_eligible)} long-eligible")
        print(f"  Symbols:    {len(metadata.symbols)} total cached")
        print(f"  Clock:      offset={clock.offset_ms:.0f}ms")
        print(f"  Run ID:     {run_id}")
        print("  Press Ctrl+C to stop")
        print(f"  Dashboard:  http://localhost:{self._config.dashboard_port}")
        print("=" * 60 + "\n")

        # ─── 8. Start background tasks ───
        self._running = True

        # Dashboard
        dashboard = DashboardServer(port=self._config.dashboard_port)
        dashboard.config = self._config
        dashboard.universe = universe
        dashboard.radar_scorer = radar_scorer
        dashboard.radar_features = radar_features
        dashboard.state_engine = state_engine
        dashboard.deep_features = deep_features
        dashboard.execution = execution
        dashboard.risk_engine = risk_engine
        dashboard.ws_manager = ws_manager
        dashboard.lifecycle = lifecycle
        dashboard.clock = clock
        await dashboard.start()

        # Clock sync
        self._tasks.append(
            asyncio.create_task(clock.start_periodic_sync(exchange.client))
        )
        # Metadata refresh
        self._tasks.append(
            asyncio.create_task(metadata.start_periodic_refresh(exchange))
        )
        # Universe refresh
        self._tasks.append(
            asyncio.create_task(universe.start_periodic_refresh(exchange))
        )
        # Mini-ticker stream for radar
        self._tasks.append(
            asyncio.create_task(
                ws_manager.start_mini_ticker_stream(radar_service.handle_mini_ticker)
            )
        )
        # Radar scoring loop
        self._tasks.append(
            asyncio.create_task(
                radar_service.run_scoring_loop(on_promotion_change)
            )
        )
        # Exit manager loop
        self._tasks.append(
            asyncio.create_task(exit_manager.run_exit_loop(execution))
        )

        # ─── 9. Main trading loop ───
        logger.info("main_loop_starting")

        try:
            while self._running:
                await asyncio.sleep(1)

                # Risk checks
                risk_engine.update_data_timestamp()
                risk_engine.check_risk_conditions(
                    execution.position_count,
                    ws_manager.last_message_time,
                )

                if not risk_engine.is_trading_allowed:
                    continue

                # DB flush (always, regardless of regime)
                self._flush_counter += 1
                if self._flush_counter % 5 == 0:
                    flushed = db.flush()
                    if flushed > 0:
                        logger.debug("db_flushed", rows=flushed)

                # CSV snapshot (internally throttled to 1-minute)
                if csv_logger.should_write():
                    csv_logger.write_snapshot()

                # V2: Missed-opportunity report (every 15m)
                if missed_opp.should_report():
                    missed_opp.generate_report()

                # V2: Regime filter — update and skip if dead
                regime_filter.compute_regime()
                if not regime_filter.is_trading_allowed:
                    continue

                # Evaluate long signals
                promoted = radar_scorer.promoted_symbols
                if not promoted:
                    continue

                entry_signals = long_engine.evaluate(promoted)

                for symbol in entry_signals:
                    # Check risk budget
                    if not risk_engine.can_open_position(execution.position_count):
                        break

                    # Attempt entry
                    position = await execution.execute_entry(
                        symbol, reason="long_confirmed"
                    )
                    if position:
                        logger.info(
                            "trade_executed",
                            symbol=symbol,
                            qty=position.quantity,
                            entry=position.entry_price,
                        )
                        db.log_trade_entry(
                            symbol, "BUY", position.entry_price, position.quantity
                        )

        except asyncio.CancelledError:
            pass
        finally:
            # ─── 10. Graceful shutdown ───
            logger.info("system_shutting_down")
            self._running = False
            radar_service.stop()

            # Final DB flush
            db.flush()

            # Close any open positions
            for symbol in list(execution.positions.keys()):
                await execution.close_position(symbol, "shutdown")

            # Cancel tasks
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)

            # Shutdown WebSocket
            await ws_manager.shutdown()
            await dashboard.stop()
            await exchange.close()

            # DB stats and close
            db_stats = db.get_run_stats()
            db.close()

            # Print final stats
            stats = risk_engine.get_status_summary()
            print("\n" + "=" * 60)
            print("  \U0001f4ca Session Summary")
            print("=" * 60)
            print(f"  Run ID:     {run_id}")
            print(f"  Trades:     {stats['daily']['trades']}")
            print(f"  Win Rate:   {stats['daily']['win_rate']}%")
            print(f"  PnL:        {stats['daily']['pnl']}")
            print(f"  Drawdown:   {stats['daily']['drawdown_pct']}%")
            print(f"  DB Ticks:   {db_stats['ticks']}")
            print(f"  DB Scores:  {db_stats['radar_scores']}")
            print(f"  Promotions: {db_stats['promotions']}")
            print("=" * 60 + "\n")

            logger.info("system_stopped", final_stats=stats)


async def main():
    system = TinyCoinsSystem()

    # Handle Ctrl+C gracefully
    loop = asyncio.get_running_loop()

    def signal_handler():
        system._running = False

    try:
        loop.add_signal_handler(signal.SIGINT, signal_handler)
        loop.add_signal_handler(signal.SIGTERM, signal_handler)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler
        pass

    await system.start()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutdown complete.")
```

---

