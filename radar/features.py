"""
Radar-layer features — cheap, broad metrics computed for the entire universe.

These features are designed to be computed per-symbol from mini-ticker data
on a 1-second cadence without requiring order book depth.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

import numpy as np
import structlog

from core.config import RadarConfig

logger = structlog.get_logger(__name__)

# Rolling window size for baseline calculations
BASELINE_WINDOW = 60  # 60 seconds of history


@dataclass
class RadarFeatureValues:
    """Computed radar feature values for one symbol."""

    symbol: str
    return_10s: float = 0.0
    return_30s: float = 0.0
    return_60s: float = 0.0
    return_acceleration: float = 0.0    # change in velocity
    volume_burst_ratio: float = 0.0     # recent vs baseline
    spread_compression: float = 0.0     # negative = compressed
    quote_activity_ratio: float = 0.0   # update freq vs baseline
    early_buildup_score: float = 0.0    # flat price + rising vol
    computed_at: float = 0.0


@dataclass
class SymbolRadarState:
    """Rolling state for a single symbol in the radar layer."""

    symbol: str
    # Price history: (timestamp, price)
    prices: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Volume history: (timestamp, quote_volume)
    volumes: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Update count history: (timestamp,)
    updates: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Spread history: (timestamp, spread_pct)
    spreads: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Last known values
    last_price: float = 0.0
    last_volume: float = 0.0
    last_update_time: float = 0.0
    warmup_start: float = 0.0

    @property
    def is_warmed_up(self) -> bool:
        """Has at least 30 seconds of data."""
        if not self.prices:
            return False
        return (time.time() - self.warmup_start) >= 30

    def record_update(
        self, price: float, quote_volume: float, spread_pct: float = 0.0
    ) -> None:
        """Record a new data point."""
        now = time.time()
        if self.warmup_start == 0:
            self.warmup_start = now

        self.prices.append((now, price))
        self.volumes.append((now, quote_volume))
        self.updates.append(now)
        if spread_pct > 0:
            self.spreads.append((now, spread_pct))

        self.last_price = price
        self.last_volume = quote_volume
        self.last_update_time = now


class RadarFeatureEngine:
    """
    Computes radar-layer features for all monitored symbols.

    Features are cheap enough to compute for hundreds of symbols per second.
    """

    def __init__(self, config: RadarConfig):
        self._config = config
        self._states: Dict[str, SymbolRadarState] = {}

    def get_or_create_state(self, symbol: str) -> SymbolRadarState:
        if symbol not in self._states:
            self._states[symbol] = SymbolRadarState(symbol=symbol)
        return self._states[symbol]

    def update(
        self, symbol: str, price: float, quote_volume: float, spread_pct: float = 0.0
    ) -> None:
        """Feed new data into the radar state."""
        state = self.get_or_create_state(symbol)
        state.record_update(price, quote_volume, spread_pct)

    def compute_features(self, symbol: str) -> Optional[RadarFeatureValues]:
        """Compute all radar features for a symbol."""
        state = self._states.get(symbol)
        if state is None or not state.is_warmed_up:
            return None

        now = time.time()
        features = RadarFeatureValues(symbol=symbol, computed_at=now)

        # ─── 1. Short-horizon returns ───
        features.return_10s = self._compute_return(state, now, 10)
        features.return_30s = self._compute_return(state, now, 30)
        features.return_60s = self._compute_return(state, now, 60)

        # Return acceleration = change in velocity (30s return - 60s return normalized)
        features.return_acceleration = features.return_30s - (features.return_60s * 0.5)

        # ─── 2. Volume burst ratio ───
        features.volume_burst_ratio = self._compute_volume_burst(state, now)

        # ─── 3. Spread compression ───
        features.spread_compression = self._compute_spread_compression(state, now)

        # ─── 4. Quote activity ratio ───
        features.quote_activity_ratio = self._compute_activity_ratio(state, now)

        # ─── 5. Early buildup score ───
        features.early_buildup_score = self._compute_buildup_score(state, features)

        return features

    def _compute_return(
        self, state: SymbolRadarState, now: float, lookback_seconds: int
    ) -> float:
        """Compute return over the given lookback period."""
        if len(state.prices) < 2:
            return 0.0

        cutoff = now - lookback_seconds
        past_price = None
        for ts, p in state.prices:
            if ts >= cutoff:
                past_price = p
                break

        if past_price is None or past_price <= 0:
            return 0.0

        current = state.last_price
        return ((current - past_price) / past_price) * 100.0

    def _compute_volume_burst(self, state: SymbolRadarState, now: float) -> float:
        """Compare recent 10s volume against 60s baseline."""
        if len(state.volumes) < 2:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_volumes = [v for ts, v in state.volumes if ts >= recent_cutoff]
        baseline_volumes = [v for ts, v in state.volumes if ts >= baseline_cutoff]

        if not baseline_volumes:
            return 0.0

        # Use the delta in cumulative volume as a proxy
        recent_vol = max(recent_volumes) - min(recent_volumes) if len(recent_volumes) > 1 else 0
        baseline_vol = max(baseline_volumes) - min(baseline_volumes) if len(baseline_volumes) > 1 else 0

        if baseline_vol <= 0:
            return 1.0 if recent_vol > 0 else 0.0

        # Normalize: ratio of recent rate vs baseline rate
        recent_rate = recent_vol / 10.0
        baseline_rate = baseline_vol / 60.0

        if baseline_rate <= 0:
            return 1.0 if recent_rate > 0 else 0.0

        return min(recent_rate / baseline_rate, 10.0)  # cap at 10x

    def _compute_spread_compression(
        self, state: SymbolRadarState, now: float
    ) -> float:
        """
        Negative = spread is tightening (good for buildup).
        Positive = spread is widening (unstable).
        """
        if len(state.spreads) < 5:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_spreads = [s for ts, s in state.spreads if ts >= recent_cutoff]
        baseline_spreads = [s for ts, s in state.spreads if ts >= baseline_cutoff]

        if not recent_spreads or not baseline_spreads:
            return 0.0

        recent_avg = np.mean(recent_spreads)
        baseline_avg = np.mean(baseline_spreads)

        if baseline_avg <= 0:
            return 0.0

        # Positive = widening, negative = compressing
        return ((recent_avg - baseline_avg) / baseline_avg) * 100.0

    def _compute_activity_ratio(self, state: SymbolRadarState, now: float) -> float:
        """How frequently the symbol is updating vs baseline."""
        if len(state.updates) < 5:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_count = sum(1 for ts in state.updates if ts >= recent_cutoff)
        baseline_count = sum(1 for ts in state.updates if ts >= baseline_cutoff)

        recent_rate = recent_count / 10.0
        baseline_rate = baseline_count / 60.0

        if baseline_rate <= 0:
            return 1.0 if recent_rate > 0 else 0.0

        return min(recent_rate / baseline_rate, 10.0)  # cap at 10x

    def _compute_buildup_score(
        self, state: SymbolRadarState, features: RadarFeatureValues
    ) -> float:
        """
        Detect early buildup: flat price + rising volume + tightening spread.

        Returns 0..1 score.
        """
        score = 0.0

        # Price should be in a tight range (small 60s return)
        price_range_pct = abs(features.return_60s)
        if price_range_pct <= self._config.buildup_price_range_max_pct:
            score += 0.3  # Price is flat — good

        # Volume should be increasing
        if features.volume_burst_ratio >= self._config.buildup_volume_increase_min:
            score += 0.4  # Volume rising against flat price

        # Spread should be compressing (negative spread_compression)
        if features.spread_compression < -5:
            score += 0.15  # Spread tightening

        # Activity should be above normal
        if features.quote_activity_ratio > 1.5:
            score += 0.15  # More updates than normal

        return min(score, 1.0)

    def cleanup_stale(self, active_symbols: set) -> None:
        """Remove state for symbols no longer in the universe."""
        stale = [s for s in self._states if s not in active_symbols]
        for s in stale:
            del self._states[s]
        if stale:
            logger.debug("radar_state_cleaned", removed=len(stale))
