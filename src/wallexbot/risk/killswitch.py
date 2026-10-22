"""Kill switch: the latest row of the append-only killswitch_events table.

Fail-closed: any error reading the state means ENGAGED. No rows means never engaged.
An engaged switch blocks new entries but never blocks reduce-only sells (exits).

Transient engagements. The guardian engages the switch for a TRANSIENT condition (strategy
heartbeat lost) with a machine-readable reason starting with AUTO_PREFIX ("auto:") and actor
'guardian'; only such a row, while it is the LATEST row, may be released by the guardian itself
(guardian/monitor.py, audited). Every other engagement (operator, bot, hard breach, balance
mismatch, tick failures, flatten) stays until an operator disengages it. A non-transient
engagement made while only a transient one holds the switch is written as a new row on top
(engage_unless_held), so the latest row always names the strongest reason and a later auto-release
can never clear it. The release itself is a compare-and-set in one IMMEDIATE store transaction
(release_transient): a row another connection wrote after the guardian looked is never released.
"""
from __future__ import annotations

from wallexbot.types import OrderIntent, Side

AUTO_PREFIX = "auto:"
AUTO_ACTOR = "guardian"


def is_transient(ev) -> bool:
    """A killswitch row is a transient guardian engagement: engaged, actor 'guardian', 'auto:' reason."""
    return ev is not None and ev.engaged is True and ev.actor == AUTO_ACTOR \
        and isinstance(ev.reason, str) and ev.reason.startswith(AUTO_PREFIX)


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

    def held_by_transient(self) -> bool:
        """The latest row is an engagement the guardian made for a transient condition. Read errors
        mean False (the switch then counts as held for a non-transient reason: fail closed)."""
        return self.latest_transient() is not None

    def latest_transient(self):
        """The latest row if it is a transient guardian engagement, else None (read errors: None)."""
        try:
            ev = self._store.latest_killswitch()
        except Exception:
            return None
        return ev if is_transient(ev) else None

    def release_transient(self, observed_id: int, reason: str, actor: str = AUTO_ACTOR,
                          ts: float | None = None) -> bool:
        """Compare-and-set release of a transient engagement, in ONE store transaction: disengage
        only if the latest row is STILL row `observed_id` and still a transient guardian engagement
        (actor 'guardian', reason 'auto:...'). An operator / bot row written in between, by any
        connection, is never released. True iff the disengage row was written."""
        return self._store.disengage_killswitch_if_latest(
            observed_id, reason, actor, require_actor=AUTO_ACTOR, require_prefix=AUTO_PREFIX, ts=ts) is not None

    def allows(self, intent: OrderIntent) -> bool:
        if intent.reduce_only and intent.side is Side.SELL:
            return True
        return not self.is_engaged()

    def engage(self, reason: str, actor: str = "system", ts: float | None = None) -> None:
        self._store.append_killswitch_event(True, reason, actor, ts=ts)

    def engage_unless_held(self, reason: str, actor: str, ts: float | None = None) -> bool:
        """Engage for a NON-transient reason unless the switch is already held by one. Supersedes a
        transient (auto) engagement. Returns True iff a row was written."""
        if self.is_engaged() and not self.held_by_transient():
            return False
        self.engage(reason, actor, ts)
        return True

    def disengage(self, reason: str, actor: str = "operator", ts: float | None = None) -> None:
        self._store.append_killswitch_event(False, reason, actor, ts=ts)
