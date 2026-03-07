"""
Session history preloader — seeds radar baselines from historical klines.

At startup, fetches 6 hours of 1-minute klines for radar-eligible symbols
to give the z-score normalizer and volume burst detector proper baselines.
"""

import asyncio
import time
from typing import Set

import structlog

from exchange.client import ExchangeClient
from radar.features import RadarFeatureEngine

logger = structlog.get_logger(__name__)

# Binance rate limit: ~1200 req/min, but be conservative
BATCH_SIZE = 10
BATCH_DELAY_S = 0.5
KLINES_LIMIT = 360  # 6 hours of 1m candles


async def preload_radar_history(
    exchange: ExchangeClient,
    radar_features: RadarFeatureEngine,
    symbols: Set[str],
    limit: int = KLINES_LIMIT,
) -> int:
    """
    Fetch historical klines and seed radar state for all symbols.

    Returns count of symbols successfully preloaded.
    """
    total = len(symbols)
    loaded = 0
    failed = 0
    symbol_list = list(symbols)

    logger.info(
        "history_preload_starting",
        symbol_count=total,
        klines_per_symbol=limit,
    )

    for i in range(0, total, BATCH_SIZE):
        batch = symbol_list[i : i + BATCH_SIZE]
        tasks = [_preload_symbol(exchange, radar_features, s, limit) for s in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, result in zip(batch, results):
            if isinstance(result, Exception):
                failed += 1
            elif result:
                loaded += 1
            else:
                failed += 1

        # Rate limit
        if i + BATCH_SIZE < total:
            await asyncio.sleep(BATCH_DELAY_S)

        # Progress log every 50 symbols
        done = min(i + BATCH_SIZE, total)
        if done % 50 == 0 or done == total:
            logger.info(
                "history_preload_progress",
                done=done,
                total=total,
                loaded=loaded,
                failed=failed,
            )

    logger.info(
        "history_preload_complete",
        loaded=loaded,
        failed=failed,
        total=total,
    )
    return loaded


async def _preload_symbol(
    exchange: ExchangeClient,
    radar_features: RadarFeatureEngine,
    symbol: str,
    limit: int,
) -> bool:
    """Fetch klines for one symbol and seed its radar state."""
    try:
        klines = await exchange.get_klines(symbol, interval="1m", limit=limit)
        if not klines:
            return False

        state = radar_features.get_or_create_state(symbol)

        prev_vol = 0.0
        for k in klines:
            # Kline format: [open_time, open, high, low, close, volume,
            #                 close_time, quote_volume, trades, ...]
            ts = k[0] / 1000.0  # convert ms to seconds
            close = float(k[4])
            quote_vol = float(k[7])

            if close <= 0:
                continue

            # Seed price history
            state.prices.append((ts, close))

            # Seed volume delta history
            if prev_vol > 0 and quote_vol >= prev_vol:
                delta = quote_vol - prev_vol
                state.volume_deltas.append((ts, delta))
            prev_vol = quote_vol

            # Seed update frequency
            state.updates.append(ts)

        # Set state timestamps
        if state.prices:
            state.last_price = state.prices[-1][1]
            state.last_update_time = state.prices[-1][0]
            state.warmup_start = state.prices[0][0]
            state.prev_cumulative_volume = prev_vol

        return True

    except Exception as e:
        logger.debug("history_preload_symbol_error", symbol=symbol, error=str(e))
        return False
