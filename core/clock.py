"""
Dual clock — maintains exchange server-time offset for consistent timestamping.
"""

import asyncio
import time
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


class Clock:
    """
    Maintains a local-to-exchange time offset by periodically calling
    the Binance server time endpoint.
    """

    def __init__(self):
        self._offset_ms: float = 0.0  # exchange_time - local_time (in ms)
        self._last_sync: float = 0.0
        self._sync_interval: float = 60.0  # seconds between syncs

    async def sync(self, client) -> None:
        """Sync with exchange server time."""
        try:
            server_time = await client.get_server_time()
            server_ms = server_time["serverTime"]
            local_ms = time.time() * 1000
            self._offset_ms = server_ms - local_ms
            self._last_sync = time.time()
            logger.info("clock_synced", offset_ms=round(self._offset_ms, 1))
        except Exception as e:
            logger.warning("clock_sync_failed", error=str(e))

    async def start_periodic_sync(self, client) -> None:
        """Background task to periodically resync."""
        while True:
            await self.sync(client)
            await asyncio.sleep(self._sync_interval)

    def local_now(self) -> float:
        """Current local time as Unix timestamp (seconds)."""
        return time.time()

    def local_now_ms(self) -> int:
        """Current local time as Unix timestamp (milliseconds)."""
        return int(time.time() * 1000)

    def exchange_now_ms(self) -> int:
        """Estimated current exchange time (milliseconds)."""
        return int(time.time() * 1000 + self._offset_ms)

    @property
    def offset_ms(self) -> float:
        return self._offset_ms

    @property
    def seconds_since_sync(self) -> float:
        if self._last_sync == 0:
            return float("inf")
        return time.time() - self._last_sync
