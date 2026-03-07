"""
Per-symbol rolling state for high-resolution monitoring.

Maintains ring buffers for price, spread, volume, flow, and order book
snapshots for promoted symbols.
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
        self.bids = update.bids
        self.asks = update.asks
        self.last_update = time.time()

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
