"""
Stream manager for promoted symbols — subscribes to depth, aggTrade, bookTicker.

Bridges WebSocket events to the per-symbol state engine.
"""

import time
from typing import Optional

import structlog

from core.config import AppConfig
from core.events import (
    AggTrade,
    BestBidAsk,
    EventSource,
    OrderBookLevel,
    OrderBookUpdate,
)
from exchange.websocket_manager import WebSocketManager
from monitor.state import StateEngine

logger = structlog.get_logger(__name__)


class PromotedStreamManager:
    """
    Manages high-resolution WebSocket subscriptions for promoted symbols.

    Translates raw WebSocket messages into normalized events and
    feeds them into the state engine.
    """

    def __init__(
        self, config: AppConfig, ws_manager: WebSocketManager, state_engine: StateEngine
    ):
        self._config = config
        self._ws = ws_manager
        self._state = state_engine

    async def subscribe(self, symbol: str) -> None:
        """Subscribe to all high-res streams for a promoted symbol."""
        state = self._state.get_or_create(symbol)

        async def on_depth(msg):
            try:
                bids = [
                    OrderBookLevel(float(p), float(q))
                    for p, q in msg.get("bids", [])
                ]
                asks = [
                    OrderBookLevel(float(p), float(q))
                    for p, q in msg.get("asks", [])
                ]
                update = OrderBookUpdate(
                    symbol=symbol,
                    bids=bids,
                    asks=asks,
                    last_update_id=msg.get("lastUpdateId", 0),
                    event_time=msg.get("E", 0),
                    local_time=time.time(),
                )
                state.update_book(update)
            except Exception as e:
                logger.debug("depth_parse_error", symbol=symbol, error=str(e))

        async def on_agg_trade(msg):
            try:
                trade = AggTrade(
                    symbol=symbol,
                    trade_id=msg.get("a", 0),
                    price=float(msg.get("p", 0)),
                    quantity=float(msg.get("q", 0)),
                    buyer_is_maker=msg.get("m", False),
                    event_time=msg.get("E", 0),
                    local_time=time.time(),
                )
                state.update_trade(trade)
            except Exception as e:
                logger.debug("agg_trade_parse_error", symbol=symbol, error=str(e))

        async def on_book_ticker(msg):
            try:
                bbo = BestBidAsk(
                    symbol=symbol,
                    bid_price=float(msg.get("b", 0)),
                    bid_qty=float(msg.get("B", 0)),
                    ask_price=float(msg.get("a", 0)),
                    ask_qty=float(msg.get("A", 0)),
                    event_time=msg.get("E", 0) if "E" in msg else int(time.time() * 1000),
                    local_time=time.time(),
                )
                state.update_bbo(bbo)
            except Exception as e:
                logger.debug("book_ticker_parse_error", symbol=symbol, error=str(e))

        await self._ws.subscribe_symbol(symbol, on_depth, on_agg_trade, on_book_ticker)
        logger.info("promoted_symbol_subscribed", symbol=symbol)

    async def unsubscribe(self, symbol: str) -> None:
        """Unsubscribe from all streams and clean up state."""
        await self._ws.unsubscribe_symbol(symbol)
        self._state.remove(symbol)
        logger.info("promoted_symbol_unsubscribed", symbol=symbol)
