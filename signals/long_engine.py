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
    confirmation_types: Set[str] = field(default_factory=set)
    confirmation_ticks: int = 0  # number of separate ticks with confirmations
    last_confirm_tick: int = 0   # tick id when last confirmation was counted
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
        self._eval_tick: int = 0  # global tick counter

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

        # V2: Liquidity stability filter — reject unstable books
        if len(state.bid_depth_history) >= 5:
            import numpy as np
            depths = [v for ts, v in state.bid_depth_history if ts >= time.time() - 10]
            if len(depths) >= 3:
                mean_depth = float(np.mean(depths))
                if mean_depth > 0:
                    cv = float(np.std(depths)) / mean_depth
                    if cv > self._lcfg.max_depth_coeff_of_variation:
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

    # ─── Watch Invalidation (Fix #11) ───

    def _check_watch_invalidation(
        self, symbol: str, features: DeepFeatures, watch: LongWatchState
    ) -> Optional[str]:
        """
        Check if watch should be invalidated based on microstructure breakdown.

        Returns reason string if invalidated, None otherwise.
        """
        state = self._state.get(symbol)
        if state is None:
            return "state_lost"

        # Flow flip: sell aggressor dominant
        if features.flow_imbalance < -0.3:
            return "flow_flip_negative"

        # Spread blowout: spread more than 2x the configured max
        if state.spread_pct > self._lcfg.max_spread_pct * 2.0:
            return "spread_blowout"

        # Depth collapse: top-5 book notional dropped below threshold
        top_notional = state.book_bid_notional(5) + state.book_ask_notional(5)
        if top_notional < self._lcfg.min_top_book_notional_usdt * 0.5:
            return "depth_collapse"

        # Sharp negative micro return during watch
        if features.micro_return < -1.5:
            return "sharp_negative_micro_return"

        # Ask refill: asks came back strongly (vacuum was fake)
        if features.ask_depletion < -0.3:
            return "ask_refill"

        return None

    # ─── Evaluation Loop ───

    def evaluate(self, promoted_symbols: Set[str]) -> List[str]:
        """
        Evaluate all promoted symbols. Returns list of symbols ready for entry.
        """
        self._eval_tick += 1
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

                # FIX #11: Check microstructure invalidation conditions
                invalidation = self._check_watch_invalidation(symbol, features, watch)
                if invalidation:
                    self._invalidate_watch(symbol, invalidation)
                    continue

                # Check watch timeout
                if watch.duration > self._lcfg.max_watch_duration_seconds:
                    self._invalidate_watch(symbol, "watch_timeout")
                    continue

                # FIX #10: Store prior high BEFORE updating
                state = self._state.get(symbol)
                if state and state.mid_price > 0:
                    prior_high = watch.high_since
                    watch.high_since = max(watch.high_since, state.mid_price)
                    watch.low_since = min(watch.low_since, state.mid_price)

                    # FIX #9: Confirmation with per-tick tracking
                    if self._check_confirmation(
                        symbol, features, watch, prior_high
                    ):
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
        self,
        symbol: str,
        features: DeepFeatures,
        watch: LongWatchState,
        prior_high: float,
    ) -> bool:
        """
        Check if confirmation conditions are met for entry.

        FIX #9: Confirmations from one tick only count as one confirmation tick.
        FIX #10: Breakout checks against prior_high (before update).
        """
        state = self._state.get(symbol)
        if state is None:
            return False

        # Collect which confirmation types fired THIS tick
        this_tick_confirmations: Set[str] = set()

        # Condition 1: Price breaks recent local high (uses prior_high)
        if prior_high > 0 and state.mid_price > prior_high * 1.001:
            this_tick_confirmations.add("price_breakout")

        # Condition 2: Sustained buy flow
        buy, sell = state.buy_sell_flow(5)
        if buy > 0 and sell > 0 and (buy / (buy + sell)) > 0.65:
            this_tick_confirmations.add("sustained_buy_flow")

        # Condition 3: Ask depletion persists with stable spread
        if features.ask_depletion > 0.3 and features.spread_stability < 0.5:
            this_tick_confirmations.add("ask_depletion_persistent")

        # Only count as a new confirmation tick if we have new types
        if this_tick_confirmations and self._eval_tick != watch.last_confirm_tick:
            watch.confirmation_types.update(this_tick_confirmations)
            watch.confirmation_ticks += 1
            watch.last_confirm_tick = self._eval_tick

        # Need enough confirmation ticks (not just enough types in one tick)
        if watch.confirmation_ticks >= self._lcfg.confirmation_count_required:
            logger.info(
                "long_confirmed",
                symbol=symbol,
                confirmation_ticks=watch.confirmation_ticks,
                confirmation_types=list(watch.confirmation_types),
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
