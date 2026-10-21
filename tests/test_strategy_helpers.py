"""Synthetic bar builders shared by the strategy tests (no tests of its own)."""
from decimal import Decimal

from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import MarketContext

T0 = 1_700_000_000
STEP = 900  # 15m


def market(fee="0.001", spread="5", slip="5"):
    """Round trip = 2*fee*1e4 + spread + slip bps (default 30 bps)."""
    return MarketContext(spread_bps=Decimal(spread), est_slippage_bps=Decimal(slip), taker_fee_rate=Decimal(fee))


def make_bars(closes, volumes=None, wick=0.002, start=100.0):
    """Candles from a close path: open = previous close, symmetric wicks, 2dp prices."""
    out, prev = [], start
    for i, c in enumerate(closes):
        o = prev
        hi, lo = max(o, c) * (1 + wick), min(o, c) * (1 - wick)
        v = 1000.0 if volumes is None else volumes[i]
        out.append(Candle(T0 + i * STEP, Decimal(f"{o:.2f}"), Decimal(f"{hi:.2f}"), Decimal(f"{lo:.2f}"),
                          Decimal(f"{c:.2f}"), Decimal(f"{v:.2f}")))
        prev = c
    return out


def wobble(i):
    """Deterministic, mean-reverting noise in about [-0.6, 0.6] percent."""
    return 0.006 * (0.6 * ((i * 7) % 11 - 5) / 5 + 0.4 * ((i * 13) % 7 - 3) / 3)


def breakout_series(n_flat=120, rise=(0.002, 0.003, 0.004, 0.006, 0.010), spike=3.0):
    """Range-bound noise for n_flat bars, then len(rise) accelerating up bars on a volume spike.
    The last bar is the breakout bar."""
    closes = [100.0 * (1 + wobble(i)) for i in range(n_flat)]
    for r in rise:
        closes.append(closes[-1] * (1 + r))
    vols = [1000.0 * (1 + 0.1 * ((i * 5) % 7 - 3) / 3) for i in range(len(closes))]
    vols[-1] = 1000.0 * spike
    return make_bars(closes, vols)
