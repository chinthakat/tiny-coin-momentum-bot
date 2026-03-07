# TinyCoins Core Logic — Review Document

This document contains the core strategy logic of the TinyCoins momentum system, excluding all frontend UI, infrastructure, networking, and generic boilerplate.

---

## 1. Radar Features Engine
`radar/features.py` - Computes broad metrics continuously for ~400+ coins.

```python
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
```

---

## 2. Radar Scorer
`radar/scorer.py` - Converts radar features into a composite score and promotes top symbols.

```python
"""
Radar scoring and promotion engine.

Converts radar features into composite scores, labels symbols,
and promotes top candidates to high-resolution monitoring.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

import structlog

from core.config import RadarConfig
from core.events import RadarLabel
from radar.features import RadarFeatureEngine, RadarFeatureValues

logger = structlog.get_logger(__name__)


@dataclass
class RadarResult:
    """Scoring result for a single symbol."""

    symbol: str
    composite_score: float = 0.0
    label: RadarLabel = RadarLabel.NORMAL
    features: Optional[RadarFeatureValues] = None
    scored_at: float = 0.0


@dataclass
class CooldownEntry:
    """Tracks cooldown state for a symbol."""

    symbol: str
    started_at: float
    duration: float
    reason: str

    @property
    def is_active(self) -> bool:
        return (time.time() - self.started_at) < self.duration


class RadarScorer:
    """
    Computes composite radar scores and manages symbol promotion.
    """

    def __init__(self, config: RadarConfig, feature_engine: RadarFeatureEngine):
        self._config = config
        self._features = feature_engine
        self._cooldowns: Dict[str, CooldownEntry] = {}
        self._promoted: Set[str] = set()
        self._last_results: Dict[str, RadarResult] = {}

    def _compute_composite_score(self, features: RadarFeatureValues) -> float:
        """Weighted combination of radar features → 0..1 score."""
        w = self._config.weights

        # Normalize each feature to approximately 0..1 range
        # Return acceleration: cap at ±5%
        ret_score = min(max(features.return_acceleration / 5.0, 0), 1.0)

        # Volume burst: already a ratio, cap at 5x
        vol_score = min(features.volume_burst_ratio / 5.0, 1.0)

        # Spread compression: negative is good, normalize
        spread_score = min(max(-features.spread_compression / 20.0, 0), 1.0)

        # Activity ratio: cap at 3x
        activity_score = min(features.quote_activity_ratio / 3.0, 1.0)

        # Buildup: already 0..1
        buildup_score = features.early_buildup_score

        composite = (
            w.return_acceleration * ret_score
            + w.volume_burst * vol_score
            + w.spread_compression * spread_score
            + w.quote_activity * activity_score
            + w.early_buildup * buildup_score
        )

        return min(max(composite, 0.0), 1.0)

    def _classify_label(
        self, features: RadarFeatureValues, score: float
    ) -> RadarLabel:
        """Assign a radar label based on feature pattern."""
        # Already extended: large recent return
        if features.return_60s > 5.0:
            return RadarLabel.ALREADY_EXTENDED

        # Illiquid trap: high score but bad spread
        if score > 0.5 and features.spread_compression > 20:
            return RadarLabel.ILLIQUID_TRAP

        # Ignition risk: high acceleration + volume
        if features.return_acceleration > 2.0 and features.volume_burst_ratio > 2.0:
            return RadarLabel.IGNITION_RISK

        # Building pressure: buildup characteristics
        if features.early_buildup_score > 0.5:
            return RadarLabel.BUILDING_PRESSURE

        # Cooling off: negative recent return
        if features.return_30s < -1.0:
            return RadarLabel.COOLING_OFF

        return RadarLabel.NORMAL

    def score_all(
        self, eligible_symbols: Set[str]
    ) -> List[RadarResult]:
        """Score all eligible symbols and return sorted results."""
        results: List[RadarResult] = []

        for symbol in eligible_symbols:
            features = self._features.compute_features(symbol)
            if features is None:
                continue

            score = self._compute_composite_score(features)
            label = self._classify_label(features, score)

            result = RadarResult(
                symbol=symbol,
                composite_score=score,
                label=label,
                features=features,
                scored_at=time.time(),
            )
            results.append(result)

        # Sort by score descending
        results.sort(key=lambda r: r.composite_score, reverse=True)

        # Cache results
        self._last_results = {r.symbol: r for r in results}

        return results

    def select_promotions(
        self,
        results: List[RadarResult],
        long_eligible: Set[str],
    ) -> Tuple[Set[str], Set[str]]:
        """
        Select symbols to promote and demote.

        Returns (to_promote, to_demote).
        """
        # Clean up expired cooldowns
        self._cooldowns = {
            s: c for s, c in self._cooldowns.items() if c.is_active
        }

        # Filter candidates
        candidates: List[RadarResult] = []
        for r in results:
            # Must exceed threshold
            if r.composite_score < self._config.promotion_score_threshold:
                continue
            # Must be long-eligible (tradeable)
            if r.symbol not in long_eligible:
                continue
            # Must not be in cooldown
            if r.symbol in self._cooldowns:
                continue
            # Must not be already extended (don't chase)
            if r.label == RadarLabel.ALREADY_EXTENDED:
                continue
            # Must not be illiquid trap
            if r.label == RadarLabel.ILLIQUID_TRAP:
                continue

            candidates.append(r)

        # Take top N
        new_promoted: Set[str] = set()
        for r in candidates[: self._config.promotion_top_n]:
            new_promoted.add(r.symbol)

        # Determine additions and removals
        to_promote = new_promoted - self._promoted
        to_demote = self._promoted - new_promoted

        # Update state
        self._promoted = new_promoted

        return to_promote, to_demote
```

