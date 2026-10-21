from decimal import Decimal

import pytest

from wallexbot.exchange.base import (BrokerTimeout, DuplicateClientId, OrderRejected, OrderStatus,
                                     RateLimited)
from wallexbot.exchange.mock_wallex import MockClock, MockWallex
from wallexbot.exchange.ratelimit import TokenBucket
from wallexbot.types import OrderType, Side

D = Decimal
SYM = "BTCUSDT"
L, M, SL, SM = OrderType.LIMIT, OrderType.MARKET, OrderType.STOP_LIMIT, OrderType.STOP_MARKET
B, S = Side.BUY, Side.SELL


def sub(broker, cid, side=B, type=L, qty="0.010000", price="60010.00", stop=None):
    return broker.submit_order(client_id=cid, symbol=SYM, side=side, type=type, quantity=D(qty),
                               price=None if price is None else D(price),
                               stop_price=None if stop is None else D(stop))


def test_quote_and_fee_rates(broker):
    q = broker.quote(SYM)
    assert (q.bid, q.ask) == (D("60000.00"), D("60010.00"))
    assert broker.fee_rates().taker == D("0.0035")


def test_duplicate_client_id_creates_nothing(broker):
    sub(broker, "c1")
    with pytest.raises(DuplicateClientId):
        sub(broker, "c1", qty="0.020000")
    assert len(broker.orders) == 1 and broker.orders["c1"].quantity == D("0.010000")


def test_marketable_limit_fills_with_base_fee_on_buy(broker):
    o = sub(broker, "c1")
    assert o.status is OrderStatus.FILLED
    assert o.filled_quote == D("600.10") and o.fee == D("0.000035") and o.fee_asset == "BTC"
    bal = broker.balances()
    assert bal["BTC"].free == D("0.009965")
    assert bal["USDT"].free == D("10000") - D("600.10") and bal["USDT"].locked == 0


def test_sell_fee_in_quote(broker):
    sub(broker, "b")
    o = sub(broker, "s", side=S, type=M, qty="0.009965", price=None)
    assert o.status is OrderStatus.FILLED and o.fee_asset == "USDT"
    assert o.fee == D("0.009965") * D("60000.00") * D("0.0035")


def test_non_marketable_limit_rests_then_fills_at_limit_when_price_crosses(broker):
    o = sub(broker, "c1", price="59000.00")
    assert o.status is OrderStatus.NEW
    assert broker.balances()["USDT"].locked == D("590.00")
    assert [x.client_id for x in broker.open_orders()] == ["c1"]
    broker.set_price(SYM, D("58900.00"), D("58950.00"))
    o = broker.get_order("c1")
    assert o.status is OrderStatus.FILLED and o.filled_quote == D("590.00")  # maker at the limit price
    assert broker.open_orders() == []


def test_stop_orders_wait_outside_book_until_trigger(broker):
    sub(broker, "b")  # own some BTC
    o = sub(broker, "stop", side=S, type=SL, qty="0.005000", price="59000.00", stop="59500.00")
    # the limit 59000 is marketable vs bid 60000, but the stop has not triggered: no fill
    assert o.status is OrderStatus.PENDING_TRIGGER and o.filled_qty == 0
    broker.set_price(SYM, D("59600.00"), D("59610.00"))
    assert broker.get_order("stop").status is OrderStatus.PENDING_TRIGGER
    broker.set_price(SYM, D("59400.00"), D("59410.00"))
    o = broker.get_order("stop")
    assert o.status is OrderStatus.FILLED and o.filled_quote == D("0.005000") * D("59400.00")


def test_stop_market_buy_triggers_on_ask(broker):
    o = sub(broker, "sm", type=SM, price=None, stop="60500.00")
    assert o.status is OrderStatus.PENDING_TRIGGER
    broker.set_price(SYM, D("60490.00"), D("60500.00"))
    assert broker.get_order("sm").status is OrderStatus.FILLED


def test_market_order_beyond_depth_cancels_remainder(broker):
    broker.set_price(SYM, D("60000.00"), D("60010.00"), depth=D("0.004"))
    o = sub(broker, "m", type=M, price=None)
    assert o.status is OrderStatus.CANCELED and o.filled_qty == D("0.004")
    assert broker.balances()["USDT"].locked == 0


