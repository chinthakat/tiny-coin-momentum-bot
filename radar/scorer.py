"""
Radar scoring and promotion engine.

Converts radar features into composite scores, labels symbols,
and promotes top candidates to high-resolution monitoring.
"""

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Set, Tuple

import numpy as np
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


@dataclass
class FeatureStats:
    """Rolling statistics for z-score normalization of a single feature."""

    values: Deque = field(default_factory=lambda: deque(maxlen=120))

    def push(self, v: float) -> None:
        self.values.append(v)

    @property
    def mean(self) -> float:
        if len(self.values) < 5:
            return 0.0
        return float(np.mean(self.values))

    @property
    def std(self) -> float:
        if len(self.values) < 5:
            return 1.0  # avoid div zero
        s = float(np.std(self.values))
        return max(s, 1e-6)

    def z_score(self, v: float) -> float:
        """Compute z-score, clipped to [0, 1]."""
        if len(self.values) < 5:
            return 0.0
        z = (v - self.mean) / self.std
        return min(max(z / 3.0, 0), 1.0)  # normalize ~3σ → [0, 1]


class RadarScorer:
    """
    Computes composite radar scores and manages symbol promotion.

    FIX #4: Uses per-symbol z-score normalization instead of fixed caps.
    FIX #5: Reserves promotion slots by label class for better coverage.
    """

    def __init__(self, config: RadarConfig, feature_engine: RadarFeatureEngine):
        self._config = config
        self._features = feature_engine
        self._cooldowns: Dict[str, CooldownEntry] = {}
        self._promoted: Set[str] = set()
        self._last_results: Dict[str, RadarResult] = {}

        # FIX #4: Per-symbol rolling stats for z-score normalization
        self._feature_stats: Dict[str, Dict[str, FeatureStats]] = {}

    @property
    def promoted_symbols(self) -> Set[str]:
        return self._promoted.copy()

    @property
    def last_results(self) -> Dict[str, RadarResult]:
        return self._last_results

    def _get_stats(self, symbol: str) -> Dict[str, FeatureStats]:
        """Get or create per-symbol feature statistics."""
        if symbol not in self._feature_stats:
            self._feature_stats[symbol] = {
                "ret_accel": FeatureStats(),
                "vol_burst": FeatureStats(),
                "spread_comp": FeatureStats(),
                "activity": FeatureStats(),
                "buildup": FeatureStats(),
            }
        return self._feature_stats[symbol]

    def _compute_composite_score(self, features: RadarFeatureValues) -> float:
        """
        FIX #4: Weighted combination using z-score normalization.

        Each feature is scored relative to its own recent history,
        making scores comparable across symbols with different regimes.
        """
        w = self._config.weights
        stats = self._get_stats(features.symbol)

        # Push raw values into history
        stats["ret_accel"].push(features.return_acceleration)
        stats["vol_burst"].push(features.volume_burst_ratio)
        stats["spread_comp"].push(-features.spread_compression)  # negate: compression is good
        stats["activity"].push(features.quote_activity_ratio)
        stats["buildup"].push(features.early_buildup_score)

        # Z-score normalize each feature
        ret_score = stats["ret_accel"].z_score(features.return_acceleration)
        vol_score = stats["vol_burst"].z_score(features.volume_burst_ratio)
        spread_score = stats["spread_comp"].z_score(-features.spread_compression)
        activity_score = stats["activity"].z_score(features.quote_activity_ratio)

        # Buildup is already 0..1 and composite, keep it raw
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
        FIX #5: Select symbols with quota-based promotion by label class.

        Reserves slots for different label types to improve coverage
        and reduce homogeneity.
        """
        # Clean up expired cooldowns
        self._cooldowns = {
            s: c for s, c in self._cooldowns.items() if c.is_active
        }

        # Filter candidates
        candidates: List[RadarResult] = []
        for r in results:
            if r.composite_score < self._config.promotion_score_threshold:
                continue
            if r.symbol not in long_eligible:
                continue
            if r.symbol in self._cooldowns:
                continue
            if r.label == RadarLabel.ALREADY_EXTENDED:
                continue
            if r.label == RadarLabel.ILLIQUID_TRAP:
                continue
            candidates.append(r)

        # FIX #5: Quota-based selection by label
        max_n = self._config.promotion_top_n
        new_promoted: Set[str] = set()

        # Define quotas per label class
        quotas = {
            RadarLabel.IGNITION_RISK: min(3, max_n // 3),
            RadarLabel.BUILDING_PRESSURE: min(5, max_n // 2),
        }
        # Remaining slots for any label
        reserved_total = sum(quotas.values())
        general_slots = max(max_n - reserved_total, 2)

        # Fill reserved slots first
        for label, quota in quotas.items():
            label_candidates = [c for c in candidates if c.label == label]
            for r in label_candidates[:quota]:
                if len(new_promoted) < max_n:
                    new_promoted.add(r.symbol)

        # Fill remaining with top overall (regardless of label)
        for r in candidates:
            if r.symbol not in new_promoted and len(new_promoted) < max_n:
                new_promoted.add(r.symbol)

        # Determine additions and removals
        to_promote = new_promoted - self._promoted
        to_demote = self._promoted - new_promoted

        # Update state
        self._promoted = new_promoted

        if to_promote:
            logger.info(
                "radar_promoted",
                symbols=list(to_promote),
                scores={
                    s: round(self._last_results[s].composite_score, 3)
                    for s in to_promote
                    if s in self._last_results
                },
            )
        if to_demote:
            logger.info("radar_demoted", symbols=list(to_demote))

        return to_promote, to_demote

    def add_cooldown(self, symbol: str, reason: str = "invalidated") -> None:
        """Put a symbol into cooldown after invalidation."""
        self._cooldowns[symbol] = CooldownEntry(
            symbol=symbol,
            started_at=time.time(),
            duration=self._config.cooldown_seconds,
            reason=reason,
        )
        self._promoted.discard(symbol)
        logger.info(
            "radar_cooldown_added",
            symbol=symbol,
            reason=reason,
            duration_s=self._config.cooldown_seconds,
        )

    def cleanup_stats(self, active_symbols: set) -> None:
        """Remove statistics for symbols no longer in the universe."""
        stale = [s for s in self._feature_stats if s not in active_symbols]
        for s in stale:
            del self._feature_stats[s]
