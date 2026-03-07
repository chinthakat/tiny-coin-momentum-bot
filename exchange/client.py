"""
Async Binance REST client wrapper.

Wraps python-binance's AsyncClient with retry logic, testnet support,
and typed convenience methods.
"""

import asyncio
from typing import Any, Dict, List, Optional

import structlog
from binance import AsyncClient, BinanceSocketManager

from core.config import AppConfig

logger = structlog.get_logger(__name__)

# Binance testnet endpoints
TESTNET_API_URL = "https://testnet.binance.vision"
TESTNET_WS_URL = "wss://testnet.binance.vision/ws"


class ExchangeClient:
    """Async Binance REST client with retry and testnet support."""

    def __init__(self, config: AppConfig):
        self._config = config
        self._client: Optional[AsyncClient] = None
        self._socket_manager: Optional[BinanceSocketManager] = None

    async def connect(self) -> None:
        """Initialize the async client connection."""
        kwargs: Dict[str, Any] = {
            "api_key": self._config.api_key,
            "api_secret": self._config.api_secret,
        }
        if self._config.testnet:
            kwargs["testnet"] = True
            logger.info("connecting_testnet")
        else:
            logger.info("connecting_live")

        self._client = await AsyncClient.create(**kwargs)
        self._socket_manager = BinanceSocketManager(self._client)
        logger.info("exchange_client_connected")

    async def close(self) -> None:
        """Close the client connection."""
        if self._client:
            await self._client.close_connection()
            logger.info("exchange_client_closed")

    @property
    def client(self) -> AsyncClient:
        if self._client is None:
            raise RuntimeError("ExchangeClient not connected. Call connect() first.")
        return self._client

    @property
    def socket_manager(self) -> BinanceSocketManager:
        if self._socket_manager is None:
            raise RuntimeError("ExchangeClient not connected. Call connect() first.")
        return self._socket_manager

    # ─── Retry wrapper ───

    async def _retry(self, coro_func, *args, max_retries: int = 3, **kwargs) -> Any:
        """Execute an async call with exponential backoff retries."""
        for attempt in range(max_retries):
            try:
                return await coro_func(*args, **kwargs)
            except Exception as e:
                wait = 2 ** attempt
                logger.warning(
                    "api_call_retry",
                    func=coro_func.__name__,
                    attempt=attempt + 1,
                    wait_seconds=wait,
                    error=str(e),
                )
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(wait)

    # ─── Convenience methods ───

    async def get_server_time(self) -> Dict:
        return await self._retry(self.client.get_server_time)

    async def get_exchange_info(self) -> Dict:
        return await self._retry(self.client.get_exchange_info)

    async def get_all_tickers(self) -> List[Dict]:
        return await self._retry(self.client.get_all_tickers)

    async def get_ticker_24h(self) -> List[Dict]:
        """Get 24h ticker stats for all symbols."""
        return await self._retry(self.client.get_ticker)

    async def get_symbol_ticker(self, symbol: str) -> Dict:
        return await self._retry(self.client.get_symbol_ticker, symbol=symbol)

    async def get_account(self) -> Dict:
        return await self._retry(self.client.get_account)

    async def get_order_book(self, symbol: str, limit: int = 20) -> Dict:
        return await self._retry(self.client.get_order_book, symbol=symbol, limit=limit)

    async def create_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        quantity: float,
        price: Optional[float] = None,
        time_in_force: Optional[str] = None,
        **kwargs,
    ) -> Dict:
        """Place a new order on Binance."""
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "quantity": quantity,
        }
        if price is not None:
            params["price"] = price
        if time_in_force is not None:
            params["timeInForce"] = time_in_force
        params.update(kwargs)

        logger.info("placing_order", **params)
        return await self._retry(self.client.create_order, **params)

    async def cancel_order(self, symbol: str, order_id: int) -> Dict:
        return await self._retry(
            self.client.cancel_order, symbol=symbol, orderId=order_id
        )

    async def get_order(self, symbol: str, order_id: int) -> Dict:
        return await self._retry(
            self.client.get_order, symbol=symbol, orderId=order_id
        )

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        kwargs = {}
        if symbol:
            kwargs["symbol"] = symbol
        return await self._retry(self.client.get_open_orders, **kwargs)

    async def get_klines(
        self, symbol: str, interval: str = "1m", limit: int = 360
    ) -> List[List]:
        """Fetch historical kline/candlestick data."""
        return await self._retry(
            self.client.get_klines, symbol=symbol, interval=interval, limit=limit
        )
