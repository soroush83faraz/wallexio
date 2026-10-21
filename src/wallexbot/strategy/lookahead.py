"""Look-ahead check for signal engines.

Structural part: at every cut point t the engine is handed `tuple(bars[:t])`, a fresh immutable
tuple holding ONLY the bars up to the cut (the backtest engine does the same, see
`backtest.engine.bars_window`). Nothing past the cut is reachable from the object the engine gets,
by index, slice, iteration or attribute, so an engine cannot read future bars through its input.

Behavioural part (what the structure cannot rule out: state kept across calls). Per cut t:
1. `clean` = evaluate(bars[:t]);
2. the engine is then shown the cut series continued by WILD future bars (the real bars after t,
   rescaled at random), a legitimate later call;
3. evaluate(bars[:t]) again must equal `clean`: an engine whose decision at t moved after it saw
   later data carries information backwards through state or caching;
4. a further repeat must equal `clean` too (determinism).
"""
from __future__ import annotations

import random
from decimal import Decimal
from typing import Sequence

from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import MarketContext, SignalEngine


def _perturb(c: Candle, rng: random.Random) -> Candle:
    k = Decimal(rng.choice(["0.1", "0.5", "3", "10"]))
    return Candle(c.ts, c.open * k, c.high * k * 2, c.low * k / 2, c.close * k, c.volume * k + 1, c.missing)


def check_no_lookahead(engine: SignalEngine, bars: Sequence[Candle], market: MarketContext,
                       sample_points: int | Sequence[int] = 25, seed: int = 0, symbol: str = "TESTUSDT") -> int:
    """Raise AssertionError on the first look-ahead/determinism violation. `sample_points` is a count
    of random cut points or an explicit list of cut lengths t (evaluate on bars[:t]). Returns the
    number of cut points that produced a Signal, so callers can assert the check was not vacuous
    (pick cuts with t < len(bars) to also exercise the wild-future step)."""
    rng = random.Random(seed)
    series = tuple(bars)
    cuts = (sorted(rng.sample(range(1, len(series) + 1), min(sample_points, len(series))))
            if isinstance(sample_points, int) else list(sample_points))
    signals = 0
    for t in cuts:
        clean = engine.evaluate(symbol, series[:t], market)
        future = tuple(_perturb(c, rng) for c in series[t:])
        if future:
            engine.evaluate(symbol, series[:t] + future, market)
        after = engine.evaluate(symbol, series[:t], market)
        if clean != after:
            raise AssertionError(f"look-ahead at cut {t}: {clean} != {after} after the engine saw later bars")
        if clean != engine.evaluate(symbol, series[:t], market):
            raise AssertionError(f"non-deterministic at cut {t}")
        signals += clean is not None
    return signals
