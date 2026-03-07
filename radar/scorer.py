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

    Promotion rules:
    - Only top-N symbols are promoted
    - Must exceed score threshold
    - Must not be in cooldown
    - Must have basic tradability
    """

    def __init__(self, config: RadarConfig, feature_engine: RadarFeatureEngine):
        self._config = config
        self._features = feature_engine
        self._cooldowns: Dict[str, CooldownEntry] = {}
        self._promoted: Set[str] = set()
        self._last_results: Dict[str, RadarResult] = {}

    @property
    def promoted_symbols(self) -> Set[str]:
        return self._promoted.copy()

    @property
    def last_results(self) -> Dict[str, RadarResult]:
        return self._last_results

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

        if to_promote:
            logger.info(
                "radar_promoted",
                symbols=list(to_promote),
                scores={s: round(self._last_results[s].composite_score, 3) for s in to_promote if s in self._last_results},
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
