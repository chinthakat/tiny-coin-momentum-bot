"""
Execution engine — decides sizing, validates entry, and places orders.

Supports minimum-quantity mode for low-risk testing
and risk-based sizing for normal operation.
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
        risk_engine=None,  # FIX #14: optional risk engine reference
    ):
        self._config = config
        self._metadata = metadata
        self._orders = order_manager
        self._state = state_engine
        self._features = feature_engine
        self._lifecycle = lifecycle
        self._risk_engine = risk_engine  # FIX #14
        self._positions: Dict[str, Position] = {}
        self._trade_history: List[Dict] = []  # completed trades

    @property
    def positions(self) -> Dict[str, Position]:
        return self._positions

    @property
    def position_count(self) -> int:
        return len(self._positions)

    @property
    def trade_history(self) -> List[Dict]:
        return self._trade_history

    def total_open_notional(self) -> float:
        """Total notional across all open positions."""
        total = 0.0
        for pos in self._positions.values():
            state = self._state.get(pos.symbol)
            price = state.mid_price if state else pos.entry_price
            total += price * pos.quantity
        return total

    def _calculate_size(
        self, symbol: str, current_price: float, stop_distance_pct: float
    ) -> Optional[float]:
        """
        Calculate position size based on trade mode.

        FIX #12: Normal mode now implements proper risk-based sizing.
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
            # ─── FIX #12: Risk-Based Sizing ───
            min_qty = self._metadata.get_min_trade_quantity(symbol, current_price)
            if min_qty is None:
                return None

            # Risk budget per trade (from config)
            risk_budget = self._config.execution.risk_per_trade_usd

            # Size from risk: risk_budget / (stop_distance * price)
            if stop_distance_pct > 0 and current_price > 0:
                risk_qty = risk_budget / (stop_distance_pct / 100.0 * current_price)
            else:
                risk_qty = min_qty

            # Liquidity cap: don't take more than 5% of visible top-5 book
            state = self._state.get(symbol)
            if state:
                ask_notional = state.book_ask_notional(5)
                if ask_notional > 0:
                    liquidity_cap_qty = (ask_notional * 0.05) / current_price
                    risk_qty = min(risk_qty, liquidity_cap_qty)

            # Max notional cap
            max_notional = self._config.execution.max_notional_per_trade
            if max_notional > 0 and current_price > 0:
                max_notional_qty = max_notional / current_price
                risk_qty = min(risk_qty, max_notional_qty)

            # Floor at minimum quantity
            final_qty = max(risk_qty, min_qty)

            # Round to step size
            info = self._metadata.get(symbol)
            if info and info.step_size > 0:
                final_qty = self._metadata.round_step_size(final_qty, info.step_size)

            logger.info(
                "sizing_risk_based",
                symbol=symbol,
                risk_qty=round(risk_qty, 8),
                final_qty=round(final_qty, 8),
                est_notional=round(final_qty * current_price, 4),
            )
            return final_qty

    async def execute_entry(self, symbol: str, reason: str = "") -> Optional[Position]:
        """
        Attempt to enter a long position on the given symbol.

        Performs final pre-entry validation before placing the order.
        """
        # ─── FIX #14: Check risk engine permission ───
        if self._risk_engine is not None:
            if not self._risk_engine.can_open_position(
                self.position_count,
                self.total_open_notional(),
            ):
                logger.info("entry_rejected_risk", symbol=symbol)
                return None

        # ─── Pre-entry validation ───
        state = self._state.get(symbol)
        if state is None:
            logger.warning("entry_no_state", symbol=symbol)
            return None

        # FIX #16: Check per-symbol staleness
        if state.is_stale(timeout_s=10.0):
            logger.warning("entry_rejected_stale_data", symbol=symbol,
                           age_s=round(time.time() - state.last_update, 1))
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

        # ─── FIX #13: Adaptive entry price buffer ───
        info = self._metadata.get(symbol)
        entry_price = state.best_ask

        # Buffer = max(spread * 0.25, 0.05%), capped at max_slippage
        spread_buffer_pct = max(state.spread_pct * 0.25, 0.05)
        max_slippage = getattr(self._config.execution, 'max_slippage_pct', 0.3)
        buffer_pct = min(spread_buffer_pct, max_slippage) / 100.0

        entry_price = entry_price * (1 + buffer_pct)

        if info and info.tick_size > 0:
            entry_price = self._metadata.round_tick_size(entry_price, info.tick_size)

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
            buffer_pct=round(buffer_pct * 100, 3),
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
        pnl_usd = (exit_price - position.entry_price) * position.quantity

        # Record trade in history
        self._trade_history.append({
            "symbol": symbol,
            "entry_price": position.entry_price,
            "exit_price": exit_price,
            "quantity": position.quantity,
            "pnl_pct": round(pnl_pct, 3),
            "pnl_usd": round(pnl_usd, 6),
            "hold_time_s": round(position.hold_time, 1),
            "reason": reason,
            "trade_mode": position.trade_mode,
            "closed_at": time.time(),
        })

        # Notify risk engine
        if self._risk_engine is not None:
            self._risk_engine.record_trade_result(pnl_usd)

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
