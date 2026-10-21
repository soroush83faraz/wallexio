"""Closed round-trip P&L must equal the round trip's exact quote cash flows (fees included)."""
import random
from decimal import Decimal, Inexact, localcontext

import pytest

from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.oms import OMS, LedgerError, apply_fill
from wallexbot.risk.engine import RiskDecision
from wallexbot.store import Store
from wallexbot.types import Fill, OrderIntent, OrderType, Position, Side, SymbolRules, round_down

D = Decimal
SYM = "BTCUSDT"
BASE, QUOTE = "BTC", "USDT"
CID = "wb_" + "0" * 32


def _fill(side, qty, fee, fee_asset, price=D("1")):
    return Fill(CID, SYM, side, price, qty, fee, fee_asset, 0.0)


def _cash_flows(fills) -> Decimal:
    """Signed quote-currency cash flows of fill rows, quote-asset fees included (exact)."""
    total = D(0)
    for f in fills:
        q, fee = D(f["quote_amount"]), D(f["fee"])
        fee_q = fee if f["fee_asset"] == QUOTE else D(0)
        total += (q - fee_q) if f["side"] == "SELL" else -(q + fee_q)
    return total


class _ApproveAll:
    def __init__(self, rules):
        self.rules = rules

    def check(self, *args):
        return RiskDecision(True, ())


class _Open:
    def allows(self, intent):
        return True


def test_verifier_counterexample_through_oms_and_mock_exchange():
    """BUY 0.266 @ 49314.21 (fee in BTC), SELL 0.088356 @ 42169.32, SELL rest @ 88897.12. The
    stored closed P&L used to be 6249.586182264319999999999995 (28-digit division residue)."""
    rules = {SYM: SymbolRules(SYM, BASE, QUOTE, amount_precision=6, price_precision=2)}
    clock = MockClock()
    broker = MockWallex(rules.values(), clock=clock, balances={QUOTE: D("1000000")})
    store = Store(":memory:", clock=clock)
    oms = OMS(store, broker, _ApproveAll(rules), _Open(), approve=lambda d: True, clock=clock)
    broker.set_price(SYM, D("49314.00"), D("49314.21"))
    assert oms.submit(OrderIntent(SYM, Side.BUY, OrderType.LIMIT, D("0.266"), D("49314.21"), "e1"),
                      None, None).sent
    broker.set_price(SYM, D("42169.32"), D("42169.50"))
    assert oms.submit(OrderIntent(SYM, Side.SELL, OrderType.MARKET, D("0.088356"), None, "x1",
                                  reduce_only=True), None, None).sent
    rest = store.latest_positions()[SYM].quantity
    assert rest == D("0.176713")
    broker.set_price(SYM, D("88897.12"), D("88897.50"))
    assert oms.submit(OrderIntent(SYM, Side.SELL, OrderType.MARKET, rest, None, "x2", reduce_only=True),
                      None, None).sent
    closed = D(store.rows("positions_snapshots")[-1]["closed_pnl"])
    exact = _cash_flows(store.rows("fills"))
    assert exact == D("6249.58618226432")
    assert closed == exact
    assert store.latest_positions()[SYM].quantity == 0
    store.close()


def test_break_even_round_trip_is_exactly_zero_not_a_loss():
    # cost 302 for 3 units -> the pro-rata release of 1 unit (100.666...) is non-terminating
    p = Position(SYM)
    p, _ = apply_fill(p, _fill(Side.BUY, D("1"), D(0), QUOTE), D("100"), BASE, QUOTE)
    p, _ = apply_fill(p, _fill(Side.BUY, D("2"), D(0), QUOTE), D("202"), BASE, QUOTE)
    p, c1 = apply_fill(p, _fill(Side.SELL, D("1"), D(0), QUOTE), D("100.66"), BASE, QUOTE)
    p, c2 = apply_fill(p, _fill(Side.SELL, D("2"), D(0), QUOTE), D("201.34"), BASE, QUOTE)
    assert c1 is None and c2 == 0 and p.realized_pnl == 0 and p.quantity == 0


