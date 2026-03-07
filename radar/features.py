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
    price_range_60s_pct: float = 0.0    # true (high-low)/mid range
    computed_at: float = 0.0


@dataclass
class SymbolRadarState:
    """Rolling state for a single symbol in the radar layer."""

    symbol: str
    # Price history: (timestamp, price)
    prices: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Volume delta history: (timestamp, delta_volume)
    volume_deltas: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Update count history: (timestamp,)
    updates: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Spread history: (timestamp, spread_pct)
    spreads: Deque = field(default_factory=lambda: deque(maxlen=120))
    # Last known values
    last_price: float = 0.0
    last_volume: float = 0.0
    prev_cumulative_volume: float = 0.0  # FIX #1: previous cumulative volume
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
        """
        Record a new data point.

        FIX #1: quote_volume from mini-ticker is 24h rolling cumulative.
        We compute the delta between consecutive updates and store that.
        Handles backwards jumps (day rollover / reset) gracefully.
        """
        now = time.time()
        if self.warmup_start == 0:
            self.warmup_start = now

        self.prices.append((now, price))
        self.updates.append(now)
        if spread_pct > 0:
            self.spreads.append((now, spread_pct))

        # FIX #1: Convert cumulative volume to delta
        if self.prev_cumulative_volume > 0 and quote_volume >= self.prev_cumulative_volume:
            delta = quote_volume - self.prev_cumulative_volume
            self.volume_deltas.append((now, delta))
        elif self.prev_cumulative_volume > 0 and quote_volume < self.prev_cumulative_volume:
            # Backwards jump: day rollover or reset — treat as small positive
            self.volume_deltas.append((now, quote_volume * 0.001))
        # else: first update, no delta yet

        self.prev_cumulative_volume = quote_volume
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

        # ─── 1. Short-horizon returns (FIX #2) ───
        features.return_10s = self._compute_return(state, now, 10)
        features.return_30s = self._compute_return(state, now, 30)
        features.return_60s = self._compute_return(state, now, 60)

        # Return acceleration = change in velocity (30s return - 60s return normalized)
        features.return_acceleration = features.return_30s - (features.return_60s * 0.5)

        # ─── 2. Volume burst ratio (FIX #1) ───
        features.volume_burst_ratio = self._compute_volume_burst(state, now)

        # ─── 3. Spread compression ───
        features.spread_compression = self._compute_spread_compression(state, now)

        # ─── 4. Quote activity ratio ───
        features.quote_activity_ratio = self._compute_activity_ratio(state, now)

        # ─── 5. True price range (FIX #3) ───
        features.price_range_60s_pct = self._compute_true_range(state, now, 60)

        # ─── 6. Early buildup score (uses true range) ───
        features.early_buildup_score = self._compute_buildup_score(state, features)

        return features

    def _compute_return(
        self, state: SymbolRadarState, now: float, lookback_seconds: int
    ) -> float:
        """
        Compute return over the given lookback period.

        FIX #2: Uses the LAST sample at or before the cutoff (not the first after).
        This gives consistent lookback horizons.
        """
        if len(state.prices) < 2:
            return 0.0

        cutoff = now - lookback_seconds
        past_price = None

        # Iterate and find the last sample with ts <= cutoff
        for ts, p in state.prices:
            if ts <= cutoff:
                past_price = p
            else:
                # Past the cutoff — if we haven't found one, use the first available
                if past_price is None:
                    past_price = p
                break

        # Fallback: if all samples are after cutoff, use the oldest
        if past_price is None:
            past_price = state.prices[0][1]

        if past_price <= 0:
            return 0.0

        current = state.last_price
        return ((current - past_price) / past_price) * 100.0

    def _compute_volume_burst(self, state: SymbolRadarState, now: float) -> float:
        """
        Compare recent 10s volume rate against 60s baseline.

        FIX #1: Uses per-update delta volumes instead of cumulative volume differences.
        """
        if len(state.volume_deltas) < 2:
            return 0.0

        recent_cutoff = now - 10
        baseline_cutoff = now - 60

        recent_vol = sum(v for ts, v in state.volume_deltas if ts >= recent_cutoff)
        baseline_vol = sum(v for ts, v in state.volume_deltas if ts >= baseline_cutoff)

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

    def _compute_true_range(
        self, state: SymbolRadarState, now: float, lookback_seconds: int
    ) -> float:
        """
        FIX #3: Compute true (high - low) / mid range over window.

        Unlike abs(return), this captures actual volatility including round-trips.
        """
        cutoff = now - lookback_seconds
        prices_in_window = [p for ts, p in state.prices if ts >= cutoff]

        if len(prices_in_window) < 2:
            return 0.0

        high = max(prices_in_window)
        low = min(prices_in_window)
        mid = (high + low) / 2.0

        if mid <= 0:
            return 0.0

        return ((high - low) / mid) * 100.0

    def _compute_buildup_score(
        self, state: SymbolRadarState, features: RadarFeatureValues
    ) -> float:
        """
        Detect early buildup: flat price + rising volume + tightening spread.

        FIX #3: Uses true price range instead of net return for flatness detection.

        Returns 0..1 score.
        """
        score = 0.0

        # FIX #3: Price should be in a tight TRUE range (not just flat net return)
        if features.price_range_60s_pct <= self._config.buildup_price_range_max_pct:
            score += 0.3  # Price is genuinely flat — good

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
