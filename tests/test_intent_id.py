"""Intent identity: value-canonical numerics, stop_price in the key, wb_ + 32 hex format."""
import re
from decimal import Decimal

import pytest

from wallexbot.types import OrderIntent, OrderType, Side, canonical_decimal

D = Decimal
SYM = "BTCUSDT"
CID = re.compile(r"wb_[0-9a-f]{32}")


def intent(qty="0.01", price="60010.00", stop=None, type=OrderType.LIMIT, sig="sig1", side=Side.BUY,
           reduce_only=False):
    return OrderIntent(SYM, side, type, D(qty), None if price is None else D(price), sig,
                       reduce_only=reduce_only, stop_price=None if stop is None else D(stop))


@pytest.mark.parametrize("a,b", [("0.01", "0.010000"), ("0.01", "1E-2"), ("100", "1E+2"),
                                 ("100", "100.000"), ("0", "0.000"), ("0", "-0"), ("0", "0E+5")])
def test_canonical_decimal_equal_values_equal_text(a, b):
    assert D(a) == D(b) and canonical_decimal(D(a)) == canonical_decimal(D(b))


def test_canonical_decimal_forms_and_no_rounding():
    assert canonical_decimal(D("0.010000")) == "0.01"
    assert canonical_decimal(D("1E+2")) == "100"
    assert canonical_decimal(D("-1.50")) == "-1.5"
    assert canonical_decimal(None) is None
    long = D("1.00000000000000000000000000000001")  # 33 significant digits: normalize() would round
    assert canonical_decimal(long) == "1.00000000000000000000000000000001"
    assert canonical_decimal(long) != canonical_decimal(D(1))
    with pytest.raises(ValueError):
        canonical_decimal(D("NaN"))


@pytest.mark.parametrize("kw_a,kw_b", [
    ({"qty": "0.01"}, {"qty": "0.010000"}),
    ({"qty": "0.01"}, {"qty": "1E-2"}),
    ({"price": "60010"}, {"price": "60010.00"}),
    ({"price": "60010"}, {"price": "6.001E+4"}),
    ({"stop": "59500", "type": OrderType.STOP_LIMIT}, {"stop": "59500.000", "type": OrderType.STOP_LIMIT}),
])
def test_equal_valued_intents_share_ids(kw_a, kw_b):
    a, b = intent(**kw_a), intent(**kw_b)
    assert a.intent_id == b.intent_id and a.client_id == b.client_id and a.fingerprint == b.fingerprint
    assert CID.fullmatch(a.client_id)


def test_stop_price_is_part_of_identity():
    base = dict(qty="0.009965", price=None, type=OrderType.STOP_MARKET, side=Side.SELL, sig="exit1",
                reduce_only=True)
    a, b = intent(stop="59500.00", **base), intent(stop="59000.00", **base)
    assert a.client_id != b.client_id
    assert intent(stop=None, **{**base, "type": OrderType.MARKET}).client_id != \
        intent(stop="59500", **{**base, "type": OrderType.MARKET}).client_id


@pytest.mark.parametrize("field,other", [("qty", "0.02"), ("price", "60011"), ("sig", "sig2"),
                                         ("side", Side.SELL), ("type", OrderType.MARKET),
                                         ("reduce_only", True)])
def test_every_shape_field_changes_identity(field, other):
    assert intent().client_id != intent(**{field: other}).client_id


def test_key_is_delimiter_safe():
    # with a "|"-joined key these two could collide; the JSON-array key cannot
    a = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("1"), D("1"), "x|BTCUSDT")
    b = OrderIntent("BTCUSDT", Side.BUY, OrderType.LIMIT, D("1"), D("1"), "x")
    assert a.client_id != b.client_id


def test_client_id_format():
    for i in (intent(), intent(qty="123.456789"), intent(stop="1", type=OrderType.STOP_LIMIT)):
        assert CID.fullmatch(i.client_id)


# ----------------------------------------------------------------------- through the OMS
def test_same_value_other_scale_resubmission_sends_once(rig):
    first = rig.submit(intent(qty="0.010000"))
    second = rig.submit(intent(qty="0.01"))
    third = rig.submit(intent(qty="1.0E-2", price="60010"))
    assert first.sent is True and second.sent is False and third.sent is False
    assert first.client_id == second.client_id == third.client_id
    assert rig.broker.submit_calls == 1 and len(rig.broker.orders) == 1
    assert len(rig.store.rows("order_intents")) == 1


def test_exits_differing_only_in_stop_price_are_both_sent(rig):
    assert rig.submit(intent(qty="0.010000")).sent is True
    held = rig.oms.positions()[SYM].quantity
    half = (held / 2).quantize(D("0.000001"), rounding="ROUND_DOWN")
    base = dict(qty=str(half), price=None, type=OrderType.STOP_MARKET, side=Side.SELL, sig="exit1",
                reduce_only=True)
    a = rig.submit(intent(stop="59500.00", **base))
    b = rig.submit(intent(stop="59000.00", **base))
    assert a.client_id != b.client_id
    assert a.sent is True and b.sent is True
    assert rig.broker.submit_calls == 3
    sent_stops = sorted(o.stop_price for o in rig.broker.orders.values() if o.stop_price is not None)
    assert sent_stops == [D("59000.00"), D("59500.00")]
