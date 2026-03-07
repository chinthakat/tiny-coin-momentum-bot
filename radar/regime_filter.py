"""
Market regime filter — tracks BTC and market-wide conditions.

Disables trading when the market is dead or excessively volatile.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Optional

import numpy as np
import structlog

logger = structlog.get_logger(__name__)


class MarketRegime(Enum):
    ACTIVE = "active"
    CHOPPY = "choppy"
    DEAD = "dead"


@dataclass
class RegimeState:
    """Rolling BTC and market-wide stats."""

    # BTC price history: (timestamp, price)
    btc_prices: Deque = field(default_factory=lambda: deque(maxlen=1800))
    # BTC volume deltas: (timestamp, delta)
    btc_volume_deltas: Deque = field(default_factory=lambda: deque(maxlen=1800))
    btc_prev_volume: float = 0.0

    # Market-wide momentum: (timestamp, pct_positive)
    market_momentum: Deque = field(default_factory=lambda: deque(maxlen=60))

    last_regime: MarketRegime = MarketRegime.ACTIVE
    last_computed: float = 0.0


class RegimeFilter:
    """
    Tracks BTC volatility, volume, and market-wide momentum
    to determine if the overall market environment supports trading.
    """

    def __init__(
        self,
        vol_lookback_s: int = 1800,     # 30m for volatility
        dead_vol_threshold: float = 0.02,   # BTC 30m std < 0.02% → dead
        dead_momentum_threshold: float = 0.3,  # < 30% of symbols positive → dead
    ):
        self._state = RegimeState()
        self._vol_lookback = vol_lookback_s
        self._dead_vol = dead_vol_threshold
        self._dead_momentum = dead_momentum_threshold

    @property
    def regime(self) -> MarketRegime:
        return self._state.last_regime

    @property
    def is_trading_allowed(self) -> bool:
        return self._state.last_regime != MarketRegime.DEAD

    def update_btc(self, price: float, quote_volume: float) -> None:
        """Feed BTC price and volume from mini-ticker."""
        now = time.time()
        self._state.btc_prices.append((now, price))

        # Volume delta
        if self._state.btc_prev_volume > 0 and quote_volume >= self._state.btc_prev_volume:
            delta = quote_volume - self._state.btc_prev_volume
            self._state.btc_volume_deltas.append((now, delta))
        self._state.btc_prev_volume = quote_volume

    def update_market_momentum(self, total_symbols: int, positive_count: int) -> None:
        """Record what fraction of symbols have positive short-term return."""
        now = time.time()
        ratio = positive_count / max(total_symbols, 1)
        self._state.market_momentum.append((now, ratio))

    def compute_regime(self) -> MarketRegime:
        """Compute the current market regime."""
        now = time.time()

        # BTC volatility (std of 1m returns over last 30m)
        btc_vol = self._compute_btc_volatility(now)

        # BTC volume trend
        btc_vol_active = self._compute_btc_volume_activity(now)

        # Market momentum
        momentum = self._compute_momentum(now)

        # Classify
        if btc_vol < self._dead_vol and momentum < self._dead_momentum:
            regime = MarketRegime.DEAD
        elif btc_vol > self._dead_vol * 3 or momentum > 0.6:
            regime = MarketRegime.ACTIVE
        else:
            regime = MarketRegime.CHOPPY

        if regime != self._state.last_regime:
            logger.info(
                "regime_change",
                old=self._state.last_regime.value,
                new=regime.value,
                btc_vol=round(btc_vol, 4),
                momentum=round(momentum, 3),
            )

        self._state.last_regime = regime
        self._state.last_computed = now
        return regime

    def _compute_btc_volatility(self, now: float) -> float:
        """Standard deviation of 1-minute BTC returns over 30m."""
        cutoff = now - self._vol_lookback
        prices = [(ts, p) for ts, p in self._state.btc_prices if ts >= cutoff]

        if len(prices) < 10:
            return 0.1  # assume active until we have data

        # Compute minute-interval returns
        returns = []
        for i in range(1, len(prices)):
            if prices[i - 1][1] > 0:
                ret = (prices[i][1] - prices[i - 1][1]) / prices[i - 1][1] * 100
                returns.append(ret)

        if not returns:
            return 0.1

        return float(np.std(returns))

    def _compute_btc_volume_activity(self, now: float) -> float:
        """Recent vs baseline BTC volume rate."""
        cutoff_recent = now - 60
        cutoff_base = now - 600

        recent = sum(v for ts, v in self._state.btc_volume_deltas if ts >= cutoff_recent)
        baseline = sum(v for ts, v in self._state.btc_volume_deltas if ts >= cutoff_base)

        rate_recent = recent / 60.0
        rate_base = baseline / 600.0

        if rate_base <= 0:
            return 1.0 if rate_recent > 0 else 0.0
        return rate_recent / rate_base

    def _compute_momentum(self, now: float) -> float:
        """Average fraction of symbols with positive return."""
        cutoff = now - 30
        vals = [r for ts, r in self._state.market_momentum if ts >= cutoff]
        if not vals:
            return 0.5  # assume neutral
        return float(np.mean(vals))

    def get_summary(self) -> dict:
        """Return current regime summary for dashboard/CSV."""
        return {
            "regime": self._state.last_regime.value,
            "trading_allowed": self.is_trading_allowed,
        }
