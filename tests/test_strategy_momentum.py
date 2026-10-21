from dataclasses import replace
from decimal import Decimal

import pytest
from test_strategy_helpers import breakout_series, make_bars, market, wobble

from wallexbot.data.candles import Candle
from wallexbot.strategy.config import MomentumParams
from wallexbot.strategy.contract import Signal
from wallexbot.strategy.features import compute_features
from wallexbot.strategy.momentum import MomentumSignalEngine

P = MomentumParams()
ENGINE = MomentumSignalEngine(P)
RULES = ["breakout", "trend", "roc_accel", "roc_positive", "roc_z", "volume", "atr_band",
         "not_late_breakout", "not_late_rsi", "not_late_advance", "cost_gate", "min_score"]


def failed(bars, params=P, mkt=None):
    """Names of failed rules except min_score (the score also drops whenever a margin shrinks)."""
    d = MomentumSignalEngine(params).diagnose("BTCUSDT", bars, mkt or market())
    assert d is not None
    return [n for n in d.failed if n != "min_score"]


def evaluate(bars, params=P, mkt=None):
    return MomentumSignalEngine(params).evaluate("BTCUSDT", bars, mkt or market())


def test_positive_series_fires_with_full_explanation():
    bars = breakout_series()
    sig = evaluate(bars)
    assert isinstance(sig, Signal)
    last = bars[-1]
    assert sig.symbol == "BTCUSDT" and sig.bar_ts == last.ts and sig.entry_ref_price == last.close
    assert sig.breakout_level == max(c.high for c in bars[-21:-1]) and sig.entry_ref_price > sig.breakout_level
    assert all(isinstance(v, Decimal) for v in (sig.entry_ref_price, sig.breakout_level, sig.atr,
                                                sig.stop_price, sig.score))
    # stop = close - 1.5*ATR, rounded down to the 2dp price tick
    assert sig.stop_price == (sig.entry_ref_price - Decimal("1.5") * sig.atr).quantize(Decimal("0.01"), "ROUND_DOWN")
    assert 0 < sig.stop_price < sig.entry_ref_price
    assert Decimal("0.6") <= sig.score <= 1
    assert set(RULES) | {"regime"} == set(sig.reasons)
    assert sig.reasons["volume"].startswith("volume/median48 3.000 >= 1.8")
    assert sig.reasons["regime"].startswith("favourable")


def test_default_params_are_the_plan_values():
    assert (P.donchian_period, P.ema_period, P.roc_period, P.roc_z_window, P.roc_z_min) == (20, 50, 6, 96, 1.0)
    assert (P.volume_mult, P.vol_median_window, P.atr_period, P.rsi_max) == (1.8, 48, 14, 85.0)
    assert (P.cost_mult, P.expected_move_atr, P.min_score, P.stop_atr_mult) == (3.0, 1.5, 0.6, 1.5)
    assert (P.cooldown_bars, P.cooldown_bars_profitable) == (8, 2)
    with pytest.raises(ValueError):
        MomentumParams(min_bars=50)


# --- each rule blocks on its own -------------------------------------------------------------

def test_breakout_blocks():
    bars = breakout_series()
    old = bars[-10]
    bars[-10] = Candle(old.ts, old.open, Decimal("200"), old.low, old.close, old.volume)  # prior high above close
    assert "breakout" in failed(bars) and evaluate(bars) is None


def test_trend_blocks_when_close_below_ema():
    closes = [130.0 if i < 80 else 100.0 for i in range(120)]  # old highs keep EMA50 above price
    closes = [c * (1 + wobble(i)) for i, c in enumerate(closes)]
    for r in (0.002, 0.003, 0.004, 0.006, 0.010):
        closes.append(closes[-1] * (1 + r))
    vols = [1000.0] * len(closes)
    vols[-1] = 3000.0
    bars = make_bars(closes, vols)
    assert "trend" in failed(bars) and evaluate(bars) is None


def test_roc_accel_blocks():
    bars = breakout_series(rise=(0.004, 0.006, 0.010, 0.012, -0.010))  # last bar gives back part of the move
    assert "roc_accel" in failed(bars) and evaluate(bars) is None


def test_roc_positive_blocks():
    bars = breakout_series(rise=(-0.012, -0.01, -0.005, -0.003, 0.010))
    assert "roc_positive" in failed(bars) and evaluate(bars) is None


@pytest.mark.parametrize("rule,override", [
    ("roc_z", dict(roc_z_min=50.0)),
    ("atr_band", dict(atr_pct_min=0.05, atr_pct_max=0.1)),
    ("atr_band", dict(atr_pct_min=0.0001, atr_pct_max=0.001)),
    ("not_late_breakout", dict(late_atr_mult=0.1)),
    ("not_late_rsi", dict(rsi_max=50.0)),
    ("not_late_advance", dict(max_advance_atr=1.0)),
])
def test_single_rule_blocks_exactly(rule, override):
    params = replace(P, **override)
    assert failed(breakout_series(), params) == [rule]
    assert evaluate(breakout_series(), params) is None