def test_ledger_exactness_does_not_depend_on_callers_decimal_context():
    with localcontext() as ctx:
        ctx.prec = 6
        p = Position(SYM)
        p, _ = apply_fill(p, _fill(Side.BUY, D("0.266"), D("0.000931"), BASE), D("13117.57986"), BASE, QUOTE)
        p, _ = apply_fill(p, _fill(Side.SELL, D("0.088356"), D("13.04049838536"), QUOTE),
                          D("3725.9104783920"), BASE, QUOTE)
        p, closed = apply_fill(p, _fill(Side.SELL, D("0.176713"), D("54.98289566916"), QUOTE),
                               D("15709.5130191200"), BASE, QUOTE)
    assert closed == D("15709.5130191200") - D("54.98289566916") + D("3725.9104783920") \
        - D("13.04049838536") - D("13117.57986")


def test_inexact_ledger_arithmetic_fails_closed():
    """An add/sub that would need rounding beyond the 1000-digit exact context raises (the OMS
    turns that into NEEDS_REVIEW) rather than storing a silently rounded P&L."""
    p = Position(SYM)
    p, _ = apply_fill(p, _fill(Side.BUY, D("1"), D(0), QUOTE), D("1E+600"), BASE, QUOTE)
    with pytest.raises(LedgerError, match="not exact"):
        apply_fill(p, _fill(Side.BUY, D("1"), D(0), QUOTE), D("1E-600"), BASE, QUOTE)


def _rand_price(rng):
    return D(rng.choice([rng.randint(1, 10**4), rng.randint(10**4, 10**8), rng.randint(1, 10**12)])).scaleb(-2)


def _rand_fee(rng, amount):
    rate = rng.choice([D(0), D("0.001"), D("0.0035"), D(rng.randint(1, 10**6)).scaleb(-8)])
    with localcontext() as ctx:  # truncating the fee is intended rounding, not a test error
        ctx.traps[Inexact] = False
        return round_down(amount * rate, 8)


def _buy(rng, pos, flows):
    qty = D(rng.randint(1, 10**7)).scaleb(-6)
    price = _rand_price(rng)
    quote = qty * price
    asset = rng.choice([BASE, QUOTE])
    fee = _rand_fee(rng, qty if asset == BASE else quote)
    if asset == BASE and fee >= qty:
        fee = D(0)
    pos, closed = apply_fill(pos, _fill(Side.BUY, qty, fee, asset, price), quote, BASE, QUOTE)
    assert closed is None
    flows.append(-(quote + (fee if asset == QUOTE else 0)))
    return pos


def _sell(rng, pos, flows, base_out):
    price = _rand_price(rng)
    asset = rng.choice([BASE, QUOTE])
    if asset == BASE:
        fee = _rand_fee(rng, base_out)
        qty = base_out - fee
        if qty <= 0:
            fee, qty = D(0), base_out
    else:
        qty = base_out
        fee = _rand_fee(rng, qty * price)
    quote = qty * price
    pos, closed = apply_fill(pos, _fill(Side.SELL, qty, fee, asset, price), quote, BASE, QUOTE)
    flows.append(quote - (fee if asset == QUOTE else 0))
    return pos, closed


@pytest.mark.parametrize("seed", range(8))
def test_property_closed_pnl_equals_exact_cash_flows(seed):
    """Random buy / partial-sell sequences (buys interleaved, fees in base or quote, prices from
    0.01 to 1e10, quantities to 1e-6, fees to 1e-8) that end flat: every closed P&L equals the
    exact sum of that round trip's signed quote cash flows, and realized P&L telescopes."""
    rng = random.Random(seed)
    with localcontext() as ctx:  # the test's own reference sums must be exact too
        ctx.prec = 200
        ctx.traps[Inexact] = True
        _run_property(rng)


def _run_property(rng):
    for _ in range(60):
        pos, realized_total = Position(SYM), D(0)
        for _trip in range(rng.randint(1, 3)):
            flows: list[Decimal] = []
            for _ in range(rng.randint(1, 3)):
                pos = _buy(rng, pos, flows)
            for _ in range(rng.randint(0, 5)):
                units = int(pos.quantity.scaleb(8))
                if units < 2:
                    break
                pos, closed = _sell(rng, pos, flows, D(rng.randint(1, units - 1)).scaleb(-8))
                assert closed is None and pos.quantity > 0
                if rng.random() < 0.3:
                    pos = _buy(rng, pos, flows)
            pos, closed = _sell(rng, pos, flows, pos.quantity)  # close exactly what is held
            assert pos.quantity == 0
            assert closed == sum(flows, D(0)), flows
            realized_total += closed
            assert pos.realized_pnl == realized_total
            assert "cost_basis" not in pos.meta
