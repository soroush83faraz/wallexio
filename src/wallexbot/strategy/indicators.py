"""Pure indicator functions over float arrays. Every function returns an array aligned to its input
(same length, index i uses only x[..i]) with NaN where the warm-up is incomplete. Indicators are
analysis only; prices that leave the strategy layer are Decimal (see features.py / momentum.py)."""
from __future__ import annotations

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


def _arr(x) -> np.ndarray:
    return np.array(x, dtype=float)


def _nan_like(x: np.ndarray) -> np.ndarray:
    return np.full(x.shape, np.nan)


def _check_n(n: int) -> None:
    if n < 1:
        raise ValueError("period must be >= 1")


def _rolling(x: np.ndarray, n: int, fn) -> np.ndarray:
    """fn over the trailing window of n values ending at i (includes x[i])."""
    out = _nan_like(x)
    if len(x) >= n:
        out[n - 1:] = fn(sliding_window_view(x, n), axis=1)
    return out


def sma(x, n: int) -> np.ndarray:
    _check_n(n)
    return _rolling(_arr(x), n, np.mean)


def ema(x, n: int) -> np.ndarray:
    """EMA with alpha = 2/(n+1), seeded with the SMA of the first n values (valid from index n-1)."""
    _check_n(n)
    x = _arr(x)
    out = _nan_like(x)
    if len(x) < n:
        return out
    alpha = 2.0 / (n + 1)
    out[n - 1] = x[:n].mean()
    for i in range(n, len(x)):
        out[i] = alpha * x[i] + (1.0 - alpha) * out[i - 1]
    return out


def true_range(high, low, close) -> np.ndarray:
    """max(h-l, |h-prev_close|, |l-prev_close|); the first bar has no prev close so it is h-l."""
    high, low, close = _arr(high), _arr(low), _arr(close)
    tr = high - low
    if len(close) > 1:
        pc = close[:-1]
        tr[1:] = np.maximum(tr[1:], np.maximum(np.abs(high[1:] - pc), np.abs(low[1:] - pc)))
    return tr


def _wilder(x: np.ndarray, n: int, start: int) -> np.ndarray:
    """Wilder smoothing seeded with the mean of x[start:start+n] (first value at start+n-1)."""
    out = _nan_like(x)
    first = start + n - 1
    if len(x) <= first:
        return out
    out[first] = x[start:start + n].mean()
    for i in range(first + 1, len(x)):
        out[i] = (out[i - 1] * (n - 1) + x[i]) / n
    return out


def atr(high, low, close, n: int) -> np.ndarray:
    """Wilder ATR: seed = mean of the first n true ranges (valid from index n-1)."""
    _check_n(n)
    return _wilder(true_range(high, low, close), n, 0)


def rsi(x, n: int) -> np.ndarray:
    """Wilder RSI (valid from index n). 100 if there were no losses, 50 if no movement at all."""
    _check_n(n)
    x = _arr(x)
    out = _nan_like(x)
    if len(x) <= n:
        return out
    d = np.zeros_like(x)
    d[1:] = np.diff(x)
    gain = _wilder(np.maximum(d, 0.0), n, 1)
    loss = _wilder(np.maximum(-d, 0.0), n, 1)
    for i in range(n, len(x)):
        g, l = gain[i], loss[i]
        out[i] = 50.0 if g == 0 and l == 0 else 100.0 if l == 0 else 100.0 - 100.0 / (1.0 + g / l)
    return out


def roc(x, n: int) -> np.ndarray:
    """Rate of change as a fraction: x[i] / x[i-n] - 1."""
    _check_n(n)
    x = _arr(x)
    out = _nan_like(x)
    if len(x) > n:
        out[n:] = x[n:] / x[:-n] - 1.0
    return out


def rolling_median(x, n: int) -> np.ndarray:
    """Median of the n values ending at i (includes x[i])."""
    _check_n(n)
    return _rolling(_arr(x), n, np.median)


def donchian_high(high, n: int) -> np.ndarray:
    """Highest high of the n bars BEFORE i (excludes the current bar; valid from index n)."""
    _check_n(n)
    high = _arr(high)
    out = _nan_like(high)
    if len(high) > n:
        out[n:] = sliding_window_view(high, n).max(axis=1)[:-1]
    return out


def donchian_low(low, n: int) -> np.ndarray:
    """Lowest low of the n bars BEFORE i (excludes the current bar; valid from index n)."""
    _check_n(n)
    low = _arr(low)
    out = _nan_like(low)
    if len(low) > n:
        out[n:] = sliding_window_view(low, n).min(axis=1)[:-1]
    return out


def efficiency_ratio(x, n: int) -> np.ndarray:
    """Kaufman ER = |x[i]-x[i-n]| / sum(|x[k]-x[k-1]|, k=i-n+1..i), in [0,1]; 0 if flat. Valid from n."""
    _check_n(n)
    x = _arr(x)
    out = _nan_like(x)
    if len(x) > n:
        path = sliding_window_view(np.abs(np.diff(x)), n).sum(axis=1)
        net = np.abs(x[n:] - x[:-n])
        out[n:] = np.divide(net, path, out=np.zeros_like(net), where=path > 0)
    return out


def zscore(x, n: int) -> np.ndarray:
    """(x[i] - mean) / population std over the n values ending at i; NaN where std is 0."""
    _check_n(n)
    x = _arr(x)
    mean = _rolling(x, n, np.mean)
    std = _rolling(x, n, np.std)
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (x - mean) / std
    z[~(std > 0)] = np.nan
    return z
