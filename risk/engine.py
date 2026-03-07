"""
Risk engine — enforces per-trade and portfolio-level risk controls.

Implements kill switches for drawdown, consecutive losses, stale data,
and order reject spikes. Includes portfolio-level exposure limits.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog

from core.config import AppConfig, RiskConfig
from exchange.order_manager import OrderManager

logger = structlog.get_logger(__name__)


@dataclass
class DailyStats:
    """Tracks daily performance metrics."""

    date: str = ""
    starting_capital: float = 0.0
    realized_pnl: float = 0.0
    trade_count: int = 0
    win_count: int = 0
    loss_count: int = 0
    consecutive_losses: int = 0
    max_consecutive_losses: int = 0

    @property
    def drawdown_pct(self) -> float:
        if self.starting_capital <= 0:
            return 0.0
        return (abs(min(self.realized_pnl, 0)) / self.starting_capital) * 100.0

    @property
    def win_rate(self) -> float:
        if self.trade_count == 0:
            return 0.0
        return self.win_count / self.trade_count


class KillSwitch:
    """Kill switch that halts all trading when triggered."""

    def __init__(self):
        self.is_triggered: bool = False
        self.triggered_at: float = 0.0
        self.reason: str = ""
        self.manual_override: bool = False

    def trigger(self, reason: str) -> None:
        if not self.is_triggered:
            self.is_triggered = True
            self.triggered_at = time.time()
            self.reason = reason
            logger.critical("KILL_SWITCH_TRIGGERED", reason=reason)

    def reset(self) -> None:
        self.is_triggered = False
        self.reason = ""
        logger.info("kill_switch_reset")


class RiskEngine:
    """
    Enforces risk limits and manages kill switches.

    FIX #15: Includes portfolio-level exposure limits.
    FIX #16: Supports per-symbol staleness checks.
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

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill_switch

    @property
    def daily_stats(self) -> DailyStats:
        return self._daily_stats

    @property
    def is_trading_allowed(self) -> bool:
        """Check if trading is currently allowed."""
        return not self._kill_switch.is_triggered

    def update_data_timestamp(self) -> None:
        """Called whenever fresh market data arrives."""
        self._last_data_time = time.time()

    def record_trade_result(self, pnl: float) -> None:
        """Record the result of a closed trade."""
        self._daily_stats.trade_count += 1
        self._daily_stats.realized_pnl += pnl

        if pnl >= 0:
            self._daily_stats.win_count += 1
            self._daily_stats.consecutive_losses = 0
        else:
            self._daily_stats.loss_count += 1
            self._daily_stats.consecutive_losses += 1
            self._daily_stats.max_consecutive_losses = max(
                self._daily_stats.max_consecutive_losses,
                self._daily_stats.consecutive_losses,
            )

    def can_open_position(
        self,
        position_count: int,
        total_open_notional: float = 0,
        symbol_notional: float = 0,
    ) -> bool:
        """
        Check if a new position is allowed given current limits.

        FIX #15: Now checks total notional, per-symbol notional,
        and concurrent position count.
        """
        if not self.is_trading_allowed:
            return False

        # Concurrent position limit
        if position_count >= self._rcfg.max_concurrent_positions:
            logger.info("risk_max_positions_reached", count=position_count)
            return False

        # FIX #15: Total open notional limit
        max_total = getattr(self._rcfg, 'max_total_notional', 0)
        if max_total > 0 and total_open_notional >= max_total:
            logger.info(
                "risk_max_notional_reached",
                total=round(total_open_notional, 2),
                limit=max_total,
            )
            return False

        # FIX #15: Per-symbol notional cap
        max_symbol = getattr(self._rcfg, 'max_symbol_notional', 0)
        if max_symbol > 0 and symbol_notional >= max_symbol:
            logger.info(
                "risk_symbol_notional_exceeded",
                notional=round(symbol_notional, 2),
                limit=max_symbol,
            )
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

        # ─── 3. Stale data (global feed) ───
        data_age = time.time() - ws_last_message_time
        if data_age > self._rcfg.stale_data_timeout_seconds:
            self._kill_switch.trigger(f"stale_data_{data_age:.0f}s")

        # ─── 4. Order reject spike ───
        if self._orders.consecutive_rejects >= self._rcfg.max_order_rejects:
            self._kill_switch.trigger(
                f"order_rejects_{self._orders.consecutive_rejects}"
            )

    def check_position_staleness(
        self,
        state_engine,
        positions: Dict,
        stale_timeout: float = 15.0,
    ) -> List[str]:
        """
        FIX #16: Check per-symbol staleness for live positions.

        Returns list of symbols with stale data that should be force-exited.
        """
        stale_symbols = []
        for symbol in positions:
            state = state_engine.get(symbol)
            if state is not None and state.is_stale(stale_timeout):
                stale_symbols.append(symbol)
                logger.warning(
                    "position_stale_data",
                    symbol=symbol,
                    age_s=round(time.time() - state.last_update, 1),
                )
        return stale_symbols

    def get_status_summary(self) -> Dict:
        """Return a summary of current risk status."""
        return {
            "trading_allowed": self.is_trading_allowed,
            "kill_switch": {
                "triggered": self._kill_switch.is_triggered,
                "reason": self._kill_switch.reason,
            },
            "daily": {
                "pnl": round(self._daily_stats.realized_pnl, 4),
                "drawdown_pct": round(self._daily_stats.drawdown_pct, 2),
                "trades": self._daily_stats.trade_count,
                "win_rate": round(self._daily_stats.win_rate * 100, 1),
                "consecutive_losses": self._daily_stats.consecutive_losses,
            },
        }
