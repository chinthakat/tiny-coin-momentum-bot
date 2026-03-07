"""
Exit manager — handles all exit logic for live positions.

Implements hard stops, flow invalidation, time stops, and trailing stops.
"""

import asyncio
import time
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig, ExitConfig
from monitor.features import DeepFeatureEngine
from monitor.state import StateEngine

logger = structlog.get_logger(__name__)


class ExitManager:
    """
    Monitors all live positions and triggers exits when conditions are met.

    Exit triggers:
    1. Hard stop: price drops below stop_price
    2. Flow invalidation: microstructure thesis breaks
    3. Time stop: position held too long
    4. Trailing stop: price pulls back from high after activation
    """

    def __init__(
        self,
        config: AppConfig,
        state_engine: StateEngine,
        feature_engine: DeepFeatureEngine,
    ):
        self._config = config
        self._ecfg: ExitConfig = config.exit
        self._state = state_engine
        self._features = feature_engine

    async def check_exits(self, execution_engine) -> List[str]:
        """
        Check all live positions for exit conditions.
        Returns list of symbols that were exited.
        """
        exited: List[str] = []
        positions = dict(execution_engine.positions)  # copy to avoid mutation during iteration

        for symbol, position in positions.items():
            state = self._state.get(symbol)
            if state is None:
                continue

            current_price = state.mid_price
            if current_price <= 0:
                continue

            # Update position tracking
            position.highest_price = max(position.highest_price, current_price)
            position.lowest_price = min(position.lowest_price, current_price)

            exit_reason = self._check_exit_conditions(
                symbol, position, current_price, state
            )

            if exit_reason:
                success = await execution_engine.close_position(symbol, exit_reason)
                if success:
                    exited.append(symbol)

        return exited

    def _check_exit_conditions(
        self, symbol: str, position, current_price: float, state
    ) -> Optional[str]:
        """Check all exit conditions. Returns reason string if should exit, else None."""

        # ─── 1. Hard Stop ───
        if current_price <= position.stop_price:
            return "hard_stop"

        # ─── 2. Flow Invalidation ───
        if self._ecfg.flow_invalidation_exit:
            features = self._features.compute(symbol)
            if features:
                # Flow turned negative: sell aggressor dominant
                if features.flow_imbalance < -0.4:
                    return "flow_invalidation_negative"

                # Ask refill: asks came back strongly
                if features.ask_depletion < -0.3:
                    return "flow_invalidation_ask_refill"

                # Support vanishing: bid side depleted
                if features.book_imbalance < -0.5:
                    return "flow_invalidation_support_gone"

        # ─── 3. Time Stop ───
        if position.hold_time > self._ecfg.time_stop_seconds:
            return "time_stop"

        # ─── 4. Trailing Stop ───
        pnl_pct = position.unrealized_pnl_pct(current_price)

        # Activate trailing after sufficient gain
        if pnl_pct >= self._ecfg.trailing_stop_activation_pct:
            position.trailing_active = True
            # Set trailing stop price
            trail_price = position.highest_price * (
                1 - self._ecfg.trailing_stop_distance_pct / 100.0
            )
            if trail_price > position.trailing_stop_price:
                position.trailing_stop_price = trail_price

        if position.trailing_active and current_price <= position.trailing_stop_price:
            return "trailing_stop"

        return None

    async def run_exit_loop(self, execution_engine, interval: float = 1.0) -> None:
        """Continuously check exits on a fixed interval."""
        while True:
            try:
                exited = await self.check_exits(execution_engine)
                if exited:
                    logger.info("exit_loop_closed", symbols=exited)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("exit_loop_error", error=str(e))
            await asyncio.sleep(interval)
