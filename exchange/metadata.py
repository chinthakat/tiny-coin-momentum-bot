"""
Exchange metadata cache — caches and refreshes symbol filters and trading rules.

Provides the critical get_min_trade_quantity() and validate_order() functions
that every order must pass through before placement.
"""

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class SymbolInfo:
    """Cached trading rules for a single symbol."""

    symbol: str
    base_asset: str
    quote_asset: str
    status: str                      # "TRADING", "BREAK", etc.
    # LOT_SIZE filter
    min_qty: float = 0.0
    max_qty: float = 0.0
    step_size: float = 0.0
    # MARKET_LOT_SIZE filter (if different from LOT_SIZE)
    market_min_qty: float = 0.0
    market_max_qty: float = 0.0
    market_step_size: float = 0.0
    # PRICE_FILTER
    min_price: float = 0.0
    max_price: float = 0.0
    tick_size: float = 0.0
    # NOTIONAL / MIN_NOTIONAL
    min_notional: float = 0.0
    # Precision
    qty_precision: int = 8
    price_precision: int = 8
    # Venue flags
    is_spot_trading: bool = False
    is_margin_trading: bool = False
    allowed_order_types: List[str] = field(default_factory=list)
    permissions: List[str] = field(default_factory=list)


def _extract_filter(filters: List[Dict], filter_type: str) -> Optional[Dict]:
    """Extract a specific filter from the symbol's filter list."""
    for f in filters:
        if f.get("filterType") == filter_type:
            return f
    return None


def _parse_symbol_info(raw: Dict) -> SymbolInfo:
    """Parse a single symbol entry from exchangeInfo response."""
    filters = raw.get("filters", [])

    # LOT_SIZE
    lot_size = _extract_filter(filters, "LOT_SIZE") or {}
    # MARKET_LOT_SIZE
    market_lot = _extract_filter(filters, "MARKET_LOT_SIZE") or {}
    # PRICE_FILTER
    price_filter = _extract_filter(filters, "PRICE_FILTER") or {}
    # MIN_NOTIONAL or NOTIONAL
    notional_filter = _extract_filter(filters, "NOTIONAL") or _extract_filter(
        filters, "MIN_NOTIONAL"
    ) or {}

    permissions = raw.get("permissions", [])
    if not permissions:
        # Newer API uses permissionSets
        perm_sets = raw.get("permissionSets", [])
        for ps in perm_sets:
            permissions.extend(ps)

    return SymbolInfo(
        symbol=raw["symbol"],
        base_asset=raw.get("baseAsset", ""),
        quote_asset=raw.get("quoteAsset", ""),
        status=raw.get("status", ""),
        min_qty=float(lot_size.get("minQty", 0)),
        max_qty=float(lot_size.get("maxQty", 0)),
        step_size=float(lot_size.get("stepSize", 0)),
        market_min_qty=float(market_lot.get("minQty", 0)),
        market_max_qty=float(market_lot.get("maxQty", 0)),
        market_step_size=float(market_lot.get("stepSize", 0)),
        min_price=float(price_filter.get("minPrice", 0)),
        max_price=float(price_filter.get("maxPrice", 0)),
        tick_size=float(price_filter.get("tickSize", 0)),
        min_notional=float(notional_filter.get("minNotional", 0)),
        qty_precision=raw.get("baseAssetPrecision", 8),
        price_precision=raw.get("quotePrecision", 8),
        is_spot_trading="SPOT" in permissions,
        is_margin_trading="MARGIN" in permissions,
        allowed_order_types=raw.get("orderTypes", []),
        permissions=permissions,
    )


