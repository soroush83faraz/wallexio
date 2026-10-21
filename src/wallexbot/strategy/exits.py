"""Exit policies E1-E4 implementing `contract.ExitPolicy`. Decimal only.

Division of labour with the caller (backtest engine or bot):
- `on_bar(state, bars)` is called once per CLOSED bar while a position is open; `bars[-1]` is the
  bar that just closed. Its decision ("exit_all" / "exit_partial") is executed by the caller at
  the NEXT bar's open; "update_stop" takes effect from the next bar (the caller only ratchets
  stops up). Exactly one decision per bar: exits take priority over stop updates.
- Intrabar levels are handled by the caller, never by `on_bar`:
  * the hard stop (`state.current_stop`, initially the signal's stop) is a resting stop that
    triggers when a bar's low touches it (gap below -> fill at the open);
  * take-profit levels come from `targets(state)` (an optional extension of the contract: a
    sequence of `TakeProfit(price, fraction, reason)`, fraction of the CURRENT quantity, >= 1
    meaning all). The caller fills them when a bar's high touches them.
  * If one bar touches both the stop and a target, the caller must assume the STOP came first.
- R is per unit: `state.entry_price - state.initial_stop`. A non-positive R means "hold".

Indicators (ATR, EMA) are computed locally over a bounded trailing window
(`indicator_window_mult * period` bars) so per-bar cost is constant; values therefore can differ
slightly from a full-history computation but depend only on bars up to `bars[-1]`.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import NamedTuple, Sequence

from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import ExitDecision, PositionState

HOLD = ExitDecision("hold")
_ONE = Decimal(1)


class TakeProfit(NamedTuple):
    price: Decimal
    fraction: Decimal   # of the current quantity; >= 1 means the whole position
    reason: str


@dataclass(frozen=True)
class ExitParams:
    tp_r: Decimal = Decimal("1.5")             # E1 target, in R
    atr_period: int = 14
    chandelier_mult: Decimal = Decimal("2")    # trail = highest high since entry - mult * ATR
    trail_activation_r: Decimal = Decimal("1")  # E2 trail starts once highest high >= entry + 1R
    ema_period: int = 9                        # E3: close < EMA9 is a momentum failure
    roc_period: int = 3                        # E3: close < close[-1-roc_period] (ROC < 0)
    volume_lookback: int = 20                  # E3: dry = volume < ratio * median(prev lookback)
    volume_dry_ratio: Decimal = Decimal("0.5")
    partial_r: Decimal = Decimal("1")          # E4: sell partial_fraction at +1R
    partial_fraction: Decimal = Decimal("0.5")
    fee_rate: Decimal = Decimal("0.0035")      # E4 breakeven+fees stop; placeholder, unverified
    # E4: extra room for the exit's own price give-up (half spread + impact) on the breakeven stop,
    # in bps of the stop. 20 = the backtest's default 7.5 bps half spread + 12.5 bps impact
    # allowance. Callers derive it from their cost model: CostModel.exit_cost_bps(...).
    breakeven_buffer_bps: Decimal = Decimal("20")
    false_breakout_bars: int = 2               # E4: close < breakout_level within this many bars
    time_stop_bars: int = 16                   # E4 time stop
    time_stop_only_without_partial: bool = True  # E4: a trade that already paid +1R is left to trail
    indicator_window_mult: int = 5

    def __post_init__(self):
        if not (0 < self.partial_fraction < 1):
            raise ValueError("partial_fraction must be in (0, 1)")
        if min(self.atr_period, self.ema_period, self.roc_period, self.volume_lookback,
               self.time_stop_bars, self.indicator_window_mult) < 1:
            raise ValueError("periods must be >= 1")
        if self.fee_rate < 0 or self.fee_rate >= 1:
            raise ValueError("fee_rate out of range")
        if not (0 <= self.breakeven_buffer_bps < 10000):
            raise ValueError("breakeven_buffer_bps out of range")

    @property
    def history_bars(self) -> int:
        """Most bars any E1-E4 rule reads (the caller must hand at least this many)."""
        return max(self.atr_period * self.indicator_window_mult + 1, self.ema_period * self.indicator_window_mult,
                   self.roc_period + 1, self.volume_lookback + 2)


# ---------------------------------------------------------------- indicators (Decimal, local)
def atr(bars: Sequence[Candle], period: int, window_mult: int = 5) -> Decimal | None:
    """Wilder ATR over the last min(len-1, period*window_mult) true ranges, seeded by the SMA of
    the first `period` of them. None when fewer than period+1 bars."""
    n = min(len(bars) - 1, period * window_mult)
    if n < period:
        return None
    w = bars[len(bars) - n - 1:]
    trs = [max(w[i].high - w[i].low, abs(w[i].high - w[i - 1].close), abs(w[i].low - w[i - 1].close))
           for i in range(1, len(w))]
    value = sum(trs[:period], Decimal(0)) / period
    for tr in trs[period:]:
        value = (value * (period - 1) + tr) / period
    return value


def ema(values: Sequence[Decimal], period: int) -> Decimal | None:
    """EMA seeded by the SMA of the first `period` values; alpha = 2/(period+1)."""
    if len(values) < period:
        return None
    value = sum(values[:period], Decimal(0)) / period
    alpha = Decimal(2) / (period + 1)
    for v in values[period:]:
        value = value + alpha * (v - value)
    return value


def _median(xs: list[Decimal]) -> Decimal:
    s = sorted(xs)
    m = len(s) // 2
    return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2


def momentum_failure(bars: Sequence[Candle], p: ExitParams) -> str | None:
    """E3 rule on closed bars: close < EMA, ROC < 0, or the last two bars both volume-dry."""
    last = bars[-1]
    window = [b.close for b in bars[max(0, len(bars) - p.ema_period * p.indicator_window_mult):]]
    e = ema(window, p.ema_period)
    if e is not None and last.close < e:
        return "momentum_fail_ema"
    if len(bars) > p.roc_period and last.close < bars[-1 - p.roc_period].close:
        return "momentum_fail_roc"
    if len(bars) >= p.volume_lookback + 2:
        def dry(i: int) -> bool:  # i: negative index of the bar tested
            prev = [b.volume for b in bars[len(bars) + i - p.volume_lookback:len(bars) + i]]
            return bars[i].volume < p.volume_dry_ratio * _median(prev)
        if dry(-1) and dry(-2):
            return "momentum_fail_volume_dry"
    return None


def _r(state: PositionState) -> Decimal | None:
    r = state.entry_price - state.initial_stop
    return r if r > 0 else None


def _stop(state: PositionState) -> Decimal:
    return state.current_stop if state.current_stop is not None else state.initial_stop


def _raise_stop(state: PositionState, candidate: Decimal | None, reason: str) -> ExitDecision:
    if candidate is not None and candidate > _stop(state):
        return ExitDecision("update_stop", new_stop=candidate, reason=reason)
    return HOLD


def chandelier(state: PositionState, bars: Sequence[Candle], p: ExitParams) -> Decimal | None:
    a = atr(bars, p.atr_period, p.indicator_window_mult)
    return None if a is None else state.highest_high_since_entry - p.chandelier_mult * a


# ---------------------------------------------------------------- policies
class _Policy:
    name = ""

    def __init__(self, params: ExitParams | None = None):
        self.params = params or ExitParams()

    def targets(self, state: PositionState) -> tuple[TakeProfit, ...]:
        return ()

    def on_bar(self, state: PositionState, bars: Sequence[Candle]) -> ExitDecision:
        return HOLD

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.params!r})"


class FixedRR(_Policy):
    """E1: hard stop (the signal's, 1.5 ATR) and a full take-profit at entry + tp_r * R."""
    name = "E1"

    def targets(self, state):
        r = _r(state)
        return () if r is None else (TakeProfit(state.entry_price + self.params.tp_r * r, _ONE, "take_profit"),)


class ChandelierTrail(_Policy):
    """E2: hard stop; once the highest high reaches entry + 1R, trail at highest high - 2 ATR."""
    name = "E2"

    def on_bar(self, state, bars):
        r = _r(state)
        if r is None or state.highest_high_since_entry < state.entry_price + self.params.trail_activation_r * r:
            return HOLD
        return _raise_stop(state, chandelier(state, bars, self.params), "chandelier_trail")


class MomentumFailure(_Policy):
    """E3: hard stop; exit at the next open after a momentum-failure close."""
    name = "E3"

    def on_bar(self, state, bars):
        why = momentum_failure(bars, self.params)
        return ExitDecision("exit_all", reason=why) if why else HOLD


class Hybrid(_Policy):
    """E4 (pre-registered leader): hard stop; sell partial_fraction at +1R, then stop to
    breakeven+fees(+exit-cost buffer) and chandelier-trail the rest; E3 failure exit; false-breakout exit when a
    close is back below breakout_level within false_breakout_bars; time stop."""
    name = "E4"

    def breakeven_stop(self, state: PositionState) -> Decimal:
        """Stop level S such that a sale triggered at S, filled up to `breakeven_buffer_bps` below S
        and charged the sell fee, repays the buy cost incl. its fee:
            S * (1 - buffer) * (1 - f) >= entry * (1 + f).
        A gap below S still fills at the (lower) open: the buffer covers spread/impact, not gaps.
        The caller puts S on the price tick (the backtest rounds a stop UP)."""
        p = self.params
        f, buffer = p.fee_rate, p.breakeven_buffer_bps / 10000
        return state.entry_price * (1 + f) / ((1 - f) * (1 - buffer))

    def targets(self, state):
        r = _r(state)
        if r is None or state.partial_taken:
            return ()
        return (TakeProfit(state.entry_price + self.params.partial_r * r,
                           self.params.partial_fraction, "partial_take_profit"),)

    def on_bar(self, state, bars):
        p = self.params
        if state.bars_held <= p.false_breakout_bars and bars[-1].close < state.breakout_level:
            return ExitDecision("exit_all", reason="false_breakout")
        if state.bars_held >= p.time_stop_bars and not (p.time_stop_only_without_partial and state.partial_taken):
            return ExitDecision("exit_all", reason="time_stop")
        why = momentum_failure(bars, p)
        if why:
            return ExitDecision("exit_all", reason=why)
        if state.partial_taken:
            cand = self.breakeven_stop(state)
            trail = chandelier(state, bars, p)
            if trail is not None and trail > cand:
                return _raise_stop(state, trail, "chandelier_trail")
            return _raise_stop(state, cand, "breakeven_plus_fees")
        return HOLD


POLICIES = {"E1": FixedRR, "E2": ChandelierTrail, "E3": MomentumFailure, "E4": Hybrid}


def make_policy(name: str, params: ExitParams | None = None) -> _Policy:
    return POLICIES[name](params)
