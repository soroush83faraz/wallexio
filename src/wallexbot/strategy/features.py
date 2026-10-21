"""Features of the LAST bar of a closed-bar series (plus the previous bar's ROC). Uses only data up
to bars[-1] and only the trailing `history_bars` bars, so cost per call is bounded."""
from __future__ import annotations

import math
from dataclasses import astuple, dataclass
from decimal import ROUND_DOWN, Decimal
from typing import NamedTuple, Sequence

import numpy as np

from wallexbot.data.candles import Candle
from wallexbot.strategy import indicators as ind
from wallexbot.strategy.config import MomentumParams


class Series(NamedTuple):
    ts: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray


def window_series(bars: Sequence[Candle], history: int) -> Series | None:
    """Float arrays of the trailing `history` bars, or None if the data is unusable
    (non-Candle items, timestamps not strictly increasing, non-finite or non-positive prices)."""
    window = list(bars[-history:])
    if not window or not all(isinstance(c, Candle) for c in window):
        return None
    ts = np.array([c.ts for c in window], dtype=np.int64)
    if np.any(np.diff(ts) <= 0):
        return None
    s = Series(ts, np.array([float(c.high) for c in window]), np.array([float(c.low) for c in window]),
               np.array([float(c.close) for c in window]), np.array([float(c.volume) for c in window]))
    if not all(np.all(np.isfinite(a)) for a in s[1:]) or np.any(s.low <= 0) or np.any(s.volume < 0):
        return None
    return s


def price_quantum(bars: Sequence[Candle], n: int = 50) -> Decimal:
    """Finest price tick seen in the last n bars (as a power of ten, at most 1)."""
    exps = [min(c.high.as_tuple().exponent, c.low.as_tuple().exponent, c.close.as_tuple().exponent)
            for c in bars[-n:]]
    return Decimal(1).scaleb(min(0, *exps))


@dataclass(frozen=True)
class Features:
    bar_ts: int
    close: float
    atr: float
    atr_pct: float            # atr / close
    ema: float
    ema_slope: float          # ema[-1] / ema[-1-ema_slope_bars] - 1
    donchian_high: float      # highest high of the prior donchian_period bars
    roc: float
    roc_prev: float           # ROC one bar earlier, for the acceleration test
    roc_z: float              # z-score of roc over roc_z_window bars
    vol_ratio: float          # volume / median volume of the prior vol_median_window bars
    zero_volume_frac: float   # share of zero-volume bars in that median window
    rsi: float
    efficiency_ratio: float
    advance_from_low: float   # close - min(low over the last low_window bars)


def compute_features(bars: Sequence[Candle], params: MomentumParams = MomentumParams()) -> Features | None:
    """Features at bars[-1], or None when there are too few bars or any needed value is non-finite
    (bad data, zero median volume, flat ROC window, ...). Never looks past bars[-1]."""
    if len(bars) < params.min_bars:
        return None
    s = window_series(bars, params.history_bars)
    if s is None:
        return None
    p = params
    close = s.close
    atr_a = ind.atr(s.high, s.low, close, p.atr_period)
    ema_a = ind.ema(close, p.ema_period)
    roc_a = ind.roc(close, p.roc_period)
    z_a = ind.zscore(roc_a, p.roc_z_window)
    prior_vol = s.volume[:-1]
    median = ind.rolling_median(prior_vol, p.vol_median_window)[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        vol_ratio = s.volume[-1] / median if median > 0 else math.nan
    f = Features(
        bar_ts=int(s.ts[-1]), close=float(close[-1]), atr=float(atr_a[-1]),
        atr_pct=float(atr_a[-1] / close[-1]), ema=float(ema_a[-1]),
        ema_slope=float(ema_a[-1] / ema_a[-1 - p.ema_slope_bars] - 1.0),
        donchian_high=float(ind.donchian_high(s.high, p.donchian_period)[-1]),
        roc=float(roc_a[-1]), roc_prev=float(roc_a[-2]), roc_z=float(z_a[-1]),
        vol_ratio=float(vol_ratio),
        zero_volume_frac=float(np.mean(prior_vol[-p.vol_median_window:] == 0)),
        rsi=float(ind.rsi(close, p.rsi_period)[-1]),
        efficiency_ratio=float(ind.efficiency_ratio(close, p.er_period)[-1]),
        advance_from_low=float(close[-1] - s.low[-p.low_window:].min()),
    )
    if not all(math.isfinite(v) for v in astuple(f)) or f.atr <= 0:
        return None
    return f


def atr_stop(close: Decimal, atr: float, mult: float, quantum: Decimal) -> tuple[Decimal, Decimal] | None:
    """(atr as Decimal, stop = close - mult*atr rounded DOWN to the price tick), or None unless
    0 < stop < close. Rounding down widens the stop, which sizes the position smaller (conservative)."""
    atr_d = Decimal(f"{atr:.8g}")
    stop = (close - Decimal(str(mult)) * atr_d).quantize(quantum, rounding=ROUND_DOWN)
    return (atr_d, stop) if 0 < stop < close else None
