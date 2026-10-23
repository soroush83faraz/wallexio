"""G1: pre-registered long-only gold trend strategy (gold/README.md). Stateless and look-ahead safe.

Decisions are made on CLOSED 1h bars; every indicator is computed on DAILY bars built from the
completed UTC days inside the bar window the caller hands over:

- a UTC day is used only if the window holds its first hour (no truncated first day) and the day
  has ended by the decision time (the close of bars[-1]); the unfinished current day never enters;
- trend: bars[-1].close > EMA(ema_days) of the daily closes;
- breakout: bars[-1].close > the highest daily high of the last `breakout_days` completed days;
- initial stop: close - stop_atr_mult * ATR(atr_days, daily, Wilder);
- exit (G1Trail): stop ratchets up to highest_high_since_entry - trail_atr_mult * daily ATR on every
  1h close; the caller (backtest engine / bot) executes stop touches.

Both components implement the repo's `SignalEngine` / `ExitPolicy` contract (strategy/contract.py),
so the same objects run in backtest/engine.py and in bot/loop.py.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal
from typing import Sequence

import numpy as np

from wallexbot.data.candles import Candle
from wallexbot.strategy import indicators as ind
from wallexbot.strategy.contract import ExitDecision, MarketContext, PositionState, Signal

STRATEGY_VERSION = "g1-0.1"
DAY_S = 86400
HOUR_S = 3600


@dataclass(frozen=True)
class G1Params:
    ema_days: int = 50
    breakout_days: int = 20
    atr_days: int = 14
    stop_atr_mult: Decimal = Decimal("2.0")
    trail_atr_mult: Decimal = Decimal("3.0")
    cooldown_bars: int = 24          # hourly bars after any exit before a new entry
    lookback_bars: int = 24 * 130    # 1h bars handed to the components (130 days of daily history)
    bar_s: int = HOUR_S

    def __post_init__(self):
        if min(self.ema_days, self.breakout_days, self.atr_days) < 1:
            raise ValueError("periods must be >= 1")
        if self.stop_atr_mult <= 0 or self.trail_atr_mult <= 0:
            raise ValueError("ATR multiples must be positive")
        if self.lookback_bars < 24 * (max(self.ema_days, self.breakout_days, self.atr_days + 1) + 2):
            raise ValueError("lookback_bars too short for the daily indicator periods")

    @property
    def history_bars(self) -> int:
        """What the backtest engine checks its lookback against (engine.declared_history_bars)."""
        return self.lookback_bars

    @property
    def min_days(self) -> int:
        return max(self.ema_days, self.breakout_days, self.atr_days + 1)


@dataclass(frozen=True)
class DailyBars:
    day: tuple[int, ...]             # UTC day number (ts // 86400)
    high: tuple[Decimal, ...]
    low: tuple[Decimal, ...]
    close: tuple[Decimal, ...]


def daily_bars(bars: Sequence[Candle], bar_s: int = HOUR_S) -> DailyBars:
    """Completed UTC days inside `bars` (oldest first); see the module docstring for the rules."""
    if not bars:
        return DailyBars((), (), (), ())
    decision_ts = bars[-1].ts + bar_s          # the instant bars[-1] closed
    first_ts = bars[0].ts
    first_day = -(-first_ts // DAY_S)           # first day whose start is inside the window
    last_day = decision_ts // DAY_S - 1         # last day that has ended by the decision time
    days: list[int] = []
    hi: list[Decimal] = []
    lo: list[Decimal] = []
    cl: list[Decimal] = []
    cur, h, lw, c_ = None, None, None, None
    for c in bars:
        d = c.ts // DAY_S
        if d < first_day or d > last_day:
            continue                            # truncated first day, or a day not finished yet
        if d != cur:
            if cur is not None:
                days.append(cur)
                hi.append(h)
                lo.append(lw)
                cl.append(c_)
            cur, h, lw = d, c.high, c.low
        else:
            if c.high > h:
                h = c.high
            if c.low < lw:
                lw = c.low
        c_ = c.close
    if cur is not None:
        days.append(cur)
        hi.append(h)
        lo.append(lw)
        cl.append(c_)
    return DailyBars(tuple(days), tuple(hi), tuple(lo), tuple(cl))


def daily_atr(db: DailyBars, period: int) -> float | None:
    if len(db.close) < period + 1:
        return None
    a = ind.atr(np.array(db.high, dtype=float), np.array(db.low, dtype=float), np.array(db.close, dtype=float),
                period)[-1]
    return float(a) if math.isfinite(a) and a > 0 else None


def _dec(x: float) -> Decimal:
    return Decimal(f"{x:.8g}")


class G1Engine:
    """Signal engine (SignalEngine protocol)."""

    def __init__(self, params: G1Params | None = None):
        self.params = params or G1Params()

    def evaluate(self, symbol: str, bars: Sequence[Candle], market: MarketContext | None = None) -> Signal | None:
        try:
            return self._evaluate(symbol, bars)
        except Exception:   # contract: never raise on bad data, abstain
            return None

    def rule_values(self, symbol: str, bars: Sequence[Candle], market: MarketContext | None = None) -> dict | None:
        """Every rule's observed value (decision log); None when there is not enough history."""
        p = self.params
        db = daily_bars(bars, p.bar_s)
        if len(db.close) < p.min_days:
            return None
        close = bars[-1].close
        ema = float(ind.ema(np.array(db.close, dtype=float), p.ema_days)[-1])
        level = max(db.high[-p.breakout_days:])
        atr = daily_atr(db, p.atr_days)
        return {"close": close, "ema": ema, "breakout_level": level, "atr": atr, "days": len(db.close),
                "trend": float(close) > ema, "breakout": close > level}

    def _evaluate(self, symbol: str, bars: Sequence[Candle]) -> Signal | None:
        p = self.params
        if not bars:
            return None
        d = self.rule_values(symbol, bars)
        if d is None or d["atr"] is None or not math.isfinite(d["ema"]):
            return None
        if not (d["trend"] and d["breakout"]):
            return None
        last = bars[-1]
        atr = _dec(d["atr"])
        tick = Decimal(1).scaleb(min(0, last.close.as_tuple().exponent, last.low.as_tuple().exponent))
        stop = (last.close - p.stop_atr_mult * atr).quantize(tick, rounding=ROUND_DOWN)
        if not 0 < stop < last.close:
            return None
        reasons = {"trend": f"close {last.close} > ema{p.ema_days}d {d['ema']:.8g}",
                   "breakout": f"close {last.close} > {p.breakout_days}d high {d['breakout_level']}",
                   "atr": f"atr{p.atr_days}d {atr}", "days": str(d["days"])}
        return Signal(symbol=symbol, bar_ts=last.ts, entry_ref_price=last.close,
                      breakout_level=d["breakout_level"], atr=atr, stop_price=stop, score=Decimal(1),
                      reasons=reasons, strategy_version=STRATEGY_VERSION)


class G1Trail:
    """Exit policy (ExitPolicy protocol): daily-ATR chandelier trail, stops only ratchet up."""

    def __init__(self, params: G1Params | None = None):
        self.params = params or G1Params()

    @property
    def history_bars(self) -> int:
        return self.params.lookback_bars

    def on_bar(self, state: PositionState, bars: Sequence[Candle]) -> ExitDecision:
        try:
            atr = daily_atr(daily_bars(bars, self.params.bar_s), self.params.atr_days)
        except Exception:
            atr = None
        if atr is None:
            return ExitDecision("hold", reason="no_atr")
        candidate = state.highest_high_since_entry - self.params.trail_atr_mult * _dec(atr)
        current = state.current_stop if state.current_stop is not None else state.initial_stop
        if candidate > current:
            return ExitDecision("update_stop", new_stop=candidate, reason="atr_trail")
        return ExitDecision("hold")

    def __repr__(self) -> str:
        return f"G1Trail({self.params!r})"