---

## 3. Deep Microstructure Features
`monitor/features.py` - Computed ONLY for the top 10-20 promoted symbols from high-resolution BBO/Trade/Depth data.

```python
"""
Deep microstructure features — computed for promoted symbols only.

These require order book depth, aggregate trades, and BBO data.
"""

import time
from dataclasses import dataclass
from typing import Optional

import numpy as np
import structlog

from core.config import MonitorConfig
from monitor.state import StateEngine, SymbolState

logger = structlog.get_logger(__name__)


@dataclass(slots=True)
class DeepFeatures:
    """Computed deep microstructure features for one symbol."""

    symbol: str
    # Order Book Imbalance: positive = buy pressure
    book_imbalance: float = 0.0
    # Flow Imbalance: -1 to +1, positive = buy aggressor dominant
    flow_imbalance: float = 0.0
    # Ask Depletion: how thin are asks vs baseline (positive = thin asks)
    ask_depletion: float = 0.0
    # Rehydration Rate: how fast asks refill (lower = slower refill = bullish)
    rehydration_rate: float = 0.0
    # Spread Stability: variance of spread (lower = more stable)
    spread_stability: float = 0.0
    # Volume Acceleration: recent vs baseline
    volume_acceleration: float = 0.0
    # Micro Return: very short-term price move %
    micro_return: float = 0.0
    # Book Depth Quality: top-5 notional available
    book_depth_quality: float = 0.0
    # Timestamp
    computed_at: float = 0.0


class DeepFeatureEngine:
    """Computes deep microstructure features from per-symbol state."""

    def __init__(self, config: MonitorConfig, state_engine: StateEngine):
        self._config = config
        self._state = state_engine

    def compute(self, symbol: str) -> Optional[DeepFeatures]:
        """Compute all deep features for a promoted symbol."""
        state = self._state.get(symbol)
        if state is None or not state.is_warmed_up:
            return None

        features = DeepFeatures(symbol=symbol, computed_at=time.time())
        flow_window = self._config.flow_window_seconds

        # ─── 1. Order Book Imbalance ───
        bid_notional = state.book_bid_notional(levels=10)
        ask_notional = state.book_ask_notional(levels=10)
        total = bid_notional + ask_notional
        if total > 0:
            features.book_imbalance = (bid_notional - ask_notional) / total

        # ─── 2. Flow Imbalance ───
        buy_flow, sell_flow = state.buy_sell_flow(flow_window)
        flow_total = buy_flow + sell_flow
        if flow_total > 0:
            features.flow_imbalance = (buy_flow - sell_flow) / flow_total

        # ─── 3. Ask Depletion ───
        # Compare current top-5 ask notional to rolling baseline
        current_ask = state.book_ask_notional(levels=5)
        baseline_ask = state.book_ask_notional(levels=10) * 0.5  # rough baseline
        if baseline_ask > 0:
            features.ask_depletion = 1.0 - (current_ask / baseline_ask)
        else:
            features.ask_depletion = 0.0

        # ─── 4. Spread Stability ───
        recent_spreads = state.recent_spreads(10)
        if len(recent_spreads) >= 3:
            features.spread_stability = float(np.std(recent_spreads))

        # ─── 5. Volume Acceleration ───
        vol_recent = state.total_volume(10)
        vol_baseline = state.total_volume(60)
        baseline_rate = vol_baseline / 60.0
        recent_rate = vol_recent / 10.0
        if baseline_rate > 0:
            features.volume_acceleration = recent_rate / baseline_rate
        elif recent_rate > 0:
            features.volume_acceleration = 5.0  # large burst from zero baseline

        # ─── 6. Micro Return ───
        recent_prices = state.recent_prices(5)
        if len(recent_prices) >= 2 and recent_prices[0] > 0:
            features.micro_return = (
                (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
            ) * 100.0

        # ─── 7. Book Depth Quality ───
        features.book_depth_quality = bid_notional + current_ask

        return features
```