class MetadataCache:
    """
    Caches exchange metadata (symbol filters) and provides validation.

    Key methods:
    - get_min_trade_quantity(symbol, current_price) → effective minimum qty
    - validate_order(symbol, side, qty, price) → raises on filter violation
    """

    def __init__(self):
        self._symbols: Dict[str, SymbolInfo] = {}
        self._last_refresh: float = 0.0
        self._refresh_interval: float = 600.0  # 10 minutes

    @property
    def symbols(self) -> Dict[str, SymbolInfo]:
        return self._symbols

    async def refresh(self, exchange_client) -> None:
        """Fetch and cache exchange info."""
        import time

        logger.info("metadata_refreshing")
        info = await exchange_client.get_exchange_info()
        symbols_raw = info.get("symbols", [])

        new_cache: Dict[str, SymbolInfo] = {}
        for s in symbols_raw:
            parsed = _parse_symbol_info(s)
            new_cache[parsed.symbol] = parsed

        self._symbols = new_cache
        self._last_refresh = time.time()
        logger.info("metadata_refreshed", symbol_count=len(self._symbols))

    async def start_periodic_refresh(self, exchange_client) -> None:
        """Background task to periodically refresh metadata."""
        while True:
            await self.refresh(exchange_client)
            await asyncio.sleep(self._refresh_interval)

    def get(self, symbol: str) -> Optional[SymbolInfo]:
        """Get cached symbol info."""
        return self._symbols.get(symbol)

    def get_all_trading(self) -> List[SymbolInfo]:
        """Get all symbols with status TRADING."""
        return [s for s in self._symbols.values() if s.status == "TRADING"]

    # ─── Quantity helpers ───

    def round_step_size(self, quantity: float, step_size: float) -> float:
        """Round quantity down to the nearest valid step size."""
        if step_size <= 0:
            return quantity
        precision = max(0, int(round(-math.log10(step_size))))
        rounded = math.floor(quantity / step_size) * step_size
        return round(rounded, precision)

    def round_tick_size(self, price: float, tick_size: float) -> float:
        """Round price to the nearest valid tick size."""
        if tick_size <= 0:
            return price
        precision = max(0, int(round(-math.log10(tick_size))))
        rounded = round(round(price / tick_size) * tick_size, precision)
        return rounded

    def get_min_trade_quantity(
        self, symbol: str, current_price: float
    ) -> Optional[float]:
        """
        Calculate the effective minimum tradeable quantity for a symbol.

        This considers BOTH:
        1. LOT_SIZE minQty — the absolute minimum lot size
        2. MIN_NOTIONAL / current_price — the quantity needed to meet min notional

        Returns the higher of the two, rounded up to stepSize.
        Returns None if the symbol is not found.
        """
        info = self.get(symbol)
        if info is None:
            logger.warning("min_qty_symbol_not_found", symbol=symbol)
            return None

        if current_price <= 0:
            logger.warning("min_qty_invalid_price", symbol=symbol, price=current_price)
            return None

        step = info.step_size if info.step_size > 0 else info.market_step_size
        if step <= 0:
            step = 10 ** (-info.qty_precision)

        # 1) LOT_SIZE minimum
        lot_min = info.min_qty

        # 2) Notional minimum → quantity
        notional_min_qty = 0.0
        if info.min_notional > 0:
            notional_min_qty = info.min_notional / current_price

        # Take the larger of the two
        effective_min = max(lot_min, notional_min_qty)

        # Round up to step size
        if step > 0:
            steps_needed = math.ceil(effective_min / step)
            effective_min = steps_needed * step

        # Final precision rounding
        precision = max(0, int(round(-math.log10(step)))) if step > 0 else info.qty_precision
        effective_min = round(effective_min, precision)

        return effective_min

    # ─── Order validation ───

    def validate_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        price: Optional[float] = None,
    ) -> List[str]:
        """
        Validate an order against all known exchange filters.

        Returns a list of error strings. Empty list = valid order.
        """
        errors: List[str] = []
        info = self.get(symbol)

        if info is None:
            return [f"Unknown symbol: {symbol}"]

        if info.status != "TRADING":
            errors.append(f"Symbol {symbol} status is {info.status}, not TRADING")

        if side not in ("BUY", "SELL"):
            errors.append(f"Invalid side: {side}")

        # LOT_SIZE checks
        if info.min_qty > 0 and quantity < info.min_qty:
            errors.append(
                f"Quantity {quantity} below minQty {info.min_qty}"
            )
        if info.max_qty > 0 and quantity > info.max_qty:
            errors.append(
                f"Quantity {quantity} above maxQty {info.max_qty}"
            )
        if info.step_size > 0:
            remainder = (quantity - info.min_qty) % info.step_size
            if remainder > info.step_size * 0.1:  # tolerance for float issues
                errors.append(
                    f"Quantity {quantity} does not align with stepSize {info.step_size}"
                )

        # PRICE_FILTER checks (only for limit orders with a price)
        if price is not None:
            if info.min_price > 0 and price < info.min_price:
                errors.append(f"Price {price} below minPrice {info.min_price}")
            if info.max_price > 0 and price > info.max_price:
                errors.append(f"Price {price} above maxPrice {info.max_price}")
            if info.tick_size > 0:
                remainder = (price - info.min_price) % info.tick_size
                if remainder > info.tick_size * 0.1:
                    errors.append(
                        f"Price {price} does not align with tickSize {info.tick_size}"
                    )

        # MIN_NOTIONAL check
        if price is not None and info.min_notional > 0:
            notional = price * quantity
            if notional < info.min_notional:
                errors.append(
                    f"Notional {notional:.4f} below minNotional {info.min_notional}"
                )

        return errors
