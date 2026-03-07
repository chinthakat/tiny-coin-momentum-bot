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

    Pipeline:
    1. Hard filters (spread, depth, warmup, extension)
    2. Composite long score from deep features
    3. Persistence filter (N consecutive ticks above threshold)
    4. Watch state management
    5. Confirmation logic (local high break, buy flow, ask depletion)
    6. Entry signal emission
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

        logger.info(
            "long_watch_entered",
            symbol=symbol,
            score=round(score, 3),
            price=mid,
        )

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
            logger.info(
                "long_confirmed",
                symbol=symbol,
                confirmations=watch.confirmation_count,
                score=round(watch.last_score, 3),
            )
            return True

        return False

    def _invalidate_watch(self, symbol: str, reason: str) -> None:
        """Cancel the watch and put symbol into cooldown."""
        self._watches.pop(symbol, None)
        self._persistence[symbol] = 0
        self._lifecycle.transition(symbol, SymbolState.COOLDOWN, reason)
        logger.info("long_watch_invalidated", symbol=symbol, reason=reason)

    def consume_entry(self, symbol: str) -> Optional[LongWatchState]:
        """Remove and return the watch state for a confirmed entry."""
        return self._watches.pop(symbol, None)
