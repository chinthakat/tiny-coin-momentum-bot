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
    # Ask Depletion: how thin are asks vs historical baseline (positive = thin asks)
    ask_depletion: float = 0.0
    # Rehydration Rate: how fast asks refill after depletion (lower = slower = bullish)
    rehydration_rate: float = 0.0
    # Spread Stability: coefficient of variation of spread (lower = more stable)
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

    def compute(self, symbol: str, require_warmup: bool = True) -> Optional[DeepFeatures]:
        """Compute all deep features for a promoted symbol."""
        state = self._state.get(symbol)
        if state is None:
            return None
            
        if require_warmup and not state.is_warmed_up:
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

        # ─── 3. Ask Depletion (FIX #6) ───
        # Compare current top-5 ask notional against historical median baseline
        current_ask = state.book_ask_notional(levels=5)
        baseline_ask = state.ask_notional_baseline(seconds=60)
        if baseline_ask > 0:
            features.ask_depletion = 1.0 - (current_ask / baseline_ask)
        else:
            features.ask_depletion = 0.0

        # ─── 4. Rehydration Rate (FIX #7) ───
        # Compare ask notional now vs N seconds ago
        # If asks were thin and are refilling quickly, rehydration is high (bearish)
        # If asks stay thin, rehydration is low (bullish — true vacuum)
        ask_5s_ago = state.ask_notional_at(5.0)
        if ask_5s_ago > 0 and current_ask > 0:
            # rehydration_rate > 1 means asks are growing (refilling)
            # rehydration_rate < 1 means asks are still depleted
            features.rehydration_rate = current_ask / ask_5s_ago
        elif ask_5s_ago <= 0 and current_ask > 0:
            features.rehydration_rate = 2.0  # asks appeared from nothing — fast refill
        else:
            features.rehydration_rate = 0.5  # no data — assume neutral

        # ─── 5. Spread Stability (FIX #8) ───
        # Use coefficient of variation instead of raw std
        recent_spreads = state.recent_spreads(10)
        if len(recent_spreads) >= 3:
            spread_mean = float(np.mean(recent_spreads))
            spread_std = float(np.std(recent_spreads))
            if spread_mean > 0:
                features.spread_stability = spread_std / spread_mean  # CoV
            else:
                features.spread_stability = 0.0

        # ─── 6. Volume Acceleration ───
        vol_recent = state.total_volume(10)
        vol_baseline = state.total_volume(60)
        baseline_rate = vol_baseline / 60.0
        recent_rate = vol_recent / 10.0
        if baseline_rate > 0:
            features.volume_acceleration = recent_rate / baseline_rate
        elif recent_rate > 0:
            features.volume_acceleration = 5.0  # large burst from zero baseline

        # ─── 7. Micro Return ───
        recent_prices = state.recent_prices(5)
        if len(recent_prices) >= 2 and recent_prices[0] > 0:
            features.micro_return = (
                (recent_prices[-1] - recent_prices[0]) / recent_prices[0]
            ) * 100.0

        # ─── 8. Book Depth Quality ───
        features.book_depth_quality = bid_notional + current_ask

        return features
