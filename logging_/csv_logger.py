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
        try:
            with open(self._filepath, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_COLUMNS)
        except PermissionError:
            logger.error("csv_header_write_failed", error="Permission denied")

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
        try:
            with open(self._filepath, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerows(rows)
        except PermissionError:
            logger.error("csv_snapshot_write_failed", error="Permission denied (file is likely open)", path=self._filepath)

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
