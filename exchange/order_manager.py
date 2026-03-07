"""
Order manager — handles order lifecycle from validation to fill tracking.

All orders pass through metadata validation before being sent to the exchange.
Supports dry-run mode where orders are logged but not placed.
"""

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig
from core.events import PositionState
from exchange.metadata import MetadataCache

logger = structlog.get_logger(__name__)


class OrderStatus(Enum):
    PENDING = "pending"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass
class OrderRecord:
    """Tracks a single order throughout its lifecycle."""

    symbol: str
    side: str                # BUY or SELL
    order_type: str          # LIMIT, MARKET
    requested_qty: float
    requested_price: Optional[float]
    status: OrderStatus = OrderStatus.PENDING
    order_id: Optional[int] = None
    filled_qty: float = 0.0
    avg_fill_price: float = 0.0
    created_at: float = field(default_factory=time.time)
    filled_at: Optional[float] = None
    cancel_reason: str = ""
    trade_mode: str = ""     # "minimum_quantity" or "normal"
    is_dry_run: bool = False

    @property
    def filled_notional(self) -> float:
        return self.filled_qty * self.avg_fill_price


class OrderManager:
    """
    Manages order placement, cancellation, and fill tracking.

    All orders are validated against exchange filters before placement.
    In dry_run mode, orders are logged and simulated but never sent.
    """

    def __init__(
        self,
        config: AppConfig,
        exchange_client,
        metadata: MetadataCache,
    ):
        self._config = config
        self._client = exchange_client
        self._metadata = metadata
        self._orders: Dict[str, OrderRecord] = {}  # internal_id → OrderRecord
        self._order_counter: int = 0
        self._consecutive_rejects: int = 0

    @property
    def consecutive_rejects(self) -> int:
        return self._consecutive_rejects

    def _next_id(self) -> str:
        self._order_counter += 1
        return f"tc_{self._order_counter}"

    async def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
        order_type: Optional[str] = None,
    ) -> Optional[OrderRecord]:
        """
        Validate and place an order.

        Returns an OrderRecord on success, or None if validation fails.
        """
        if order_type is None:
            order_type = self._config.execution.order_type

        # ─── Pre-flight validation ───
        errors = self._metadata.validate_order(symbol, side, quantity, price)
        if errors:
            logger.error(
                "order_validation_failed",
                symbol=symbol,
                side=side,
                qty=quantity,
                price=price,
                errors=errors,
            )
            self._consecutive_rejects += 1
            return None

        internal_id = self._next_id()
        record = OrderRecord(
            symbol=symbol,
            side=side,
            order_type=order_type,
            requested_qty=quantity,
            requested_price=price,
            trade_mode=self._config.trade_mode,
            is_dry_run=self._config.dry_run,
        )

        # ─── Dry run ───
        if self._config.dry_run:
            record.status = OrderStatus.FILLED
            record.filled_qty = quantity
            record.avg_fill_price = price if price else 0.0
            record.filled_at = time.time()
            self._orders[internal_id] = record
            self._consecutive_rejects = 0
            logger.info(
                "order_dry_run",
                internal_id=internal_id,
                symbol=symbol,
                side=side,
                qty=quantity,
                price=price,
                trade_mode=record.trade_mode,
            )
            return record

        # ─── Live order ───
        try:
            params = {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "quantity": quantity,
            }
            if order_type == "LIMIT" and price is not None:
                params["price"] = price
                params["time_in_force"] = "GTC"

            result = await self._client.create_order(**params)

            record.order_id = result.get("orderId")
            status_str = result.get("status", "")
            if status_str == "FILLED":
                record.status = OrderStatus.FILLED
                record.filled_qty = float(result.get("executedQty", 0))
                # Calculate average fill price from fills
                fills = result.get("fills", [])
                if fills:
                    total_qty = sum(float(f["qty"]) for f in fills)
                    total_cost = sum(float(f["qty"]) * float(f["price"]) for f in fills)
                    record.avg_fill_price = total_cost / total_qty if total_qty > 0 else 0
                else:
                    record.avg_fill_price = float(result.get("price", 0))
                record.filled_at = time.time()
            elif status_str == "PARTIALLY_FILLED":
                record.status = OrderStatus.PARTIALLY_FILLED
                record.filled_qty = float(result.get("executedQty", 0))
            else:
                record.status = OrderStatus.PENDING

            self._orders[internal_id] = record
            self._consecutive_rejects = 0

            logger.info(
                "order_placed",
                internal_id=internal_id,
                order_id=record.order_id,
                symbol=symbol,
                side=side,
                qty=quantity,
                price=price,
                status=record.status.value,
                trade_mode=record.trade_mode,
            )
            return record

        except Exception as e:
            self._consecutive_rejects += 1
            record.status = OrderStatus.REJECTED
            record.cancel_reason = str(e)
            self._orders[internal_id] = record
            logger.error(
                "order_placement_failed",
                internal_id=internal_id,
                symbol=symbol,
                error=str(e),
                consecutive_rejects=self._consecutive_rejects,
            )
            return None

    async def cancel_order(self, symbol: str, order_id: int) -> bool:
        """Cancel an open order on the exchange."""
        if self._config.dry_run:
            logger.info("cancel_dry_run", symbol=symbol, order_id=order_id)
            return True

        try:
            await self._client.cancel_order(symbol=symbol, order_id=order_id)
            logger.info("order_cancelled", symbol=symbol, order_id=order_id)
            return True
        except Exception as e:
            logger.error("cancel_failed", symbol=symbol, order_id=order_id, error=str(e))
            return False

    async def check_order_status(self, symbol: str, order_id: int) -> Optional[Dict]:
        """Check current order status on the exchange."""
        if self._config.dry_run:
            return {"status": "FILLED"}

        try:
            return await self._client.get_order(symbol=symbol, order_id=order_id)
        except Exception as e:
            logger.error("status_check_failed", symbol=symbol, order_id=order_id, error=str(e))
            return None

    def get_order_history(self) -> List[OrderRecord]:
        """Return all tracked orders."""
        return list(self._orders.values())
