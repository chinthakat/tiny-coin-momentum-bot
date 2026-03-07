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

        try:
            with open(self._filepath, "a", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerows(rows)
        except PermissionError:
            logger.error("missed_opportunity_csv_write_failed", error="Permission denied (file is likely open)", path=self._filepath)

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
