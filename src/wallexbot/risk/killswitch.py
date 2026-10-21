"""Kill switch: the latest row of the append-only killswitch_events table.

Fail-closed: any error reading the state means ENGAGED. No rows means never engaged.
An engaged switch blocks new entries but never blocks reduce-only sells (exits).
"""
from __future__ import annotations

from wallexbot.types import OrderIntent, Side


class KillSwitch:
    def __init__(self, store) -> None:
        self._store = store

    def is_engaged(self) -> bool:
        try:
            ev = self._store.latest_killswitch()
            if ev is None:
                return False
            return ev.engaged is not False  # anything but an explicit False => engaged
        except Exception:
            return True

    def allows(self, intent: OrderIntent) -> bool:
        if intent.reduce_only and intent.side is Side.SELL:
            return True
        return not self.is_engaged()

    def engage(self, reason: str, actor: str = "system", ts: float | None = None) -> None:
        self._store.append_killswitch_event(True, reason, actor, ts=ts)

    def disengage(self, reason: str, actor: str = "operator", ts: float | None = None) -> None:
        self._store.append_killswitch_event(False, reason, actor, ts=ts)