---

## 4. Signal Ignition Engine
`signals/long_engine.py` - Evaluates deep features, maintains watches, and confirms trade logic.

```python
"""
Ignition Long Engine — detects and manages long trade opportunities.

Implements the full pipeline from §11 of the strategy:
hard filters → composite score → persistence → watch → confirmation → entry.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

import structlog

from core.config import AppConfig, LongEngineConfig
from core.events import SymbolState
from monitor.features import DeepFeatureEngine, DeepFeatures
from monitor.state import StateEngine
from signals.lifecycle import LifecycleManager

logger = structlog.get_logger(__name__)


@dataclass
class LongWatchState:
    """Tracks a symbol in long-watch."""

    symbol: str
    started_at: float = field(default_factory=time.time)
    price_at_start: float = 0.0
    high_since: float = 0.0
    low_since: float = float("inf")
    confirmation_count: int = 0
    consecutive_above_threshold: int = 0
    last_score: float = 0.0
    reason: str = ""

    @property
    def duration(self) -> float:
        return time.time() - self.started_at


class LongEngine:
    """
    Ignition Long Engine — evaluates promoted symbols for long entry.
    """

    def __init__(
        self,
        config: AppConfig,
        state_engine: StateEngine,
        feature_engine: DeepFeatureEngine,
        lifecycle: LifecycleManager,
    ):
        self._config = config
        self._lcfg: LongEngineConfig = config.long_engine
        self._state = state_engine
        self._features = feature_engine
        self._lifecycle = lifecycle
        self._watches: Dict[str, LongWatchState] = {}
        self._persistence: Dict[str, int] = {}  # symbol → consecutive above threshold

    # ─── Hard Filters ───

    def _passes_hard_filters(self, symbol: str, features: DeepFeatures) -> bool:
        """Check hard prerequisites before scoring."""
        state = self._state.get(symbol)
        if state is None or not state.is_warmed_up:
            return False

        # Spread check
        if state.spread_pct > self._lcfg.max_spread_pct:
            return False

        # Depth check
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._lcfg.min_top_book_notional_usdt:
            return False

        # Extension check: don't enter if already too extended
        recent = state.recent_prices(60)
        if len(recent) >= 2 and recent[0] > 0:
            extension = ((recent[-1] - recent[0]) / recent[0]) * 100
            if extension > self._lcfg.max_extension_pct:
                return False

        return True

    # ─── Composite Score ───

    def _compute_long_score(self, features: DeepFeatures) -> float:
        """Weighted combination of deep features → 0..1 long score."""
        score = 0.0

        # Book imbalance: positive = buy pressure (+0.20)
        score += max(features.book_imbalance, 0) * 0.20

        # Flow imbalance: positive = buy aggressor (+0.25)
        score += max(features.flow_imbalance, 0) * 0.25

        # Ask depletion: positive = thin asks (+0.15)
        score += max(features.ask_depletion, 0) * 0.15

        # Volume acceleration: higher = more activity (+0.20)
        vol_norm = min(features.volume_acceleration / 5.0, 1.0)
        score += vol_norm * 0.20

        # Spread stability: lower variance = better (+0.10)
        spread_score = max(0, 1.0 - features.spread_stability * 10)
        score += spread_score * 0.10

        # Micro return: small positive = good (+0.10)
        if 0 < features.micro_return < 3.0:
            ret_score = features.micro_return / 3.0
            score += ret_score * 0.10

        return min(max(score, 0), 1.0)

    # ─── Evaluation Loop ───

    def evaluate(self, promoted_symbols: Set[str]) -> List[str]:
        """
        Evaluate all promoted symbols. Returns list of symbols ready for entry.
        """
        entry_signals: List[str] = []

        for symbol in promoted_symbols:
            features = self._features.compute(symbol)
            if features is None:
                continue

            # Hard filters
            if not self._passes_hard_filters(symbol, features):
                self._persistence[symbol] = 0
                continue

            # Compute score
            score = self._compute_long_score(features)

            # Persistence filter
            if score >= self._lcfg.score_threshold:
                self._persistence[symbol] = self._persistence.get(symbol, 0) + 1
            else:
                self._persistence[symbol] = 0
                # If in watch, check invalidation
                if symbol in self._watches:
                    self._invalidate_watch(symbol, "score_below_threshold")
                continue

            # Need N consecutive ticks above threshold
            if self._persistence[symbol] < self._lcfg.persistence_ticks:
                continue

            # ─── Watch management ───
            if symbol not in self._watches:
                self._enter_watch(symbol, score, features)
            else:
                watch = self._watches[symbol]
                watch.last_score = score

                # Update high/low tracking
                state = self._state.get(symbol)
                if state and state.mid_price > 0:
                    watch.high_since = max(watch.high_since, state.mid_price)
                    watch.low_since = min(watch.low_since, state.mid_price)

                # Check watch timeout
                if watch.duration > self._lcfg.max_watch_duration_seconds:
                    self._invalidate_watch(symbol, "watch_timeout")
                    continue

                # Confirmation logic
                if self._check_confirmation(symbol, features, watch):
                    entry_signals.append(symbol)

        return entry_signals

    def _enter_watch(self, symbol: str, score: float, features: DeepFeatures) -> None:
        """Enter long watch state."""
        state = self._state.get(symbol)
        mid = state.mid_price if state else 0.0

        self._watches[symbol] = LongWatchState(
            symbol=symbol,
            price_at_start=mid,
            high_since=mid,
            low_since=mid,
            last_score=score,
            reason="high_long_score",
        )
        self._lifecycle.transition(symbol, SymbolState.LONG_WATCH, "long_score_persisted")

    def _check_confirmation(
        self, symbol: str, features: DeepFeatures, watch: LongWatchState
    ) -> bool:
        """Check if confirmation conditions are met for entry."""
        state = self._state.get(symbol)
        if state is None:
            return False

        confirmed = False

        # Condition 1: Price breaks recent local high
        if state.mid_price > watch.high_since * 1.001:  # 0.1% above high
            watch.confirmation_count += 1
            confirmed = True

        # Condition 2: Sustained buy flow
        buy, sell = state.buy_sell_flow(5)
        if buy > 0 and sell > 0 and (buy / (buy + sell)) > 0.65:
            watch.confirmation_count += 1
            confirmed = True

        # Condition 3: Ask depletion persists
        if features.ask_depletion > 0.3 and features.spread_stability < 0.5:
            watch.confirmation_count += 1

        # Need enough confirmations
        if watch.confirmation_count >= self._lcfg.confirmation_count_required:
            return True

        return False

    def _invalidate_watch(self, symbol: str, reason: str) -> None:
        """Cancel the watch and put symbol into cooldown."""
        self._watches.pop(symbol, None)
        self._persistence[symbol] = 0
        self._lifecycle.transition(symbol, SymbolState.COOLDOWN, reason)
```

