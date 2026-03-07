"""
Universe Manager — builds and refreshes the tradeable symbol universe.

Applies filters from config.yaml to determine which symbols are
monitored, radar-eligible, long-eligible, and short-eligible.
"""

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import structlog

from core.config import AppConfig, UniverseConfig
from exchange.metadata import MetadataCache, SymbolInfo

logger = structlog.get_logger(__name__)


@dataclass
class UniverseSnapshot:
    """Current state of the symbol universe."""

    monitored: Set[str] = field(default_factory=set)
    radar_eligible: Set[str] = field(default_factory=set)
    long_eligible: Set[str] = field(default_factory=set)
    short_eligible: Set[str] = field(default_factory=set)
    updated_at: float = 0.0


class UniverseManager:
    """
    Maintains the monitored and tradeable symbol universe.

    Responsibilities:
    - Fetch 24h ticker data and apply volume/spread/activity filters
    - Exclude stablecoins and leveraged tokens
    - Maintain separate eligible lists per venue
    - Refresh slowly to preserve rolling state
    """

    def __init__(self, config: AppConfig, metadata: MetadataCache):
        self._config = config
        self._ucfg: UniverseConfig = config.universe
        self._metadata = metadata
        self._universe = UniverseSnapshot()
        self._ticker_data: Dict[str, Dict] = {}

    @property
    def universe(self) -> UniverseSnapshot:
        return self._universe

    @property
    def ticker_data(self) -> Dict[str, Dict]:
        return self._ticker_data

    def _is_excluded_base(self, base_asset: str) -> bool:
        """Check if the base asset matches excluded patterns (leveraged tokens)."""
        for pattern in self._ucfg.excluded_base_patterns:
            if base_asset.upper().endswith(pattern):
                return True
        return False

    def _is_excluded_quote(self, quote_asset: str) -> bool:
        """Check if the quote asset is in the excluded list."""
        return quote_asset.upper() in [q.upper() for q in self._ucfg.excluded_quote_assets]

    async def refresh(self, exchange_client) -> UniverseSnapshot:
        """Refresh the universe from current exchange data."""
        logger.info("universe_refresh_starting")

        # Fetch 24h ticker stats
        try:
            tickers = await exchange_client.get_ticker_24h()
        except Exception as e:
            logger.error("universe_refresh_failed", error=str(e))
            return self._universe

        # Index tickers by symbol
        ticker_map: Dict[str, Dict] = {}
        for t in tickers:
            ticker_map[t["symbol"]] = t
        self._ticker_data = ticker_map

        monitored: Set[str] = set()
        radar_eligible: Set[str] = set()
        long_eligible: Set[str] = set()
        short_eligible: Set[str] = set()

        for sym, info in self._metadata.symbols.items():
            # Must be active
            if info.status != "TRADING":
                continue

            # Exclude non-USDT quotes for simplicity (focus on USDT pairs)
            if info.quote_asset != "USDT":
                continue

            # Exclude stablecoins and leveraged tokens
            if self._is_excluded_base(info.base_asset):
                continue
            if self._is_excluded_quote(info.quote_asset):
                continue

            # Must have a ticker entry
            ticker = ticker_map.get(sym)
            if ticker is None:
                continue

            quote_volume = float(ticker.get("quoteVolume", 0))
            trade_count = int(ticker.get("count", 0))

            # ─── Volume filter ───
            if quote_volume < self._ucfg.min_24h_quote_volume_usdt:
                continue

            # ─── Trade count filter ───
            if trade_count < self._ucfg.min_trade_count_24h:
                continue

            # ─── Spread filter (approximate from high/low) ───
            high = float(ticker.get("highPrice", 0))
            low = float(ticker.get("lowPrice", 0))
            last = float(ticker.get("lastPrice", 0))
            if last > 0 and high > 0 and low > 0:
                # Use weighted avg price for more accurate spread estimate
                weighted_avg = float(ticker.get("weightedAvgPrice", last))
                if weighted_avg > 0:
                    # Rough spread estimate: use bid/ask from ticker if available
                    bid = float(ticker.get("bidPrice", 0))
                    ask = float(ticker.get("askPrice", 0))
                    if bid > 0 and ask > 0:
                        spread_pct = ((ask - bid) / ((ask + bid) / 2)) * 100
                        if spread_pct > self._ucfg.max_spread_pct:
                            continue

            # ─── Blacklist filter ───
            if sym in self._config.risk.symbol_blacklist:
                continue

            # Symbol passes all filters
            monitored.add(sym)

            # Determine radar eligibility (all monitored USDT spot pairs)
            if info.is_spot_trading:
                radar_eligible.add(sym)
                long_eligible.add(sym)

            # Short eligibility requires margin
            if info.is_margin_trading:
                short_eligible.add(sym)

        self._universe = UniverseSnapshot(
            monitored=monitored,
            radar_eligible=radar_eligible,
            long_eligible=long_eligible,
            short_eligible=short_eligible,
            updated_at=time.time(),
        )

        logger.info(
            "universe_refreshed",
            monitored=len(monitored),
            radar_eligible=len(radar_eligible),
            long_eligible=len(long_eligible),
            short_eligible=len(short_eligible),
        )

        return self._universe

    async def start_periodic_refresh(self, exchange_client) -> None:
        """Background task to periodically refresh the universe."""
        while True:
            await self.refresh(exchange_client)
            await asyncio.sleep(self._ucfg.refresh_interval_seconds)
