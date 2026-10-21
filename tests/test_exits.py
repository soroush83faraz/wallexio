from decimal import Decimal

import pytest

from wallexbot.data.candles import Candle
from wallexbot.strategy.contract import ExitPolicy, PositionState
from wallexbot.strategy.exits import (ChandelierTrail, ExitParams, FixedRR, Hybrid, MomentumFailure, TakeProfit,
                                      atr, ema, make_policy, momentum_failure)

D = Decimal


def c(i, o, h, l, cl, v="100"):
    return Candle(i * 900, D(o), D(h), D(l), D(cl), D(v))


def rising(n, start=100, v="100"):
    """Closes rise by 1 per bar; every bar has high-low = 2 and no gaps -> true range 2."""
    return [c(i, start + i - 0.5, start + i + 1, start + i - 1, start + i, v) for i in range(n)]


def st(**kw):
    base = dict(symbol="AAAUSDT", entry_price=D("100"), entry_bar_ts=0, quantity=D("10"), initial_stop=D("90"),
                breakout_level=D("99"), highest_high_since_entry=D("100"), bars_held=3, partial_taken=False,
                current_stop=D("90"))
    base.update(kw)
    return PositionState(**base)


def test_atr_and_ema_hand_computed():
    bars = [c(0, 10, 10, 10, 10), c(1, 10, 12, 9, 11), c(2, 11, 11.5, 10.5, 11), c(3, 11, 14, 11, 13)]
    # true ranges 3, 1, 3; Wilder(2): seed (3+1)/2 = 2, then (2*1 + 3)/2 = 2.5
    assert atr(bars, 2, window_mult=10) == D("2.5")
    assert atr(bars[:2], 2) is None
    assert atr(rising(40), 14) == D(2)
    # EMA(3) of 1..5: seed 2, alpha 0.5 -> 3 -> 4
    assert ema([D(x) for x in (1, 2, 3, 4, 5)], 3) == D(4)
    assert ema([D(1)], 3) is None


def test_policies_satisfy_protocol():
    for name in ("E1", "E2", "E3", "E4"):
        p = make_policy(name)
        assert isinstance(p, (FixedRR, ChandelierTrail, MomentumFailure, Hybrid))
        _: ExitPolicy = p
        assert p.on_bar(st(), rising(30)).action in ("hold", "exit_all", "update_stop")


def test_e1_fixed_rr_target_and_hold():
    p = FixedRR()
    assert p.targets(st()) == (TakeProfit(D("115.0"), D(1), "take_profit"),)   # 100 + 1.5 * 10
    assert p.on_bar(st(), rising(30)).action == "hold"
    assert p.targets(st(initial_stop=D("100"))) == ()                         # R <= 0: no target


def test_e2_chandelier_activation_and_ratchet():
    p = ChandelierTrail()
    bars = rising(40)
    assert p.on_bar(st(highest_high_since_entry=D("109.99")), bars).action == "hold"   # < +1R
    d = p.on_bar(st(highest_high_since_entry=D("112")), bars)
    assert (d.action, d.new_stop) == ("update_stop", D("108"))                     # 112 - 2 * ATR(2)
    assert p.on_bar(st(highest_high_since_entry=D("112"), current_stop=D("109")), bars).action == "hold"


def test_e3_momentum_failure_rules():
    healthy = rising(30)
    assert momentum_failure(healthy, ExitParams()) is None
    assert MomentumFailure().on_bar(st(), healthy).action == "hold"
    # close below EMA9
    ema_fail = healthy + [c(30, 129, 129.5, 110, 111)]
    d = MomentumFailure().on_bar(st(), ema_fail)
    assert (d.action, d.reason) == ("exit_all", "momentum_fail_ema")
    # above EMA9 (~125) but below the close 3 bars earlier (127): ROC < 0
    roc_fail = healthy + [c(30, 128, 128.5, 126, 126.5)]
    assert ema([b.close for b in roc_fail[-45:]], 9) < D("126.5")
    assert momentum_failure(roc_fail, ExitParams()) == "momentum_fail_roc"
    # two consecutive bars with volume < 0.5 x median of the previous 20
    dry = rising(30, v="100")
    dry[-2] = c(28, 127.5, 129, 127, 128, "40")
    dry[-1] = c(29, 128.5, 130, 128, 129, "40")
    assert momentum_failure(dry, ExitParams()) == "momentum_fail_volume_dry"
    one_dry = rising(30)
    one_dry[-1] = c(29, 128.5, 130, 128, 129, "40")
    assert momentum_failure(one_dry, ExitParams()) is None


def test_e4_partial_target_then_breakeven_plus_fees_then_trail():
    p = Hybrid()
    bars = rising(40)
    assert p.targets(st()) == (TakeProfit(D("110"), D("0.5"), "partial_take_profit"),)
    assert p.targets(st(partial_taken=True)) == ()
    # before the partial: nothing to do on a healthy bar
    assert p.on_bar(st(), bars).action == "hold"
    # after the partial, chandelier (101.5 - 4 = 97.5) is below breakeven+fees -> breakeven+fees
    d = p.on_bar(st(partial_taken=True, highest_high_since_entry=D("101.5")), bars)
    assert d.action == "update_stop" and d.reason == "breakeven_plus_fees"
    assert d.new_stop == D("100") * D("1.0035") / D("0.9965")
    # a sale at that stop, net of a 0.35% fee, repays the buy incl. its 0.35% fee
    assert d.new_stop * (1 - D("0.0035")) >= D("100") * (1 + D("0.0035")) - D("1e-20")
    # chandelier above breakeven -> trail
    d = p.on_bar(st(partial_taken=True, highest_high_since_entry=D("111")), bars)
    assert (d.action, d.new_stop, d.reason) == ("update_stop", D("107"), "chandelier_trail")


def test_e4_false_breakout_time_stop_and_failure():
    p = Hybrid()
    bars = rising(40)                       # last close 139
    fb = st(bars_held=2, breakout_level=D("139.5"))
    assert p.on_bar(fb, bars).reason == "false_breakout"
    assert p.on_bar(st(bars_held=3, breakout_level=D("139.5")), bars).action == "hold"
    assert p.on_bar(st(bars_held=16), bars).reason == "time_stop"
    assert p.on_bar(st(bars_held=15), bars).action == "hold"
    # a trade that already paid +1R is left to the trailing stop
    assert p.on_bar(st(bars_held=16, partial_taken=True), bars).action == "update_stop"
    assert Hybrid(ExitParams(time_stop_only_without_partial=False)).on_bar(
        st(bars_held=16, partial_taken=True), bars).reason == "time_stop"
    fail = bars + [c(40, 139, 139.5, 120, 121)]
    assert p.on_bar(st(), fail).reason == "momentum_fail_ema"


def test_params_validation():
    with pytest.raises(ValueError):
        ExitParams(partial_fraction=D("1"))
    with pytest.raises(ValueError):
        ExitParams(atr_period=0)
