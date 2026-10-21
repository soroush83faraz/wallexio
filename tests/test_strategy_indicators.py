"""Indicators against hand-computed values; NaN warm-up and alignment."""
import math

import numpy as np
import pytest

from wallexbot.strategy import indicators as ind

NAN = math.nan


def eq(actual, expected):
    actual = np.asarray(actual, dtype=float)
    assert actual.shape == (len(expected),)
    np.testing.assert_allclose(actual, np.asarray(expected, dtype=float), rtol=1e-12, atol=1e-12)


def test_sma():
    eq(ind.sma([1, 2, 3, 4, 5], 3), [NAN, NAN, 2, 3, 4])


def test_ema_seeded_with_sma():
    # alpha = 0.5; seed = mean(2,4,6) = 4; then 0.5*5+0.5*4 = 4.5; 0.5*3+0.5*4.5 = 3.75
    eq(ind.ema([2, 4, 6, 5, 3], 3), [NAN, NAN, 4, 4.5, 3.75])


def test_true_range():
    # bar0: h-l = 2; bar1: max(3, |12-9|, |9-9|) = 3; bar2: max(2, |11-11|, |9-11|) = 2
    eq(ind.true_range([10, 12, 11], [8, 9, 9], [9, 11, 10]), [2, 3, 2])


def test_atr_wilder():
    # seed = mean(2,3) = 2.5; next = (2.5*1 + 2)/2 = 2.25
    eq(ind.atr([10, 12, 11], [8, 9, 9], [9, 11, 10], 2), [NAN, 2.5, 2.25])


def test_rsi_wilder():
    # diffs after bar0: +1 +1 -1 0. i=2: gain 1, loss 0 -> 100. i=3: gain .5, loss .5 -> 50. i=4: .25/.25 -> 50
    eq(ind.rsi([1, 2, 3, 2, 2], 2), [NAN, NAN, 100, 50, 50])


def test_rsi_flat_is_50():
    eq(ind.rsi([5, 5, 5, 5], 2), [NAN, NAN, 50, 50])


def test_roc():
    eq(ind.roc([100, 110, 121, 99], 1), [NAN, 0.1, 0.1, 99 / 121 - 1])
    eq(ind.roc([100, 110, 121, 99], 2), [NAN, NAN, 0.21, -0.1])


def test_rolling_median():
    eq(ind.rolling_median([1, 5, 2, 8], 3), [NAN, NAN, 2, 5])


def test_donchian_excludes_current_bar():
    # i=2: max(1,3)=3 (the current 2 is excluded); i=3: max(3,2)=3; i=4: max(2,5)=5
    eq(ind.donchian_high([1, 3, 2, 5, 4], 2), [NAN, NAN, 3, 3, 5])
    eq(ind.donchian_low([5, 3, 4, 1, 2], 2), [NAN, NAN, 3, 3, 1])


def test_efficiency_ratio():
    # i=2: net 3 / path (1+2) = 1; i=3: net 1 / path (2+1) = 1/3; i=4: net 2 / path (1+3) = .5
    eq(ind.efficiency_ratio([1, 2, 4, 3, 6], 2), [NAN, NAN, 1, 1 / 3, 0.5])
    eq(ind.efficiency_ratio([5, 5, 5, 5], 2), [NAN, NAN, 0, 0])


def test_zscore_population_std():
    eq(ind.zscore([1, 2, 3], 3), [NAN, NAN, 1 / math.sqrt(2 / 3)])
    eq(ind.zscore([4, 4, 4], 3), [NAN, NAN, NAN])  # zero variance


@pytest.mark.parametrize("fn", [lambda x: ind.sma(x, 5), lambda x: ind.ema(x, 5), lambda x: ind.roc(x, 5),
                                lambda x: ind.rsi(x, 5), lambda x: ind.zscore(x, 5),
                                lambda x: ind.donchian_high(x, 5), lambda x: ind.efficiency_ratio(x, 5)])
def test_series_shorter_than_period_is_all_nan(fn):
    assert np.isnan(fn([1.0, 2.0, 3.0])).all()


def test_no_lookahead_in_indicators():
    """Value at i must not change when later data changes."""
    rng = np.random.default_rng(1)
    x = rng.normal(100, 1, 80)
    y = x.copy()
    y[50:] += 1000
    for fn in (lambda a: ind.sma(a, 10), lambda a: ind.ema(a, 10), lambda a: ind.rsi(a, 10),
               lambda a: ind.roc(a, 5), lambda a: ind.zscore(a, 10), lambda a: ind.donchian_high(a, 10),
               lambda a: ind.efficiency_ratio(a, 10), lambda a: ind.rolling_median(a, 10),
               lambda a: ind.atr(a + 1, a - 1, a, 10)):
        np.testing.assert_array_equal(fn(x)[:50], fn(y)[:50])


def test_bad_period_rejected():
    with pytest.raises(ValueError):
        ind.sma([1, 2, 3], 0)
