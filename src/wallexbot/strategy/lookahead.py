"""Look-ahead check for signal engines. At each cut point t the engine is evaluated on bars[:t] and,
again, on the same t bars viewed through a series whose FUTURE bars (index >= t) were replaced by
wild values. The two Signals must be identical, and a repeat call must reproduce the first.

The perturbed view hides everything at index >= t (len, indexing, slicing, iteration), so an engine
that reaches past the cut by any route sees an error (which makes it abstain, a mismatch if the
clean run signalled) or, through shared state/caching, the perturbed values."""
from __future__ import annotations

import random
from decimal import Decimal
from typing import Sequence

from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import MarketContext, SignalEngine


class _PrefixView(Sequence):
    """First `n` items of `items`; everything past n does not exist."""

    def __init__(self, items: Sequence[Candle], n: int):
        self._items, self._n = items, n

    def __len__(self) -> int:
        return self._n

    def __getitem__(self, i):
        if isinstance(i, slice):
            return [self._items[j] for j in range(*i.indices(self._n))]
        if not -self._n <= i < self._n:
            raise IndexError(i)
        return self._items[i if i >= 0 else self._n + i]


def _perturb(c: Candle, rng: random.Random) -> Candle:
    k = Decimal(rng.choice(["0.1", "0.5", "3", "10"]))
    return Candle(c.ts, c.open * k, c.high * k * 2, c.low * k / 2, c.close * k, c.volume * k + 1, c.missing)


def check_no_lookahead(engine: SignalEngine, bars: Sequence[Candle], market: MarketContext,
                       sample_points: int | Sequence[int] = 25, seed: int = 0, symbol: str = "TESTUSDT") -> int:
    """Raise AssertionError on the first look-ahead/determinism violation. `sample_points` is a count
    of random cut points or an explicit list of cut lengths t (evaluate on bars[:t]). Returns the
    number of cut points that produced a Signal, so callers can assert the check was not vacuous."""
    rng = random.Random(seed)
    cuts = (sorted(rng.sample(range(1, len(bars) + 1), min(sample_points, len(bars))))
            if isinstance(sample_points, int) else list(sample_points))
    signals = 0
    for t in cuts:
        clean = engine.evaluate(symbol, bars[:t], market)
        future = [_perturb(c, rng) for c in bars[t:]]
        view = _PrefixView(list(bars[:t]) + future, t)
        perturbed = engine.evaluate(symbol, view, market)
        if clean != perturbed:
            raise AssertionError(f"look-ahead at cut {t}: {clean} != {perturbed}")
        if clean != engine.evaluate(symbol, bars[:t], market):
            raise AssertionError(f"non-deterministic at cut {t}")
        signals += clean is not None
    return signals
