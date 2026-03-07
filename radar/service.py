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

                # Notify callback if there are changes
                if (to_promote or to_demote) and on_promotion_change:
                    await on_promotion_change(to_promote, to_demote)

                # Periodic cleanup
                if self._tick_count % 60 == 0:
                    self._features.cleanup_stale(eligible)

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
