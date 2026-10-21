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


def test_checker_catches_a_stateful_engine():
    class Peeker:
        """Output depends on call count, like an engine caching data across calls."""
        calls = 0

        def evaluate(self, symbol, bars, market):
            self.calls += 1
            return BASE.evaluate(symbol, bars, market) if self.calls % 2 else None

    with pytest.raises(AssertionError):
        check_no_lookahead(Peeker(), uptrend(), market(), [80])
