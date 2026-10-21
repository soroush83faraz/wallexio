"""S0 baseline engine and the look-ahead checker (applied to both engines)."""
import pytest
from test_strategy_helpers import breakout_series, make_bars, market

from wallexbot.strategy.lookahead import check_no_lookahead
from wallexbot.strategy.momentum import MomentumSignalEngine
from wallexbot.strategy.trend_baseline import TrendBaselineEngine

BASE = TrendBaselineEngine()


def uptrend(n=80, drift=0.003):
    return make_bars([100.0 * (1 + drift) ** i for i in range(n)])


def test_baseline_fires_on_steady_uptrend():
    bars = uptrend()
    sig = BASE.evaluate("BTCUSDT", bars, market())
    assert sig is not None
    assert sig.entry_ref_price == bars[-1].close and sig.entry_ref_price > sig.breakout_level
    assert 0 < sig.stop_price < sig.entry_ref_price
    assert sig.strategy_version == "s0-0.1" and set(sig.reasons) == {"trend", "sma_cross", "breakout"}
    # stop = close - 2.5 ATR (2dp tick, rounded down)
    assert sig.stop_price <= sig.entry_ref_price - 2 * sig.atr


@pytest.mark.parametrize("bars", [
    make_bars([100.0 * 0.997 ** i for i in range(80)]),     # downtrend
    make_bars([100.0 + (i % 2) for i in range(80)]),        # chop: no breakout/trend
    uptrend(40),                                            # too few bars
    [],
])
def test_baseline_abstains(bars):
    assert BASE.evaluate("BTCUSDT", bars, market()) is None


def test_baseline_garbage_returns_none():
    assert BASE.evaluate("BTCUSDT", ["x"] * 100, market()) is None
    assert BASE.evaluate("BTCUSDT", None, market()) is None


def test_baseline_is_deterministic():
    assert BASE.evaluate("A", uptrend(), market()) == BASE.evaluate("A", uptrend(), market())


def test_lookahead_momentum():
    bars = breakout_series()
    n = check_no_lookahead(MomentumSignalEngine(), bars, market(), [len(bars), len(bars) - 1, 60, 121])
    assert n == 1                      # fired at the full series only: the check is not vacuous
    check_no_lookahead(MomentumSignalEngine(), bars, market(), sample_points=30, seed=3)


def test_lookahead_baseline():
    bars = uptrend(120)
    n = check_no_lookahead(BASE, bars, market(), list(range(60, 121)))
    assert n > 0
    check_no_lookahead(BASE, bars, market(), sample_points=30, seed=5)


def _continued(bars, n=40, drift=0.004):
    """`bars` followed by n more bars that keep trending (the cut series' real future)."""
    closes = [float(bars[-1].close) * (1 + drift) ** (i + 1) for i in range(n)]
    tail = make_bars(closes, start=float(bars[-1].close))
    step = bars[1].ts - bars[0].ts
    return list(bars) + [type(c)(bars[-1].ts + (i + 1) * step, c.open, c.high, c.low, c.close, c.volume)
                         for i, c in enumerate(tail)]


def test_lookahead_momentum_is_not_vacuous_with_a_future_present():
    """The verifier found S1 never fired at a cut that HAD future bars, so the wild-future step was
    never exercised on a signal. Here the breakout bar is followed by 40 real bars; S1 must fire at
    the cut and the check must survive the engine seeing wild futures in between."""
    base = breakout_series()
    bars = _continued(base)
    t = len(base)                                      # cut right after the breakout bar
    assert t < len(bars)
    n = check_no_lookahead(MomentumSignalEngine(), bars, market(), [t, t - 1, t + 1], seed=11)
    assert n >= 1


def test_checker_hands_only_bars_up_to_the_cut():
    seen = []

    class Spy:
        def evaluate(self, symbol, bars, market):
            assert isinstance(bars, tuple)
            for attr in ("_items", "_n", "_bars"):
                assert not hasattr(bars, attr)
            seen.append((len(bars), bars[-1].ts))
            return None

    bars = uptrend(30)
    check_no_lookahead(Spy(), bars, market(), [10])
    # clean call, then the wild-future continuation (a later, longer call), then the re-checks
    assert seen == [(10, bars[9].ts), (30, bars[29].ts), (10, bars[9].ts), (10, bars[9].ts)]


def test_checker_catches_state_carried_back_from_later_bars():
    class Hindsight:
        """Signals at bar t only if it has EVER seen a close far above bars[-1] (a cache of the
        series max): its decision at t changes after it is shown later bars."""
        top = 0

        def evaluate(self, symbol, bars, market):
            self.top = max(self.top, max(float(b.close) for b in bars))
            return BASE.evaluate(symbol, bars, market) if self.top < 2 * float(bars[-1].close) else None

    bars = uptrend(120)
    with pytest.raises(AssertionError, match="look-ahead"):
        check_no_lookahead(Hindsight(), bars, market(), [80])


def test_checker_catches_a_stateful_engine():
    class Peeker:
        """Output depends on call count, like an engine caching data across calls."""
        calls = 0

        def evaluate(self, symbol, bars, market):
            self.calls += 1
            return BASE.evaluate(symbol, bars, market) if self.calls % 2 else None

    with pytest.raises(AssertionError):
        check_no_lookahead(Peeker(), uptrend(), market(), [80])