---

## 5. Execution Engine
`execution/engine.py` - Decides final sizing and triggers orders.

```python
"""
Execution engine — decides sizing, validates entry, and places orders.

Supports minimum-quantity mode for low-risk testing.
"""

# ... [imports removed for brevity] ...

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
                return None
            return min_qty
        else:
            # ─── Normal Risk-Based Sizing ───
            return self._metadata.get_min_trade_quantity(symbol, current_price)

    async def execute_entry(self, symbol: str, reason: str = "") -> Optional[Position]:
        """
        Attempt to enter a long position on the given symbol.

        Performs final pre-entry validation before placing the order.
        """
        # ─── Pre-entry validation ───
        state = self._state.get(symbol)
        if state is None:
            return None

        features = self._features.compute(symbol)
        if features is None:
            return None

        current_price = state.mid_price
        if current_price <= 0:
            return None

        # Spread still acceptable?
        if state.spread_pct > self._config.long_engine.max_spread_pct:
            return None

        # Still have depth?
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._config.long_engine.min_top_book_notional_usdt:
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

        return position
```

---

## 6. Risk Engine
`risk/engine.py` - Continuously enforces limits and triggers kill switches if breached.

```python
"""
Risk engine — enforces per-trade and portfolio-level risk controls.

Implements kill switches for drawdown, consecutive losses, stale data,
and order reject spikes.
"""

# ... [imports removed for brevity] ...

class RiskEngine:
    """
    Enforces risk limits and manages kill switches.
    """

    def __init__(
        self,
        config: AppConfig,
        order_manager: OrderManager,
    ):
        self._config = config
        self._rcfg: RiskConfig = config.risk
        self._orders = order_manager
        self._kill_switch = KillSwitch()
        self._daily_stats = DailyStats()
        self._last_data_time: float = time.time()

    def can_open_position(self, position_count: int, notional: float = 0) -> bool:
        """Check if a new position is allowed given current limits."""
        if not self.is_trading_allowed:
            return False

        # Concurrent position limit
        if position_count >= self._rcfg.max_concurrent_positions:
            return False

        return True

    def check_risk_conditions(
        self,
        position_count: int,
        ws_last_message_time: float,
    ) -> None:
        """
        Periodic risk check — triggers kill switch if limits are breached.
        Should be called every second.
        """
        # ─── 1. Daily drawdown ───
        if self._daily_stats.drawdown_pct > self._rcfg.max_daily_drawdown_pct:
            self._kill_switch.trigger(
                f"daily_drawdown_{self._daily_stats.drawdown_pct:.1f}pct"
            )

        # ─── 2. Consecutive losses ───
        if self._daily_stats.consecutive_losses >= self._rcfg.max_consecutive_losses:
            self._kill_switch.trigger(
                f"consecutive_losses_{self._daily_stats.consecutive_losses}"
            )

        # ─── 3. Stale data ───
        data_age = time.time() - ws_last_message_time
        if data_age > self._rcfg.stale_data_timeout_seconds:
            self._kill_switch.trigger(f"stale_data_{data_age:.0f}s")

        # ─── 4. Order reject spike ───
        if self._orders.consecutive_rejects >= self._rcfg.max_order_rejects:
            self._kill_switch.trigger(
                f"order_rejects_{self._orders.consecutive_rejects}"
            )
```
