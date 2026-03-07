"""
Normalized event types for the trading system.

Every incoming market data message is converted into one of these types
before being consumed by features, scoring, or execution.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


class EventSource(Enum):
    MINI_TICKER = "mini_ticker"
    DEPTH = "depth"
    AGG_TRADE = "agg_trade"
    BOOK_TICKER = "book_ticker"
    TICKER_24H = "ticker_24h"


# ─── Radar-layer (lightweight) events ───


@dataclass(slots=True)
class MarketStatUpdate:
    """Lightweight update from mini-ticker or 24h ticker stream."""
    symbol: str
    open_price: float
    high_price: float
    low_price: float
    close_price: float
    base_volume: float
    quote_volume: float
    event_time: int          # exchange ms timestamp
    local_time: float        # time.time()
    source: EventSource = EventSource.MINI_TICKER


# ─── High-resolution events ───


@dataclass(slots=True)
class OrderBookLevel:
    """A single price level in the order book."""
    price: float
    quantity: float

    @property
    def notional(self) -> float:
        return self.price * self.quantity


@dataclass(slots=True)
class OrderBookUpdate:
    """Partial or full order book update."""
    symbol: str
    bids: List[OrderBookLevel]
    asks: List[OrderBookLevel]
    last_update_id: int
    event_time: int
    local_time: float
    source: EventSource = EventSource.DEPTH


@dataclass(slots=True)
class AggTrade:
    """Aggregate trade event."""
    symbol: str
    trade_id: int
    price: float
    quantity: float
    buyer_is_maker: bool     # True = seller aggressor (hit bid)
    event_time: int
    local_time: float
    source: EventSource = EventSource.AGG_TRADE

    @property
    def notional(self) -> float:
        return self.price * self.quantity

    @property
    def is_buy_aggressor(self) -> bool:
        """True when the buyer is the taker (lifted offer)."""
        return not self.buyer_is_maker


@dataclass(slots=True)
class BestBidAsk:
    """Best bid/ask (book ticker) update."""
    symbol: str
    bid_price: float
    bid_qty: float
    ask_price: float
    ask_qty: float
    event_time: int
    local_time: float
    source: EventSource = EventSource.BOOK_TICKER

    @property
    def mid_price(self) -> float:
        return (self.bid_price + self.ask_price) / 2.0

    @property
    def spread(self) -> float:
        return self.ask_price - self.bid_price

    @property
    def spread_pct(self) -> float:
        mid = self.mid_price
        if mid <= 0:
            return 0.0
        return (self.spread / mid) * 100.0


# ─── Symbol lifecycle states ───


class SymbolState(Enum):
    """Lifecycle states a symbol moves through."""
    NORMAL = "normal"
    SUSPICIOUS = "suspicious"
    DANGER_WATCH = "danger_watch"
    HIGH_RES_MONITOR = "high_res_monitor"
    LONG_WATCH = "long_watch"
    SHORT_WATCH = "short_watch"
    ORDER_PENDING = "order_pending"
    LIVE_POSITION = "live_position"
    COOLDOWN = "cooldown"


# ─── Radar labels ───


class RadarLabel(Enum):
    """Radar classification labels."""
    BUILDING_PRESSURE = "building_pressure"
    IGNITION_RISK = "ignition_risk"
    ALREADY_EXTENDED = "already_extended"
    ILLIQUID_TRAP = "illiquid_trap"
    COOLING_OFF = "cooling_off"
    NORMAL = "normal"


# ─── Position states ───


class PositionState(Enum):
    """Trade position lifecycle."""
    CANDIDATE = "candidate"
    WATCH = "watch"
    CONFIRMED = "confirmed"
    ORDER_PENDING = "order_pending"
    LIVE = "live"
    REDUCING = "reducing"
    EXITED = "exited"
    CANCELLED = "cancelled"