def test_market_order_moving_price_over_2pct_cancels_remainder(broker):
    broker.set_book(SYM, [(D("60000.00"), D("1"))],
                    [(D("60010.00"), D("0.004")), (D("61000.00"), D("1")), (D("61300.00"), D("1"))])
    o = sub(broker, "m", type=M, price=None)
    # 61000 is within 2% of 60010 (61210.2); 61300 is beyond and never touched
    assert o.status is OrderStatus.FILLED
    assert o.filled_quote == D("0.004") * D("60010.00") + D("0.006") * D("61000.00")
    broker.set_book(SYM, [(D("60000.00"), D("1"))], [(D("60010.00"), D("0.004")), (D("61300.00"), D("1"))])
    o = sub(broker, "m2", type=M, price=None)
    assert o.status is OrderStatus.CANCELED and o.filled_qty == D("0.004")


def test_cancel_releases_lock_and_is_idempotent(broker):
    sub(broker, "c", price="59000.00")
    assert broker.cancel_order("c").status is OrderStatus.CANCELED
    assert broker.cancel_order("c").status is OrderStatus.CANCELED
    assert broker.balances()["USDT"] .free == D("10000") and broker.balances()["USDT"].locked == 0
    assert broker.cancel_order("nope") is None


def test_rejections_create_nothing(broker):
    with pytest.raises(OrderRejected):
        sub(broker, "p", qty="0.0100001")         # amount precision
    with pytest.raises(OrderRejected):
        sub(broker, "pp", price="60010.001")      # price precision
    with pytest.raises(OrderRejected):
        sub(broker, "big", qty="1.000000")        # insufficient balance
    with pytest.raises(OrderRejected):
        sub(broker, "nosell", side=S)             # no BTC to sell
    assert broker.orders == {}


@pytest.mark.parametrize("mode,exc,created", [
    ("reject", OrderRejected, False), ("429", RateLimited, False),
    ("timeout_before_accept", BrokerTimeout, False), ("timeout_after_accept", BrokerTimeout, True)])
def test_fault_modes_raise(broker, mode, exc, created):
    broker.queue_fault(mode)
    with pytest.raises(exc):
        sub(broker, "f")
    assert ("f" in broker.orders) is created
    if created:
        assert broker.get_order("f").status is OrderStatus.FILLED


def test_fault_modes_partial_and_rest(broker):
    broker.queue_fault("partial", "rest")
    o = sub(broker, "p")
    assert o.status is OrderStatus.PARTIALLY_FILLED and o.filled_qty == D("0.005000")
    o = sub(broker, "r")
    assert o.status is OrderStatus.NEW and o.filled_qty == 0
    broker.set_price(SYM, D("60000.00"), D("60010.00"))  # next tick matches resting orders
    assert broker.get_order("p").status is OrderStatus.FILLED
    assert broker.get_order("r").status is OrderStatus.FILLED
    with pytest.raises(ValueError):
        broker.queue_fault("nonsense")


def test_lookup_failures(broker):
    sub(broker, "c")
    broker.lookup_failures = 2
    with pytest.raises(BrokerTimeout):
        broker.get_order("c")
    with pytest.raises(BrokerTimeout):
        broker.open_orders()
    assert broker.get_order("c") is not None


def test_server_side_rate_limit_10_per_10s(rules):
    clock = MockClock()
    b = MockWallex(rules.values(), clock=clock, balances={"USDT": D("100000")})
    b.set_price(SYM, D("60000.00"), D("60010.00"))
    for i in range(10):
        sub(b, f"c{i}", qty="0.001000", price="50000.00")
    with pytest.raises(RateLimited):
        sub(b, "c10", qty="0.001000", price="50000.00")
    assert "c10" not in b.orders
    clock.advance(1.0)
    sub(b, "c10", qty="0.001000", price="50000.00")


def test_token_bucket():
    t = [0.0]
    tb = TokenBucket(10, 10.0, clock=lambda: t[0])
    assert all(tb.try_acquire() for _ in range(10))
    assert not tb.try_acquire()
    t[0] = 0.5
    assert not tb.try_acquire()
    t[0] = 1.0
    assert tb.try_acquire() and not tb.try_acquire()
    t[0] = 100.0
    assert tb.available() == 10.0  # capped at capacity
    t[0] = 50.0                    # clock going backwards never adds tokens
    assert tb.available() == 10.0
    with pytest.raises(ValueError):
        TokenBucket(0, 1.0)
