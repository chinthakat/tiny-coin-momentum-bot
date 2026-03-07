"""
Symbol lifecycle state machine — manages transitions between states.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import structlog

from core.events import SymbolState

logger = structlog.get_logger(__name__)


@dataclass
class LifecycleEntry:
    """Tracks lifecycle state for a single symbol."""

    symbol: str
    state: SymbolState = SymbolState.NORMAL
    entered_at: float = field(default_factory=time.time)
    history: List[tuple] = field(default_factory=list)  # (state, timestamp)
    metadata: Dict = field(default_factory=dict)

    def transition(self, new_state: SymbolState, reason: str = "") -> None:
        """Transition to a new state, recording history."""
        old = self.state
        self.history.append((old.value, self.entered_at))
        self.state = new_state
        self.entered_at = time.time()
        logger.info(
            "lifecycle_transition",
            symbol=self.symbol,
            from_state=old.value,
            to_state=new_state.value,
            reason=reason,
        )

    @property
    def time_in_state(self) -> float:
        return time.time() - self.entered_at


# Valid transitions
VALID_TRANSITIONS = {
    SymbolState.NORMAL: {SymbolState.SUSPICIOUS, SymbolState.HIGH_RES_MONITOR},
    SymbolState.SUSPICIOUS: {SymbolState.DANGER_WATCH, SymbolState.NORMAL, SymbolState.HIGH_RES_MONITOR},
    SymbolState.DANGER_WATCH: {SymbolState.HIGH_RES_MONITOR, SymbolState.NORMAL, SymbolState.COOLDOWN},
    SymbolState.HIGH_RES_MONITOR: {SymbolState.LONG_WATCH, SymbolState.SHORT_WATCH, SymbolState.COOLDOWN, SymbolState.NORMAL},
    SymbolState.LONG_WATCH: {SymbolState.ORDER_PENDING, SymbolState.COOLDOWN, SymbolState.HIGH_RES_MONITOR, SymbolState.NORMAL},
    SymbolState.SHORT_WATCH: {SymbolState.ORDER_PENDING, SymbolState.COOLDOWN, SymbolState.HIGH_RES_MONITOR, SymbolState.NORMAL},
    SymbolState.ORDER_PENDING: {SymbolState.LIVE_POSITION, SymbolState.COOLDOWN, SymbolState.NORMAL},
    SymbolState.LIVE_POSITION: {SymbolState.COOLDOWN, SymbolState.NORMAL},
    SymbolState.COOLDOWN: {SymbolState.NORMAL},
}


class LifecycleManager:
    """Manages lifecycle state transitions for all symbols."""

    def __init__(self):
        self._entries: Dict[str, LifecycleEntry] = {}

    def get_or_create(self, symbol: str) -> LifecycleEntry:
        if symbol not in self._entries:
            self._entries[symbol] = LifecycleEntry(symbol=symbol)
        return self._entries[symbol]

    def get(self, symbol: str) -> Optional[LifecycleEntry]:
        return self._entries.get(symbol)

    def transition(
        self, symbol: str, new_state: SymbolState, reason: str = ""
    ) -> bool:
        """
        Attempt a state transition. Returns True if successful.
        Validates against allowed transitions.
        """
        entry = self.get_or_create(symbol)
        allowed = VALID_TRANSITIONS.get(entry.state, set())

        if new_state not in allowed:
            logger.warning(
                "invalid_transition",
                symbol=symbol,
                current=entry.state.value,
                requested=new_state.value,
            )
            return False

        entry.transition(new_state, reason)
        return True

    def get_symbols_in_state(self, state: SymbolState) -> List[str]:
        return [s for s, e in self._entries.items() if e.state == state]

    def cleanup(self, active_symbols: set) -> None:
        """Remove entries for symbols no longer tracked."""
        stale = [s for s in self._entries if s not in active_symbols and self._entries[s].state == SymbolState.NORMAL]
        for s in stale:
            del self._entries[s]
