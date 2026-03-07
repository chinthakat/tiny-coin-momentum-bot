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
