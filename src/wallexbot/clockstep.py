"""Elapsed time since a STORED timestamp that survives a backward clock step.

`now - ts` goes negative when the wall clock steps back (NTP, VM resume, operator) or a stored
timestamp is bogus, and a timer written as `now - ts >= timeout` then stalls until the clock
catches up with ts, possibly for hours. StepAnchors re-anchors such a timer instead: a timestamp
found AFTER `now` is replaced by the earliest `now` at which it was seen, so the timer still runs
its full duration but always expires (the same rule as RiskEngine._cooldown and the token bucket in
exchange/ratelimit.py). A non-finite timestamp is anchored the same way (unknown => start now).
"""
from __future__ import annotations

import math
from typing import Hashable, Iterable


class StepAnchors:
    def __init__(self) -> None:
        self._seen: dict[Hashable, tuple[float, float]] = {}   # key -> (stored ts, anchor)

    def elapsed(self, key: Hashable, ts: float, now: float) -> float:
        now, ts = float(now), float(ts)
        prev = self._seen.get(key)
        same = prev is not None and (prev[0] == ts or (math.isnan(prev[0]) and math.isnan(ts)))
        if not ts <= now:   # in the future, or NaN
            anchor = min(prev[1], now) if same else now
            self._seen[key] = (ts, anchor)
            return now - anchor
        if same:            # the clock caught up: keep the earlier anchor
            return now - min(ts, prev[1])
        return now - ts

    def keep_only(self, keys: Iterable[Hashable]) -> None:
        """Forget anchors of keys no longer tracked (bounded memory)."""
        live = set(keys)
        for k in [k for k in self._seen if k not in live]:
            del self._seen[k]