def test_min_score_blocks_alone():
    params = replace(P, min_score=0.99)
    d = MomentumSignalEngine(params).diagnose("X", breakout_series(), market())
    assert d.failed == ["min_score"] and evaluate(breakout_series(), params) is None


def test_volume_blocks():
    bars = breakout_series(spike=1.0)
    assert "volume" in failed(bars) and evaluate(bars) is None


def test_cost_gate_blocks_when_costs_are_high():
    bars = breakout_series()
    assert evaluate(bars, mkt=market(fee="0.001")) is not None            # 30 bps round trip, 3x = 90 < ~124
    high = market(fee="0.0035")                                           # 80 bps round trip, 3x = 240 > ~124
    assert failed(bars, mkt=high) == ["cost_gate"]
    assert evaluate(bars, mkt=high) is None
    assert evaluate(bars, mkt=market(spread="100")) is None               # wide spread alone is enough


# --- regime tightening -----------------------------------------------------------------------

def test_tightened_regime_raises_volume_bar():
    bars = breakout_series(spike=2.0)                                     # 2.0x: above 1.8, below 2.2
    assert evaluate(bars) is not None
    unfavourable = replace(P, er_min=0.9)                                 # efficiency ratio ~0.33 < 0.9
    d = MomentumSignalEngine(unfavourable).diagnose("X", bars, market())
    assert d.regime == "tightened" and "volume" in d.failed
    assert "2.2" in [r for r in d.rules if r.name == "volume"][0].text


def test_tightened_regime_raises_cost_multiple():
    bars = breakout_series()
    mkt = market(fee="0.0015")                                            # 40 bps: 3x = 120 <= ~124 < 4x = 160
    assert evaluate(bars, mkt=mkt) is not None
    assert failed(bars, replace(P, er_min=0.9), mkt) == ["cost_gate"]
    assert evaluate(bars, replace(P, er_min=0.9), mkt) is None


# --- abstention on bad or insufficient data --------------------------------------------------

def test_insufficient_bars_abstain():
    bars = breakout_series()
    assert evaluate(bars[-(P.min_bars - 1):]) is None
    assert evaluate([]) is None


def test_garbage_inputs_return_none():
    good = breakout_series()
    assert evaluate(None) is None
    assert evaluate(["x"] * 150) is None
    assert evaluate([None] * 150) is None
    assert evaluate(good[:-1] + [good[-2]]) is None                        # duplicate / non-increasing ts
    assert MomentumSignalEngine(P).evaluate("BTCUSDT", good, None) is None  # no market context


def test_zero_volume_heavy_and_flat_data_abstain():
    closes = [100.0 * (1 + wobble(i)) for i in range(120)]
    for r in (0.002, 0.003, 0.004, 0.006, 0.010):
        closes.append(closes[-1] * (1 + r))
    assert evaluate(make_bars(closes, [0.0] * 125)) is None
    assert compute_features(make_bars(closes, [0.0] * 125)) is None       # median volume 0
    mostly_zero = [0.0 if i % 2 else 1000.0 for i in range(125)]
    mostly_zero[-1] = 5000.0
    assert evaluate(make_bars(closes, mostly_zero)) is None
    assert evaluate(make_bars([100.0] * 125)) is None                      # no breakout


# --- determinism -----------------------------------------------------------------------------

def test_same_input_same_signal_and_id():
    a = evaluate(breakout_series())
    b = MomentumSignalEngine(MomentumParams()).evaluate("BTCUSDT", breakout_series(), market())
    assert a is not None and a == b and a.signal_id == b.signal_id and a.signal_id.startswith("sig_")
    longer = breakout_series(n_flat=121)
    assert evaluate(longer) is None or evaluate(longer).signal_id != a.signal_id


def test_trailing_window_makes_result_independent_of_older_history():
    bars = breakout_series(n_flat=400)
    assert evaluate(bars) == evaluate(bars[-P.history_bars:])


# --- features --------------------------------------------------------------------------------

def test_features_at_last_bar():
    bars = breakout_series()
    f = compute_features(bars)
    assert f is not None and f.bar_ts == bars[-1].ts and f.close == float(bars[-1].close)
    assert f.donchian_high == float(max(c.high for c in bars[-21:-1]))
    assert f.roc == pytest.approx(float(bars[-1].close / bars[-7].close - 1))
    assert f.roc_prev == pytest.approx(float(bars[-2].close / bars[-8].close - 1))
    import statistics
    prior = [float(c.volume) for c in bars[-49:-1]]
    assert f.vol_ratio == pytest.approx(float(bars[-1].volume) / statistics.median(prior))
    assert f.advance_from_low == pytest.approx(float(bars[-1].close - min(c.low for c in bars[-24:])))
    assert compute_features(bars[:50]) is None
