import random
from decimal import Decimal

import pytest

from wallexbot.config import RiskConfig
from wallexbot.strategy.sizing import size_position
from wallexbot.types import SymbolRules, round_down

D = Decimal
RULES = SymbolRules("AAAUSDT", "AAA", "USDT", amount_precision=4, price_precision=2)


def risk(**kw):
    base = dict(risk_per_trade_pct=D("1"), max_position_pct=D("25"), max_exposure_pct=D("60"),
                max_open_positions=3, max_order_value=D("100000"), daily_loss_soft_pct=D("4"),
                daily_loss_hard_pct=D("6"), drawdown_soft_pct=D("8"), drawdown_hard_pct=D("12"),
                consecutive_loss_cooldown=3, min_notional=D("5"))
    base.update(kw)
    return RiskConfig(**base)


def size(equity="10000", entry="100", stop="95", cash="100000", fee="0", **kw):
    r = kw.pop("risk", risk())
    return size_position(D(equity), D(entry), D(stop), kw.pop("rules", RULES), r, D(cash), D(fee), **kw)


def test_risk_formula_hand_computed():
    # 10000 * 1% = 100 at risk; 100 / (100 - 95) = 20 units; notional 2000 <= 25% of 10000
    r = size()
    assert (r.qty, r.notional, r.binding, r.risk_budget) == (D("20"), D("2000"), "risk", D("100"))
    assert r.ok and r.reasons == ()


def test_low_score_halves_risk():
    r = size(score=D("0.69"))
    assert r.qty == D("10") and "low_score_half_risk" in r.reasons
    assert size(score=D("0.7")).qty == D("20")


def test_caps_hand_computed():
    assert size(risk=risk(max_order_value=D("1000"))).qty == D("10")              # 1000 / 100
    assert size(stop="99").qty == D("25")                                          # 25% of 10000 / 100
    # cash 500 with a 0.35% fee reserve: 500 / 1.0035 / 100 = 4.98256... -> 4.9825 (rounded down)
    r = size(cash="500", fee="0.0035")
    assert r.qty == D("4.9825") and r.binding == "cash"
    assert r.notional * (1 + D("0.0035")) <= D("500")
    assert size(depth_notional=D("5000")).qty == D("10")                           # 5000 / 5 / 100
    assert size(exposure_headroom=D("700")).qty == D("7")


def test_zero_with_reasons():
    assert size(stop="100").reasons[-1] == "stop_not_below_entry"
    assert size(stop="101").qty == 0
    assert size(equity="0").reasons == ("non_positive_equity",)
    # 10000 * 1% / 5 = 20 units, but max order 4 quote -> 0.04 units -> notional 4 < min 5
    r = size(risk=risk(max_order_value=D("4")))
    assert r.qty == 0 and "below_min_notional" in r.reasons
    # rules min_notional larger than config: the larger one applies
    rules = SymbolRules("AAAUSDT", "AAA", "USDT", 4, 2, min_notional=D("2500"))
    assert size(rules=rules).reasons[-1] == "below_min_notional"
    assert size(cash="0").reasons[-1] == "zero_after_caps_and_precision"


def test_property_random_inputs_never_exceed_any_cap():
    rng = random.Random(20261022)
    for _ in range(3000):
        prec = rng.randint(0, 8)
        rules = SymbolRules("X", "X", "USDT", prec, 2, min_notional=None)
        rc = risk(risk_per_trade_pct=D(rng.choice(["0.5", "1", "2", "3.3"])),
                  max_position_pct=D(rng.choice(["5", "25", "100"])),
                  max_order_value=D(str(rng.randint(1, 5000))), min_notional=D(rng.choice(["0", "5"])))
        equity = D(str(round(rng.uniform(1, 1e6), 2)))
        entry = D(str(round(rng.uniform(0.0001, 70000), 4))) + D("0.0001")
        stop = round_down(entry * D(str(rng.uniform(0.5, 0.9999))), 6)
        cash = D(str(round(rng.uniform(0, 2e5), 2)))
        fee = D(rng.choice(["0", "0.001", "0.0035"]))
        depth = D(str(round(rng.uniform(0, 1e6), 2))) if rng.random() < 0.5 else None
        head = D(str(round(rng.uniform(-10, 1e5), 2))) if rng.random() < 0.3 else None
        score = D(str(round(rng.random(), 2))) if rng.random() < 0.5 else None
        r = size_position(equity, entry, stop, rules, rc, cash, fee, depth, score, exposure_headroom=head)
        if stop <= 0:
            assert r.qty == 0
            continue
        pct = rc.risk_per_trade_pct / 2 if score is not None and score < D("0.7") else rc.risk_per_trade_pct
        budget = equity * pct / 100
        assert r.qty >= 0
        assert round_down(r.qty, prec) == r.qty                       # never more precise -> never rounded up
        assert r.qty == 0 or r.qty * (entry - stop) <= budget          # risk budget
        n = r.qty * entry
        assert n == r.notional
        assert n <= equity * rc.max_position_pct / 100
        assert n <= rc.max_order_value
        assert n * (1 + fee) <= cash
        if depth is not None:
            assert n * 5 <= depth
        if head is not None:
            assert n <= max(head, D(0))
        if r.qty > 0:
            assert n >= rc.min_notional
            # rounding down is tight: one more step would break some limit or the risk budget
            step = D(1).scaleb(-prec)
            q2 = r.qty + step
            n2 = q2 * entry
            assert (q2 * (entry - stop) > budget or n2 > equity * rc.max_position_pct / 100
                    or n2 > rc.max_order_value or n2 * (1 + fee) > cash
                    or (depth is not None and n2 * 5 > depth) or (head is not None and n2 > head))


@pytest.mark.parametrize("prec", [0, 2, 6])
def test_never_rounds_up_at_boundary(prec):
    # 1/3-type quotients: risk qty = 100 / 3 = 33.333...; result must truncate
    r = size_position(D("10000"), D("30"), D("27"), SymbolRules("X", "X", "USDT", prec, 2), risk(min_notional=D("0")),
                      D("1e9"), D("0"))
    assert r.qty == round_down(D(100) / D(3), prec)
