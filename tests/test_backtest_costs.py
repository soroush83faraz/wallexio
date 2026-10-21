from decimal import Decimal

import pytest

from wallexbot.backtest.costs import CostModel

D = Decimal
CM = CostModel(taker_rate=D("0.001"), maker_rate=D("0.001"), half_spread_bps=D("10"), impact_k=D("0.1"),
               impact_cap_bps=D("50"))


def test_buy_with_impact_capped():
    # notional 1000 / bar 10000 = 0.1 participation -> 0.1 * 0.1 = 1% impact, capped at 50 bps
    f = CM.fill("BUY", D("100"), D("10"), D("10000"))
    assert f.price == D("100.6")                 # 100 * (1 + 0.001 + 0.005)
    assert f.notional == D("1006")
    assert f.fee == D("1.006")
    assert f.spread_cost == D("1")               # 10 * 100 * 0.001
    assert f.impact_cost == D("5")               # 6 adverse - 1 spread


def test_buy_uncapped_and_sell():
    f = CM.fill("BUY", D("100"), D("10"), D("100000"))   # 1% participation -> 10 bps impact
    assert (f.price, f.fee, f.spread_cost, f.impact_cost) == (D("100.2"), D("1.002"), D("1"), D("1"))
    s = CM.fill("SELL", D("110"), D("10"), D("100000"))  # 1100/100000 = 1.1% -> 11 bps
    assert s.price == D("109.769")                       # 110 * (1 - 0.001 - 0.0011)
    assert s.notional == D("1097.69")
    assert s.fee == D("1.09769")
    assert s.spread_cost == D("1.1")
    assert s.impact_cost == D("1.21")


def test_missing_liquidity_pays_cap_and_rounding_is_adverse():
    assert CM.impact_fraction(D("1"), None) == D("0.005")
    assert CM.impact_fraction(D("1"), D("0")) == D("0.005")
    b = CM.fill("BUY", D("100.003"), D("1"), D("1e12"), price_precision=2)    # raw 100.10310... -> up
    s = CM.fill("SELL", D("100.003"), D("1"), D("1e12"), price_precision=2)   # raw 99.90289... -> down
    assert (b.price, s.price) == (D("100.11"), D("99.90"))
    # rounding is booked as impact so attribution stays exact
    assert b.spread_cost + b.impact_cost == b.price - D("100.003")
    assert s.spread_cost + s.impact_cost == D("100.003") - s.price


def test_market_context_and_validation():
    mc = CostModel().market_context()
    assert (mc.spread_bps, mc.est_slippage_bps, mc.taker_fee_rate) == (D("15.0"), D("50"), D("0.0035"))
    with pytest.raises(ValueError):
        CostModel(taker_rate=D("-0.1"))
    with pytest.raises(ValueError):
        CM.fill("HOLD", D("1"), D("1"), None)
