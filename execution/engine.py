"""
Execution engine — decides sizing, validates entry, and places orders.

Supports minimum-quantity mode for low-risk testing.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig
from core.events import SymbolState
from exchange.metadata import MetadataCache
from exchange.order_manager import OrderManager, OrderRecord
from monitor.features import DeepFeatureEngine
from monitor.state import StateEngine
from signals.lifecycle import LifecycleManager

logger = structlog.get_logger(__name__)


@dataclass
class Position:
    """Live position record."""

    symbol: str
    side: str                     # BUY
    entry_price: float = 0.0
    quantity: float = 0.0
    entry_time: float = field(default_factory=time.time)
    highest_price: float = 0.0
    lowest_price: float = float("inf")
    stop_price: float = 0.0
    trailing_active: bool = False
    trailing_stop_price: float = 0.0
    trade_mode: str = ""
    order_record: Optional[OrderRecord] = None

    @property
    def notional(self) -> float:
        return self.entry_price * self.quantity

    @property
    def hold_time(self) -> float:
        return time.time() - self.entry_time

    def unrealized_pnl_pct(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return ((current_price - self.entry_price) / self.entry_price) * 100.0


class ExecutionEngine:
    """
    Handles trade sizing, entry validation, and order placement.

    In minimum_quantity mode, position size = exchange minQty.
    In normal mode, size = min(risk-budget, liquidity-cap, max-notional).
    """

    def __init__(
        self,
        config: AppConfig,
        metadata: MetadataCache,
        order_manager: OrderManager,
        state_engine: StateEngine,
        feature_engine: DeepFeatureEngine,
        lifecycle: LifecycleManager,
    ):
        self._config = config
        self._metadata = metadata
        self._orders = order_manager
        self._state = state_engine
        self._features = feature_engine
        self._lifecycle = lifecycle
        self._positions: Dict[str, Position] = {}

    @property
    def positions(self) -> Dict[str, Position]:
        return self._positions

    @property
    def position_count(self) -> int:
        return len(self._positions)

    def _calculate_size(
        self, symbol: str, current_price: float, stop_distance_pct: float
    ) -> Optional[float]:
        """
        Calculate position size based on trade mode.
        """
        if self._config.is_minimum_quantity_mode:
            # ─── Minimum Quantity Mode ───
            min_qty = self._metadata.get_min_trade_quantity(symbol, current_price)
            if min_qty is None:
                logger.error("min_qty_unavailable", symbol=symbol)
                return None
            logger.info(
                "sizing_minimum_quantity",
                symbol=symbol,
                min_qty=min_qty,
                est_notional=round(min_qty * current_price, 4),
            )
            return min_qty
        else:
            # ─── Normal Risk-Based Sizing ───
            # TODO: implement full risk-based sizing
            # For now, use minimum quantity as a safe default
            return self._metadata.get_min_trade_quantity(symbol, current_price)

    async def execute_entry(self, symbol: str, reason: str = "") -> Optional[Position]:
        """
        Attempt to enter a long position on the given symbol.

        Performs final pre-entry validation before placing the order.
        """
        # ─── Pre-entry validation ───
        state = self._state.get(symbol)
        if state is None:
            logger.warning("entry_no_state", symbol=symbol)
            return None

        features = self._features.compute(symbol)
        if features is None:
            logger.warning("entry_no_features", symbol=symbol)
            return None

        current_price = state.mid_price
        if current_price <= 0:
            logger.warning("entry_invalid_price", symbol=symbol)
            return None

        # Spread still acceptable?
        if state.spread_pct > self._config.long_engine.max_spread_pct:
            logger.info("entry_rejected_spread", symbol=symbol, spread=state.spread_pct)
            return None

        # Still have depth?
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._config.long_engine.min_top_book_notional_usdt:
            logger.info("entry_rejected_depth", symbol=symbol, depth=top_notional)
            return None

        # ─── Size calculation ───
        stop_distance = self._config.exit.hard_stop_pct
        qty = self._calculate_size(symbol, current_price, stop_distance)
        if qty is None or qty <= 0:
            return None

        # ─── Compute entry price for limit order ───
        # Use a marketable limit: best ask + small buffer
        info = self._metadata.get(symbol)
        entry_price = state.best_ask
        if info and info.tick_size > 0:
            entry_price = self._metadata.round_tick_size(
                entry_price * 1.001, info.tick_size  # slightly above ask
            )

        # ─── Lifecycle transition ───
        self._lifecycle.transition(symbol, SymbolState.ORDER_PENDING, f"entry_{reason}")

        # ─── Place order ───
        order_type = self._config.execution.order_type
        order = await self._orders.place_order(
            symbol=symbol,
            side="BUY",
            quantity=qty,
            price=entry_price if order_type == "LIMIT" else None,
            order_type=order_type,
        )

        if order is None:
            self._lifecycle.transition(symbol, SymbolState.COOLDOWN, "order_failed")
            return None

        # ─── Create position ───
        fill_price = order.avg_fill_price if order.avg_fill_price > 0 else entry_price
        stop_price = fill_price * (1 - stop_distance / 100.0)

        position = Position(
            symbol=symbol,
            side="BUY",
            entry_price=fill_price,
            quantity=order.filled_qty if order.filled_qty > 0 else qty,
            highest_price=fill_price,
            stop_price=stop_price,
            trade_mode=self._config.trade_mode,
            order_record=order,
        )

        self._positions[symbol] = position
        self._lifecycle.transition(symbol, SymbolState.LIVE_POSITION, "filled")

        logger.info(
            "position_opened",
            symbol=symbol,
            side="BUY",
            qty=position.quantity,
            entry_price=position.entry_price,
            stop_price=round(stop_price, 8),
            notional=round(position.notional, 4),
            trade_mode=position.trade_mode,
            is_dry_run=order.is_dry_run,
        )

        return position

    async def close_position(
        self, symbol: str, reason: str = "manual"
    ) -> bool:
        """Close a live position."""
        position = self._positions.get(symbol)
        if position is None:
            return False

        state = self._state.get(symbol)
        current_price = state.mid_price if state else position.entry_price

        # Place sell order
        order = await self._orders.place_order(
            symbol=symbol,
            side="SELL",
            quantity=position.quantity,
            price=None,  # Market sell for exits
            order_type="MARKET",
        )

        exit_price = order.avg_fill_price if order and order.avg_fill_price > 0 else current_price
        pnl_pct = position.unrealized_pnl_pct(exit_price)

        del self._positions[symbol]
        self._lifecycle.transition(symbol, SymbolState.COOLDOWN, f"exit_{reason}")

        logger.info(
            "position_closed",
            symbol=symbol,
            entry_price=position.entry_price,
            exit_price=exit_price,
            pnl_pct=round(pnl_pct, 3),
            hold_time_s=round(position.hold_time, 1),
            reason=reason,
            trade_mode=position.trade_mode,
        )

        return True
