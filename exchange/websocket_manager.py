"""
WebSocket lifecycle manager — handles subscriptions, reconnects, and cleanup.

Manages combined streams with Binance WebSocket limits, automatic reconnection,
and clean subscription/unsubscription of promoted symbols.
"""

import asyncio
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

import structlog
from binance import BinanceSocketManager

from core.config import AppConfig

logger = structlog.get_logger(__name__)

# Binance limit: 200 streams per single WebSocket connection
MAX_STREAMS_PER_CONNECTION = 200


class WebSocketManager:
    """
    Manages WebSocket connections for market data streams.

    Supports:
    - Broad radar streams (mini-ticker for all symbols)
    - Per-symbol high-resolution streams (depth, aggTrade, bookTicker)
    - Automatic reconnect on disconnect
    - Clean subscribe/unsubscribe lifecycle
    """

    def __init__(self, config: AppConfig, socket_manager: BinanceSocketManager):
        self._config = config
        self._bsm = socket_manager
        self._active_streams: Dict[str, Any] = {}  # stream_key → task
        self._active_symbols: Set[str] = set()
        self._running = False
        self._last_message_time: float = time.time()

    @property
    def last_message_time(self) -> float:
        return self._last_message_time

    @property
    def active_symbol_count(self) -> int:
        return len(self._active_symbols)

    # ─── Broad Radar Stream ───

    async def start_mini_ticker_stream(
        self, callback: Callable[[List[Dict]], Coroutine]
    ) -> None:
        """Subscribe to the all-market mini ticker stream."""
        self._running = True

        async def _listen():
            while self._running:
                try:
                    ts = self._bsm.miniticker_socket()
                    async with ts as stream:
                        logger.info("mini_ticker_stream_connected")
                        while self._running:
                            msg = await asyncio.wait_for(stream.recv(), timeout=30)
                            self._last_message_time = time.time()
                            if msg:
                                await callback(msg)
                except asyncio.TimeoutError:
                    logger.warning("mini_ticker_stream_timeout", action="reconnecting")
                except asyncio.CancelledError:
                    logger.info("mini_ticker_stream_cancelled")
                    return
                except Exception as e:
                    logger.error("mini_ticker_stream_error", error=str(e))
                    await asyncio.sleep(2)

        task = asyncio.create_task(_listen())
        self._active_streams["mini_ticker"] = task

    # ─── Per-Symbol High-Resolution Streams ───

    async def subscribe_symbol(
        self,
        symbol: str,
        on_depth: Callable[[Dict], Coroutine],
        on_agg_trade: Callable[[Dict], Coroutine],
        on_book_ticker: Callable[[Dict], Coroutine],
    ) -> None:
        """Subscribe to depth, aggTrade, and bookTicker for a symbol."""
        if symbol in self._active_symbols:
            return

        symbol_lower = symbol.lower()
        depth_levels = self._config.monitor.book_depth_levels
        speed_ms = self._config.monitor.book_update_speed_ms

        async def _listen_depth():
            while self._running and symbol in self._active_symbols:
                try:
                    ts = self._bsm.depth_socket(
                        symbol, depth=depth_levels, interval=speed_ms
                    )
                    async with ts as stream:
                        logger.info("depth_stream_connected", symbol=symbol)
                        while self._running and symbol in self._active_symbols:
                            msg = await asyncio.wait_for(stream.recv(), timeout=30)
                            self._last_message_time = time.time()
                            if msg:
                                await on_depth(msg)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("depth_stream_error", symbol=symbol, error=str(e))
                    await asyncio.sleep(1)

        async def _listen_agg_trade():
            while self._running and symbol in self._active_symbols:
                try:
                    ts = self._bsm.aggtrade_socket(symbol)
                    async with ts as stream:
                        logger.info("agg_trade_stream_connected", symbol=symbol)
                        while self._running and symbol in self._active_symbols:
                            msg = await asyncio.wait_for(stream.recv(), timeout=30)
                            self._last_message_time = time.time()
                            if msg:
                                await on_agg_trade(msg)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("agg_trade_stream_error", symbol=symbol, error=str(e))
                    await asyncio.sleep(1)

        async def _listen_book_ticker():
            while self._running and symbol in self._active_symbols:
                try:
                    ts = self._bsm.symbol_book_ticker_socket(symbol)
                    async with ts as stream:
                        logger.info("book_ticker_stream_connected", symbol=symbol)
                        while self._running and symbol in self._active_symbols:
                            msg = await asyncio.wait_for(stream.recv(), timeout=30)
                            self._last_message_time = time.time()
                            if msg:
                                await on_book_ticker(msg)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("book_ticker_stream_error", symbol=symbol, error=str(e))
                    await asyncio.sleep(1)

        self._active_symbols.add(symbol)
        self._active_streams[f"{symbol}_depth"] = asyncio.create_task(_listen_depth())
        self._active_streams[f"{symbol}_agg"] = asyncio.create_task(_listen_agg_trade())
        self._active_streams[f"{symbol}_bbo"] = asyncio.create_task(_listen_book_ticker())

        logger.info("symbol_subscribed", symbol=symbol, total=len(self._active_symbols))

    async def unsubscribe_symbol(self, symbol: str) -> None:
        """Unsubscribe from all streams for a symbol."""
        if symbol not in self._active_symbols:
            return

        self._active_symbols.discard(symbol)

        for suffix in ["_depth", "_agg", "_bbo"]:
            key = f"{symbol}{suffix}"
            task = self._active_streams.pop(key, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        logger.info("symbol_unsubscribed", symbol=symbol, remaining=len(self._active_symbols))

    # ─── Shutdown ───

    async def shutdown(self) -> None:
        """Cancel all streams and clean up."""
        self._running = False
        for key, task in list(self._active_streams.items()):
            if not task.done():
                task.cancel()
        # Wait for all tasks to finish
        if self._active_streams:
            await asyncio.gather(
                *self._active_streams.values(), return_exceptions=True
            )
        self._active_streams.clear()
        self._active_symbols.clear()
        logger.info("websocket_manager_shutdown")
