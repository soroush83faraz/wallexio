"""Per-symbol, bars-based re-entry cooldown. Pure state, serializable to a dict. The caller (backtest
or bot) records exits and asks `allows` before acting on a Signal; the engines stay stateless.

Rules: after an exit, no entry for `cooldown_bars` bars (`cooldown_bars_profitable` after a profitable
exit with the trend intact). After a stop-out, no entry on the same breakout_level (same leg), until
a signal with a different breakout level appears."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from wallexbot.strategy.config import MomentumParams


@dataclass(frozen=True)
class _Exit:
    ts: int                      # open time of the bar on which the exit happened
    wait_bars: int
    blocked_level: Decimal | None  # set after a stop-out


class CooldownTracker:
    def __init__(self, bar_seconds: int, cooldown_bars: int = MomentumParams.cooldown_bars,
                 cooldown_bars_profitable: int = MomentumParams.cooldown_bars_profitable):
        if bar_seconds <= 0:
            raise ValueError("bar_seconds must be positive")
        self.bar_seconds = bar_seconds
        self.cooldown_bars = cooldown_bars
        self.cooldown_bars_profitable = cooldown_bars_profitable
        self._exits: dict[str, _Exit] = {}

    def record_exit(self, symbol: str, exit_bar_ts: int, *, profitable: bool, trend_intact: bool = False,
                    stopped_out: bool = False, breakout_level: Decimal | None = None) -> None:
        short = profitable and trend_intact and not stopped_out
        blocked = breakout_level if stopped_out else None
        self._exits[symbol] = _Exit(exit_bar_ts, self.cooldown_bars_profitable if short else self.cooldown_bars,
                                    blocked)

    def allows(self, symbol: str, bar_ts: int, breakout_level: Decimal | None = None) -> bool:
        """True if a new entry on the bar opening at bar_ts is allowed for this symbol."""
        e = self._exits.get(symbol)
        if e is None:
            return True
        if (bar_ts - e.ts) // self.bar_seconds < e.wait_bars:
            return False
        return not (e.blocked_level is not None and breakout_level == e.blocked_level)

    def to_dict(self) -> dict:
        return {"bar_seconds": self.bar_seconds, "cooldown_bars": self.cooldown_bars,
                "cooldown_bars_profitable": self.cooldown_bars_profitable,
                "exits": {s: {"ts": e.ts, "wait_bars": e.wait_bars,
                              "blocked_level": None if e.blocked_level is None else str(e.blocked_level)}
                          for s, e in sorted(self._exits.items())}}

    @classmethod
    def from_dict(cls, d: dict) -> "CooldownTracker":
        t = cls(d["bar_seconds"], d["cooldown_bars"], d["cooldown_bars_profitable"])
        for s, e in d["exits"].items():
            lvl = e["blocked_level"]
            t._exits[s] = _Exit(e["ts"], e["wait_bars"], None if lvl is None else Decimal(lvl))
        return t
